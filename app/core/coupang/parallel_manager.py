"""쿠팡 병렬 인스턴스 매니저 — 순수 코어(Qt 비의존, 병렬 확장 M2).

M1 이 앱 모듈로 포팅한 파이프라인 코어(app.core.coupang.parallel_pipeline,
patchright_top_sellers, patchright_top_thousand) 위에서 인스턴스별 세션
일정을 잡는다. 스레드 실행·Qt 시그널 브리지는 워커(M3) 책임이고 이 모듈은
동기 호출만 한다 — run_due(now) 가 도래한 인스턴스의 세션을 순차 실행하고
1세션 = run_one_instance(instance) 동기 실행이다.

가드(실접속 안전 장치) 소유권: run_top_pages/run_top_seller_batch 가 세션
시작 시 claim_live_attempt, 정상 종료 시 settle_live_attempt 를 인스턴스
장부(canary_guard.json)에 직접 기록한다. 매니저가 같은 장부를 먼저 claim
하면 러너의 claim 이 항상 간격 제한(60분)으로 거부돼 어떤 세션도 돌지
못한다. 그래서 매니저의 사전 점검(세션 절차 3단계)은 장부 사본을 임시
폴더에 두고 claim_live_attempt 를 돌려보는 프로브로 판정만 한다. 실제
claim/settle 은 러너가 실장부에 남긴다(프로브는 기록을 남기지 않는다).

인스턴스 구성 규칙(사용자 결정 2026-10-01): 1번 인스턴스는 직접 회선
(집), 2~N번은 Decodo 스티키 회선. Decodo 자격이 없으면 1개(직접)로
강등하고 degraded 이벤트를 낸다.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.base import CancelledError, Control
from app.core.config import PROJECT_ROOT
from app.core.coupang.parallel_pipeline import (
    append_run_log,
    build_all_finals,
    check_and_rotate_session,
    choose_action,
)
from app.core.coupang.patchright_canary import claim_live_attempt
from app.core.coupang.patchright_top_sellers import run_top_seller_batch
from app.core.coupang.patchright_top_thousand import (
    MAX_LISTING_ITEMS,
    MAX_SESSION_PAGES,
    MAX_SESSION_PRODUCTS,
    TOP_CATEGORIES,
    run_top_pages,
)

__all__ = [
    "FAMILY_DEFINITION_FILENAME",
    "MAX_INSTANCES",
    "ParallelCoupangManager",
    "ParallelInstanceConfig",
    "ParallelRunConfig",
    "STATE_FILENAME",
    "build_instances",
]

# 매니저 상태 영속 파일 — 실행 설정(output_dir) 안에 남긴다.
STATE_FILENAME = "coupang_parallel_state.json"
# 가드 장부 파일명 — patchright_canary._guard_path 가 쓰는 이름과 같다.
# 프로브가 장부를 복사해 점검할 때 이 이름으로 찾는다.
GUARD_LEDGER_FILENAME = "canary_guard.json"
# 인스턴스가 담당한 카테고리 가족 정의 — build_all_finals 에 넘겨 루트별
# final 생성 여부를 판단하게 하는 파일(기본 A 가족이 아닐 때만 만든다).
FAMILY_DEFINITION_FILENAME = "coupang_family.json"

MAX_INSTANCES = 8  # 7-병렬 실측(10-04) 반영 — 여유 1까지 허용

# 인스턴스 상태 — waiting(다음 세션 대기)/collecting(세션 실행 중)/
# blocked(차단으로 이 인스턴스 일정 중단)/complete(가족 완주 순간)/
# done(담당 가족 전부 완료)/error(정지 필요).
STATUS_WAITING = "waiting"
STATUS_COLLECTING = "collecting"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETE = "complete"
STATUS_DONE = "done"
STATUS_ERROR = "error"
_TERMINAL_STATUSES = frozenset({STATUS_BLOCKED, STATUS_ERROR, STATUS_DONE})
_PERSIST_STATUSES = _TERMINAL_STATUSES | {STATUS_WAITING}
# 러너 결과 이벤트 중 정상 종료로 보는 이름들(간격 재스케줄).
# blocked/cancelled/guard_refused/failed/seller_halted 는 별도 분류다.
_RESULT_CANCELLED = "cancelled"
_RESULT_BLOCKED = "blocked"
_RESULT_GUARD_REFUSED = "guard_refused"
_RESULT_FAILED = frozenset({"failed", "seller_halted"})


def _decodo_credentials_ready() -> bool:
    """Decodo 자격 준비 여부. 모듈·설정 파일이 없으면 False 로 간주한다."""
    try:
        from app.core.decodo import credentials_ready
    except Exception:  # noqa: BLE001 - 모듈 부재는 미비와 같다
        return False
    try:
        return bool(credentials_ready())
    except Exception:  # noqa: BLE001 - 설정 파일 손상도 미비와 같다
        return False


def _decodo_proxy(session_id: str) -> dict | None:
    """유효 sid로 스티키 프록시 dict를 만든다. 자격 미비·오류면 None."""
    try:
        from app.core.decodo import load_settings, sticky_proxy_dict
    except Exception:  # noqa: BLE001 - 모듈 부재 시 자격 없음과 같다
        return None
    try:
        return sticky_proxy_dict(load_settings(), session_id)
    except Exception:  # noqa: BLE001 - 설정 읽기 실패도 프록시 불가로
        return None


def _state_root_for(instance_id: str) -> Path:
    """인스턴스별 안전 장치·브라우저 프로필 루트.

    Windows 는 %LOCALAPPDATA%\\SellerCollector\\coupang_parallel\\{id},
    그 외는 PROJECT_ROOT/runtime_profile/coupang_parallel/{id} —
    patchright_canary.state_dir 의 배포 원칙을 인스턴스 단위로 적용한다.
    """
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (
            Path(os.environ["LOCALAPPDATA"])
            / "SellerCollector"
            / "coupang_parallel"
            / instance_id
        )
    return PROJECT_ROOT / "runtime_profile" / "coupang_parallel" / instance_id


@dataclass
class ParallelInstanceConfig:
    """병렬 인스턴스 1개의 고정 구성(세션 사이에 변하지 않는 값)."""

    instance_id: str  # "1" ~ "N"
    name: str         # 표시 이름(예: "인스턴스 1")
    line: str         # "direct"(직접 회선) | "decodo"(스티키 프록시)
    session_id: str   # decodo 시작 sid(예: "i2"). direct 는 빈 문자열.
    state_root: Path  # 가드 장부 + 브라우저 프로필 루트(인스턴스 전용)
    output_root: Path  # 가족별 출력 하위 폴더들의 부모


@dataclass
class ParallelRunConfig:
    """병렬 수집 실행 설정 — 사용자가 실행 대화상자에서 정하는 값."""

    families: list[list[tuple[str, str]]]  # 가족 = [(카테고리id, 이름), ...]
    # output_dir 을 기본값 있는 필드들보다 앞에 둔다(데이터클래스 제약:
    # 기본값 없는 필드가 뒤에 올 수 없다). 인스턴스 수 등은 뒤의 기본값.
    output_dir: Path
    instance_count: int = 3    # 1~8
    interval_minutes: int = 80  # 인스턴스별 세션 간격(분)
    listing_pages: int = 8      # 목록 단계 1세션 페이지 수(1~10)
    seller_limit: int = 130     # 판매자 단계 1세션 상품 수 상한

    def __post_init__(self) -> None:
        if not isinstance(self.instance_count, int) or isinstance(
            self.instance_count, bool
        ):
            raise ValueError("instance_count는 정수여야 합니다.")
        if not 1 <= self.instance_count <= MAX_INSTANCES:
            raise ValueError(
                f"instance_count는 1~{MAX_INSTANCES}여야 합니다."
            )
        if not isinstance(self.families, list) or not self.families:
            raise ValueError("families는 비어 있지 않은 목록이어야 합니다.")
        for family in self.families:
            if not isinstance(family, list) or not family:
                raise ValueError("각 가족은 비어 있지 않은 카테고리 목록이어야 합니다.")
        if not isinstance(self.interval_minutes, int) or self.interval_minutes < 1:
            raise ValueError("interval_minutes는 1분 이상이어야 합니다.")
        if not 1 <= self.listing_pages <= MAX_SESSION_PAGES:
            raise ValueError(
                f"listing_pages는 1~{MAX_SESSION_PAGES}이어야 합니다."
            )
        if not 1 <= self.seller_limit <= MAX_SESSION_PRODUCTS:
            raise ValueError(
                f"seller_limit는 1~{MAX_SESSION_PRODUCTS}이어야 합니다."
            )


def build_instances(
    config: ParallelRunConfig,
    on_event: Callable[[dict], None] | None = None,
) -> list[ParallelInstanceConfig]:
    """실행 설정에서 인스턴스 구성을 만든다(정적 팩토리).

    1번 인스턴스는 직접 회선, 2~N번은 Decodo. Decodo 자격이 준비되지
    않았으면 인스턴스 1개(직접)로 강등하고 on_event 로 degraded 경고를
    낸다 — 자격 없는 스티키 프록시 인스턴스는 세션마다 실패하므로 아예
    만들지 않는다.
    """
    count = config.instance_count
    if count > 1 and not _decodo_credentials_ready():
        count = 1
        if on_event is not None:
            on_event(
                {
                    "type": "degraded",
                    "instance": "",
                    "message": (
                        "Decodo 자격이 준비되지 않아 직접 회선 인스턴스 "
                        "1개로 강등합니다. 설정 탭에서 Decodo 계정을 "
                        "저장하면 인스턴스를 다시 늘릴 수 있습니다."
                    ),
                }
            )
    instances: list[ParallelInstanceConfig] = []
    for number in range(1, count + 1):
        instance_id = str(number)
        line = "direct" if number == 1 else "decodo"
        instances.append(
            ParallelInstanceConfig(
                instance_id=instance_id,
                name=f"인스턴스 {number}",
                line=line,
                # 시작 sid는 인스턴스 번호 기본값(예: "i2"). 실제 세션은
                # 가족 출력 폴더의 상태 파일(sid 마지막 성공값)을 우선한다.
                session_id="" if line == "direct" else f"i{number}",
                state_root=_state_root_for(instance_id),
                output_root=config.output_dir,
            )
        )
    return instances


@dataclass
class _InstanceState:
    """인스턴스별 가변 상태 — 매 세션 후 coupang_parallel_state.json 에 영속된다."""

    config: ParallelInstanceConfig
    status: str = STATUS_WAITING
    family_index: int | None = None  # 현재 담당 가족(인덱스)
    next_run_at: float = 0.0         # 다음 세션 예정 시각(epoch 초)
    total_products: int = 0          # 현재 가족 누적 3P 상품 수
    total_sellers: int = 0           # 현재 가족 누적 확보 판매자 수
    effective_sid: str = ""          # 마지막 유효 decodo sid(회전 반영)


def _count_csv_rows(path: Path, *, only_saved: bool = False) -> int:
    """저장소 CSV 행 수를 센다. 파일이 없으면 0(신규 가족 직후 상태)."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return 0
    if only_saved:
        return sum(1 for row in rows if row.get("status") == "saved")
    return len(rows)


class ParallelCoupangManager:
    """병렬 인스턴스 일정·세션 실행·상태 영속을 맡는 순수 코어 매니저.

    사용 흐름(워커): __init__ → (재시작이면 상태 복원) → 주기적으로
    run_due(time.time()) → all_done() 이 True 가 되면 종료. 이벤트는
    on_event 콜백으로 나가며 표준 형태는
    {"type": "status|progress|log|exit_ip|blocked|complete|error|degraded|all_done",
     "instance": instance_id, ...} 이다.
    """

    def __init__(
        self,
        config: ParallelRunConfig,
        control: Control,
        on_event: Callable[[dict], None],
        browser_scope_factory: Callable | None = None,
    ) -> None:
        self.config = config
        self.control = control  # 호출자 소유 — 취소/일시정지는 워커가 지시
        self.on_event = on_event
        # None 이면 러너 기본값(patchright_browser)을 쓴다. 테스트는 가짜
        # 팩토리를 주입해 프록시 인자·프로필 경로를 캡처한다.
        self.browser_scope_factory = browser_scope_factory
        self.instances: dict[str, _InstanceState] = {}
        self.pending_families: list[int] = []
        self._interval_seconds = config.interval_minutes * 60
        self._all_done_emitted = False
        built = build_instances(config, on_event=on_event)
        if not self._restore_state(built):
            for instance in built:
                self.instances[instance.instance_id] = _InstanceState(
                    config=instance
                )
            self.plan()

    # ── 배분 ───────────────────────────────────────────────────────────

    def plan(self) -> None:
        """가족 초기 배분 — 인스턴스 수만큼 앞에서부터 1개씩, 나머지는 대기 큐.

        라운드로빈(가족 i → 인스턴스 i mod N)과 같은 결과를 내는 앞순서
        배분이다. 완주한 인스턴스는 대기 큐의 다음 가족을 승계한다(세션
        절차의 complete 경로). 재시작 재개(상태 복원) 시에는 호출하지 않는다.
        """
        assigned = 0
        for state in self._ordered_instances():
            if assigned < len(self.config.families):
                state.family_index = assigned
                state.status = STATUS_WAITING
                state.next_run_at = 0.0
                assigned += 1
            else:
                state.family_index = None
        self.pending_families = list(
            range(assigned, len(self.config.families))
        )
        self._emit(
            {
                "type": "log",
                "instance": "",
                "message": (
                    f"가족 {len(self.config.families)}개를 인스턴스 "
                    f"{len(self.instances)}개에 배분했습니다"
                    + (
                        f"(대기 {len(self.pending_families)}개)."
                        if self.pending_families
                        else "."
                    )
                ),
            }
        )
        self.persist_state()

    # ── 세션 실행 ──────────────────────────────────────────────────────

    def run_due(self, now: float) -> bool:
        """next_run_at 이 도래한 인스턴스의 세션을 순차 실행한다.

        스레드 병렬은 워커 책임 — 매니저는 한 번의 호출에서 due 인 인스턴스를
        차례로 동기 실행한다. 세션을 1개 이상 실행했으면 True.
        """
        ran = False
        for state in self._ordered_instances():
            if state.status != STATUS_WAITING:
                continue  # blocked/error/done 은 일정에서 빠진다
            if state.next_run_at > now:
                continue  # 간격 미달 — 스킵
            self.run_one_instance(state.config, now=now)
            ran = True
        if self.all_done() and not self._all_done_emitted:
            self._all_done_emitted = True
            self._emit({"type": "all_done", "instance": ""})
        return ran

    def run_one_instance(
        self,
        instance: ParallelInstanceConfig,
        *,
        now: float | None = None,
    ) -> dict:
        """인스턴스 1개의 세션을 동기 실행하고 요약 이벤트를 반환한다.

        세션 절차(병렬 확장 설계 §5): (1) 취소 점검 (2) 회선 준비(decodo 는
        출구 IP 점검 + sid 자동 교체) (3) 가드 사전 점검(거부면 연기)
        (4) 다음 작업 선택 — halted 면 인스턴스 error, complete 면 완성형
        생성 후 가족 승계/done (5) 판매자/목록 세션 실행 (6) 결과 분류 —
        정상이면 다음 세션 예약, blocked 면 이 인스턴스만 중단, cancelled
        면 대기 복귀 (7) 누적 집계 + 상태 영속 + 진행 이벤트.
        """
        state = self.instances[instance.instance_id]
        current = time.time() if now is None else now
        state.status = STATUS_COLLECTING
        self._emit(
            {
                "type": "status",
                "instance": instance.instance_id,
                "status": STATUS_COLLECTING,
                "family_index": state.family_index,
            }
        )
        summary = {
            "type": "progress",
            "instance": instance.instance_id,
            "family_index": state.family_index,
            "action": "",
            "result": "",
        }
        try:
            # 배정받은 가족이 없으면 대기 큐에서 승계하고, 큐가 비었으면 종료.
            if state.family_index is None:
                if not self._take_next_family(state, current):
                    state.status = STATUS_DONE
                    summary["result"] = "done"
                    summary["status"] = state.status
                    self._emit(
                        {
                            "type": "status",
                            "instance": instance.instance_id,
                            "status": STATUS_DONE,
                            "message": "담당할 가족이 더 없습니다.",
                        }
                    )
                    self.persist_state()
                    return summary
                summary["family_index"] = state.family_index

            family = [tuple(pair) for pair in self.config.families[state.family_index]]
            output_dir = self._family_output_dir(state)

            # (1) 취소 점검 — 사용자 취소면 대기 상태로 돌아간다.
            self.control.checkpoint()

            # (2) 회선 준비. direct 는 프록시 없이, decodo 는 출구 IP 점검으로
            # sid 를 확인·교체하고 스티키 프록시 dict를 만든다.
            proxy: dict | None = None
            if instance.line == "decodo":
                seed_sid = state.effective_sid or instance.session_id
                rotate_event, effective_sid = check_and_rotate_session(
                    output_dir, seed_sid
                )
                state.effective_sid = effective_sid
                self._emit(
                    {
                        "type": "exit_ip",
                        "instance": instance.instance_id,
                        "family_index": state.family_index,
                        **rotate_event,
                    }
                )
                append_run_log(output_dir, rotate_event)
                proxy = _decodo_proxy(effective_sid)
                if not rotate_event.get("ok") or proxy is None:
                    # 자격/회선 실패로 proxy=None 으로 진행하지 않는다 —
                    # 직접 회선으로 쿠팡을 때리는 것과 같아서다.
                    reason = (
                        "Decodo 프록시 자격을 만들지 못했습니다."
                        if proxy is None and rotate_event.get("ok")
                        else "Decodo 회선 점검에 실패했습니다("
                        + str(rotate_event.get("error_kind") or "원인 미상")
                        + ")."
                    )
                    state.status = STATUS_ERROR
                    self._emit(
                        {
                            "type": "error",
                            "instance": instance.instance_id,
                            "family_index": state.family_index,
                            "reason": reason,
                        }
                    )
                    self.persist_state()
                    summary.update(status=state.status, result="proxy_error")
                    return summary

            # (3) 가드 사전 점검 — 장부 사본으로 판정만 한다(모듈 docstring
            # 참조). 거부(간격·차단·복구 잠금)면 세션을 실행하지 않고 연기.
            allowed, reason = self._guard_probe(state, current)
            if not allowed:
                state.status = STATUS_WAITING
                state.next_run_at = current + self._interval_seconds
                self._emit(
                    {
                        "type": "log",
                        "instance": instance.instance_id,
                        "family_index": state.family_index,
                        "message": f"안전 장치가 세션을 연기합니다: {reason}",
                        "reason": reason,
                        "next_run_at": state.next_run_at,
                    }
                )
                self.persist_state()
                summary.update(
                    status=state.status,
                    result=_RESULT_GUARD_REFUSED,
                    next_run_at=state.next_run_at,
                )
                return summary

            # (4) 다음 작업 선택(네트워크 없음).
            decision = choose_action(output_dir, family)
            action = str(decision.get("action") or "")
            summary["action"] = action
            if action == "halted":
                reason_text = str(decision.get("reason") or "")
                state.status = STATUS_ERROR
                self._emit(
                    {
                        "type": "error",
                        "instance": instance.instance_id,
                        "family_index": state.family_index,
                        "reason": reason_text,
                    }
                )
                append_run_log(
                    output_dir,
                    {
                        "event": "parallel_session",
                        "instance": instance.instance_id,
                        "family_index": state.family_index,
                        "action": action,
                        "result": "halted",
                        "reason": reason_text,
                    },
                )
                self.persist_state()
                summary.update(status=state.status, result="halted")
                return summary
            if action == "complete":
                # 완주 — 완성형 파일을 만들고 가족 승계 또는 done.
                status = self._complete_family(state, current)
                summary.update(
                    status=status,
                    result="complete",
                    next_run_at=state.next_run_at,
                    total_products=state.total_products,
                    total_sellers=state.total_sellers,
                )
                if status == STATUS_WAITING:
                    # 승계한 다음 가족 인덱스를 함께 알린다.
                    summary["inherited_family"] = state.family_index
                self._emit(dict(summary))
                return summary

            # (5) 세션 실행 — 다른 인스턴스와 출력 폴더·가드 장부가 분리된다
            # (output_dir 는 가족 전용 하위 폴더, state_root 는 인스턴스 전용).
            if action == "sellers":
                result = run_top_seller_batch(
                    output_dir=output_dir,
                    limit=self.config.seller_limit,
                    control=self.control,
                    state_root=instance.state_root,
                    browser_profile_root=instance.state_root,
                    browser_scope_factory=self.browser_scope_factory,
                    proxy=proxy,
                )
            else:
                result = run_top_pages(
                    output_dir=output_dir,
                    page_count=self.config.listing_pages,
                    control=self.control,
                    on_event=self._runner_bridge(
                        instance.instance_id, state.family_index
                    ),
                    state_root=instance.state_root,
                    categories=family,
                    browser_scope_factory=self.browser_scope_factory,
                    proxy=proxy,
                )
            summary["result"] = str(result.get("event") or "")

            # (6) 결과 분류 — 각 인스턴스 상태로 반영.
            self._apply_session_result(state, result, current)
        except CancelledError:
            # 사용자 취소 — next_run_at 를 그대로 두고 대기로 복귀한다
            # (재개하면 같은 예정 시각에 이어서 진행).
            state.status = STATUS_WAITING
            summary["result"] = _RESULT_CANCELLED
            self._emit(
                {
                    "type": "status",
                    "instance": instance.instance_id,
                    "status": STATUS_WAITING,
                    "result": _RESULT_CANCELLED,
                }
            )
        except Exception as error:  # noqa: BLE001 - 매니저 단계 실패는 인스턴스 정지
            state.status = STATUS_ERROR
            summary["result"] = "failed"
            self._emit(
                {
                    "type": "error",
                    "instance": instance.instance_id,
                    "family_index": state.family_index,
                    "reason": f"{type(error).__name__}: {error}",
                }
            )

        # (7) 누적 집계 + 세션 결과 로그 + 진행 이벤트 + 영속.
        if state.family_index is not None:
            self._refresh_totals(state)
            summary["family_index"] = state.family_index
            if summary.get("action"):
                append_run_log(
                    self._family_output_dir(state),
                    {
                        "event": "parallel_session",
                        "instance": instance.instance_id,
                        "family_index": state.family_index,
                        "action": summary.get("action"),
                        "result": summary.get("result"),
                        "status": state.status,
                        "next_run_at": state.next_run_at,
                    },
                )
        if state.status == STATUS_COLLECTING:  # 안전망 — 분류 누락 방지
            state.status = STATUS_WAITING
        summary["status"] = state.status
        summary["next_run_at"] = state.next_run_at
        summary["total_products"] = state.total_products
        summary["total_sellers"] = state.total_sellers
        self._emit(dict(summary))
        self.persist_state()
        return summary

    def all_done(self) -> bool:
        """전 인스턴스가 종료 상태(done/blocked/error)면 True."""
        return all(
            state.status in _TERMINAL_STATUSES
            for state in self.instances.values()
        )

    # ── 상태 영속 ──────────────────────────────────────────────────────

    def persist_state(self) -> None:
        """인스턴스별 상태 전부 + 대기 가족 큐를 원자적으로 저장한다.

        앱 설정 파일 패턴(임시 파일 작성 후 os.replace)으로 쓴다 — 같은
        출력 폴더로 다시 시작하면 배분·예정 시각이 그대로 복원된다.
        """
        payload = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "interval_minutes": self.config.interval_minutes,
            "family_count": len(self.config.families),
            "pending_families": list(self.pending_families),
            "instances": [
                {
                    "instance_id": state.config.instance_id,
                    "name": state.config.name,
                    "line": state.config.line,
                    "session_id": state.config.session_id,
                    "state_root": str(state.config.state_root),
                    "output_root": str(state.config.output_root),
                    "status": state.status,
                    "family_index": state.family_index,
                    "next_run_at": state.next_run_at,
                    "total_products": state.total_products,
                    "total_sellers": state.total_sellers,
                    "effective_sid": state.effective_sid,
                }
                for state in self._ordered_instances()
            ],
        }
        path = self.config.output_dir / STATE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @classmethod
    def load_state(cls, path: Path) -> dict | None:
        """저장된 매니저 상태를 읽는다. 없거나 형식이 틀리면 None."""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (
            not isinstance(raw, dict)
            or raw.get("version") != 1
            or not isinstance(raw.get("instances"), list)
        ):
            return None
        return raw

    # ── 내부 ───────────────────────────────────────────────────────────

    def _ordered_instances(self) -> list[_InstanceState]:
        """인스턴스 번호순 목록(배분·실행 순서를 결정적으로 유지)."""
        return sorted(
            self.instances.values(),
            key=lambda state: (
                len(state.config.instance_id),
                state.config.instance_id,
            ),
        )

    def _emit(self, event: dict) -> None:
        """표준 이벤트를 워커로 보낸다. 수신자 오류가 수집을 멈추지 않게한다."""
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 - 이벤트 소비자는 워커 영역이다
            pass

    def _runner_bridge(
        self, instance_id: str, family_index: int | None
    ) -> Callable[[dict], None]:
        """목록 세션의 중간 이벤트를 표준 진행 이벤트로 감싼다."""

        def bridge(snapshot: dict) -> None:
            self._emit(
                {
                    "type": "progress",
                    "instance": instance_id,
                    "family_index": family_index,
                    "runner": dict(snapshot),
                }
            )

        return bridge

    def _family_output_dir(self, state: _InstanceState) -> Path:
        """가족 전용 출력 폴더 — 가족 인덱스 기준 하위 폴더(인스턴스 공용 부모)."""
        return state.config.output_root / f"family_{(state.family_index or 0) + 1:02d}"

    def _guard_probe(
        self, state: _InstanceState, now: float
    ) -> tuple[bool, str]:
        """실접속 사전 점검 — 장부 사본에서 claim_live_attempt 를 돌려본다.

        러너(run_top_pages/run_top_seller_batch)가 실장부를 claim/settle
        하므로, 매니저는 거부 여부만 판정한다. 예정 규모는 목록 단계 기준
        상한(listing_pages × 60)으로 잡는다 — 판매자 세션은 이보다 작게
        claim 하므로 목록 기준으로 통일해도 안전하다(사본이라 기록 안 남음).
        """
        planned_items = min(
            self.config.listing_pages * MAX_LISTING_ITEMS, MAX_SESSION_PRODUCTS
        )
        with tempfile.TemporaryDirectory(prefix="coupang-guard-probe-") as tmp:
            probe_root = Path(tmp)
            source = state.config.state_root / GUARD_LEDGER_FILENAME
            if source.exists():
                shutil.copy2(source, probe_root / GUARD_LEDGER_FILENAME)
            return claim_live_attempt(
                probe_root,
                now=now,
                planned_items=planned_items,
                planned_pages=self.config.listing_pages,
            )

    def _apply_session_result(
        self, state: _InstanceState, result: dict, current: float
    ) -> None:
        """러너 결과 이벤트를 인스턴스 상태로 분류한다."""
        instance_id = state.config.instance_id
        event_name = str(result.get("event") or "")
        if event_name == _RESULT_BLOCKED:
            # 차단 — 이 인스턴스의 일정만 중단한다(장부가 인스턴스별이라
            # 다른 인스턴스는 무관). 러너가 이미 record_block 을 남겼다.
            state.status = STATUS_BLOCKED
            state.next_run_at = 0.0
            self._emit(
                {
                    "type": "blocked",
                    "instance": instance_id,
                    "family_index": state.family_index,
                    "reason": str(result.get("reason") or "차단 신호를 확인했습니다."),
                    "reference": str(result.get("reference") or ""),
                }
            )
        elif event_name == _RESULT_CANCELLED:
            state.status = STATUS_WAITING  # next_run_at 유지 — 재개 시 이어서
            self._emit(
                {
                    "type": "status",
                    "instance": instance_id,
                    "status": STATUS_WAITING,
                    "result": _RESULT_CANCELLED,
                }
            )
        elif event_name == _RESULT_GUARD_REFUSED:
            # 러너 내부 claim 이 거부한 경우(사전 점검과의 경합) — 연기.
            state.status = STATUS_WAITING
            state.next_run_at = current + self._interval_seconds
            self._emit(
                {
                    "type": "log",
                    "instance": instance_id,
                    "family_index": state.family_index,
                    "message": (
                        "안전 장치가 세션을 연기합니다: "
                        + str(result.get("reason") or "")
                    ),
                }
            )
        elif event_name in _RESULT_FAILED:
            state.status = STATUS_ERROR
            self._emit(
                {
                    "type": "error",
                    "instance": instance_id,
                    "family_index": state.family_index,
                    "reason": str(
                        result.get("reason") or result.get("error") or event_name
                    ),
                }
            )
        else:
            # 정상 종료 — 다음 세션 예약. settle_live_attempt 는 러너가
            # 실장부에 이미 호출했다(미사용 예약량 반납 포함).
            state.status = STATUS_WAITING
            state.next_run_at = current + self._interval_seconds

    def _complete_family(self, state: _InstanceState, current: float) -> str:
        """완주한 가족의 완성형 파일을 만들고 승계/종료를 처리한다."""
        instance_id = state.config.instance_id
        finished_index = state.family_index
        family = [tuple(pair) for pair in self.config.families[finished_index]]
        output_dir = self._family_output_dir(state)
        finals = build_all_finals(
            output_dir, categories_file=self._family_definition_file(output_dir, family)
        )
        state.status = STATUS_COMPLETE
        event = {
            "type": "complete",
            "instance": instance_id,
            "family_index": finished_index,
            "finals": finals,
        }
        append_run_log(
            output_dir,
            {
                "event": "family_complete",
                "instance": instance_id,
                "family_index": finished_index,
                "finals": finals,
            },
        )
        if self._take_next_family(state, current):
            event["inherited_family"] = state.family_index
            event["next_run_at"] = state.next_run_at
        else:
            state.status = STATUS_DONE
            state.next_run_at = 0.0
        self._emit(event)
        self.persist_state()
        return state.status

    def _take_next_family(self, state: _InstanceState, current: float) -> bool:
        """대기 큐의 다음 가족을 승계한다. 큐가 비었으면 False(종료 예정)."""
        if not self.pending_families:
            return False
        next_index = self.pending_families.pop(0)
        state.family_index = next_index
        state.status = STATUS_WAITING
        # 새 가족도 같은 인스턴스 장부를 쓰므로 세션 간격을 그대로 둔다.
        state.next_run_at = current + self._interval_seconds
        state.total_products = 0
        state.total_sellers = 0
        self._emit(
            {
                "type": "log",
                "instance": state.config.instance_id,
                "message": (
                    f"가족 {next_index + 1}을(를) 승계해 이어서 수집합니다."
                ),
                "family_index": next_index,
                "next_run_at": state.next_run_at,
            }
        )
        self.persist_state()
        return True

    def _family_definition_file(
        self, output_dir: Path, family: list[tuple[str, str]]
    ) -> Path | None:
        """가족 정의 JSON 파일 경로 — 기본 A 가족이면 None(루트별 final 생성).

        build_all_finals 는 categories_file 유무로 루트별 final 생성 여부를
        정한다(M1 규약). 매니저의 가족은 인메모리 목록이므로, 기본 A 가족
        (TOP_CATEGORIES)이 아닐 때만 load_categories_file 형식으로 저장해
        넘긴다. 파일은 가족 출력 폴더에 남아 재시작·감사에도 쓰인다.
        """
        if tuple(family) == tuple(TOP_CATEGORIES):
            return None
        path = output_dir / FAMILY_DEFINITION_FILENAME
        if not path.exists():
            output_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "instance": "parallel_manager",
                        "description": "병렬 매니저가 배분한 카테고리 가족",
                        "categories": [
                            [category_id, name] for category_id, name in family
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return path

    def _refresh_totals(self, state: _InstanceState) -> None:
        """세션 후 저장소 파일에서 현재 가족 누적(3P 상품·확보 판매자)을 집계."""
        output_dir = self._family_output_dir(state)
        state.total_products = _count_csv_rows(output_dir / "top_products.csv")
        state.total_sellers = _count_csv_rows(
            output_dir / "top_sellers.csv", only_saved=True
        )

    def _restore_state(self, built: list[ParallelInstanceConfig]) -> bool:
        """같은 출력 폴더의 저장 상태로 재개한다. 복원 실패 시 False."""
        raw = self.load_state(self.config.output_dir / STATE_FILENAME)
        if raw is None:
            return False
        records = {}
        for record in raw.get("instances") or []:
            if isinstance(record, dict) and record.get("instance_id"):
                records[str(record["instance_id"])] = record
        if set(records) != {instance.instance_id for instance in built}:
            # 인스턴스 구성이 바뀌었다(강등 등) — 저장 상태를 믿을 수 없다.
            return False
        assigned: set[int] = set()
        for instance in built:
            record = records[instance.instance_id]
            state = _InstanceState(config=instance)
            status = str(record.get("status") or "")
            state.status = status if status in _PERSIST_STATUSES else STATUS_WAITING
            family_index = record.get("family_index")
            if (
                isinstance(family_index, int)
                and 0 <= family_index < len(self.config.families)
                and family_index not in assigned
            ):
                state.family_index = family_index
                assigned.add(family_index)
            next_run_at = record.get("next_run_at")
            state.next_run_at = (
                float(next_run_at)
                if isinstance(next_run_at, (int, float))
                else 0.0
            )
            state.total_products = int(record.get("total_products") or 0)
            state.total_sellers = int(record.get("total_sellers") or 0)
            state.effective_sid = str(record.get("effective_sid") or "")
            self.instances[instance.instance_id] = state
        pending = raw.get("pending_families")
        self.pending_families = [
            index
            for index in (pending if isinstance(pending, list) else [])
            if isinstance(index, int)
            and 0 <= index < len(self.config.families)
            and index not in assigned
        ]
        self._emit(
            {
                "type": "log",
                "instance": "",
                "message": (
                    "저장된 병렬 상태를 찾아 가족 배분과 예정 시각을 "
                    "복원했습니다."
                ),
            }
        )
        return True
