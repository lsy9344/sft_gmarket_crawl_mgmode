"""카테고리 정의, 엔드포인트, 상수, 타이밍 설정 (WORK_ORDER §4.2 / §5 / §11).

이 모듈이 모든 상위 계층(prescan/crawler/workers/ui)의 단일 설정 소스이다.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from app.models.records import SOURCE_BEST, SOURCE_SUPERDEAL


def _default_project_root() -> Path:
    """개발 실행과 PyInstaller 번들 실행 모두에서 올바른 프로젝트 루트를 반환.

    PyInstaller one-file 번들에서는 `__file__` 이 프로세스 종료 시 삭제되는
    임시 추출 디렉터리(`sys._MEIPASS`)를 가리키므로, 이를 기준으로 출력
    경로를 잡으면 종료와 동시에 결과/상태 파일이 사라진다. `sys.frozen` 이면
    실제 실행 파일이 위치한 디렉터리(`sys.executable`)를 기준으로 삼는다.
    https://pyinstaller.org/en/stable/runtime-information.html
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # app/core/config.py → parents[2] == 프로젝트 루트
    return Path(__file__).resolve().parents[2]


# ── 경로 ───────────────────────────────────────────────────────────────
PROJECT_ROOT = _default_project_root()
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

# 쿠팡 영속 브라우저 프로필 — 쿠키(로그인 세션)·방문 이력을 실행 간 누적한다.
# exe 옆에 생성되며 세션 데이터를 담으므로 저장소에 커밋하지 않는다(.gitignore).
DEFAULT_COUPANG_PROFILE_DIR = PROJECT_ROOT / "runtime_profile"

COLLECTED_IDS_FILENAME = "collected_ids.json"
STATE_FILENAME = "fastcrawl_state.json"
# 체크포인트 승격 콘텐츠 해시 manifest — 정상 저장/승격 어느 경로로든 이미
# 최종 파일로 저장된 내용을 기록해, 다른 경로가 같은 내용을 중복 승격하지
# 않도록 한다(5차 리뷰 HIGH-2/3 회귀 방지).
PROMOTED_MANIFEST_FILENAME = ".promoted_hashes.json"

# ── 엔드포인트 / 헤더 ──────────────────────────────────────────────────
SELLER_INFO_URL = "https://mg.gmarket.co.kr/SellerInfo/SellerInfo?goodscode={}"
ITEM_URL = "https://item.gmarket.co.kr/Item?goodscode={}"
WARMUP_URL = "https://www.gmarket.co.kr"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ── 타이밍 / 제어 상수 (WORK_ORDER §5.6 / §11) ─────────────────────────
DEFAULT_DELAY = 0.5          # 요청 기본 간격(초)
DELAY_JITTER = 0.3           # 요청 간격 랜덤 가산(초): delay + random(0, JITTER)
SECONDS_PER_ITEM = 0.7       # 예상 소요 시간 산출 기준(건당)
SAVE_INTERVAL = 50           # 중간 저장 주기(건)
MAX_RETRIES = 3              # 네트워크 오류 재시도 횟수
HTTP_TIMEOUT = 10            # Phase 2 요청 타임아웃(초)
DEFAULT_MAX_ITEMS = 200      # 카테고리당 최대 수집 수
# 판매자정보(mg) 요청이 연속 이 횟수만큼 실패하면 카테고리를 중단한다 —
# 정상 응답(OK/SKIP/MISS)은 리셋되고 네트워크 오류·403/429/5xx 만 누적된다.
# 건당 3회 내부 재시도까지 실패한 연속 10건은 사이트 측 제한(차단 진행)의
# 강한 신호이며, 계속 때리면 회복을 늦출 뿐이다(2026-09-09 검토 반영).
CONSECUTIVE_FAIL_ABORT = 10

# Phase 0/1 리스팅 관련
WARMUP_WAIT = 3              # Cloudflare 쿠키 획득 대기(초)
BEST_TIMEOUT_MS = 15000      # 베스트 리스팅 fetch 타임아웃(ms)
SUPERDEAL_TIMEOUT_MS = 20000 # 슈퍼딜 리스팅 fetch 타임아웃(ms)
WARMUP_TIMEOUT_MS = 20000
BEST_WAIT_SELECTOR = 'a[href*="goodscode"]'
CLOUDFLARE_WAIT = 30         # 403 차단 감지 시 대기(초)
LISTING_MAX_RETRIES = 2      # 리스팅 fetch 재시도 횟수(차단 대응)

# ── 전체 카테고리 탭 (Gmarket 카테고리) ────────────────────────────────────
# Phase A 리스팅은 Bright Data Web Unlocker 로 fetch 한다 — 로컬 IP(사무실/
# 데이터센터) 평판과 무관하게 Cloudflare Managed Challenge 를 통과한다.
# 실측(2026-09-08, docs/gmarket/ACCESS_ROUTES_RESEARCH_20260908.md §6):
#   - `categoryCode=` 는 홈으로 리다이렉트되고 **`category=`(레거시)만 목록 반환**
#   - 페이지네이션은 `&page=N` 이 아니라 **`&k=0&p={N}&keep-ssid=y`**
CATEGORY_LIST_URL = "https://www.gmarket.co.kr/n/list?category={}"
# 대/중/소 트리 소스 — Cloudflare 없는 레거시 카테고리 페이지. 이 페이지에
# 대분류 네비 + 대분류별 중/소분류(`n/list?category=` 링크)가 한글명으로 내장된다.
CATEGORY_TREE_URL = "https://category.gmarket.co.kr/listview/L{}.aspx"
CATEGORY_TIMEOUT_MS = 20000          # 카테고리 리스팅 fetch 타임아웃(ms)
CATEGORY_TREE_TIMEOUT = 15           # 트리 페이지 HTTP 타임아웃(초)
CATEGORY_TREE_CACHE_HOURS = 24 * 7   # 트리 캐시 수명 (7일)
CATEGORY_MAX_PAGES = 20              # 카테고리 리스팅 최대 페이지 기본값
CATEGORY_EMPTY_PAGE_TOLERANCE = 2    # 연속 빈 페이지 허용치(종료 판정)
CATEGORY_PAGE_DELAY_MIN = 8.0        # 리스팅 페이지 간 딜레이 최소(초)
CATEGORY_PAGE_DELAY_MAX = 12.0       # 리스팅 페이지 간 딜레이 최대(초)

# Bright Data Web Unlocker 요청 설정 (Phase A — 카테고리 리스팅)
BRIGHTDATA_API_URL = "https://api.brightdata.com/request"
BRIGHTDATA_ZONE = "gm_unlocker"
BRIGHTDATA_COUNTRY = "kr"        # 한국 가정용 IP 출발 강제 — 국내 한글 카탈로그 필수
BRIGHTDATA_TIMEOUT = 90          # Unlocker 1요청 타임아웃(초) — 실측 20~70초
UNLOCKER_RETRY_WAIT = 10         # Unlocker 실패 재시도 전 대기(초)
# 빈 껍데기(200 수신 + goodscode 0개) 재요청 전 대기(초). 차단/오류 재시도보다
# 짧게 — 대량 실측(ACCESS_ROUTES_RESEARCH §9)에서 이 형태는 재시도 1회로
# 343건 중 278건이 회복됐다.
UNLOCKER_EMPTY_RETRY_WAIT = 5
# Unlocker 성공 요청 단가(USD) — 시작 전 예상 비용 안내용 (약 $3/1,000건 실측)
BRIGHTDATA_COST_PER_REQUEST = 0.003


def brightdata_api_token() -> str:
    """Bright Data API 토큰 — 환경변수 우선, 없으면 gitignore 된 로컬 토큰 파일.

    토큰은 저장소에 커밋하지 않는다(output/ 는 .gitignore). 환경변수
    BRIGHTDATA_API_TOKEN 또는 output/brightdata_token.txt 파일로 공급한다.
    """
    env = os.environ.get("BRIGHTDATA_API_TOKEN", "").strip()
    if env:
        return env
    token_file = DEFAULT_OUTPUT_DIR / "brightdata_token.txt"
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

# 봇/캡차 감지 키워드 (리스팅 HTML 내 존재 시 차단으로 판단)
BOT_KEYWORDS = ["기다리십시오", "확인 안내", "확인 절차", "자동입력 방지", "접근이 제한"]

# 출력 파일명 프리픽스
FILE_PREFIX = "gmarket_fast"
ALL_LABEL = "ALL"


@dataclass(frozen=True)
class CategoryDef:
    """수집 대상 카테고리 정의."""

    name: str        # 카테고리명 (예: "신선식품")
    source: str      # SOURCE_BEST | SOURCE_SUPERDEAL
    url: str         # 리스팅 페이지 URL

    @property
    def is_best(self) -> bool:
        return self.source == SOURCE_BEST


BEST_CATEGORIES: list[CategoryDef] = [
    CategoryDef("신선식품", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100000006"),
    CategoryDef("가공식품", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100000005"),
    CategoryDef("생필품/육아", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100000007"),
    CategoryDef("생활/주방", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100001001"),
    CategoryDef("패션/잡화", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100000001"),
    CategoryDef("뷰티", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100000003"),
    CategoryDef("디지털/가전", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100001007"),
    CategoryDef("가구/홈", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100001004"),
    CategoryDef("스포츠/건강", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100001002"),
    CategoryDef("취미/문구/펫", SOURCE_BEST, "https://www.gmarket.co.kr/n/best?groupCode=100001003"),
]

SUPERDEAL_CATEGORIES: list[CategoryDef] = [
    CategoryDef("추천", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal"),
    CategoryDef("브랜드패션", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000135"),
    CategoryDef("트랜드패션", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000136"),
    CategoryDef("뷰티/잡화", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000137"),
    CategoryDef("유아동", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000138"),
    CategoryDef("식품", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000139"),
    CategoryDef("생필품", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000140"),
    CategoryDef("가구/침구", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000141"),
    CategoryDef("생활/건강", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000143"),
    CategoryDef("스포츠/레저", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000146"),
    CategoryDef("가전/컴퓨터", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000148"),
    CategoryDef("디지털", SOURCE_SUPERDEAL, "https://www.gmarket.co.kr/n/superdeal?categoryCode=400000149"),
]

ALL_CATEGORIES: list[CategoryDef] = BEST_CATEGORIES + SUPERDEAL_CATEGORIES


def all_categories() -> list[CategoryDef]:
    """베스트 10 + 슈퍼딜 12 = 22개 카테고리 전체."""
    return list(ALL_CATEGORIES)


def category_by_name(name: str) -> CategoryDef | None:
    for cat in ALL_CATEGORIES:
        if cat.name == name:
            return cat
    return None
