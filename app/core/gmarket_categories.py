"""Gmarket 전체 카테고리 트리 — 대/중/소 3단계 데이터·파서·캐시·트리 로드 (Qt 비의존).

데이터 소스 (실측 2026-09-08, docs/gmarket/CATEGORY_RESEARCH.md):
  `category.gmarket.co.kr/listview/L{code}.aspx` — Cloudflare 없는 레거시
  카테고리 페이지. 모든 페이지에 서버 렌더링으로 다음이 내장되어 있다:
    1) 전체 대분류 네비게이션 — `listview/LList.aspx?gdlc_cd={code}` 한글 링크
    2) 해당 대분류(코드)의 중/소분류 트리 — `/n/list?category={code}` 한글 링크
       (중분류 200000xxx 다음에 그 소분류 300000xxx 들이 문서 순서로 나열)

  코드 체계는 국내 PC / 모바일 글로벌 공용: 첫 자리 1=대분류, 2=중분류,
  3=소분류(9자리). 실제 상품 리스팅은 국내 PC `www.gmarket.co.kr/n/list`
  (Cloudflare 보호下, StealthySession 필요) — 본 모듈은 트리만 다룬다.

  새로고침은 대분류 수(약 60)만큼의 정적 페이지 GET 이 전부라 브라우저가
  필요 없고 가볍다. 캐시(7일) + 번들 seed fallback 구조로 사이트 장애에
  대비한다(쿠팡 카테고리 탭과 동일 패턴).
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests

from app.core import config

# ── 레벨 상수 (쿠팡/글로벌 코드와 동일 의미) ───────────────────────────────
LEVEL_LARGE = "L"
LEVEL_MIDDLE = "M"
LEVEL_SMALL = "S"

# 부트스트랩 대분류 코드 — 모든 listview 페이지에 전체 대분류 네비가 내장되므로
# 아무 페이지나 1장으로 시작한다. 실측된 대분류(브랜드 여성의류)를 기본값으로.
DEFAULT_BOOTSTRAP_CODE = "100000103"

# 단일 EXE에 포함되는 검증 완료 기본 목록(resources/). 실시간 새로고침이
# 불가능한 상황에서도 카테고리 선택 기능을 사용할 수 있게 하며, 네트워크
# 캐시와 달리 만료시키지 않는다(쿠팡 카테고리 seed 와 동일 역할).
DEFAULT_SEED_CACHE_PATH = (
    Path(__file__).resolve().parents[1] / "resources" / "gmarket_category_tree.json"
)

# n/list 카테고리 링크 파라미터 — 신규(categoryCode)와 레거시(category) 모두 허용
_CATEGORY_LINK_RE = re.compile(
    r'<a\b[^>]*\bhref="([^"]*(?:categoryCode|category)=(\d{9})[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_LARGE_NAV_RE = re.compile(
    r'<a\b[^>]*\bhref="([^"]*[?&]gdlc_cd=(\d{9})[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

LogFn = Callable[[str], None]


def _noop_log(_msg: str) -> None:  # pragma: no cover
    pass


@dataclass
class GmarketCategoryNode:
    """카테고리 트리 노드 (code = `/n/list` 의 카테고리 번호)."""

    code: str                      # 대/중/소 공통 9자리 숫자 코드
    name: str                      # 한글 카테고리명
    level: str = LEVEL_LARGE       # LEVEL_LARGE | LEVEL_MIDDLE | LEVEL_SMALL
    children: list["GmarketCategoryNode"] = field(default_factory=list)

    def count(self) -> int:
        return 1 + sum(c.count() for c in self.children)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "level": self.level,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "GmarketCategoryNode":
        return cls(
            code=str(raw.get("code", "")),
            name=str(raw.get("name", "")),
            level=str(raw.get("level", LEVEL_LARGE)),
            children=[cls.from_dict(c) for c in raw.get("children", [])],
        )


def count_nodes(roots: list[GmarketCategoryNode]) -> int:
    return sum(n.count() for n in roots)


def find_node(roots: list[GmarketCategoryNode], code: str) -> GmarketCategoryNode | None:
    """코드로 노드 탐색 (DFS, 최초 일치 반환)."""
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node.code == str(code):
            return node
        stack.extend(node.children)
    return None


def flatten_descendants(node: GmarketCategoryNode) -> list[GmarketCategoryNode]:
    """자신 제외 모든 후손 — 깊이 우선 순서 (중복 코드 제거)."""
    out: list[GmarketCategoryNode] = []
    seen: set[str] = set()

    def walk(n: GmarketCategoryNode) -> None:
        for c in n.children:
            if c.code and c.code not in seen:
                seen.add(c.code)
                out.append(c)
            walk(c)

    walk(node)
    return out


# ── 파서 (순수 함수) ────────────────────────────────────────────────────────


def _clean_text(raw: str) -> str:
    """앵커 내부 HTML 을 걷어내고 공백을 정리한다."""
    text = _TAG_STRIP_RE.sub("", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_nav(html: str) -> list[GmarketCategoryNode]:
    """listview 페이지의 전체 대분류 네비 → 대분류 노드(children 없음).

    문서 순서 유지, 중복 코드 제거. 네비가 비어 있으면 [].
    """
    roots: list[GmarketCategoryNode] = []
    seen: set[str] = set()
    for _href, code, raw in _LARGE_NAV_RE.findall(html or ""):
        name = _clean_text(raw)
        if not name or code in seen:
            continue
        seen.add(code)
        roots.append(GmarketCategoryNode(code=code, name=name, level=LEVEL_LARGE))
    return roots


def parse_subtree(html: str) -> list[GmarketCategoryNode]:
    """listview 페이지에 내장된 해당 대분류의 중/소분류 트리를 재구성.

    문서 순서대로 `/n/list?category=...` 링크가 나열된다(중분류 링크 뒤에 그
    소분류 링크들). 코드 첫 자리(2=중, 3=소)로 계층을 복원한다. 대분류
    자체('1'로 시작)나 알 수 없는 깊이는 무시한다.
    """
    roots: list[GmarketCategoryNode] = []
    current: GmarketCategoryNode | None = None
    seen_mid: set[str] = set()
    seen_small: set[str] = set()

    for href, code, raw in _CATEGORY_LINK_RE.findall(html or ""):
        if "/n/list" not in href:
            continue
        name = _clean_text(raw)
        if not name:
            continue
        if code.startswith("2"):  # 중분류
            if code in seen_mid:
                continue
            seen_mid.add(code)
            current = GmarketCategoryNode(code=code, name=name, level=LEVEL_MIDDLE)
            roots.append(current)
        elif code.startswith("3") and current is not None:  # 소분류
            if code in seen_small:
                continue
            seen_small.add(code)
            current.children.append(
                GmarketCategoryNode(code=code, name=name, level=LEVEL_SMALL)
            )
        # 그 외(대분류/4단계 등)는 수집 대상이 아니므로 무시
    return roots


# ── 캐시 ────────────────────────────────────────────────────────────────────


@dataclass
class GmarketCategoryTreeCache:
    """카테고리 트리 캐시 (쿠팡 CategoryTreeCache 패턴).

    ``path``의 최신 네트워크 캐시를 우선 사용하고, 없거나 만료·손상됐으면
    ``seed_path``의 검증 완료 기본 목록을 사용한다. 기본 목록은 차단/오프라인
    안전망이므로 만료시키지 않는다.
    """

    path: Path
    ttl_hours: float = config.CATEGORY_TREE_CACHE_HOURS
    seed_path: Path | None = None

    @staticmethod
    def _valid_raw_node(raw: object, depth: int = 0) -> bool:
        if depth > 6 or not isinstance(raw, dict):
            return False
        code, name, level = raw.get("code"), raw.get("name"), raw.get("level")
        children = raw.get("children")
        if not all(isinstance(v, str) and v for v in (code, name, level)):
            return False
        if not (code.isdigit() and len(code) == 9):
            return False
        if not isinstance(children, list):
            return False
        return all(
            GmarketCategoryTreeCache._valid_raw_node(c, depth + 1) for c in children
        )

    def _load_path(self, path: Path, *, enforce_ttl: bool) -> list[GmarketCategoryNode] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(raw, dict):
            return None
        if enforce_ttl:
            fetched_ts_raw = raw.get("fetched_ts", 0)
            if isinstance(fetched_ts_raw, bool) or not isinstance(fetched_ts_raw, (int, float)):
                return None
            fetched_ts = float(fetched_ts_raw)
            now_ts = datetime.now().timestamp()
            if not math.isfinite(fetched_ts):
                return None
            if fetched_ts <= 0 or fetched_ts > now_ts:
                return None
            if (now_ts - fetched_ts) / 3600 > self.ttl_hours:
                return None
        roots_raw = raw.get("roots", [])
        if not isinstance(roots_raw, list) or not roots_raw:
            return None
        if not all(self._valid_raw_node(n) for n in roots_raw):
            return None
        roots = [GmarketCategoryNode.from_dict(n) for n in roots_raw]
        if count_nodes(roots) <= 0:
            return None
        return roots

    def load(self) -> list[GmarketCategoryNode] | None:
        """최신 로컬 캐시, 없으면 만료 없는 seed 를 반환한다."""
        cached = self._load_path(self.path, enforce_ttl=True)
        if cached is not None:
            return cached
        if self.seed_path is None or self.seed_path == self.path:
            return None
        return self._load_path(self.seed_path, enforce_ttl=False)

    def save(self, roots: list[GmarketCategoryNode]) -> str:
        now = datetime.now()
        payload = {
            "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "fetched_ts": now.timestamp(),
            "node_count": count_nodes(roots),
            "roots": [n.to_dict() for n in roots],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload["fetched_at"]


# ── 트리 로드 (순수 HTTP, 브라우저 불필요) ───────────────────────────────────


class GmarketCategoryFetchError(RuntimeError):
    """카테고리 트리 로드 실패 (네트워크/차단/파싱 실패)."""


class GmarketCategoryTreeFetcher:
    """Cloudflare 없는 listview 페이지들로 전체 대/중/소 트리를 로드한다.

    1) 부트스트랩 페이지 1장 → 전체 대분류 네비(코드+한글명) 추출
    2) 대분류별 L{code}.aspx 페이지 GET → 중/소분류 서브트리 추출·부착

    HTTP 세션은 requests.Session(설정 헤더). 취소/일시정지는 ``control``
    checkpoint 로 건 경계 처리. 페이지 간 ``delay`` 초 휴식.
    """

    def __init__(
        self,
        control=None,
        on_log: LogFn = _noop_log,
        session: requests.Session | None = None,
        bootstrap_code: str = DEFAULT_BOOTSTRAP_CODE,
        delay: float = 0.4,
    ) -> None:
        self.control = control
        self.on_log = on_log
        self.session = session or requests.Session()
        if session is None:
            self.session.headers.update(config.HEADERS)
            self.session.headers["Accept-Language"] = "ko-KR,ko;q=0.9"
        self.bootstrap_code = str(bootstrap_code)
        self.delay = delay

    # ── 공개 API ──────────────────────────────────────────────────────────
    def fetch(self) -> list[GmarketCategoryNode]:
        """대분류(60개 내외) → 중분류 → 소분류 전체 트리의 루트 목록 반환."""
        self._checkpoint()
        nav = self._fetch_nav()
        if not nav:
            raise GmarketCategoryFetchError(
                "카테고리 대분류 목록을 읽지 못했습니다 — 페이지 구조 변경 또는 차단 가능성"
            )

        roots: list[GmarketCategoryNode] = []
        total = len(nav)
        for idx, large in enumerate(nav, start=1):
            self._checkpoint()
            self.on_log(f"[카테고리 트리] ({idx}/{total}) {large.name} 조사 중...")
            children = self._fetch_subtree(large.code)
            if children:
                large.children = children
            else:
                self.on_log(f"  {large.name}: 중/소분류를 찾지 못함 (children 없이 유지)")
            roots.append(large)
            if idx < total:
                self._sleep(self.delay)

        if count_nodes(roots) <= 0:
            raise GmarketCategoryFetchError("카테고리 트리가 비어 있습니다")
        self.on_log(f"[카테고리 트리] 완료 — {len(roots)}개 대분류, {count_nodes(roots)}개 노드")
        return roots

    # ── 내부 ──────────────────────────────────────────────────────────────
    def _page_url(self, large_code: str) -> str:
        return config.CATEGORY_TREE_URL.format(large_code)

    def _get_html(self, url: str) -> str:
        """GET + euc-kr(euc-kr 실패 시 utf-8) 디코딩. HTTP 오류는 예외 전파."""
        try:
            r = self.session.get(url, timeout=config.CATEGORY_TREE_TIMEOUT)
        except requests.RequestException as e:
            raise GmarketCategoryFetchError(f"카테고리 페이지 요청 실패: {e}") from e
        if r.status_code != 200:
            raise GmarketCategoryFetchError(f"카테고리 페이지 HTTP {r.status_code}: {url}")
        content = r.content
        for enc in ("euc-kr", "utf-8"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("euc-kr", "replace")

    def _fetch_nav(self) -> list[GmarketCategoryNode]:
        bootstrap = self._get_html(self._page_url(self.bootstrap_code))
        self._checkpoint()
        return parse_nav(bootstrap)

    def _fetch_subtree(self, large_code: str) -> list[GmarketCategoryNode]:
        try:
            html = self._get_html(self._page_url(large_code))
        except GmarketCategoryFetchError as e:
            self.on_log(f"  {large_code} 조회 실패(스킵): {e}")
            return []
        self._checkpoint()
        return parse_subtree(html)

    def _checkpoint(self) -> None:
        if self.control is not None:
            self.control.checkpoint()

    def _sleep(self, seconds: float) -> None:
        if self.control is not None:
            self.control.sleep(seconds)
        else:
            time.sleep(seconds)
