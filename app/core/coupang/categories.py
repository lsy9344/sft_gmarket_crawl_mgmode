"""Coupang 카테고리 트리 — 데이터 계약·파서·캐시·실시간 로드 (Qt 비의존 코어).

컨셉 (2026-08-24 사용자 결정): 키워드 '검색' 수집 → 카테고리 '선택' 수집.

데이터 소스:
    /n-api/web-adapter/category-list (JSONP) — 쿠팡 홈페이지 '카테고리' 버튼
    메가메뉴의 데이터. 실측(2026-08-24, poc21) 결과 카테고리 노드 3,169개
    (쇼핑 15 톱 / 테마 14 톱 / 여행), 최대 3뎁스.

수집 경로:
    선택 카테고리 → /np/categories/{id}?page=1..N (비로그인 SSR, 실측 상한
    ~17페이지 — SEARCH_POC_FINDINGS.md rev.11~24).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# JSONP 콜백 — 페이지 자신이 쓰는 패턴 재현 (청크 6913 분석 근거)
CATEGORY_LIST_FETCH_JS = """
async () => {
    const cb = '__cat_list_cb_' + Date.now();
    const url = '/n-api/web-adapter/category-list?callback=' + cb + '&_=' + Date.now();
    return await new Promise((resolve) => {
        const timer = setTimeout(() => resolve({error: 'timeout'}), 20000);
        window[cb] = (data) => {
            clearTimeout(timer);
            try { delete window[cb]; } catch (e) {}
            try { document.head.removeChild(s); } catch (e) {}
            resolve({ok: true, payload: data});
        };
        const s = document.createElement('script');
        s.src = url;
        s.onerror = () => { clearTimeout(timer); resolve({error: 'script_error'}); };
        document.head.appendChild(s);
    });
}
"""

# 응답 data.gnb 의 그룹 키 → UI 표시명 (순서 = 노출 순서)
CATEGORY_GROUPS = (
    ("shoppingComponent", "쇼핑"),
    ("themeComponent", "테마"),
    ("travelComponent", "여행"),
    ("travelDomesticComponent", "여행(국내)"),
)

CATEGORY_URI_PREFIX = "/np/categories/"
# 캐시 기본 수명 — 카테고리 트리 변경 주기는 느림 (7일)
CACHE_TTL_HOURS = 24 * 7
# 단일 EXE에 포함되는 검증 완료 기본 목록. 실시간 요청이 차단돼도 카테고리
# 선택 기능을 사용할 수 있도록 하며, 네트워크 캐시와 달리 만료시키지 않는다.
DEFAULT_SEED_CACHE_PATH = (
    Path(__file__).resolve().parents[2] / "resources" / "coupang_category_tree.json"
)


@dataclass
class CategoryNode:
    """카테고리 트리 노드 (id = URL 의 카테고리 번호)."""

    id: str
    name: str
    uri: str
    children: list["CategoryNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "uri": self.uri,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "CategoryNode":
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            uri=str(raw.get("uri", "")),
            children=[cls.from_dict(c) for c in raw.get("children", [])],
        )

    def count(self) -> int:
        return 1 + sum(c.count() for c in self.children)


# ── 파서 (순수 함수) ─────────────────────────────────────────────────────


def parse_category_node(raw: dict) -> CategoryNode | None:
    """gnb 컴포넌트 노드 → CategoryNode. 카테고리 링크가 아니면 제외.

    - 자식이 카테고리인 비카테고리 부모(캠페인 모음 등)는 자식을 승격.
    - 중복 id 는 최초 1회만 유지.
    """
    uri = raw.get("linkUri") or ""
    children_raw = raw.get("visibleChildren") or []
    children: list[CategoryNode] = []
    seen: set[str] = set()
    for c in children_raw:
        node = parse_category_node(c)
        if node is None:
            continue
        if node.id in seen:
            continue
        seen.add(node.id)
        children.append(node)

    if isinstance(uri, str) and uri.startswith(CATEGORY_URI_PREFIX):
        cid = uri[len(CATEGORY_URI_PREFIX):].split("?")[0].split("/")[0]
        if cid.isdigit():
            return CategoryNode(id=cid, name=str(raw.get("name") or cid),
                                uri=uri, children=children)
    # 비카테고리 노드: 카테고리 자식이 있으면 승격 (없으면 버림)
    return CategoryNode(id="", name=str(raw.get("name") or ""), uri="",
                        children=children) if children else None


def _extract_gnb(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("gnb"), dict):
        return data["gnb"]
    if isinstance(payload.get("gnb"), dict):  # 중계 계층이 data 를 벗긴 형태 방어
        return payload["gnb"]
    return {}


def parse_category_groups(payload: dict) -> list[tuple[str, list[CategoryNode]]]:
    """category-list 응답 페이로드 → [(그룹명, 루트 노드 목록), ...].

    빈 그룹은 제외. 그룹 내 루트 순서·이름은 응답 그대로 유지.
    비카테고리 부모에서 승격된 자식들은 루트 목록에 평탄화된다.
    """
    gnb = _extract_gnb(payload)
    groups: list[tuple[str, list[CategoryNode]]] = []
    for key, label in CATEGORY_GROUPS:
        roots_raw = gnb.get(key) or []
        roots: list[CategoryNode] = []
        seen: set[str] = set()
        for raw in roots_raw:
            node = parse_category_node(raw)
            if node is None:
                continue
            if node.id:  # 카테고리 루트
                if node.id in seen:
                    continue
                seen.add(node.id)
                roots.append(node)
            else:  # 승격된 자식들 평탄화
                for child in node.children:
                    if child.id in seen:
                        continue
                    seen.add(child.id)
                    roots.append(child)
        if roots:
            groups.append((label, roots))
    return groups


def count_nodes(groups: list[tuple[str, list[CategoryNode]]]) -> int:
    return sum(n.count() for _, roots in groups for n in roots)


def find_node(groups: list[tuple[str, list[CategoryNode]]],
              category_id: str) -> CategoryNode | None:
    stack = [n for _, roots in groups for n in roots]
    while stack:
        node = stack.pop()
        if node.id == str(category_id):
            return node
        stack.extend(node.children)
    return None


def flatten_descendants(node: CategoryNode) -> list[CategoryNode]:
    """자신 제외 모든 후손 노드 — 깊이 우선 순서 (중복 id 제거)."""
    out: list[CategoryNode] = []
    seen: set[str] = set()

    def walk(n: CategoryNode) -> None:
        for c in n.children:
            if c.id and c.id not in seen:
                seen.add(c.id)
                out.append(c)
            walk(c)

    walk(node)
    return out


# ── 캐시 ────────────────────────────────────────────────────────────────


@dataclass
class CategoryTreeCache:
    """카테고리 트리 캐시.

    ``path``의 최신 네트워크 캐시를 우선 사용한다. 캐시가 없거나 만료·손상됐으면
    ``seed_path``의 검증 완료 기본 목록을 사용한다. 기본 목록은 쿠팡 차단 상황의
    오프라인 안전망이므로 만료시키지 않는다.
    """

    path: Path
    ttl_hours: float = CACHE_TTL_HOURS
    seed_path: Path | None = None

    @staticmethod
    def _valid_cached_node(node: CategoryNode, depth: int = 0) -> bool:
        """캐시가 만든 정상 CategoryNode 구조인지 제한된 깊이로 검증한다."""
        if depth > 10:
            return False
        if not node.name.strip():
            return False
        if node.id:
            if not node.id.isdigit() or not node.uri.startswith(CATEGORY_URI_PREFIX):
                return False
        elif node.uri or not node.children:
            # 비카테고리 구조 부모는 자식이 있을 때만 허용한다.
            return False
        return all(
            CategoryTreeCache._valid_cached_node(child, depth + 1)
            for child in node.children
        )

    def _load_path(
        self, path: Path, *, enforce_ttl: bool
    ) -> tuple[list[tuple[str, list[CategoryNode]]], str] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(raw, dict):
            return None
        fetched_at = str(raw.get("fetched_at", ""))
        if enforce_ttl:
            try:
                fetched_ts = float(raw.get("fetched_ts", 0))
                now_ts = datetime.now().timestamp()
                age_h = (now_ts - fetched_ts) / 3600
            except (TypeError, ValueError, OverflowError):
                return None
            if fetched_ts <= 0 or fetched_ts > now_ts + 300 or age_h > self.ttl_hours:
                return None
        groups_raw = raw.get("groups", [])
        if not isinstance(groups_raw, list):
            return None
        groups: list[tuple[str, list[CategoryNode]]] = []
        try:
            for item in groups_raw:
                if not isinstance(item, list) or len(item) != 2:
                    return None
                label, nodes_raw = item
                if not isinstance(label, str) or not label.strip():
                    return None
                if not isinstance(nodes_raw, list) or not nodes_raw:
                    return None
                nodes = [CategoryNode.from_dict(n) for n in nodes_raw]
                if not all(self._valid_cached_node(node) for node in nodes):
                    return None
                groups.append((label, nodes))
            if not groups or count_nodes(groups) <= 0:
                return None
        except (AttributeError, TypeError, ValueError, RecursionError):
            return None
        return groups, fetched_at

    def load(self) -> tuple[list[tuple[str, list[CategoryNode]]], str] | None:
        """최신 로컬 캐시, 없으면 만료 없는 기본 목록을 반환한다."""
        cached = self._load_path(self.path, enforce_ttl=True)
        if cached is not None:
            return cached
        if self.seed_path is None or self.seed_path == self.path:
            return None
        return self._load_path(self.seed_path, enforce_ttl=False)

    def save(self, groups: list[tuple[str, list[CategoryNode]]]) -> str:
        now = datetime.now()
        payload = {
            "fetched_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "fetched_ts": now.timestamp(),
            "node_count": count_nodes(groups),
            "groups": [[label, [n.to_dict() for n in roots]] for label, roots in groups],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload["fetched_at"]


# ── 실시간 로드 (전용 1회 세션) ───────────────────────────────────────────


class CategoryFetchError(RuntimeError):
    """카테고리 트리 로드 실패 (차단·타임아웃·파싱 실패)."""


class CategoryTreeFetcher:
    """카테고리 트리 1회 로드 세션.

    규율: 홈 웜업 + in-page 요청 1회가 전부. 차단 감지 시 즉시 중단.
    """

    def __init__(self, control=None, browser_factory=None, on_log=None,
                 warmup_time: float = 15.0) -> None:
        self._control = control
        self._browser_factory = browser_factory
        self._on_log = on_log
        self._warmup_time = warmup_time

    def _log(self, msg: str) -> None:
        if self._on_log is not None:
            try:
                self._on_log(msg)
            except Exception:  # noqa: BLE001 - 로그 경계 격리
                pass

    def _sleep(self, seconds: float) -> None:
        if self._control is not None:
            self._control.sleep(seconds)
        else:
            time.sleep(seconds)

    def _checkpoint(self) -> None:
        if self._control is not None:
            self._control.checkpoint()

    def fetch(self) -> list[tuple[str, list[CategoryNode]]]:
        from app.core.coupang.crawler import COUPANG_HOME
        from app.core.coupang.search_parser import is_blocked

        browser, cm = self._create_browser()
        try:
            page = browser.new_page()
            self._log("카테고리 트리 로드: 홈 웜업...")
            page.goto(COUPANG_HOME, wait_until="domcontentloaded", timeout=40000)
            html = page.content()
            blocked, reason = is_blocked(html)
            if blocked:
                raise CategoryFetchError(f"쿠팡 차단 감지: {reason}. 쿨다운 후 재시도하세요.")
            self._natural_warmup(page)
            self._checkpoint()
            self._log("카테고리 목록 요청 (in-page, 1회)...")
            result = page.evaluate(CATEGORY_LIST_FETCH_JS)
            if not isinstance(result, dict) or not result.get("ok"):
                raise CategoryFetchError(
                    f"카테고리 목록 요청 실패: {result.get('error') if isinstance(result, dict) else '알 수 없음'}")
            groups = parse_category_groups(result.get("payload") or {})
            total = count_nodes(groups)
            if total == 0:
                raise CategoryFetchError("카테고리 트리가 비어 있습니다 (응답 파싱 실패 가능)")
            self._log(f"카테고리 트리 확보: {len(groups)}개 그룹, {total}개 노드")
            return groups
        finally:
            self._cleanup(browser, cm)

    def _create_browser(self):
        if self._browser_factory is not None:
            return self._browser_factory(), None
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as e:
            raise CategoryFetchError(
                "Camoufox 패키지 미설치: pip install camoufox[geoip]==0.5.4 "
                "&& python -m camoufox fetch") from e
        cm = Camoufox(headless=False, geoip=True, locale="ko-KR", humanize=True)
        browser = cm.__enter__()
        return browser, cm

    def _cleanup(self, browser, cm) -> None:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
                return
            except Exception as e:  # noqa: BLE001 - cleanup 경계
                self._log(f"브라우저 종료 실패, 재시도: {e}")
        if browser is not None:
            try:
                browser.close()
            except Exception as e:  # noqa: BLE001 - cleanup 경계
                self._log(f"브라우저 종료 실패: {e}")

    def _natural_warmup(self, page) -> None:
        """CoupangCrawler._natural_interaction 과 동일 패턴의 경량 웜업."""
        import random

        end = time.monotonic() + max(0.0, self._warmup_time)
        while time.monotonic() < end:
            self._checkpoint()
            page.mouse.move(random.randint(100, 900), random.randint(100, 600))
            page.mouse.wheel(0, random.randint(50, 250))
            self._sleep(random.uniform(0.5, 1.5))
