"""AliExpress 카테고리 탭 — Phase A(리스팅) + Phase B(판매자정보) 엔진 (Qt 비의존).

쿠팡 카테고리 탭(app/core/coupang/decodo_run.py)과 동일한 회선·재개 규율을 적용한다:
- Phase A: 대상 카테고리 1~max_pages 페이지네이션 순회하여 상품 목록 확보
- Plan: 스마트 판매자 중복 제거 및 수집 계획 확정 (시드 캐시 활용)
- Phase B: Decodo 한국 주거용 고정 회선(Sticky 1440min Session) 기반 브라우저 세션 운용
  - rotation_batch_size 건 단위 자동 회선 순환 + WAF punish 감지 시 즉시 세션 교체
  - 회선 확보 시 실제 출발 국가를 확인해(concatenated _korea_proxy 규율) 한국이 아니면
    최대 MAX_GEO_ROTATIONS 회 새 회선으로 교체한다
  - 상품 데이터(mtop) 미수신 페이지는 기록하지 않고 같은 회선에서 재시도한다 —
    빈 껍데기를 "(상세 미기재)"로 확정 저장해 재개 대상에서 누락시키는 일을 막는다
  - 실행 경계에서 반복 실패하는 상품(삭제·변경 SKU)은 누적 실패가
    ITEM_GIVE_UP_RUNS 회에 도달하면 재시도를 포기한다 — 포기 상품 하나 때문에
    완료 봉인이 영원히 불가능한 비수렴을 끊는다
  - 연속 차단이 CONSECUTIVE_BLOCK_ABORT 에 도달하면 그 지점을 저장한 채 안전 중단한다.
    다음 시작(우회 회선 선택)에서 저장된 지점부터 이어서 수집된다
- 재개 규율(쿠팡 search_crawler 동일): 재개 경계는 상품이 확인된 마지막
  페이지(last_item_page)이며, 그 페이지를 다시 확인한 뒤 다음 페이지부터
  이어간다. 0건으로 기록된 페이지(소프트 차단·렌더 지연)는 미확인으로 간주해
  다시 읽고, 빈 페이지 연속 판정(empty_streak)은 새 회선에서 0부터 다시 관찰한다
- 시도(MAX_ATTEMPTS)마다 새 회선으로 재시작하며 진행 기록(resume.sqlite3)을 공유한다.
  시도 간 대기(RETRY_WAIT_SECONDS)는 취소·일시정지 가능한 분할 대기다
- 비동기 mtop API 인터셉트를 통한 공정위 7대 사업자 정보 추출
- 실시간 CSV append 저장 (중간 유실 0건) + JSON 주기 재작성 (대량 수집 I/O 완화)
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from patchright.async_api import async_playwright

from app.core import config, decodo
from app.core.aliexpress_resume_store import (
    STATUS_FINISHED,
    STATUS_LISTING_DONE,
    AliexpressResumeStore,
    ResumeStoreError,
    ali_category_run_dir,
)
from app.core.base import CancelledError, Control

LogFn = Callable[[str], None]
ProgressFn = Callable[[str, int, int], None]  # (label, current, total)
PhaseFn = Callable[[str, int, int], None]     # (phase_name, current_phase, total_phases)
CollectedFn = Callable[[dict], None]
StatsFn = Callable[[object], None]
ErrorFn = Callable[[str], None]
ExitIpFn = Callable[[dict], object]           # decodo.fetch_exit_ip 와 동일 시그니처


def _noop(*_a, **_k) -> None:
    pass


# ── 쿠팡 decodo_run 과 동일한 시도·회선 규율 상수 ─────────────────────
MAX_ATTEMPTS = 3               # 카테고리 1건을 최대 몇 회(회선)까지 재시작할까
RETRY_WAIT_SECONDS = 90.0      # 시도(회선) 간 대기 — 제어 가능한 분할 대기
MAX_GEO_ROTATIONS = 3          # 세션 1개 확보 시 한국 확인 재시도 상한
CONSECUTIVE_BLOCK_ABORT = 10   # 연속 차단 상한 — 도달 시 안전 중단(저장 지점 보존)
PHASE1_PAGE_RETRIES = 3        # 목록 페이지 1장의 로드·차단 재시도 상한
PDP_ITEM_RETRIES = 3           # 상세 페이지 1건의 재시도 상한
# 상품별 누적 실패 실행 수 상한 — 도달한 상품은 재시도를 포기한다. 데이터를
# 끝내 주지 않는 삭제·변경 SKU 가 재개 대상에 영원히 남아 완료 봉인이 불가능한
# 비수렴을 끊는다(검토 2026-09-18). 회선 문제의 실패는 CONSECUTIVE_BLOCK_ABORT 가
# 실행 자체를 조기 중단시키므로 이 카운터가 쌓일 여지가 적다.
ITEM_GIVE_UP_RUNS = 3
# 결과 JSON 전체 재작성 주기(건) — 매건 재작성은 대량 수집에서 O(n²) I/O 다.
# 50건 모일 때와 중단·완료 시점에만 재작성하고, CSV 는 매건 append 로 실시간을
# 유지한다. 재개 시 CSV/JSON 을 저장소에서 재구축하므로 유실은 없다.
JSON_FLUSH_INTERVAL = 50

HOME_WARMUP_URL = "https://ko.aliexpress.com/?spm=a2g0o.home.logo.1.6c2f52d1NGQ4SZ"

# 차단 중단 안내 — UI 다이얼로그와 동일 문구를 요약 error 로도 운반한다
BLOCKED_ABORT_GUIDE = (
    "IP 차단이 해소되지 않아 수집을 중단했습니다. "
    "다시 시작할 때 우회 회선(Decodo)을 선택하면 중단 지점부터 이어서 수집됩니다."
)


def _is_blocked_url(url: str) -> bool:
    """AliExpress WAF 차단 신호 URL(punish / tmd) 판정."""
    return "punish" in (url or "") or "_____tmd_____" in (url or "")


class _LineFailure(RuntimeError):
    """한국 우회 회선 확보 실패 — 시도를 중단하고 다른 회선으로 재시작 대상."""


COUPANG_DATASET_FIELDS = [
    "vendor_id",                 # 판매자/스토어 식별자
    "url",                       # 상품 상세 URL
    "store_name",                # 스토어명
    "company_name",              # 회사/상호명 (사업자명)
    "ceo_name",                  # 대표자명
    "business_number",           # 사업자등록번호
    "phone",                     # 사업자 전화번호 (고객센터/소비자상담)
    "email",                     # 사업자 이메일 주소
    "address",                   # 사업장 소재지 (주소)
    "ecommerce_report_number",   # 통신판매업신고번호
    "power_seller",              # 우수판매자 여부 (TRUE/FALSE)
    "power_seller_title",        # 우수판매자 배지 명칭
    "rating_count",              # 누적 판매량
    "thumb_up_ratio",            # 긍정 평가율 (만족도)
    "product_title",             # 상품명
    "price",                     # 판매 가격
    "collected_at"               # 수집 일시
]


@dataclass(frozen=True)
class AliexpressCategoryRunConfig:
    """AliExpress 카테고리 탭 실행 설정."""

    output_dir: Path
    category_name: str
    category_url: str
    max_pages: int = 30
    rotation_batch_size: int = 25
    delay: float = 2.0
    use_proxy: bool = True
    start_fresh: bool = False
    block_cooldown_seconds: float = 15.0  # 차단 감지 직후의 안전 대기(로컬 회선·재시도 직전)

    def __post_init__(self) -> None:
        if not self.category_name.strip():
            raise ValueError("카테고리 이름이 비어 있습니다.")
        if not self.category_url.strip():
            raise ValueError("카테고리 URL이 비어 있습니다.")
        if not 1 <= self.max_pages <= 100:
            raise ValueError("max_pages 는 1~100 사이여야 합니다.")


@dataclass
class AliexpressCrawlSummary:
    """수집 결과 요약 통계.

    termination_reason 은 쿠팡 decodo_run 의 종료 사유 체계와 맞춘 값이다:
    success / empty / blocked / line_error / error / resume_store_error / cancelled
    """

    total_items: int = 0
    collected_items: int = 0
    unique_vendors: int = 0
    has_email: int = 0
    has_business_number: int = 0
    has_ceo_name: int = 0
    # 누적 실패 ITEM_GIVE_UP_RUNS 회로 재시도를 포기해 결과에서 제외된 상품 수
    given_up_items: int = 0
    csv_file: Path | None = None
    json_file: Path | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None
    cancelled: bool = False
    termination_reason: str = ""


class AliexpressCategoryCrawler:
    """AliExpress 카테고리 2단계 수집 엔진.

    crawl() 은 내부적으로 최대 MAX_ATTEMPTS 회 시도하며 시도마다 새 우회 회선을
    쓴다. 진행 기록은 시도 사이에 공유되므로 차단으로 끊겨도 다음 시도·다음
    실행이 저장된 지점부터 이어서 수집한다.
    """

    def __init__(
        self,
        config: AliexpressCategoryRunConfig,
        control: Control | None = None,
        on_log: LogFn = _noop,
        on_phase: PhaseFn = _noop,
        on_progress: ProgressFn = _noop,
        on_collected: CollectedFn = _noop,
        on_stats: StatsFn = _noop,
        on_error: ErrorFn = _noop,
        exit_ip_fn: ExitIpFn | None = None,
    ) -> None:
        self.config = config
        self.control = control
        self.on_log = on_log
        self.on_phase = on_phase
        self.on_progress = on_progress
        self.on_collected = on_collected
        self.on_stats = on_stats
        self.on_error = on_error
        self._exit_ip_fn = exit_ip_fn or decodo.fetch_exit_ip
        # 재개 시 저장본을 테이블에 다시 흘릴 때 중복 행이 생기지 않게 하는 가드
        self._emitted_urls: set[str] = set()

    def crawl(self) -> AliexpressCrawlSummary:
        """동기 인터페이스: 시도 루프를 이벤트 루프에서 실행."""
        return asyncio.run(self._run_with_attempts())

    def _checkpoint(self) -> None:
        """일시정지/취소 체크포인트."""
        if self.control:
            self.control.checkpoint()

    def _sleep(self, seconds: float) -> None:
        """취소·일시정지 가능한 대기. 제어 객체가 없으면 단순 대기."""
        if seconds <= 0:
            return
        if self.control:
            self.control.sleep(seconds)
        else:
            time.sleep(seconds)

    def _new_summary(self, csv_file: Path, json_file: Path) -> AliexpressCrawlSummary:
        return AliexpressCrawlSummary(csv_file=csv_file, json_file=json_file)

    def _apply_store_stats(self, summary: AliexpressCrawlSummary, store: AliexpressResumeStore) -> None:
        """실패·중단 요약에도 저장 누적분이 보이도록 진행 기록에서 통계를 채운다."""
        try:
            summary.collected_items = store.item_result_count
            summary.unique_vendors = store.confirmed_seller_count
        except ResumeStoreError:
            pass

    # ── 시도 루프 (쿠팡 run_category_attempts 대응) ──────────────────────
    async def _run_with_attempts(self) -> AliexpressCrawlSummary:
        start_time = time.time()
        safe_name = re.sub(r'[\\/*?:"<>| ]', "_", self.config.category_name)
        run_dir = ali_category_run_dir(
            self.config.output_dir, self.config.category_name, self.config.category_url
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        csv_file = run_dir / f"ali_category_{safe_name}.csv"
        json_file = run_dir / f"ali_category_{safe_name}.json"

        summary = self._new_summary(csv_file, json_file)

        if self.config.use_proxy and decodo.sticky_proxy_dict() is None:
            summary.termination_reason = "error"
            summary.error = (
                "Decodo 계정(사용자명/비밀번호)이 없습니다. "
                "설정 탭에 입력·저장한 뒤 다시 시작하세요."
            )
            self.on_log(f"[Decodo] {summary.error}")
            summary.elapsed_seconds = round(time.time() - start_time, 1)
            return summary

        store = AliexpressResumeStore(run_dir)
        try:
            try:
                if self.config.start_fresh:
                    archived = store.archive()
                    if archived:
                        self.on_log(f"[아카이브] 이전 진행 기록 보관 완료: {archived.name}")
                store.open()
                if not self.config.start_fresh and store.status == STATUS_FINISHED and store.has_state():
                    # 완료 봉인된 기록은 이어서 수집 대상이 아니다 — 다시 시작은
                    # 새 수집이다(쿠팡 decodo_run 과 동일). 기록은 아카이브로 보존.
                    archived = store.archive()
                    if archived:
                        self.on_log(
                            "[재개] 이 카테고리의 이전 실행은 완료됐습니다 — 진행 기록을 "
                            f"보관하고 처음부터 새로 수집합니다: {archived.name}"
                        )
                    store.open()
                mismatch = store.check_config(
                    self.config.category_name, self.config.category_url, self.config.max_pages
                )
            except ResumeStoreError as e:
                summary.termination_reason = "resume_store_error"
                summary.error = f"진행 기록을 사용할 수 없어 재개를 중단합니다: {e}"
                self.on_log(f"[재개] {summary.error}")
                summary.elapsed_seconds = round(time.time() - start_time, 1)
                return summary

            if mismatch:
                summary.termination_reason = "error"
                summary.error = (
                    f"저장된 진행 기록과 설정이 일치하지 않아 재개를 중단합니다. {mismatch} "
                    "'처음부터 다시 수집'을 선택하거나 설정을 되돌리세요."
                )
                self.on_log(f"[재개] {summary.error}")
                summary.elapsed_seconds = round(time.time() - start_time, 1)
                return summary

            if store.has_state() and not self.config.start_fresh:
                boundary = store.last_item_page
                self.on_log(
                    f"[작업 이어하기] 이전 진행 기록 발견: 1~{store.last_completed_page}페이지 기록 "
                    f"(상품 {store.product_count:,}건 확보 / 판매자 {store.confirmed_seller_count:,}개사 수집 완료) "
                    f"→ {boundary}페이지를 경계로 다시 확인한 뒤 이어 수집합니다."
                )

            self.on_log(f"[Ali 카테고리] 수집 시작: '{self.config.category_name}' (최대 {self.config.max_pages}페이지)")
            self.on_log(f"[설정] 저장 폴더: {run_dir}")

            for attempt in range(1, MAX_ATTEMPTS + 1):
                self._checkpoint()
                self.on_log(
                    f"[시도 {attempt}/{MAX_ATTEMPTS}] "
                    + ("우회 회선(Decodo 한국 고정)" if self.config.use_proxy else "로컬 회선 안전 모드")
                )
                try:
                    summary = await self._crawl_attempt(store, run_dir, csv_file, json_file)
                except CancelledError:
                    live = getattr(self, "_active_summary", None)
                    summary = live if live is not None else self._new_summary(csv_file, json_file)
                    summary.csv_file = csv_file
                    summary.json_file = json_file
                    if int(summary.collected_items or 0) == 0:
                        # 목록 단계 진행 중 취소 — 요약이 비어 있으면 저장 누적분을
                        # 채워 UI 가 실제 확보 규모를 보고하게 한다.
                        self._apply_store_stats(summary, store)
                    summary.cancelled = True
                    summary.termination_reason = "cancelled"
                    self.on_log("[Ali 카테고리] 사용자에 의해 수집이 취소되었습니다.")
                    break
                except ResumeStoreError as e:
                    # 진행 기록 쓰기 실패 — 회선을 바꿔도 같은 지점에서 다시
                    # 실패하므로 재시도하지 않는다(쿠팡 decodo_run 규율 동일).
                    summary = self._new_summary(csv_file, json_file)
                    summary.termination_reason = "resume_store_error"
                    summary.error = f"진행 기록 저장 실패로 중단했습니다: {e}"
                    self.on_log(f"[재개] {summary.error}")
                    return summary
                except _LineFailure as e:
                    summary = self._new_summary(csv_file, json_file)
                    summary.termination_reason = "line_error"
                    summary.error = str(e)
                    self._apply_store_stats(summary, store)
                    self.on_log(f"[Decodo] 시도 {attempt} 회선 확보 실패 — {e}")
                except Exception as e:
                    summary = self._new_summary(csv_file, json_file)
                    summary.termination_reason = "error"
                    summary.error = f"{type(e).__name__}: {e}"
                    self._apply_store_stats(summary, store)
                    self.on_log(f"[오류] 시도 {attempt} 실패: {summary.error}")
                else:
                    if summary.cancelled or summary.termination_reason in ("success", "empty"):
                        break
                    if int(summary.collected_items or 0) == 0:
                        # 목록 단계에서 끝난 시도 — 요약에 저장 누적분이라도
                        # 비치도록 채운다(시도 소진 시 마지막 요약이 최종 표시된다).
                        self._apply_store_stats(summary, store)
                    self.on_log(f"[Ali 카테고리] 시도 {attempt} 중단 ({summary.termination_reason})")

                if attempt >= MAX_ATTEMPTS:
                    self.on_log(f"[Ali 카테고리] {MAX_ATTEMPTS}회 모두 실패 — 마지막 상태로 종료합니다")
                    break
                self.on_log(f"[Ali 카테고리] {int(RETRY_WAIT_SECONDS)}초 후 새 회선으로 다시 시도합니다")
                try:
                    self._sleep(RETRY_WAIT_SECONDS)
                except CancelledError:
                    summary.cancelled = True
                    summary.termination_reason = "cancelled"
                    self.on_log("[Ali 카테고리] 재시도 대기 중 취소되었습니다.")
                    break
        finally:
            store.close()

        summary.elapsed_seconds = round(time.time() - start_time, 1)
        if summary.termination_reason == "success":
            self.on_log(
                f"\n[Ali 카테고리 완료] 총 {summary.collected_items:,}건 수집 완료 "
                f"(고유 판매자 {summary.unique_vendors}개사, 소요 {summary.elapsed_seconds}초)"
            )
            self.on_log(f"  -> 이메일 확보: {summary.has_email:,}건 | 사업자번호: {summary.has_business_number:,}건")
            self.on_log(f"  -> 파일: {csv_file}")
        return summary

    # ── 1회 시도: 브라우저 세션 1개(회선 1개)로 전체 파이프라인 실행 ────
    async def _crawl_attempt(
        self,
        store: AliexpressResumeStore,
        run_dir: Path,
        csv_file: Path,
        json_file: Path,
    ) -> AliexpressCrawlSummary:
        summary = self._new_summary(csv_file, json_file)
        # 취소·예외로 시도가 끊겨도 지금까지의 통계를 잃지 않게 요약을 보관한다
        self._active_summary = summary

        async with async_playwright() as p:
            session_counter = 0
            current_browser = None
            current_context = None
            current_page = None

            async def close_session() -> None:
                nonlocal current_browser, current_context, current_page
                if current_page:
                    try:
                        await current_page.close()
                    except Exception:
                        pass
                    current_page = None
                if current_context:
                    try:
                        await current_context.close()
                    except Exception:
                        pass
                    current_context = None
                if current_browser:
                    try:
                        await current_browser.close()
                    except Exception:
                        pass
                    current_browser = None

            async def open_new_session() -> None:
                """새 스티키 세션 확보. 우회 모드에서는 한국 출발 회선만 채택한다
                (쿠팡 _korea_proxy 와 동일 — 확인 실패 시 MAX_GEO_ROTATIONS 회 교체)."""
                nonlocal session_counter, current_browser, current_context, current_page
                await close_session()

                proxy = None
                if self.config.use_proxy:
                    last_geo_reason = ""
                    for geo in range(1, MAX_GEO_ROTATIONS + 1):
                        self._checkpoint()
                        session_counter += 1
                        sid = f"ali_cat_{int(time.time()) % 100000}_{session_counter}"
                        candidate = decodo.sticky_proxy_dict(session_id=sid)
                        if candidate is None:
                            raise _LineFailure(
                                "Decodo 계정(사용자명/비밀번호)을 읽지 못했습니다. "
                                "로컬 회선으로 잘못 수집하는 일을 막기 위해 중단합니다."
                            )
                        self.on_log(
                            f"[Decodo] 회선 확인 {geo}/{MAX_GEO_ROTATIONS} — {decodo.proxy_summary(candidate)}"
                        )
                        try:
                            info = await asyncio.to_thread(self._exit_ip_fn, candidate)
                        except CancelledError:
                            raise
                        except Exception as e:  # DecodoError 포함 — 다른 회선으로
                            last_geo_reason = str(e)
                            self.on_log(f"[Decodo] 회선 확인 실패 — {last_geo_reason}. 다른 회선으로 바꿉니다.")
                            continue
                        country = info.country_name or info.country_code or "?"
                        if info.is_korea:
                            self.on_log(f"[Decodo] 한국 회선 확인 — {info.ip} ({country})")
                            proxy = candidate
                            break
                        last_geo_reason = country
                        self.on_log(f"[Decodo] 한국이 아닌 회선 ({country}) — 새 회선으로 교체")
                    if proxy is None:
                        raise _LineFailure(
                            "한국 회선을 확보하지 못했습니다"
                            + (f" ({last_geo_reason})" if last_geo_reason else "")
                        )

                launch_args = {
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                    ]
                }
                if proxy:
                    launch_args["proxy"] = proxy

                current_browser = await p.chromium.launch(**launch_args)
                current_context = await current_browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    locale="ko-KR",
                    viewport={"width": 1440, "height": 900}
                )
                await current_context.add_cookies([
                    {"name": "aep_usuc_f", "value": "region=KR&b_locale=ko_KR&site=kor&c_tp=KRW", "domain": ".aliexpress.com", "path": "/"},
                    {"name": "xman_us_f", "value": "x_locale=ko_KR&x_l=0", "domain": ".aliexpress.com", "path": "/"}
                ])
                current_page = await current_context.new_page()

                # 홈 웜업
                try:
                    await current_page.goto(HOME_WARMUP_URL, wait_until="domcontentloaded", timeout=35000)
                    await current_page.wait_for_timeout(2000)
                except Exception:
                    pass

            async def block_backoff() -> None:
                """차단 신호 직후의 안전 조치 — 우회 모드면 회선 교체, 로컬이면 쿨다운."""
                if self.config.use_proxy:
                    self.on_log("    보안 차단 감지 — 새 우회 회선으로 교체합니다")
                    await open_new_session()
                else:
                    self.on_log(
                        f"    보안 차단 감지 — {self.config.block_cooldown_seconds:.0f}초 안전 대기 후 재시도"
                    )
                    if current_page:
                        await current_page.wait_for_timeout(int(self.config.block_cooldown_seconds * 1000))

            # ── Phase 1: 카테고리 리스팅 순회 ────────────────────────────
            self.on_phase("Phase 1: 카테고리 목록 탐색", 1, 2)
            unique_products: dict[str, dict] = {}

            if store.status == STATUS_LISTING_DONE:
                for prod in store.load_products():
                    unique_products[prod["id"]] = prod
                self.on_log(f"[Phase 1 건너뛰기] 이미 카테고리 목록 수집 완료됨 (총 {len(unique_products):,}개 상품 복원)")
            else:
                if store.last_completed_page > 0:
                    for prod in store.load_products():
                        unique_products[prod["id"]] = prod
                    self.on_log(
                        f"  [목록 복원] 1~{store.last_completed_page}페이지 기확보 상품 {len(unique_products):,}개 복원"
                    )
                    # 이전 실행의 빈 페이지 연속값을 이월하지 않는다 — 새 회선에서
                    # 0부터 다시 관찰한다(쿠팡 search_crawler reset_empty_streak 규율).
                    store.reset_empty_streak()

                # 재개 경계는 상품이 확인된 마지막 페이지(last_item_page)다. 0건으로
                # 기록된 페이지(소프트 차단·렌더 지연)는 미확인으로 간주해 다시 읽는다.
                boundary = store.last_item_page
                start_page = boundary + 1
                empty_streak = store.empty_streak

                if start_page <= self.config.max_pages:
                    if current_page is None:
                        await open_new_session()

                    sep = "&" if "?" in self.config.category_url else "?"
                    page_failures = 0

                    async def load_listing_page(p_num: int, label: str = "") -> tuple[list[dict], str]:
                        """목록 페이지 1장을 로드해 상품 카드를 추출한다.

                        로드 지연·차단 backoff 는 같은 페이지로 재시도하며, 재시도를
                        소진하면 요약에 종료 사유를 남기고 상태를 반환한다 —
                        "ok" / "line_stop" / "blocked_stop".
                        """
                        nonlocal page_failures
                        shown = label or f"{p_num}/{self.config.max_pages}"
                        p_url = f"{self.config.category_url}{sep}page={p_num}"
                        self.on_log(f"  [페이지 {shown}] 로드 중...")

                        while True:
                            try:
                                await current_page.goto(p_url, wait_until="domcontentloaded", timeout=35000)
                                await current_page.wait_for_timeout(1800)
                            except Exception as e:
                                page_failures += 1
                                if page_failures >= PHASE1_PAGE_RETRIES:
                                    summary.termination_reason = "line_error"
                                    summary.error = (
                                        f"목록 {p_num}페이지를 {page_failures}회 불러오지 못해 중단합니다: {e}"
                                    )
                                    return [], "line_stop"
                                self.on_log(f"    페이지 로드 지연 — 같은 페이지 재시도 ({page_failures}/{PHASE1_PAGE_RETRIES})")
                                await current_page.wait_for_timeout(3000)
                                continue

                            if _is_blocked_url(current_page.url):
                                page_failures += 1
                                if page_failures >= PHASE1_PAGE_RETRIES:
                                    summary.termination_reason = "blocked"
                                    summary.error = BLOCKED_ABORT_GUIDE
                                    self.on_log(
                                        f"    목록 {p_num}페이지 차단 {page_failures}회 지속 — 안전 중단. "
                                        "저장된 지점부터 재개됩니다."
                                    )
                                    return [], "blocked_stop"
                                await block_backoff()
                                continue
                            page_failures = 0

                            await current_page.evaluate("window.scrollBy(0, 1500)")
                            await current_page.wait_for_timeout(800)
                            await current_page.evaluate("window.scrollBy(0, 1500)")
                            await current_page.wait_for_timeout(800)

                            items = await current_page.evaluate("""() => {
                                const list = [];
                                const cards = Array.from(document.querySelectorAll('a[href*=\\"/item/\\"]'));
                                for (const a of cards) {
                                    const href = a.href || '';
                                    const m = href.match(/item\\/(\\d+)\\.html/);
                                    if (!m) continue;
                                    const id = m[1];
                                    if (list.some(x => x.id === id)) continue;

                                    const card = a.closest('[class*=\\"search-item\\"], [class*=\\"list--item\\"], div') || a;
                                    const text = card.innerText || '';
                                    const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);

                                    let title = a.title || '';
                                    if (!title) {
                                        title = lines.find(l => !l.startsWith('-') && !l.startsWith('₩') && l.length > 5) || lines[0] || '';
                                    }

                                    let price = '';
                                    const pm = text.match(/₩[\\d,]+/);
                                    if (pm) price = pm[0];

                                    let orders = '';
                                    const om = text.match(/([\\d,]+(?:\\+|개)?\\s*판매)/);
                                    if (om) orders = om[1];

                                    const isTop = text.includes('TOP셀러') || text.includes('Top Seller');

                                    list.push({
                                        id: id,
                                        url: href,
                                        title: title.trim(),
                                        price: price,
                                        orders: orders,
                                        is_top_seller: isTop
                                    });
                                }
                                return list;
                            }""")
                            return items, "ok"

                    def take_new(items: list[dict]) -> int:
                        """신규 상품만 unique_products 에 합치고 신규 수를 반환."""
                        fresh = 0
                        for itm in items:
                            if itm["id"] not in unique_products:
                                unique_products[itm["id"]] = itm
                                fresh += 1
                        return fresh

                    if boundary > 0:
                        # 경계 페이지 재확인 — 마지막 저장 페이지를 새 회선에서 다시
                        # 읽어 누락·순서 변경을 되돌린 뒤 다음 페이지로 넘어간다
                        # (쿠팡 search_crawler 경계확인 규율 동일).
                        items_b, status_b = await load_listing_page(
                            boundary, f"{boundary}/{self.config.max_pages} 경계확인"
                        )
                        if status_b != "ok":
                            self.on_log(f"    {summary.error} — 다음 시도(회선)에서 이어서 수집합니다.")
                            await close_session()
                            return summary
                        fresh_b = take_new(items_b)
                        # 재동기화 단계 — 상품이 보이면 빈 페이지 판정을 리셋한다.
                        store.record_page(boundary, items_b, new_count=len(items_b))
                        empty_streak = store.empty_streak
                        if fresh_b > 0:
                            self.on_log(
                                f"  [재개] 경계 페이지 {boundary}에서 신규 상품 {fresh_b}개 — "
                                "목록 순서가 바뀌었습니다. 재확인분을 누적에 반영했습니다."
                            )

                    p_num = start_page
                    while p_num <= self.config.max_pages:
                        self._checkpoint()
                        self.on_progress("목록 탐색", p_num, self.config.max_pages)
                        items, status = await load_listing_page(p_num)
                        if status != "ok":
                            self.on_log(f"    {summary.error} — 다음 시도(회선)에서 이어서 수집합니다.")
                            await close_session()
                            return summary

                        new_count = take_new(items)
                        store.record_page(p_num, items, new_count=new_count)
                        empty_streak = store.empty_streak
                        self.on_log(f"    -> 발견 {len(items)}개 (신규 {new_count}개 / 누적 {len(unique_products)}개)")

                        if empty_streak >= 2:
                            self.on_log(f"    -> 연속 빈 페이지 감지: 목록 순회 종료 (총 {len(unique_products)}개 확보)")
                            break

                        await current_page.wait_for_timeout(1000)
                        p_num += 1

                    store.mark_listing_done(end_reason="empty" if empty_streak >= 2 else "max_pages")
                else:
                    store.mark_listing_done(end_reason="max_pages")

            total_items = len(unique_products)
            summary.total_items = total_items
            product_targets = list(unique_products.values())

            if total_items == 0:
                self.on_log("[오류] 상품 목록을 찾을 수 없습니다. 카테고리 URL을 확인하세요.")
                summary.termination_reason = "empty"
                await close_session()
                return summary

            self.on_log(f"[Phase 1 완료] 총 {total_items:,}개 상품 목록 확보 완료.")

            # ── Phase 2: 판매자 상세 사업자 정보 수집 ─────────────────────
            self.on_phase("Phase 2: 판매자 상세 수집", 2, 2)
            self.on_log(f"[Phase 2 시작] {total_items:,}개 상품 대상 사업자정보(이메일, 대표자, 사업자번호 등) 수집...")

            collected_records: list[dict] = store.load_item_results()
            processed_items: set[str] = store.processed_item_ids()
            vendor_cache: dict[str, dict] = store.confirmed_sellers()
            # 영구 실패 추적 — 누적 실패가 상한에 도달한 상품은 이번 실행에서도
            # 재시도하지 않는다(ITEM_GIVE_UP_RUNS 주석의 비수렴 방지 규율).
            give_up_ids = store.give_up_item_ids(ITEM_GIVE_UP_RUNS)
            req_count_in_session = 0

            # 기존 저장소에서 고유 판매자 시드 자동 로드
            for f_path in list(run_dir.glob("*.json")) + list(self.config.output_dir.glob("*.json")):
                if f_path.name == json_file.name:
                    continue
                try:
                    with open(f_path, "r", encoding="utf-8") as f:
                        prev_records = json.load(f)
                        if isinstance(prev_records, list):
                            for pr in prev_records:
                                vid = pr.get("vendor_id")
                                cname = pr.get("company_name")
                                bnum = pr.get("business_number")
                                if vid and cname and bnum and cname != "(상세 미기재)" and vid not in vendor_cache:
                                    vendor_cache[vid] = {
                                        "store_name": pr.get("store_name", ""),
                                        "company_name": cname,
                                        "ceo_name": pr.get("ceo_name", ""),
                                        "business_number": bnum,
                                        "phone": pr.get("phone", ""),
                                        "email": pr.get("email", ""),
                                        "address": pr.get("address", ""),
                                        "ecommerce_report_number": pr.get("ecommerce_report_number", ""),
                                    }
                except Exception:
                    pass

            if collected_records:
                self.on_log(
                    f"  [진행 복원] 기확보 상품 {len(collected_records):,}건, 판매자 {len(vendor_cache):,}개사 결과 복원 완료."
                )
                for r in collected_records:
                    r_url = str(r.get("url") or "")
                    if r_url and r_url in self._emitted_urls:
                        continue
                    if r_url:
                        self._emitted_urls.add(r_url)
                    self.on_collected(r)
                summary.collected_items = len(collected_records)
                summary.unique_vendors = len(vendor_cache)
                summary.has_email = sum(1 for r in collected_records if r.get("email"))
                summary.has_business_number = sum(1 for r in collected_records if r.get("business_number"))
                summary.has_ceo_name = sum(1 for r in collected_records if r.get("ceo_name"))
                self.on_stats(summary)
            elif vendor_cache:
                self.on_log(f"  [시드 로드] 기존 검증된 판매자 {len(vendor_cache)}개사 정보 자동 탑재 완료.")

            # CSV 헤더 작성 및 복원 데이터 기록
            with open(csv_file, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
                writer.writeheader()
                for r in collected_records:
                    writer.writerow(r)

            def flush_json() -> None:
                """누적 결과 전체를 JSON 에 기록 — 주기 도달·중단·완료 시점 호출."""
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(collected_records, f, ensure_ascii=False, indent=2)

            flush_json()
            records_since_flush = 0

            unprocessed = [itm for itm in product_targets if itm["id"] not in processed_items]
            if unprocessed and current_page is None:
                await open_new_session()

            consecutive_failures = 0
            dropped_count = 0

            for idx, itm in enumerate(product_targets, 1):
                self._checkpoint()
                self.on_progress("상세 수집", idx, total_items)

                p_id = itm["id"]
                p_url = itm["url"]
                p_title = itm["title"]

                # 이미 수집 완료된 상품은 고속 스킵
                if p_id in processed_items:
                    continue

                # 누적 실패 상한 도달 상품 — 재시도하지 않고 결과에서 제외
                if p_id in give_up_ids:
                    summary.given_up_items += 1
                    self.on_log(
                        f"  [{idx}/{total_items}] 누적 실패 {ITEM_GIVE_UP_RUNS}회 상품 — "
                        "재시도를 포기합니다 (삭제·변경된 상품으로 보입니다)"
                    )
                    continue

                # rotation_batch_size 주기 회선 자동 순환 (프록시 사용 시에만)
                if self.config.use_proxy and req_count_in_session >= self.config.rotation_batch_size:
                    self.on_log(f"  [회선 자동 순환] {req_count_in_session}건 도달 -> 새 주거용 회선 세션 교체...")
                    await open_new_session()
                    req_count_in_session = 0

                record = None
                clean_url = f"https://ko.aliexpress.com/item/{p_id}.html"

                for attempt in range(1, PDP_ITEM_RETRIES + 1):
                    self._checkpoint()
                    pdp_json_data = {}
                    pdp_received = False

                    async def handle_response(response):
                        nonlocal pdp_json_data, pdp_received
                        if "mtop.aliexpress.pdp.pc.query" in response.url and response.status == 200:
                            try:
                                b = await response.body()
                                txt = b.decode("utf-8", errors="ignore")
                                if "PRODUCT_PROP_PC" in txt or "SHOP_CARD_PC" in txt:
                                    m = re.search(r'^[^{]*(\{.*\})[^}]*$', txt, re.DOTALL)
                                    if m:
                                        pdp_json_data = json.loads(m.group(1))
                                        pdp_received = True
                            except Exception:
                                pass

                    current_page.on("response", handle_response)

                    try:
                        await current_page.goto(clean_url, wait_until="domcontentloaded", timeout=25000)
                        for _ in range(35):
                            if pdp_received:
                                break
                            await current_page.wait_for_timeout(100)
                    except Exception:
                        await current_page.wait_for_timeout(500)

                    current_page.remove_listener("response", handle_response)
                    req_count_in_session += 1

                    if _is_blocked_url(current_page.url):
                        self.on_log(
                            f"  [{idx}/{total_items}] 보안 신호 감지 -> "
                            + ("새 회선 세션으로 자가 교체" if self.config.use_proxy else "안전 쿨다운 대기")
                            + f" (재시도 {attempt}/3)..."
                        )
                        await block_backoff()
                        if self.config.use_proxy:
                            req_count_in_session = 0
                        continue

                    if not pdp_received:
                        # 소프트 차단(페이지는 열리지만 상품 데이터 미수신) — 빈
                        # 껍데기를 기록하지 않고 같은 회선에서 재시도한다.
                        self.on_log(
                            f"  [{idx}/{total_items}] 상품 데이터 미수신(차단 의심) — 재시도 {attempt}/3..."
                        )
                        await current_page.wait_for_timeout(int(self.config.block_cooldown_seconds * 1000))
                        continue

                    # ── 파싱 (상품 데이터를 받았을 때만 기록으로 이어진다) ──
                    result = pdp_json_data.get("data", {}).get("result", {})
                    shop_card = result.get("SHOP_CARD_PC", {})
                    product_props = result.get("PRODUCT_PROP_PC", {})
                    global_data = result.get("GLOBAL_DATA", {}).get("globalData", {})

                    store_name = shop_card.get("storeName") or ""
                    store_rating = ""
                    for b in shop_card.get("benefitInfoList", []):
                        if "좋아요" in b.get("title", "") or "positive" in b.get("title", "").lower():
                            store_rating = b.get("value", "")

                    vendor_id = str(global_data.get("sellerId") or "")
                    if not vendor_id and shop_card.get("licenseInfo"):
                        lm = re.search(r'storeNum=(\d+)', shop_card["licenseInfo"].get("actionTarget", ""))
                        if lm:
                            vendor_id = lm.group(1)
                    if not vendor_id:
                        vendor_id = p_id

                    # 캐시 적중 확인
                    if vendor_id in vendor_cache:
                        c_info = vendor_cache[vendor_id]
                        record = {
                            "vendor_id": vendor_id,
                            "url": p_url,
                            "store_name": store_name or c_info.get("store_name", ""),
                            "company_name": c_info["company_name"],
                            "ceo_name": c_info["ceo_name"],
                            "business_number": c_info["business_number"],
                            "phone": c_info["phone"],
                            "email": c_info["email"],
                            "address": c_info["address"],
                            "ecommerce_report_number": c_info["ecommerce_report_number"],
                            "power_seller": "TRUE" if itm.get("is_top_seller") else "FALSE",
                            "power_seller_title": "AliExpress TOP셀러" if itm.get("is_top_seller") else "",
                            "rating_count": itm.get("orders", ""),
                            "thumb_up_ratio": store_rating,
                            "product_title": p_title,
                            "price": itm.get("price", ""),
                            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        break

                    # 7대 핵심 정보 파싱
                    company_name = ""
                    ceo_name = ""
                    business_number = ""
                    phone = ""
                    email = ""
                    address = ""
                    ecommerce_report_number = ""

                    showed_props = product_props.get("showedProps", [])
                    for prop in showed_props:
                        name = prop.get("attrName", "").strip()
                        val = prop.get("attrValue", "").strip()

                        if name in ("회사 이름", "회사명", "상호명", "상호", "제조업체", "제조자", "생산자"):
                            if val and not company_name and "참조" not in val:
                                company_name = val
                        elif name in ("대표자", "대표자명", "대표", "성명"):
                            ceo_name = val
                        elif name in ("사업자번호", "사업자등록번호"):
                            business_number = val
                        elif name in ("소비자상담전화번호", "전화번호", "연락처", "고객센터"):
                            if "참조" not in val:
                                phone = val
                        elif name in ("이메일 주소", "이메일", "E-mail", "Email"):
                            email = val
                        elif name in ("사업장소재지", "주소", "소재지"):
                            address = val
                        elif name in ("통신판매업신고번호", "통신판매업신고"):
                            ecommerce_report_number = val
                        elif name == "원산지" and not address:
                            address = val

                    props_map = product_props.get("showedPropsMap", {})
                    for k, v in props_map.items():
                        name = v.get("attrName", "").strip()
                        val = v.get("attrValue", "").strip()
                        if name == "이메일 주소" and not email: email = val
                        if name == "대표자" and not ceo_name: ceo_name = val
                        if name == "사업자번호" and not business_number: business_number = val
                        if name in ("회사 이름", "상호명") and not company_name and "참조" not in val: company_name = val
                        if name in ("소비자상담전화번호", "고객센터") and not phone and "참조" not in val: phone = val
                        if name == "사업장소재지" and not address: address = val
                        if name == "통신판매업신고번호" and not ecommerce_report_number: ecommerce_report_number = val

                    if not company_name and not store_name:
                        company_name = "(상세 미기재)"

                    v_data = {
                        "store_name": store_name,
                        "company_name": company_name,
                        "ceo_name": ceo_name,
                        "business_number": business_number,
                        "phone": phone,
                        "email": email,
                        "address": address,
                        "ecommerce_report_number": ecommerce_report_number,
                    }
                    if company_name and company_name != "(상세 미기재)":
                        vendor_cache[vendor_id] = v_data
                    store.record_seller(vendor_id, v_data)

                    record = {
                        "vendor_id": vendor_id,
                        "url": p_url,
                        "store_name": store_name,
                        "company_name": company_name,
                        "ceo_name": ceo_name,
                        "business_number": business_number,
                        "phone": phone,
                        "email": email,
                        "address": address,
                        "ecommerce_report_number": ecommerce_report_number,
                        "power_seller": "TRUE" if itm.get("is_top_seller") else "FALSE",
                        "power_seller_title": "AliExpress TOP셀러" if itm.get("is_top_seller") else "",
                        "rating_count": itm.get("orders", ""),
                        "thumb_up_ratio": store_rating,
                        "product_title": p_title,
                        "price": itm.get("price", ""),
                        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    break

                if record:
                    consecutive_failures = 0
                    collected_records.append(record)
                    processed_items.add(p_id)
                    store.record_item_result(p_id, record["vendor_id"], record)

                    # 실시간 CSV append + 주기적 JSON 전체 재작성
                    with open(csv_file, "a", encoding="utf-8-sig", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=COUPANG_DATASET_FIELDS)
                        writer.writerow(record)

                    records_since_flush += 1
                    if records_since_flush >= JSON_FLUSH_INTERVAL:
                        flush_json()
                        records_since_flush = 0

                    # 콜백 및 통계 — 증분 집계(매건 전체 재순회 금지)
                    if p_url not in self._emitted_urls:
                        self._emitted_urls.add(p_url)
                    self.on_collected(record)
                    if record.get("email"):
                        summary.has_email += 1
                    if record.get("business_number"):
                        summary.has_business_number += 1
                    if record.get("ceo_name"):
                        summary.has_ceo_name += 1
                    summary.collected_items = len(collected_records)
                    summary.unique_vendors = len(vendor_cache)
                    self.on_stats(summary)

                    biz_desc = f"[{record['company_name'] or record['store_name']}] 대표: {record['ceo_name'] or '-'} | 사업자: {record['business_number'] or '-'} | 이메일: {record['email'] or '-'}"
                    self.on_log(f"  [{idx}/{total_items}] 수집: {biz_desc} (누적 {len(collected_records)}건, 고유판매자 {len(vendor_cache)}개사)")
                else:
                    consecutive_failures += 1
                    total_failures = store.record_item_failure(p_id)
                    if total_failures >= ITEM_GIVE_UP_RUNS:
                        # 이 실행에서 포기 — dropped_count 에 넣지 않아 남은 수집이
                        # 모이면 완료 봉인이 가능해진다(비수렴 방지).
                        give_up_ids.add(p_id)
                        summary.given_up_items += 1
                        self.on_log(
                            f"  [{idx}/{total_items}] 수집 실패 누적 {total_failures}회 — "
                            "이 상품은 재시도를 포기합니다. "
                            f"(연속 실패 {consecutive_failures}/{CONSECUTIVE_BLOCK_ABORT})"
                        )
                    else:
                        dropped_count += 1
                        self.on_log(
                            f"  [{idx}/{total_items}] 수집 실패(차단 의심) — 건너뜁니다. "
                            f"다음 시작 때 이 상품부터 다시 시도됩니다. "
                            f"(연속 실패 {consecutive_failures}/{CONSECUTIVE_BLOCK_ABORT}, "
                            f"누적 {total_failures}/{ITEM_GIVE_UP_RUNS})"
                        )
                    if consecutive_failures >= CONSECUTIVE_BLOCK_ABORT:
                        summary.termination_reason = "blocked"
                        summary.error = BLOCKED_ABORT_GUIDE
                        self.on_log(f"[차단 중단] 연속 {consecutive_failures}건 실패 — 안전 중단. {BLOCKED_ABORT_GUIDE}")
                        flush_json()
                        await close_session()
                        return summary

                await current_page.wait_for_timeout(int(self.config.delay * 1000))

            if dropped_count > 0:
                # 실패(데이터 미수신) 상품이 남으면 완료 봉인하지 않는다 — 다음
                # 시작 때 이 상품부터 다시 시도된다.
                summary.termination_reason = "blocked"
                summary.error = BLOCKED_ABORT_GUIDE
                self.on_log(
                    f"[차단 중단] 수집 못 건 상품 {dropped_count}건 — 완료 봉인하지 않고 종료. {BLOCKED_ABORT_GUIDE}"
                )
                flush_json()
                await close_session()
                return summary

            if summary.given_up_items > 0:
                self.on_log(
                    f"[완료 제외] 누적 실패 {ITEM_GIVE_UP_RUNS}회로 재시도를 포기한 상품 "
                    f"{summary.given_up_items}건은 결과에서 제외됐습니다."
                )
            summary.termination_reason = "success"
            store.mark_finished()
            flush_json()
            await close_session()

        return summary
