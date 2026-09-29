"""Coupang 카테고리 1건 실행 — Decodo 스티키 세션 + 실패 시 회선 교체 재시도.

탭에서 카테고리 하나를 고르고 시작하면 이 함수가 엔진을 최대 3회 돌린다.
Qt 비의존. 채택 조건: 상품 > 0 이고 종료가 완료 또는 일부 수집(page_limit).
"""

from __future__ import annotations

import json
import random
import re
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core import config as app_config
from app.core.base import CancelledError, Control
from app.core.coupang.env_probe import EnvProbeResult, ProbeFn
from app.core.coupang.outcome import RunOutcome, determine_outcome
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
# 새 Decodo 회선(프록시) 차단이 이 횟수만큼 연달아 나오면 환경 자체가
# 차단 상태로 본다. 2026-09-28 12:58·2026-09-29 08:28 실측 — 이 상태의
# 재시도는 전부 실패(재시도 위치 성공 0/8)하고 회선 평판만 악화한다.
# 그다음 시도를 건너뛰고 '다음 날 재개'를 안내한다. 브라우저를 띄우지
# 않는 실패(사전점검 403)도 같이 센다.
CONSECUTIVE_PROXY_BLOCK_ABORT = 2
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
    if summary.termination_reason == "empty_category":
        return bool(summary.json_path) and not summary.save_error and not summary.error
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


def _is_proxy_line_block(summary: CoupangRunSummary) -> bool:
    """프록시 회선(목록 세션)에서 막힌 차단 — 새 회선 재시도 대상."""
    return (
        not summary.cancelled
        and summary.termination_reason == "blocked"
        and not getattr(summary, "blocked_direct", False)
    )


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


def completed_category_result(config: Any) -> CoupangRunSummary | None:
    """Return a saved result only when this category finished with matching settings."""
    run_dir = category_run_dir(
        Path(config.output_dir), config.category_id, config.category_name,
    )
    db = run_dir / "resume.sqlite3"
    if not db.is_file():
        return None
    store = ResumeStore(db)
    try:
        store.open()
        if store.status != STATUS_FINISHED or not store.has_state():
            return None
        if store.check_config(
            category_id=config.category_id,
            exclude_rocket=config.exclude_rocket,
            max_pages=config.max_pages,
        ):
            return None
        if store.max_pages != config.max_pages:
            return None
        files = sorted(
            run_dir.glob("coupang_category_*_try*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return None
        try:
            records = json.loads(files[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(records, list):
            return None
        result = CoupangRunSummary(
            products_seen=store.product_count,
            unique_vendors=len({
                str(record.get("vendor_id")) for record in records
                if isinstance(record, dict) and record.get("vendor_id")
            }),
            business_info_success=len(records),
            records=records,
            json_path=str(files[0]),
            termination_reason="empty_category" if not records else "success",
        )
        csv_path = files[0].with_suffix(".csv")
        if csv_path.is_file():
            result.csv_path = str(csv_path)
        return result
    except ResumeStoreError:
        return None
    finally:
        store.close()


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
    probe_fn: ProbeFn | None = None,
) -> CoupangRunSummary:
    """카테고리 1개를 최대 max_attempts 회, 시도마다 새 스티키 세션으로 실행.

    시도(회선) 간에 카테고리 폴더의 진행 기록(ResumeStore)을 공유해, 차단·
    종료 직전까지 저장된 페이지·판매자부터 이어서 수집한다(재개). 기존 기록과
    설정이 불일치하면 재개하지 않고 이유를 알린다. start_fresh=True 면 기존
    기록을 아카이브로 보존한 뒤 처음부터 시작한다. probe_fn 을 주면 각 시도
    전에 홈 사전 점검(직접+프록시)을 돌리고 막힌 회선은 브라우저 없이 넘긴다.
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
        consecutive_proxy_blocks = 0

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

            # 사전 점검(선택) — 브라우저·프로필을 만들기 전에 curl_cffi 홈
            # GET(직접 1회 + 이 프록시 회선 1회)으로 막힌 회선을 걸러낸다.
            # 통과한 회선을 그대로 이 시도의 회선으로 쓴다(추가 회선 소모 없음).
            probe_blocked = False
            if probe_fn is not None:
                try:
                    control.checkpoint()
                except CancelledError:
                    if last.error == _NOT_RUN_ERROR:
                        last = CoupangRunSummary()
                    last.cancelled = True
                    last.termination_reason = "cancelled"
                    log("[Decodo] 취소됨")
                    return _with_saved(last, best_saved)
                probe: EnvProbeResult = probe_fn(proxy)
                vendor_direct = (
                    str(getattr(config, "vendor_phase_line", "direct")) == "direct"
                )
                if probe.direct_blocked and vendor_direct:
                    last = CoupangRunSummary()
                    last.termination_reason = "blocked"
                    last.blocked_direct = True
                    last.error = (
                        "사전 점검에서 이 PC 회선(직접 접속)이 차단(403) 상태입니다 — "
                        "판매자 단계를 진행할 수 없습니다. 오늘은 중단하고 다음 날 재개하세요."
                    )
                    log(f"[사전점검] {last.error}")
                    return _with_saved(last, best_saved)
                if probe.proxy_blocked:
                    probe_blocked = True
                    last = CoupangRunSummary()
                    last.termination_reason = "blocked"
                    last.error = (
                        "사전 점검 — 새 Decodo 회선이 홈에서 차단(403)되었습니다."
                    )
                    log("[사전점검] 새 Decodo 회선 홈 403 — 브라우저를 띄우지 않습니다.")
                else:
                    log(
                        "[사전점검] 결과 — "
                        f"직접 {probe.direct_status if probe.direct_status is not None else '불명'}, "
                        f"프록시 {probe.proxy_status if probe.proxy_status is not None else '불명'} "
                        "— 진행합니다."
                    )

            if not probe_blocked:
                prefix = str(config.output_prefix or "coupang_category")
                # Camoufox 프로필을 Chromium에 넘기지 않고, 회선마다 새 쿠키 저장소를
                # 만든다. 같은 카테고리를 다시 실행해도 이전 회선의 쿠키를 재사용하지
                # 않도록 난수 접미사를 붙인다.
                profile_dir = run_dir / f".chromium_profile_try{attempt}_{uuid4().hex[:10]}"
                profile_created = not profile_dir.exists()
                attempt_config = replace(
                    config,
                    output_dir=run_dir,
                    browser_engine="chromium",
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
                finally:
                    if profile_created:
                        try:
                            if profile_dir.is_symlink() or profile_dir.is_file():
                                profile_dir.unlink()
                            elif profile_dir.is_dir():
                                shutil.rmtree(profile_dir)
                        except OSError as e:
                            log(f"[정리] Chromium 프로필 삭제 실패: {e}")

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
            if _is_proxy_line_block(last):
                consecutive_proxy_blocks += 1
                if consecutive_proxy_blocks >= CONSECUTIVE_PROXY_BLOCK_ABORT:
                    last.error = (
                        f"새 Decodo 회선 연속 {consecutive_proxy_blocks}회 차단 — "
                        "환경(프록시 풀) 차단 상태로 보입니다. 오늘은 중단하고 "
                        "다음 날 재개하세요. 반복 재시도는 회선 평판을 악화합니다"
                        "(2026-09-28 12:58·2026-09-29 08:28 실측)."
                    )
                    log(f"[Decodo] {last.error}")
                    return _with_saved(last, best_saved)
            else:
                consecutive_proxy_blocks = 0
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


def run_category_batch(
    configs: tuple[Any, ...],
    settings: DecodoSettings,
    control: Control,
    *,
    crawler_factory: CrawlerFactory,
    on_log: LogFn | None = None,
    on_attempt_start: AttemptFn | None = None,
    on_target_start: Callable[[int, int, str], None] | None = None,
    on_saved_record: Callable[[dict], None] | None = None,
    start_fresh: bool = False,
    skip_finished: bool = False,
    exit_ip_fn: ExitIpFn | None = None,
    probe_fn: ProbeFn | None = None,
) -> tuple[CoupangRunSummary, int]:
    """Run each category in its own resume folder, stopping at the first gap."""
    if not configs:
        raise ValueError("수집 대상 카테고리가 없습니다")
    log = on_log or (lambda _message: None)
    total = len(configs)
    combined = CoupangRunSummary(termination_reason="success")
    completed = 0
    unverified_empty: list[str] = []
    numeric_fields = (
        "products_seen", "unique_vendors", "business_info_success",
        "brand_seller_skipped", "request_errors", "power_sellers",
        "store_name_present", "store_name_missing",
        "duplicate_business_numbers_observed",
    )
    if start_fresh:
        for config in configs:
            db = category_run_dir(
                Path(config.output_dir), config.category_id, config.category_name,
            ) / "resume.sqlite3"
            if db.is_file():
                store = ResumeStore(db)
                archived = store.archive()
                if archived is not None:
                    log(f"[재개] 이전 진행 기록 보관: {archived}")
    previous_crawled_config = None
    for index, config in enumerate(configs, 1):
        try:
            control.checkpoint()
        except CancelledError:
            combined.cancelled = True
            combined.termination_reason = "cancelled"
            return combined, completed
        label = f"{config.category_name} ({config.category_id})"
        if on_target_start is not None:
            on_target_start(index, total, label)
        log(f"[카테고리 {index}/{total}] {label} 시작")
        part = completed_category_result(config) if skip_finished else None
        if part is not None:
            log(f"[카테고리 {index}/{total}] 이전 완료 결과 사용 — {label}")
            if on_saved_record is not None:
                for record in part.records:
                    on_saved_record(record)
        else:
            if previous_crawled_config is not None:
                cooldown = random.uniform(
                    previous_crawled_config.category_cooldown_min,
                    previous_crawled_config.category_cooldown_max,
                )
                if cooldown > 0:
                    log(f"  카테고리 전환 쿨다운 {cooldown:.0f}초...")
                    try:
                        control.sleep(cooldown)
                    except CancelledError:
                        combined.cancelled = True
                        combined.termination_reason = "cancelled"
                        return combined, completed
            part = run_category_attempts(
                config, settings, control,
                crawler_factory=crawler_factory,
                on_log=log,
                on_attempt_start=on_attempt_start,
                start_fresh=False,
                exit_ip_fn=exit_ip_fn,
                probe_fn=probe_fn,
            )
            previous_crawled_config = config
        for field in numeric_fields:
            setattr(combined, field, getattr(combined, field) + getattr(part, field))
        combined.records.extend(part.records)
        if part.json_path:
            combined.json_path = part.json_path
        if part.csv_path:
            combined.csv_path = part.csv_path
        outcome = determine_outcome(part)
        if outcome == RunOutcome.SUCCESS:
            completed += 1
            log(f"[카테고리 {index}/{total}] 완료 — {label}")
            continue
        if outcome == RunOutcome.NO_RECORDS and part.termination_reason == "no_items" and not part.products_seen:
            unverified_empty.append(label)
            log(f"[카테고리 {index}/{total}] 상품 0건 — 다른 카테고리도 계속 확인합니다: {label}")
            continue
        combined.cancelled = part.cancelled
        combined.blocked_direct = part.blocked_direct
        combined.save_error = part.save_error
        combined.cleanup_error = part.cleanup_error
        combined.termination_reason = part.termination_reason
        combined.error = part.error
        if outcome == RunOutcome.NO_RECORDS:
            combined.error = f"{label}: 상품을 읽지 못했습니다"
        elif outcome == RunOutcome.ERROR:
            combined.error = f"{label}: {part.error or '수집 오류'}"
        log(f"[카테고리 {index}/{total}] 중단 — {label} ({outcome.value})")
        return combined, completed
    if unverified_empty:
        combined.termination_reason = "no_items"
        combined.error = "상품 0건으로 확인이 필요한 카테고리: " + ", ".join(unverified_empty)
    elif not combined.records and combined.json_path:
        combined.termination_reason = "empty_category"
    return combined, completed
