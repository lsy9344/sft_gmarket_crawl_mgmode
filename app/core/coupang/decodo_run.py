"""Coupang 카테고리 1건 실행 — Decodo 스티키 세션 + 실패 시 회선 교체 재시도.

탭에서 카테고리 하나를 고르고 시작하면 이 함수가 엔진을 최대 3회 돌린다.
Qt 비의존. 채택 조건: 상품 > 0 이고 종료가 완료 또는 일부 수집(page_limit).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.core import config as app_config
from app.core.base import CancelledError, Control
from app.core.coupang.resume_store import (
    STATUS_FINISHED,
    ResumeStore,
    ResumeStoreError,
)
from app.core.decodo import (
    DecodoError,
    DecodoSettings,
    ExitIpInfo,
    credentials_ready,
    fetch_exit_ip,
    make_session_id,
    proxy_summary,
    sticky_proxy_dict,
)
from app.models.coupang_records import CoupangRunSummary

MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 90.0
MAX_GEO_ROTATIONS = 3
# 아직 어떤 시도도 결과를 내지 않았을 때의 자리표시자 문구 — 취소 시 이 값이
# 남아 있으면 실제 결과가 없다는 뜻이므로 요약을 새로 만든다.
_NOT_RUN_ERROR = "실행되지 않았습니다"

LogFn = Callable[[str], None]
AttemptFn = Callable[[int, int], None]
CrawlerFactory = Callable[[Any], Any]
ExitIpFn = Callable[[dict], ExitIpInfo]

_FOLDER_UNSAFE = re.compile(r"[^\w가-힣]+")


def _global_block_state_dir(config: Any) -> Path | None:
    """이 PC 회선 차단의 전역 기록 폴더 — 출력 루트. 카테고리 폴더와 같으면 None.

    트리 로드(CategoryWorker)도 같은 위치에 차단을 기록하므로, 카테고리 수집
    게이트가 트리 로드 차단까지 일관되게 존중하게 된다.
    """
    output_root = Path(getattr(config, "output_dir", ".") or ".")
    global_dir = Path(app_config.DEFAULT_OUTPUT_DIR)
    if global_dir == output_root:
        return None
    return global_dir


def should_keep_attempt(summary: CoupangRunSummary) -> bool:
    """이 시도를 재시도 없이 채택한다. 완료와 일부 수집(page_limit)을 포함한다.

    resume_shifted·resume_pending·mapping_pending 은 재개 실행의 정상 종료
    (보수적 일부 수집 표시)이므로 채택 대상이다 — 회선을 바꿔도 더 나아지지
    않는다.
    """
    if summary.cancelled:
        return False
    if int(summary.products_seen or 0) <= 0:
        return False
    return summary.termination_reason in (
        "success", "page_limit", "resume_shifted", "resume_pending",
        "mapping_pending",
    )


def _remember_saved(
    best: CoupangRunSummary | None, current: CoupangRunSummary,
) -> CoupangRunSummary | None:
    if not current.json_path:
        return best
    if best is None:
        return current
    if int(current.products_seen or 0) >= int(best.products_seen or 0):
        return current
    return best


def _with_saved(
    summary: CoupangRunSummary, saved: CoupangRunSummary | None,
) -> CoupangRunSummary:
    """마지막 시도가 비어도 앞선 저장본을 결과 열기에 남긴다."""
    if saved is None or summary.json_path:
        return summary
    summary.json_path = saved.json_path
    summary.csv_path = saved.csv_path
    if not summary.records and saved.records:
        summary.records = saved.records
    if int(summary.products_seen or 0) == 0:
        summary.products_seen = saved.products_seen
        summary.unique_vendors = saved.unique_vendors
        summary.business_info_success = saved.business_info_success
    return summary


def should_retry(summary: CoupangRunSummary) -> bool:
    """홈 IP 12시간 쿨다운·사용자 취소·판매자 단계(회선 IP) 차단·진행 기록
    저장 실패는 회선을 바꿔도 소용 없다."""
    if summary.cancelled:
        return False
    if summary.termination_reason in ("block_cooldown", "resume_store_error"):
        return False
    if getattr(summary, "blocked_direct", False):
        # 브라우저 회선이 아니라 이 PC 회선이 막힌 것 — Decodo 세션을 바꿔
        # 목록 단계부터 다시 돌리는 것은 무의미하다(작업지시서 §3.3).
        return False
    return not should_keep_attempt(summary)


def category_run_dir(
    output_dir: Path,
    category_id: str = "",
    category_name: str = "",
) -> Path:
    """사용자 선택 폴더 아래 카테고리 전용 하위 폴더.

    프로필·차단 기록이 카테고리 사이에 섞이지 않게 한다.
    """
    cid = "".join(ch for ch in str(category_id or "") if ch.isdigit()) or "run"
    raw = str(category_name or "").strip() or "category"
    name = _FOLDER_UNSAFE.sub("_", raw).strip("._")[:40] or "category"
    return Path(output_dir) / f"{name}_{cid}"


def _korea_proxy(
    settings: DecodoSettings,
    control: Control,
    log: LogFn,
    attempt: int,
    *,
    exit_ip_fn: ExitIpFn,
    rotations: int = MAX_GEO_ROTATIONS,
) -> tuple[dict | None, str]:
    """한국 출발이 확인된 스티키 세션. 아니면 (None, 사유)."""
    last_reason = "회선 확인 실패"
    for geo in range(1, max(1, int(rotations)) + 1):
        control.checkpoint()
        sid = make_session_id(attempt * 100 + geo)
        proxy = sticky_proxy_dict(settings, sid)
        log(
            f"[Decodo] 회선 확인 {geo}/{rotations} — "
            f"{proxy_summary(proxy)}"
        )
        try:
            info = exit_ip_fn(proxy)
        except CancelledError:
            raise
        except DecodoError as e:
            last_reason = str(e)
            log(f"[Decodo] 회선 확인 실패 — {last_reason}. 다른 회선으로 바꿉니다.")
            continue
        country = info.country_name or info.country_code or "?"
        if info.is_korea:
            log(f"[Decodo] 한국 회선 확인 — {info.ip} ({country})")
            return proxy, ""
        last_reason = country
        log(
            f"[Decodo] 한국이 아닌 회선 ({country}"
            f"{', ' + info.ip if info.ip else ''}) — 새 회선"
        )
    return None, last_reason


def run_category_attempts(
    config: Any,
    settings: DecodoSettings,
    control: Control,
    *,
    crawler_factory: CrawlerFactory,
    on_log: LogFn | None = None,
    on_attempt_start: AttemptFn | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    retry_wait: float = RETRY_WAIT_SECONDS,
    exit_ip_fn: ExitIpFn | None = None,
    start_fresh: bool = False,
) -> CoupangRunSummary:
    """카테고리 1개를 최대 max_attempts 회, 시도마다 새 스티키 세션으로 실행.

    시도(회선) 간에 카테고리 폴더의 진행 기록(ResumeStore)을 공유해, 차단·
    종료 직전까지 저장된 페이지·판매자부터 이어서 수집한다(재개). 기존 기록과
    설정이 불일치하면 재개하지 않고 이유를 알린다. start_fresh=True 면 기존
    기록을 아카이브로 보존한 뒤 처음부터 시작한다.
    """
    log = on_log or (lambda _m: None)
    check_exit = exit_ip_fn or fetch_exit_ip
    attempts = max(1, int(max_attempts))
    last = CoupangRunSummary()
    last.termination_reason = "error"
    last.error = _NOT_RUN_ERROR

    if not credentials_ready(settings):
        last.error = (
            "Decodo 계정(사용자명/비밀번호)이 없습니다. "
            "설정 탭에 입력·저장한 뒤 다시 시작하세요."
        )
        log(f"[Decodo] {last.error}")
        return last

    run_dir = category_run_dir(
        Path(config.output_dir),
        str(getattr(config, "category_id", "") or ""),
        str(getattr(config, "category_name", "") or ""),
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"[Decodo] 카테고리 폴더: {run_dir}")

    store: ResumeStore | None = None
    try:
        try:
            store = ResumeStore(run_dir / "resume.sqlite3")
            store.open()
            if start_fresh:
                archived = store.archive()
                if archived is not None:
                    log(f"[재개] '처음부터 다시 수집' — 기존 진행 기록을 보관했습니다: {archived.name}")
                store.open()
            elif store.status == STATUS_FINISHED and store.has_state():
                # 완료 봉인된 기록은 이어서 수집 대상이 아니다 — 다시 시작하는
                # 행위는 새 수집이다(2026-09-16 검토). 기존 기록은 아카이브로
                # 보존하며, 저장된(구) 상품을 새 결과에 섞지 않는다.
                archived = store.archive()
                if archived is not None:
                    log(
                        "[재개] 이 카테고리의 이전 실행은 완료됐습니다 — 진행 기록을 "
                        f"보관하고 처음부터 새로 수집합니다: {archived.name}"
                    )
                store.open()
            mismatch = store.check_config(
                category_id=str(getattr(config, "category_id", "") or "").strip(),
                exclude_rocket=bool(getattr(config, "exclude_rocket", True)),
                max_pages=int(getattr(config, "max_pages", 0) or 0),
            )
        except ResumeStoreError as e:
            store = None
            last.error = f"진행 기록을 사용할 수 없어 재개를 중단합니다: {e}"
            log(f"[재개] {last.error}")
            return last
        if mismatch:
            last.error = (
                f"저장된 진행 기록과 설정이 일치하지 않아 재개를 중단합니다. {mismatch} "
                "'처음부터 다시 수집'을 선택하거나 설정을 되돌리세요."
            )
            log(f"[재개] {last.error}")
            return last
        if store.has_state():
            log(f"[재개] 이어서 수집 — {store.preview_text()}")

        best_saved: CoupangRunSummary | None = None

        for attempt in range(1, attempts + 1):
            try:
                control.checkpoint()
                proxy, geo_reason = _korea_proxy(
                    settings, control, log, attempt, exit_ip_fn=check_exit,
                )
            except CancelledError:
                if last.error == _NOT_RUN_ERROR:
                    last = CoupangRunSummary()
                last.cancelled = True
                last.termination_reason = "cancelled"
                log("[Decodo] 취소됨")
                return _with_saved(last, best_saved)
            if proxy is None:
                last = CoupangRunSummary()
                last.termination_reason = "error"
                last.error = (
                    "한국 회선을 확보하지 못했습니다"
                    + (f" ({geo_reason})" if geo_reason else "")
                )
                log(f"[Decodo] {last.error}")
                if attempt >= attempts:
                    return _with_saved(last, best_saved)
                continue

            prefix = str(config.output_prefix or "coupang_category")
            profile_dir = run_dir / f".profile_try{attempt}"
            attempt_config = replace(
                config,
                output_dir=run_dir,
                proxy=proxy,
                require_login=False,
                subcategories=(),
                output_prefix=f"{prefix}_try{attempt}",
                profile_dir=profile_dir,
                use_persistent_profile=True,
                resume_store=store,
                # 이 PC 회선 차단의 전역 기록 — 출력 루트(트리 로드가 기록하는
                # 위치와 같음). 다른 카테고리 시작 시 같은 쿨다운 게이트를 적용.
                global_block_state_dir=_global_block_state_dir(config),
            )
            log(
                f"[Decodo] 시도 {attempt}/{attempts} — 한국 고정 회선 "
                f"({proxy_summary(proxy)})"
            )
            if on_attempt_start is not None:
                on_attempt_start(attempt, attempts)

            try:
                crawler = crawler_factory(attempt_config)
                last = crawler.run()
            except CancelledError:
                if last.error == _NOT_RUN_ERROR:
                    last = CoupangRunSummary()
                last.cancelled = True
                last.termination_reason = "cancelled"
                log("[Decodo] 취소됨")
                return _with_saved(last, best_saved)
            except ResumeStoreError as e:
                # 크롤러 밖(팩토리 단계 등)에서 진행 기록 오류가 난 경우의
                # 안전망이다. 파이프라인 안의 오류는 crawler.run() 이
                # termination_reason="resume_store_error" 로 구조화해 전달하며
                # 아래 루프에서 재시도 없이 중단된다(작업지시서 §3.1).
                last = CoupangRunSummary()
                last.termination_reason = "resume_store_error"
                last.error = f"진행 기록 저장 실패로 중단했습니다: {e}"
                log(f"[재개] {last.error}")
                return _with_saved(last, best_saved)

            best_saved = _remember_saved(best_saved, last)
            if should_keep_attempt(last):
                kind = {
                    "page_limit": "일부 수집",
                    "resume_shifted": "일부 수집(재개·목록 변동 미확인)",
                    "resume_pending": "일부 수집(재개·미확인 판매자)",
                    "mapping_pending": "일부 수집(매핑 누락)",
                }.get(last.termination_reason, "성공")
                log(
                    f"[Decodo] 시도 {attempt} {kind} — 상품 {last.products_seen}개, "
                    f"판매자 {last.unique_vendors}명"
                )
                return last

            reason = last.termination_reason or "error"
            log(f"[Decodo] 시도 {attempt} 실패 ({reason})")
            if reason == "resume_store_error":
                # 진행 기록 쓰기 실패 — 회선을 바꿔도 같은 지점에서 다시
                # 실패하므로 재시도하지 않는다(작업지시서 §3.1).
                log("[재개] 진행 기록 저장 실패 — 재시도하지 않고 중단합니다")
                return _with_saved(last, best_saved)
            if not should_retry(last):
                return _with_saved(last, best_saved)
            if attempt >= attempts:
                log(f"[Decodo] {attempts}회 모두 실패")
                return _with_saved(last, best_saved)
            log(f"[Decodo] {int(retry_wait)}초 후 다른 회선으로 다시 시도합니다")
            try:
                control.sleep(retry_wait)
            except CancelledError:
                last.cancelled = True
                last.termination_reason = "cancelled"
                log("[Decodo] 재시도 대기 중 취소됨")
                return _with_saved(last, best_saved)

        return _with_saved(last, best_saved)
    finally:
        if store is not None:
            store.close()
