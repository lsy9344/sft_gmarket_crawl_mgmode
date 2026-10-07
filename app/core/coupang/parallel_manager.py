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
from app.core.coupang.patchright_canary import (
    advance_recovery_ramp,
    authorize_block_recovery,
    authorize_recovery_resume,
    claim_live_attempt,
    guard_lock_state,
    guard_ramp_limit,
)
from app.core.coupang.patchright_top_sellers import (
    authorize_http_503_retry,
    run_top_seller_batch,
)
from app.core.coupang.patchright_top_thousand import (
    MAX_LISTING_ITEMS,
    MAX_SESSION_PAGES,
    MAX_SESSION_PRODUCTS,
    TOP_CATEGORIES,
    read_state,
    run_top_pages,
)
from app.core.coupang.work_plan import (
    PLAN_VERSION,
    WORK_KIND_PAGES,
    WORK_KIND_SELLERS,
    WORK_KIND_WHOLE,
    WorkUnit,
    build_seller_units,
    count_product_rows,
    round_robin_shards,
    unit_from_dict,
    unit_to_dict,
)

__all__ = [
    "FAMILY_DEFINITION_FILENAME",
    "MAX_INSTANCES",
    "SHARDS_PER_INSTANCE",
    "STATE_FILENAME",
    "ParallelCoupangManager",
    "ParallelInstanceConfig",
    "ParallelRunConfig",
    "build_instances",
    "split_family_into_shards",
]

# 매니저 상태 영속 파일 — 실행 설정(output_dir) 안에 남긴다.
STATE_FILENAME = "coupang_parallel_state.json"
# 가드 장부 파일명 — patchright_canary._guard_path 가 쓰는 이름과 같다.
# 프로브가 장부를 복사해 점검할 때 이 이름으로 찾는다.
GUARD_LEDGER_FILENAME = "canary_guard.json"
# 인스턴스가 담당한 카테고리 가족 정의 — build_all_finals 에 넘겨 루트별
# final 생성 여부를 판단하게 하는 파일(기본 A 가족이 아닐 때만 만든다).
FAMILY_DEFINITION_FILENAME = "coupang_family.json"

MAX_INSTANCES = 20  # 20-병렬 확장(2026-10-06 사용자 결정) — 십진 sid 네임스페이스는 임의 인스턴스 수까지 자연 확장(i90, i100, …)
# 분할 모드에서 인스턴스 1개당 만들 샤드 수 — 2배로 쪼개면 먼저 끝난
# 인스턴스가 대기 큐의 남은 샤드를 승계하는 기존 규칙이 그대로
# 워크 스틸링 밸런서가 된다(느린/빠른 카테고리 불균형 흡수).
SHARDS_PER_INSTANCE = 2

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
    session_id: str   # decodo 시작 sid(예: "i20"). direct 는 빈 문자열.
    # 인스턴스마다 십진 네임스페이스(i{N}0~i{N}9)를 쓴다 — 세션 자동 교체가
    # 끝자리만 증가시키고 9에서 0으로 되감아 순환하므로(next_session_id),
    # 누적 회전이 쌓여도 다른 인스턴스의 sid와 절대 겹치지 않는다
    # (2026-10-04 7-병렬 실측에서 회전 자리올림(i2→i3)이 인스턴스 3의
    # 기본 sid와 충돌해 동일 출구 IP를 공유한 사고의 재발 방지).
    state_root: Path  # 가드 장부 + 브라우저 프로필 루트(인스턴스 전용)
    output_root: Path  # 가족별 출력 하위 폴더들의 부모


@dataclass
class ParallelRunConfig:
    """병렬 수집 실행 설정 — 사용자가 실행 대화상자에서 정하는 값.

    families: 가족 = [(카테고리id, 이름), ...]
    shard_mode 가 True 면 families 는 단일 카테고리를 분할한 샤드 목록이고,
    작업 폴더 접두사가 family_ 대신 shard_ 가 되며 전 샤드 완주 시
    finalize() 가 루트 병합을 수행한다.

    unit_specs/root_family 는 볼륨 인지 분할(2026-10-06 설계 §5) 확장이다.
    unit_specs 는 families 와 1:1인 작업 단위 사양(work_plan.unit_to_dict
    형식) — 있으면 매니저가 조각(work pages/sellers) 규약으로 운영하고,
    없으면 현행 통짜 샤드 규약 그대로다. root_family 는 분할 모드에서
    원본 가족 전체 — 구버전 상태를 이전 규약으로 재개할 때 라운드로빈
    재분할의 입력으로 쓴다(§5.5).
    """

    families: list[list[tuple[str, str]]]  # 가족 = [(카테고리id, 이름), ...]
    # output_dir 을 기본값 있는 필드들보다 앞에 둔다(데이터클래스 제약:
    # 기본값 없는 필드가 뒤에 올 수 없다). 인스턴스 수 등은 뒤의 기본값.
    output_dir: Path
    instance_count: int = 3    # 1~MAX_INSTANCES(20)
    interval_minutes: int = 80  # 인스턴스별 세션 간격(분)
    listing_pages: int = 8      # 목록 단계 1세션 페이지 수(1~10)
    seller_limit: int = 130     # 판매자 단계 1세션 상품 수 상한
    shard_mode: bool = False    # 단일 카테고리 분할 수집 여부
    unit_specs: list[dict] | None = None   # 작업 단위 사양(없으면 통짜 규약)
    root_family: list[tuple[str, str]] | None = None  # 분할 모드 원본 가족

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
        if not isinstance(self.shard_mode, bool):
            raise ValueError("shard_mode는 불리언이어야 합니다.")
        self._validate_unit_specs()
        self._validate_root_family()

    def _validate_unit_specs(self) -> None:
        """unit_specs 형식 검증 — families 와 1:1·단위 카테고리 일치."""
        if self.unit_specs is None:
            return
        if not isinstance(self.unit_specs, list) or len(self.unit_specs) != len(
            self.families
        ):
            raise ValueError("unit_specs는 families와 같은 길이의 목록이어야 합니다.")
        for index, spec in enumerate(self.unit_specs):
            try:
                unit = unit_from_dict(spec)
            except ValueError as error:
                raise ValueError(
                    f"작업 단위 사양({index + 1}번째)이 올바르지 않습니다: {error}"
                ) from error
            if list(unit.categories) != [
                tuple(pair) for pair in self.families[index]
            ]:
                raise ValueError(
                    f"작업 단위 사양({index + 1}번째)이 가족 목록과 일치하지 않습니다."
                )

    def _validate_root_family(self) -> None:
        if self.root_family is None:
            return
        if not isinstance(self.root_family, list) or not self.root_family:
            raise ValueError("root_family는 비어 있지 않은 카테고리 목록이어야 합니다.")
        for pair in self.root_family:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("root_family 항목은 (id, 이름) 튜플이어야 합니다.")

    def work_dir_name(self, family_index: int) -> str:
        """작업 단위(가족/샤드) 출력 하위 폴더 이름."""
        prefix = "shard" if self.shard_mode else "family"
        return f"{prefix}_{family_index + 1:02d}"

    @property
    def work_unit_label(self) -> str:
        """작업 단위 표시어 — 로그·안내 문구에 쓴다(가족/샤드)."""
        return "샤드" if self.shard_mode else "가족"


def split_family_into_shards(
    family: list[tuple[str, str]], shard_count: int
) -> list[list[tuple[str, str]]]:
    """가족(루트+후손 카테고리)을 겹치지 않는 샤드로 나눈다.

    카테고리 크기는 수집 전에 알 수 없으므로 라운드로빈 교차 분할로
    편차를 흡수한다(트리 순서대로 index % N 번째 샤드에 하나씩 나눠
    한쪽 샤드로 몰리지 않게 한다). 빈 샤드는 반환 목록에서 제외해
    실행 설정 검증(비어 있지 않은 가족)을 그대로 통과하게 한다.

    구현은 work_plan.round_robin_shards — 물량 미지 폴백(§4 3순위)과
    같은 함수를 공유한다.
    """
    return round_robin_shards(family, shard_count)


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
                # 시작 sid는 인스턴스별 십진 네임스페이스(예: "i20"). 실제 세션은
                # 가족 출력 폴더의 상태 파일(sid 마지막 성공값)을 우선한다.
                session_id="" if line == "direct" else f"i{number}0",
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
        # 작업 단위 — 신규 실행이면 실행 설정의 사양(unit_specs)에서,
        # 재시작이면 상태 파일에서 복원한다(_restore_state 가 덮어쓴다).
        self.units: list[WorkUnit] = []
        self.plan_version = 1
        self._normalize_units()
        built = build_instances(config, on_event=on_event)
        if not self._restore_state(built):
            for instance in built:
                self.instances[instance.instance_id] = _InstanceState(
                    config=instance
                )
            self.plan()

    # ── 배분 ───────────────────────────────────────────────────────────

    def _normalize_units(self) -> None:
        """실행 설정(families/unit_specs)에서 작업 단위 목록을 만든다.

        unit_specs 이 있으면 조각 규약(plan_version 2), 없으면 families
        그대로 통짜(work) 단위 — 현행 규약과 완전히 같은 동작이다.
        """
        if self.config.unit_specs is not None:
            self.units = [unit_from_dict(spec) for spec in self.config.unit_specs]
            self.plan_version = PLAN_VERSION
            return
        self.units = [
            WorkUnit(
                kind=WORK_KIND_WHOLE,
                categories=tuple(tuple(pair) for pair in family),
            )
            for family in self.config.families
        ]
        self.plan_version = 1

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
                    f"{self.config.work_unit_label} "
                    f"{len(self.config.families)}개를 인스턴스 "
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
                            "message": (
                                "담당할 "
                                + self.config.work_unit_label
                                + "이(가) 더 없습니다."
                            ),
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
            # 참조). 복구 확대 단계(차단 회복 직후 60→180→600 아이템)가
            # 살아 있으면 이번 세션 규모를 단계 허용량에 맞춰 줄인다.
            ramp_limit = guard_ramp_limit(instance.state_root)
            listing_pages, seller_limit = self._ramp_capped_limits(ramp_limit)
            allowed, reason = self._guard_probe(state, current, listing_pages)
            if not allowed:
                lock = guard_lock_state(instance.state_root)
                if lock in ("blocked", "recovery_hold"):
                    # 영구 잠금 — 회복 조건(차단 1시간, 복구 확인 다음 날)이
                    # 지났으면 잠금 해제를 자동 승인하고 다시 점검한다.
                    approved, _approve_message = self._authorize_guard_recovery(
                        state, lock, current
                    )
                    if approved:
                        ramp_limit = guard_ramp_limit(instance.state_root)
                        listing_pages, seller_limit = self._ramp_capped_limits(
                            ramp_limit
                        )
                        allowed, reason = self._guard_probe(
                            state, current, listing_pages
                        )
                if not allowed and guard_lock_state(instance.state_root) in (
                    "blocked",
                    "recovery_hold",
                    "invalid",
                ):
                    # 잠금이 그대로면 연기해도 같은 거부가 돌아온다 — 이번
                    # 실행에서는 인스턴스를 종료 상태로 둔다(무한 연기 방지).
                    # 재시작하면 _restore_state 가 다시 심사를 맡긴다.
                    state.status = STATUS_BLOCKED
                    state.next_run_at = 0.0
                    blocked_reason = (
                        "안전 기록이 손상되어 실접속을 계속 거부합니다."
                        if guard_lock_state(instance.state_root) == "invalid"
                        else "차단 잠금이 풀리지 않아 이 인스턴스를 중단합니다."
                        f" (재시작 시 회복 조건을 다시 심사합니다: {reason})"
                    )
                    self._emit(
                        {
                            "type": "blocked",
                            "instance": instance.instance_id,
                            "family_index": state.family_index,
                            "reason": blocked_reason,
                            "reference": "",
                        }
                    )
                    self.persist_state()
                    summary.update(
                        status=state.status,
                        result=_RESULT_BLOCKED,
                        next_run_at=state.next_run_at,
                    )
                    return summary
                if not allowed:
                    # 잠금 없는 거부는 일시(간격 미달 등) — 다음 예정으로 연기.
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

            # (4) 다음 작업 선택(네트워크 없음) — 단계 판단은 작업 단위
            # 종류를 따른다(§5.3 혼합 큐): whole 은 폴더 안 목록→판매자
            # 고정 순서, pages 는 목록만, sellers 는 매핑 대기열만 본다.
            # HTTP 503 으로 멈춘 판매자 작업은 3시간이 지났으면 여기서
            # 다시 열린다(재시도 1회 계약).
            retried_503, _retry_note = authorize_http_503_retry(
                output_dir, now=current
            )
            if retried_503:
                self._emit(
                    {
                        "type": "log",
                        "instance": instance.instance_id,
                        "family_index": state.family_index,
                        "message": "HTTP 503 재시도 조건이 지나 판매자 작업을 다시 엽니다.",
                    }
                )
            unit = self.units[state.family_index]
            decision = self._choose_action_for_unit(unit, output_dir, family)
            action = str(decision.get("action") or "")
            summary["action"] = action
            if action == "halted":
                reason_text = str(decision.get("reason") or "")
                if "HTTP 503" in reason_text:
                    # 503 재시도 대기 — 인스턴스를 멈추지 않고 다음 예정에
                    # 다시 연다(3시간 경과 시 (4)에서 자동 재개).
                    state.status = STATUS_WAITING
                    state.next_run_at = current + self._interval_seconds
                    self._emit(
                        {
                            "type": "log",
                            "instance": instance.instance_id,
                            "family_index": state.family_index,
                            "message": (
                                "HTTP 503 재시도 대기 — 다음 예정 시각에 다시 "
                                "확인합니다."
                            ),
                        }
                    )
                    self.persist_state()
                    summary.update(
                        status=state.status,
                        result="http_503_retry_wait",
                        next_run_at=state.next_run_at,
                    )
                    return summary
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
            # 복구 확대 단계 중에는 (3)에서 줄인 규모로 실행한다.
            if action == "sellers":
                result = run_top_seller_batch(
                    output_dir=output_dir,
                    limit=seller_limit,
                    control=self.control,
                    state_root=instance.state_root,
                    browser_profile_root=instance.state_root,
                    browser_scope_factory=self.browser_scope_factory,
                    proxy=proxy,
                    products_paths=self._seller_source_paths(unit),
                    slice_index=(
                        unit.slice_index
                        if unit.kind == WORK_KIND_SELLERS
                        else None
                    ),
                    slice_count=(
                        unit.slice_count if unit.kind == WORK_KIND_SELLERS else 1
                    ),
                )
            else:
                result = run_top_pages(
                    output_dir=output_dir,
                    page_count=listing_pages,
                    control=self.control,
                    on_event=self._runner_bridge(
                        instance.instance_id, state.family_index
                    ),
                    state_root=instance.state_root,
                    categories=family,
                    browser_scope_factory=self.browser_scope_factory,
                    proxy=proxy,
                    page_from=(
                        unit.page_from if unit.kind == WORK_KIND_PAGES else None
                    ),
                    page_to=(unit.page_to if unit.kind == WORK_KIND_PAGES else None),
                )
            summary["result"] = str(result.get("event") or "")

            # (6) 결과 분류 — 각 인스턴스 상태로 반영. 확대 단계 중 세션이
            # 정상 끝났으면 한 단계 올린다(60→180→600→해제).
            self._apply_session_result(state, result, current)
            if ramp_limit is not None and summary["result"] not in (
                _RESULT_BLOCKED,
                _RESULT_CANCELLED,
                _RESULT_GUARD_REFUSED,
                *_RESULT_FAILED,
            ):
                advanced, _advance_note = advance_recovery_ramp(
                    ramp_limit, instance.state_root, now=current
                )
                if advanced:
                    self._emit(
                        {
                            "type": "log",
                            "instance": instance.instance_id,
                            "family_index": state.family_index,
                            "message": (
                                f"복구 검증({ramp_limit}개)을 통과해 다음 단계로"
                                " 확대합니다."
                            ),
                        }
                    )
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

    def finalize(self) -> dict | None:
        """분할 모드 전 샤드 완주 시 루트 병합을 수행한다.

        조건: shard_mode 이고 모든 인스턴스가 done(차단/오류 없이)이며
        병합 표시 파일(coupang_shard_merge.json)이 아직 없을 때. 그 외엔
        병합하지 않고 None 을 돌려준다 — 차단/오류로 남은 샤드 데이터를
        반쪽짜리 완성형으로 굳히지 않는다(재개해 마저 수집하면 된다).
        워커는 스케줄 루프가 끝난 뒤 이걸 1회 호출한다.
        """
        if not self.config.shard_mode:
            return None
        if not all(
            state.status == STATUS_DONE for state in self.instances.values()
        ):
            return None
        from app.core.coupang.parallel_merge import (
            MERGE_STATE_FILENAME,
            merge_shard_outputs,
        )

        if (self.config.output_dir / MERGE_STATE_FILENAME).exists():
            return None  # 이미 병합된 실행 — 다시 합치지 않는다
        shard_dirs = [
            self.config.output_dir / self.config.work_dir_name(index)
            for index in range(len(self.config.families))
        ]
        self._emit(
            {
                "type": "log",
                "instance": "",
                "message": (
                    f"전 샤드 완주 — {len(shard_dirs)}개 샤드 출력을 "
                    "루트에서 병합합니다."
                ),
            }
        )
        summary = merge_shard_outputs(self.config.output_dir, shard_dirs)
        self._emit({"type": "merge_complete", "instance": "", **summary})
        return summary

    # ── 상태 영속 ──────────────────────────────────────────────────────

    def persist_state(self) -> None:
        """인스턴스별 상태 전부 + 대기 가족 큐를 원자적으로 저장한다.

        앱 설정 파일 패턴(임시 파일 작성 후 os.replace)으로 쓴다 — 같은
        출력 폴더로 다시 시작하면 배분·예정 시각이 그대로 복원된다.
        """
        payload = {
            "version": 1,
            "plan_version": self.plan_version,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "interval_minutes": self.config.interval_minutes,
            "shard_mode": self.config.shard_mode,
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
        # 조각 규약(plan v2)에서만 단위 사양을 남긴다 — 실행 중 배출된
        # 판매자 조각까지 포함해 재시작이 같은 단위 목록을 복원한다.
        # v1 파일은 종전 형식 그대로(구버전 코드도 읽을 수 있게).
        if self.plan_version == PLAN_VERSION:
            payload["units"] = [unit_to_dict(unit) for unit in self.units]
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
        """작업 단위 전용 출력 폴더 — 인덱스 기준 하위 폴더(인스턴스 공용 부모).

        분할 모드(shard_mode)에서는 shard_NN, 가족 모드에서는 family_NN.
        """
        index = state.family_index if state.family_index is not None else 0
        return state.config.output_root / self.config.work_dir_name(index)

    def _choose_action_for_unit(
        self, unit: WorkUnit, output_dir: Path, family: list[tuple[str, str]]
    ) -> dict:
        """작업 단위 종류에 맞는 다음 작업 판정(§5.3 — 단계 판단 이관)."""
        if unit.kind == WORK_KIND_PAGES:
            return choose_action(
                output_dir,
                family,
                page_from=unit.page_from,
                page_to=unit.page_to,
            )
        if unit.kind == WORK_KIND_SELLERS:
            return choose_action(
                output_dir,
                family,
                products_paths=self._seller_source_paths(unit),
                slice_index=unit.slice_index,
                slice_count=unit.slice_count,
            )
        return choose_action(output_dir, family)

    def _seller_source_paths(self, unit: WorkUnit) -> list[Path] | None:
        """판매자 조각이 상품을 읽을 경로들 — 목록 조각 폴더의 top_products.csv.

        product_sources 는 작업 폴더 이름(shard_NN)이므로 실행 루트를 붙여
        경로로 만든다. 통짜 단위는 None(자기 폴더의 상품 파일을 쓰는 현행 규약).
        """
        if unit.kind != WORK_KIND_SELLERS or not unit.product_sources:
            return None
        return [
            self.config.output_dir / name / "top_products.csv"
            for name in unit.product_sources
        ]

    def _listing_complete_on_disk(self, unit_index: int) -> bool:
        """목록 조각의 완주 여부를 폴더 상태 파일(top_state.json)로 판정한다.

        배출 규칙의 사실 근원 — 실행 중 배출과 재시작 복원 배출이 같은
        판정을 내도록 디스크를 본다(메모리 상태와 무관). 조각이 아직
        시작 전이면 상태가 없어 running 으로 간주한다.
        """
        unit = self.units[unit_index]
        output_dir = (
            self.config.output_dir / self.config.work_dir_name(unit_index)
        )
        try:
            state = read_state(
                output_dir,
                list(unit.categories),
                page_from=unit.page_from,
                page_to=unit.page_to,
            )
        except Exception:  # noqa: BLE001 - 읽을 수 없으면 미완료로 본다
            return False
        return state["status"] != "running"

    def _sweep_emit_seller_units(self) -> int:
        """배출 규칙(§5.3) — 목록이 전부 완료된 카테고리의 판매자 조각을
        대기 큐에 내보낸다. 이미 이 카테고리의 판매자 조각이 있으면 건너뛴다
        (멱등 — 완주 시마다·재시작 복원 시마다 불러도 중복 배출이 없다).

        K(슬라이스 수)는 목록 조각 폴더들의 상품 행 수로 정한다 — 세션 1개
        분량(seller_limit)보다 작은 조각은 병렬 이득이 없어 만들지 않고,
        인스턴스 수를 상한으로 한다(work_plan.seller_slice_count_for).
        """
        pages_indices: dict[str, list[int]] = {}
        for index, unit in enumerate(self.units):
            if unit.kind == WORK_KIND_PAGES:
                pages_indices.setdefault(unit.category_id, []).append(index)
        seller_categories = {
            unit.category_id
            for unit in self.units
            if unit.kind == WORK_KIND_SELLERS
        }
        emitted = 0
        for category_id, indices in pages_indices.items():
            if category_id in seller_categories:
                continue
            if not all(
                self._listing_complete_on_disk(index) for index in indices
            ):
                continue
            category = self.units[indices[0]].categories[0]
            sources = [self.config.work_dir_name(index) for index in indices]
            row_count = count_product_rows(self.config.output_dir, sources)
            new_units = build_seller_units(
                category,
                sources,
                row_count,
                len(self.instances),
                seller_limit=self.config.seller_limit,
            )
            first_index = len(self.units)
            for offset, unit in enumerate(new_units):
                self.units.append(unit)
                self.config.families.append(list(unit.categories))
                self.pending_families.append(first_index + offset)
            emitted += len(new_units)
            self._emit(
                {
                    "type": "units_appended",
                    "instance": "",
                    "first_family_index": first_index,
                    "labels": [unit.label for unit in new_units],
                }
            )
            self._emit(
                {
                    "type": "log",
                    "instance": "",
                    "message": (
                        f"{category[1]} 목록 완료 — 판매자 조각 "
                        f"{len(new_units)}개를 대기 큐에 배출합니다"
                        f"(상품 {row_count:,}개 · 소스 {len(sources)}폴더)."
                    ),
                }
            )
        if emitted:
            self._revive_done_instances()
            self.persist_state()
        return emitted

    def _revive_done_instances(self) -> int:
        """대기 큐가 다시 찼을 때 종료(DONE) 인스턴스를 깨운다(§5.3).

        인스턴스는 대기 큐가 빈 순간 DONE 이 되는데, 판매자 조각은 목록
        완료 시점(실행 후반)에 배출되므로 그대로 두면 마지막 한 인스턴스가
        배출 조각 전부를 80분 간격으로 홀로 순차 처리한다 — "어떤 회선도
        굶지 않는다"(§5.3)가 런의 끝자락에서 깨진다. 차단/오류 인스턴스는
        안전 장치 재심사 대상이므로 건드리지 않는다(재시작 시 _restore_state
        가 다시 연다). 부활 인스턴스는 다음 run_due 에서 대기 큐를 승계한다.
        """
        revived = 0
        for state in self._ordered_instances():
            if state.status != STATUS_DONE:
                continue
            state.status = STATUS_WAITING
            state.family_index = None
            state.next_run_at = 0.0
            state.total_products = 0
            state.total_sellers = 0
            revived += 1
        if revived:
            self._emit(
                {
                    "type": "log",
                    "instance": "",
                    "message": (
                        f"배출된 조각을 받을 종료 인스턴스 {revived}개를"
                        " 다시 가동합니다."
                    ),
                }
            )
        return revived

    def _ramp_capped_limits(self, ramp_limit: int | None) -> tuple[int, int]:
        """복구 확대 단계에 맞춘 이번 세션 규모 — (목록 페이지, 판매자 상한).

        확대 중이 아니면 설정값 그대로. 단계 허용량(60/180/600 아이템)을
        넘지 않게 목록 페이지는 60아이템/쪽 기준으로, 판매자 상한은 아이템
        수 기준으로 자른다(최소 1).
        """
        if ramp_limit is None:
            return self.config.listing_pages, self.config.seller_limit
        pages = max(
            1, min(self.config.listing_pages, ramp_limit // MAX_LISTING_ITEMS)
        )
        sellers = max(1, min(self.config.seller_limit, ramp_limit))
        return pages, sellers

    def _authorize_guard_recovery(
        self, state: _InstanceState, lock: str, current: float
    ) -> tuple[bool, str]:
        """잠긴 안전 장치의 회복 승인을 시도한다(차단 1시간·복구 확인 다음 날).

        승인 조건은 장부 스스로 가린다(authorize_* 가 남은 시간을 검사).
        승인되면 매니저는 곧바로 사전 점검을 다시 돈다 — 프로토타입의 다단계
        수동 절차(소량 확인 → 대기 → 확대) 대신, 세션 간격(기본 80분)과 일일
        상한이 회복 직후 과부하를 이미 막아준다.
        """
        instance = state.config
        if lock == "recovery_hold":
            approved, message = authorize_recovery_resume(
                instance.state_root, now=current
            )
        else:
            approved, message = authorize_block_recovery(
                instance.state_root, now=current
            )
        if approved:
            self._emit(
                {
                    "type": "log",
                    "instance": instance.instance_id,
                    "family_index": state.family_index,
                    "message": (
                        "차단 복구 조건이 지나 안전 장치 잠금을 해제했습니다"
                        " — 이어서 수집합니다."
                    ),
                }
            )
        return approved, message

    def _guard_probe(
        self,
        state: _InstanceState,
        now: float,
        listing_pages: int | None = None,
    ) -> tuple[bool, str]:
        """실접속 사전 점검 — 장부 사본에서 claim_live_attempt 를 돌려본다.

        러너(run_top_pages/run_top_seller_batch)가 실장부를 claim/settle
        하므로, 매니저는 거부 여부만 판정한다. 예정 규모는 목록 단계 기준
        상한(listing_pages × 60)으로 잡는다 — 판매자 세션은 이보다 작게
        claim 하므로 목록 기준으로 통일해도 안전하다(사본이라 기록 안 남음).
        pages 는 복구 확대 단계에서 줄인 세션 규모를 반영한다.
        """
        pages = self.config.listing_pages if listing_pages is None else listing_pages
        planned_items = min(pages * MAX_LISTING_ITEMS, MAX_SESSION_PRODUCTS)
        with tempfile.TemporaryDirectory(prefix="coupang-guard-probe-") as tmp:
            probe_root = Path(tmp)
            source = state.config.state_root / GUARD_LEDGER_FILENAME
            if source.exists():
                shutil.copy2(source, probe_root / GUARD_LEDGER_FILENAME)
            return claim_live_attempt(
                probe_root,
                now=now,
                planned_items=planned_items,
                planned_pages=pages,
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
        """완주한 가족의 완성형 파일을 만들고 승계/종료를 처리한다.

        완주 단위가 목록 조각(pages)이면 승계 전에 배출 규칙(§5.3)을 돈다 —
        그 카테고리의 목록 전 조각이 완료됐으면 판매자 조각을 대기 큐에
        배출해 다음 도래한 아무 인스턴스나 가져가게 한다. 이 인스턴스가
        곧바로 승계할 수도 있다(emit 이 take 보다 먼저).
        """
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
        if self.units[finished_index].kind == WORK_KIND_PAGES:
            self._sweep_emit_seller_units()
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
                    f"{self.config.work_unit_label} {next_index + 1}을(를)"
                    " 승계해 이어서 수집합니다."
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
        """같은 출력 폴더의 저장 상태로 재개한다. 복원 실패 시 False.

        plan_version 복원 규약(§5.5): v2 상태는 상태 파일의 단위 목록이
        사실 근원이다(실행 중 배출된 판매자 조각 포함). 구버전 상태는
        이전 규약(통짜 샤드)으로 그대로 재개한다 — 새 계획(unit_specs)으로
        시작했어도 root_family 라운드로빈 재분할로 되돌려 같은 폴더 이어받기
        를 보장한다. 신규 실행만 새 배분을 받는다.
        """
        raw = self.load_state(self.config.output_dir / STATE_FILENAME)
        if raw is None:
            return False
        # 배분 모드가 바뀌면 작업 폴더 접두사(family_/shard_)도 달라져
        # 저장된 배분을 그대로 믿을 수 없다 — 새로 배분한다. 진행 데이터는
        # 폴더 안에 있으므로 새 배분으로 이어서 확인된다.
        if bool(raw.get("shard_mode")) != bool(self.config.shard_mode):
            return False
        if not self._restore_units(raw):
            return False
        records = {}
        for record in raw.get("instances") or []:
            if isinstance(record, dict) and record.get("instance_id"):
                records[str(record["instance_id"])] = record
        if set(records) != {instance.instance_id for instance in built}:
            # 인스턴스 구성이 바뀌었다(강등 등) — 저장 상태를 믿을 수 없다.
            return False
        assigned: set[int] = set()
        requeued_stopped = 0
        for instance in built:
            record = records[instance.instance_id]
            state = _InstanceState(config=instance)
            status = str(record.get("status") or "")
            state.status = status if status in _PERSIST_STATUSES else STATUS_WAITING
            if state.status in (STATUS_BLOCKED, STATUS_ERROR):
                # 재시작하면 차단/오류 인스턴스도 다시 심사받는다 — 회복
                # 조건은 안전 장치 장부(차단 1시간 잠금)와 세션 절차가
                # 가린다. 오류 사유가 남아 있으면(halted 등) 곧바로 같은
                # 오류로 수렴하므로 되돌려도 안전하다. 재시작 예약이 종료
                # 상태로 굳어 아무것도 하지 않고 끝나는 일(맡은 작업이
                # 고아가 되는 일)을 막는다.
                state.status = STATUS_WAITING
                state.next_run_at = 0.0
                requeued_stopped += 1
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
        restored_note = (
            "저장된 병렬 상태를 찾아 "
            f"{self.config.work_unit_label} 배분과 예정 시각을 복원했습니다."
        )
        if requeued_stopped:
            restored_note += (
                f" 차단/오류 인스턴스 {requeued_stopped}개를 다시 심사합니다"
                "(안전 장치와 세션 절차가 회복 조건을 판정합니다)."
            )
        self._emit({"type": "log", "instance": "", "message": restored_note})
        if self.plan_version == PLAN_VERSION:
            # 종료 직전 완주한 목록 조각의 판매자 조각을 놓치지 않게
            # 복원 직후 배출 규칙을 한 번 더 돈다(멱등 — 중복 배출 없음).
            self._sweep_emit_seller_units()
        return True

    def _restore_units(self, raw: dict) -> bool:
        """상태 파일의 plan_version 에 맞춰 작업 단위를 복원한다(§5.5)."""
        plan_version = raw.get("plan_version", 1)
        if isinstance(plan_version, bool) or not isinstance(plan_version, int):
            plan_version = 1
        if plan_version == PLAN_VERSION:
            specs = raw.get("units")
            if not isinstance(specs, list) or not specs:
                return False  # 깨진 v2 상태 — 새 배분으로 돌아간다
            try:
                units = [unit_from_dict(spec) for spec in specs]
            except (ValueError, TypeError):
                return False
            self.units = units
            self.plan_version = PLAN_VERSION
            # 실행 중 배출된 판매자 조각까지 상태 파일의 목록이 사실 근원 —
            # 실행 설정의 families/unit_specs 를 여기에 맞춰 덮어쓴다.
            self.config.families = [list(unit.categories) for unit in units]
            self.config.unit_specs = [unit_to_dict(unit) for unit in units]
            return True
        # 구버전(통짜 샤드) 상태를 새 계획(unit_specs)으로 시작한 폴더에서
        # 만났으면 이전 규약 분할로 되돌려 재개한다. root_family 가 없으면
        # 재구성할 수 없으므로 새 배분(현행 폴백)으로 돌아간다.
        if self.config.unit_specs is not None:
            legacy = self._legacy_families_for(raw)
            if legacy is None:
                return False
            self.config.families = legacy
            self.config.unit_specs = None
            self._normalize_units()
            self._emit(
                {
                    "type": "log",
                    "instance": "",
                    "message": (
                        "구버전 배분 상태를 찾았습니다 — 이전 규약(통짜 샤드)"
                        " 분할로 그대로 이어서 재개합니다. 새 볼륨 인지 배분은"
                        " 새 출력 폴더에서 시작할 때 적용됩니다."
                    ),
                }
            )
        return True

    def _legacy_families_for(
        self, raw: dict
    ) -> list[list[tuple[str, str]]] | None:
        """구버전 상태의 통짜 샤드 목록 재구성 — 라운드로빈은 결정적이므로
        같은 카테고리·샤드 수를 넣으면 항상 같은 분할을 낸다."""
        family_count = raw.get("family_count")
        if isinstance(family_count, bool) or not isinstance(family_count, int):
            return None
        if family_count < 1:
            return None
        root = self.config.root_family
        if not root:
            return None
        return split_family_into_shards(
            [tuple(pair) for pair in root], family_count
        )
