"""Phase 2(판매자정보 건별 수집) 엔진 (WORK_ORDER §5) — 확정된 CrawlPlan 실행기.

Qt 비의존. 워커가 Control(취소/일시정지)과 콜백(로그/진행/수집)을 주입한다.

내구성 프로토콜 (HIGH-1 회귀 방지):
    성공적으로 fetch 한 goodscode 는 즉시 공유 collected_ids 에 반영하지 않는다.
    로컬 `newly_collected` 집합에만 쌓아두고, `_commit_checkpoint()` 가
    save_partial_results() 로 결과를 먼저 durable 저장한 뒤에만 collected_ids
    에 병합·저장한다. save_partial_results 가 실패하면 예외가 그대로 전파되고
    collected_ids 는 호출 전 상태 그대로 유지된다 — 그래야 이후 어떤 예외
    처리 경로(바깥 except 등)에서도 "아직 디스크에 없는 결과의 ID" 가 실수로
    커밋되는 일이 없다.

체크포인트 승격 (HIGH-3 회귀 방지):
    crawl() 시작 시 Pre-scan 결과와 무관하게 출력 디렉터리를 직접 훑어
    (Storage.find_leftover_partials) 모든 잔여 체크포인트를 최종 파일로
    승격한다. 어떤 카테고리가 다음 조사에서 '완료됨'으로 판정되어 계획에서
    빠지더라도, 이전에 죽은 실행이 남긴 체크포인트가 영구히 숨겨지지 않는다.

    승격은 fail-closed 이다(3차 리뷰 HIGH-1 회귀 방지): 승격(결과 저장) 자체가
    실패하거나 체크포인트가 손상되어 자동 복구할 수 없는 경우, crawl() 은 그
    자리에서 즉시 summary.error 를 설정하고 반환하며 단 하나의 카테고리도
    실행하지 않는다(ReconcileResult.blocking). 예전에는 승격 실패를 로그만
    남기고 계속 진행했는데, 그러면 실패한 카테고리를 이어서 새로 수집할 때
    save_partial_results() 가 (라벨로 고정된) 같은 체크포인트 경로에 새
    데이터를 덮어써 미승격 상태로 남아있던 이전 데이터가 영구히 사라진다.
    손상된 체크포인트(격리되어 *.corrupt_*.json 으로 백업만 된 경우)도
    자동으로는 정상 결과로 복구되지 않았으므로 동일하게 차단한다(4차 리뷰
    아키텍처 BLOCK-2) — 사용자가 백업 파일을 직접 확인해야 한다.

    체크포인트 삭제 실패는 별도로 다룬다(cleanup_failed_labels): 결과
    데이터는 이미 durable 하게 저장됐으므로 fail-closed 대상은 아니지만,
    삭제되지 않은 체크포인트가 다음 실행에서 또 승격되어 중복 결과 파일을
    만들지 않도록, 이미 collected_ids 에 전부 포함된 체크포인트는 재승격
    없이 정리만 재시도한다(4차 리뷰 HIGH).

    crawl() 은 UI 를 거치지 않고 미리 만들어둔 CrawlPlan 으로 직접 호출될 수도
    있다. 이 경우 reconcile 이 collected_ids 를 바꿨는데 그 계획이 여전히
    이미-커밋된 코드를 target_codes 로 담고 있다면(낡은 계획), 그대로
    실행하면 같은 goodscode 가 두 번 저장된다. 그런 겹침이 발견되면
    summary.replan_required=True 로 표시하고 계획을 실행하지 않는다(4차 리뷰
    아키텍처 BLOCK-1). main_window.on_start() 는 계획을 만들기 *전에* 이미
    reconcile 을 실행하므로 정상 UI 경로에서는 이 조건이 발생하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from app.core import config
from app.core.base import CancelledError, Control
from app.core.plan import CategoryPlan, CrawlPlan
from app.core.storage import LoadStatus, Storage
from app.models.records import CORE_CONTENT_FIELDS, SellerRecord

# ── 콜백 타입 ──────────────────────────────────────────────────────────
LogFn = Callable[[str], None]
CategoryFn = Callable[[str, int, int], None]      # (category_name, current, total)
ItemFn = Callable[[str, int, int], None]          # (goodscode, current, total)
CollectedFn = Callable[[dict], None]              # 수집 레코드
ErrorFn = Callable[[str], None]


def _noop(*_a, **_k) -> None:  # pragma: no cover
    pass


def _checkpoint_content_hash(records: list[dict]) -> str:
    """레코드 전체 내용(필드 값 포함)에서 계산한 결정적 해시.

    goodscode 목록만 해싱하면(이전 버전, 5차 리뷰 HIGH) 같은 상품이라도
    판매자 정보가 나중에 갱신되어 다른 내용으로 다시 체크포인트된 경우 예전
    해시와 충돌해, promote_partial() 이 서로 다른 내용을 같은 결정적 파일에
    덮어써 이전 값을 유실시킬 위험이 있었다. 전체 레코드의 canonical(키
    정렬) JSON 을 해싱하면 내용이 조금이라도 다르면 다른 해시가 나와 이
    위험이 없어진다. goodscode 로 정렬해 레코드 순서 차이가 해시에 영향을
    주지 않게 한다(같은 집합이면 항상 같은 해시).
    """
    normalized = sorted(
        (rec for rec in records if isinstance(rec, dict)),
        key=lambda r: str(r.get("goodscode", "")),
    )
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _extract_codes(records: list[dict]) -> set[str]:
    """레코드 목록에서 유효한 문자열 goodscode 만 뽑아 set[str] 으로 반환.

    Pyright 등 정적 타입 검사기가 `rec.get("goodscode")` 를 바로 집합
    컴프리헨션에 넣으면 `str | None` 로 추론해 set[str] 이 보장되지 않는다는
    지적이 있었다(5차 리뷰 MEDIUM). 명시적 isinstance narrowing 을 거치는
    별도 함수로 분리해 반환 타입을 확실히 한다.
    """
    codes: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        code = rec.get("goodscode")
        if isinstance(code, str) and code:
            codes.add(code)
    return codes


@dataclass
class ReconcileResult:
    """체크포인트 승격(reconcile_leftover_checkpoints) 결과를 상태별로 구분한다.

    (4차 리뷰 "promoted_ids/corrupt/cleanup_failed/commit_failed/state_changed
    처럼 구분되는 구조화된 결과" 요청 반영)
    """

    # 이번 호출에서 새로 collected_ids 에 추가된 goodscode(성공적으로 승격된 것들).
    promoted_ids: set[str] = field(default_factory=set)
    # save_results() 자체(결과 저장)가 실패한 라벨 — 데이터가 아직 어디에도
    # durable 하게 저장되지 않았으므로 fail-closed 대상이다.
    failed_labels: list[str] = field(default_factory=list)
    # 손상되어 격리(백업)됐지만 자동으로 정상 결과로 복구되지 않은 라벨 —
    # 데이터 자체는 *.corrupt_*.bak 으로 보존되지만, 사용자가 수동으로
    # 확인하기 전까지는 fail-closed 대상이다.
    corrupt_labels: list[str] = field(default_factory=list)
    # 손상까지 감지했지만 격리(백업)조차 실패해 원본이 원래 자리에 위험하게
    # 남아있는 라벨 — corrupt_labels 보다 더 심각하다(원본이 아직 보호되지
    # 않았다). 절대 자동으로 건드리지 않고 fail-closed 로 막는다(5차 리뷰 HIGH).
    quarantine_failed_labels: list[str] = field(default_factory=list)
    # 결과 저장은 성공했지만 체크포인트 파일 삭제가 실패한 라벨 — 데이터는
    # 안전하므로 fail-closed 대상이 아니다(다음 실행에서 정리만 재시도).
    cleanup_failed_labels: list[str] = field(default_factory=list)
    # 결과 저장은 성공했지만 collected_ids 커밋이 실패한 라벨 — 데이터는
    # 안전하므로 fail-closed 대상이 아니다(다음 조사에서 재수집될 수 있음).
    commit_failed_labels: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """True 면 신규 수집을 시작하면 안 된다 — 사용자가 먼저 문제를 해결해야 한다."""
        return bool(self.failed_labels or self.corrupt_labels or self.quarantine_failed_labels)

    @property
    def state_changed(self) -> bool:
        """collected_ids 가 이번 호출로 실제로 바뀌었는지 여부."""
        return bool(self.promoted_ids)


# ── 체크포인트 승격 (HIGH-3 / 3차 리뷰 HIGH-1 회귀 방지) ────────────────────
def reconcile_leftover_checkpoints(
    storage: Storage, collected_ids: set[str], on_log: LogFn = _noop
) -> ReconcileResult:
    """Pre-scan/계획과 무관하게 디스크의 모든 잔여 체크포인트를 최종 파일로 승격.

    이전 실행이 체크포인트+ID 커밋까지는 성공했지만 최종 save_results 를
    마치기 전에 죽었다면, 다음 Pre-scan 은 해당 카테고리를 '완료됨'으로
    판단해 계획(target_codes)에서 제외한다. 이 함수를 CrawlPlan 확정 이전에
    (main_window.on_start) 그리고 crawl() 시작 시 다시 한 번(방어적으로)
    무조건 실행해야, 그런 카테고리의 체크포인트도 유실 없이 승격되고, Pre-scan
    에서 사용자가 확인하는 대상과 실제로 수집되는 대상이 어긋나지 않는다.

    상태 판정은 Storage.load_partial_results_status() 의 LoadStatus 를 그대로
    쓴다(5차 리뷰 HIGH 회귀 방지) — 예전에는 `path.exists()` 만으로 "격리
    됐는지"를 유추했는데, 격리(백업) 자체가 실패하면(rename/복사 모두 실패)
    원본이 원래 자리에 그대로 있어 "빈 체크포인트"로 오인되어 다음 코드가
    그 유일한 원본을 그냥 삭제해버리는 사고가 있었다. QUARANTINE_FAILED 는
    절대 건드리지 않고 fail-closed 로 막는다.

    "이미 승격됐는지" 판단은 **manifest 를 맹신하지 않는다**(6차 리뷰
    HIGH-2 회귀 방지): promote_partial() 의 파일명은 (label, content_hash)
    로 완전히 결정적이므로, manifest 없이도 그 정확한 경로가 디스크에 실제
    존재하는지 직접 확인할 수 있다. manifest 는 "정상 최종 저장(save_results,
    타임스탬프 파일명)으로 이미 처리된 내용"을 가리키는 참고용 캐시일
    뿐이며, 이 경우도 참조된 파일이 실제로 존재하는지 검증한 뒤에만
    신뢰한다. 이 설계 덕분에 manifest 가 손상되거나 사라져도(6차 리뷰
    HIGH-3) 최악의 경우 promote_partial() 로 중복 파일 하나가 다시 생길 뿐
    데이터가 유실되지는 않는다 — manifest 로드 실패를 fail-closed 로 막을
    필요가 없다.

    승격 순서 (재시도해도 결과 파일이 중복 생성되지 않고, ID 커밋 실패 시
    체크포인트가 사라지지 않도록):
        1. (이미 승격된 파일이 없다면) promote_partial() 로 먼저 durable
           저장한다. 저장 자체가 실패하면 체크포인트를 그대로 보존하고
           실패 라벨로 기록한다(fail-closed).
        2. 이 (해시 → 경로) 를 manifest(Storage.mark_promoted)에 기록한다.
        3. collected_ids 를 갱신하고 저장한다 — **이 저장이 성공한 경우에만**
           다음 단계로 진행한다(6차 리뷰 HIGH-1 회귀 방지: 예전에는 ID
           커밋 성공 여부와 무관하게 체크포인트를 지웠다. 그러면 ID 저장이
           실패해도 체크포인트가 사라져, 재시작 후 collected_ids.json 에는
           없고 체크포인트도 없어 다음 조사가 그 goodscode 를 "신규"로
           오판했다).
        4. ID 커밋이 성공했을 때만 체크포인트 파일 삭제를 시도한다
           (clear_partial 의 반환값을 반드시 확인한다). ID 커밋이 실패하면
           체크포인트를 그대로 남겨둔다 — 다음 재시도가 같은 체크포인트를
           발견해도 1단계에서 "이미 승격된 파일이 존재함"을 확인하고
           재승격 없이 곧장 3단계(ID 커밋)부터 다시 시도하므로 안전하다.
    """
    result = ReconcileResult()
    manifest = storage.load_promotion_manifest()

    for label, _path in storage.find_leftover_partials():
        status, leftover = storage.load_partial_results_status(label)

        if status == LoadStatus.QUARANTINE_FAILED:
            # 손상 감지 + 격리(백업)도 실패 — 원본이 위험한 상태로 그대로
            # 남아있다. 절대 자동으로 지우거나 빈 것으로 취급하지 않는다.
            result.quarantine_failed_labels.append(label)
            on_log(
                f"[체크포인트 승격] {label}: 체크포인트가 손상됐고 격리(백업)"
                f"에도 실패해 원본이 위험한 상태로 남아 있습니다. 자동으로 "
                f"건드리지 않으니 저장 경로를 직접 확인해 수동으로 백업하세요."
            )
            continue
        if status == LoadStatus.QUARANTINED:
            # 손상 감지 + 격리(백업) 성공 — 데이터는 *.corrupt_*.bak 으로
            # 안전하게 보존됐지만 자동으로 정상 결과로 복구되지는 않았다.
            # 조용히 넘어가면 사용자가 이 사실을 영영 모를 수 있으므로
            # 명시적으로 알리고 fail-closed 로 막는다(4차 리뷰 아키텍처 BLOCK-2).
            result.corrupt_labels.append(label)
            on_log(
                f"[체크포인트 승격] {label}: 체크포인트가 손상되어 자동 복구할 "
                f"수 없습니다. 원본은 백업 파일(*.corrupt_*.bak)로 보존됐으니 "
                f"내용을 확인한 뒤 다시 시도하세요."
            )
            continue
        if status == LoadStatus.MISSING:
            continue  # 이미 다른 경로로 정리됨(정상) — find 와 load 사이 경쟁 등
        if status == LoadStatus.EMPTY:
            if not storage.clear_partial(label):
                result.cleanup_failed_labels.append(label)
                on_log(f"[체크포인트 승격] {label}: 빈 체크포인트 삭제 실패(무시).")
            continue

        # status == LoadStatus.VALID
        codes = _extract_codes(leftover)
        content_hash = _checkpoint_content_hash(leftover)

        # 1) promote_partial() 자신의 결정적 경로가 이미 디스크에 존재하는지
        #    직접 확인한다(manifest 불필요, 파일시스템이 진실의 근거).
        rec_json, rec_csv = storage.promoted_partial_path(label, content_hash)
        already_saved = rec_json.exists() and rec_csv.exists()

        if not already_saved:
            # 2) manifest 에서 "정상 저장으로 이미 처리됨" 힌트를 찾고, 참조된
            #    파일이 실제로 아직 존재하는지 검증한 뒤에만 신뢰한다.
            entry = manifest.get(content_hash)
            if entry:
                ej, ec = Path(entry["json"]), Path(entry["csv"])
                already_saved = ej.exists() and ec.exists()

        if already_saved:
            on_log(f"[체크포인트 승격] {label}: 이미 승격된 파일이 확인되어 재승격 없이 정리만 시도합니다.")
        else:
            on_log(
                f"[체크포인트 승격] {label}: 이전 실행의 미종결 결과 "
                f"{len(leftover)}건을 최종 파일로 승격합니다."
            )
            try:
                storage.promote_partial(leftover, label, content_hash)
            except Exception as e:  # noqa: BLE001 - 승격 실패는 fail-closed 대상
                result.failed_labels.append(label)
                on_log(f"[체크포인트 승격] {label} 결과 저장 실패: {e}")
                continue
            try:
                storage.mark_promoted(content_hash, rec_json, rec_csv)
                manifest[content_hash] = {"json": str(rec_json), "csv": str(rec_csv)}
            except Exception as e:  # noqa: BLE001
                on_log(f"[체크포인트 승격] {label}: manifest 기록 실패(무시, 데이터는 안전): {e}")

        collected_ids.update(codes)
        result.promoted_ids.update(codes)
        try:
            storage.save_collected_ids(collected_ids)
        except Exception as e:  # noqa: BLE001
            result.commit_failed_labels.append(label)
            on_log(
                f"[체크포인트 승격] {label} ID 커밋 실패 — 체크포인트를 보존합니다"
                f"(다음 실행에서 재시도): {e}"
            )
            continue  # ID 커밋이 실패하면 체크포인트를 지우지 않는다(6차 리뷰 HIGH-1).

        # 결과 저장 + manifest 기록 + ID 커밋이 모두 끝난 뒤에만 체크포인트를
        # 정리한다 — ID 커밋이 실패한 경우 위 continue 로 여기 도달하지 않는다.
        if not storage.clear_partial(label):
            result.cleanup_failed_labels.append(label)
            on_log(
                f"[체크포인트 승격] {label}: 결과/ID 는 이미 안전하게 저장됐지만 "
                f"체크포인트 삭제에 실패했습니다(다음 실행에서 중복 없이 정리만 "
                f"재시도됩니다)."
            )

    return result


# 판매자 페이지 라벨 → 레코드 필드 매핑 (WORK_ORDER §5.3)
LABEL_MAP = {
    "seller": "company_name",
    "representative": "phone",
    "e-mail": "email",
    "business registration no": "business_number",
    "address": "address",
}

# fetch_seller_info 결과 판정
OUTCOME_OK = "ok"       # 성공 (레코드 존재)
OUTCOME_MISS = "miss"   # 200 이지만 유효 데이터 없음
OUTCOME_SKIP = "skip"   # 302 등 유효하지 않은 goodscode
OUTCOME_FAIL = "fail"   # 재시도 후에도 네트워크/서버 오류


@dataclass
class FetchResult:
    outcome: str
    record: dict | None = None


@dataclass
class CrawlStats:
    total: int = 0
    success: int = 0
    miss: int = 0
    skip: int = 0
    fail: int = 0

    @property
    def failed(self) -> int:
        """성공을 제외한 비수집(미스/스킵/실패) 합계."""
        return self.miss + self.skip + self.fail


@dataclass
class CrawlSummary:
    total_success: int = 0
    total_failed: int = 0
    per_category: dict[str, CrawlStats] = field(default_factory=dict)
    all_records: list[dict] = field(default_factory=list)
    files: list[tuple[Path, Path]] = field(default_factory=list)
    all_files: tuple[Path, Path] | None = None
    cancelled: bool = False
    # 예상치 못한 오류로 중단된 경우 오류 메시지/카테고리 (None 이면 정상/취소 종료).
    # 이 값이 설정되면 UI 는 "완료"가 아니라 명확한 실패로 표시해야 한다.
    error: str | None = None
    failed_category: str | None = None
    # True 면 체크포인트 복구가 collected_ids 를 바꿨는데 전달받은 CrawlPlan 이
    # 그 변경을 반영하지 못한 낡은 계획이라 실행하지 않았다는 뜻이다(4차 리뷰
    # 아키텍처 BLOCK-1). 호출자는 collected_ids 를 다시 읽어 계획을 새로
    # 만든 뒤 재시도해야 한다. UI(main_window.on_start)는 계획 생성 전에
    # 이미 reconcile 을 실행하므로 정상 경로에서는 절대 True 가 되지 않는다.
    replan_required: bool = False


class SellerCrawler:
    """Phase 2 판매자정보 수집 엔진 — CrawlPlan 을 그대로 실행한다."""

    def __init__(
        self,
        storage: Storage,
        control: Control | None = None,
        on_log: LogFn = _noop,
        on_category: CategoryFn = _noop,
        on_item: ItemFn = _noop,
        on_collected: CollectedFn = _noop,
        on_error: ErrorFn = _noop,
        delay: float = config.DEFAULT_DELAY,
    ) -> None:
        self.storage = storage
        self.control = control or Control()
        self.on_log = on_log
        self.on_category = on_category
        self.on_item = on_item
        self.on_collected = on_collected
        self.on_error = on_error
        self.delay = delay
        self._session = requests.Session()
        self._session.headers.update(config.HEADERS)

    # ── Phase 2: 단일 건 ───────────────────────────────────────────
    def fetch_seller_info(self, goodscode: str) -> FetchResult:
        """mg.gmarket.co.kr 판매자정보 GET → 파싱. 네트워크 오류 시 최대 3회 재시도."""
        url = config.SELLER_INFO_URL.format(goodscode)

        last_err: Exception | None = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                r = self._session.get(
                    url, timeout=config.HTTP_TIMEOUT, allow_redirects=False
                )
            except requests.RequestException as e:
                last_err = e
                if attempt < config.MAX_RETRIES:
                    self.on_log(f"    [재시도 {attempt}/{config.MAX_RETRIES}] {goodscode}: {e}")
                    time.sleep(0.5 * attempt)
                    continue
                self.on_error(f"{goodscode}: 네트워크 오류 - {e}")
                return FetchResult(OUTCOME_FAIL)

            # 302: 유효하지 않은 goodscode → 건너뜀
            if r.status_code in (301, 302, 303, 307, 308):
                return FetchResult(OUTCOME_SKIP)
            if r.status_code != 200:
                self.on_error(f"{goodscode}: HTTP {r.status_code}")
                return FetchResult(OUTCOME_FAIL)

            record = self._parse(goodscode, r.text)
            if record is None:
                return FetchResult(OUTCOME_MISS)
            return FetchResult(OUTCOME_OK, record.to_dict())

        # 도달 불가지만 방어
        if last_err:
            return FetchResult(OUTCOME_FAIL)
        return FetchResult(OUTCOME_MISS)

    @staticmethod
    def _parse(goodscode: str, html: str) -> SellerRecord | None:
        soup = BeautifulSoup(html, "html.parser")
        record = SellerRecord(
            goodscode=goodscode,
            url=config.ITEM_URL.format(goodscode),
            # 의도적으로 naive local datetime — CSV/JSON 출력의 collected_at
            # 형식을 유지한다(tzinfo 를 추가하면 isoformat() 에 +09:00 같은
            # 오프셋이 붙어 기존 사용자가 내보낸 파일과 형식이 달라진다).
            collected_at=datetime.now().isoformat(),  # noqa: DTZ005
        )

        store_el = soup.select_one("h3.store-title")
        if store_el:
            record.store_name = store_el.get_text(strip=True)

        for row in soup.select("ul.vip-seller-info-list li.list-row"):
            label_el = row.select_one("span.list-label")
            value_el = row.select_one("span.list-selection")
            if not label_el or not value_el:
                continue
            label_text = label_el.get_text(strip=True).lower()
            value_text = value_el.get_text(strip=True)
            if not value_text:
                continue
            for key, field_name in LABEL_MAP.items():
                if key in label_text:
                    setattr(record, field_name, value_text)
                    break

        return record if record.is_valid() else None

    # ── 내구성 커밋 헬퍼 (HIGH-1) ────────────────────────────────────
    def _commit_checkpoint(
        self,
        records: list[dict],
        collected_ids: set[str],
        newly_collected: set[str],
        label: str,
    ) -> None:
        """결과를 먼저 durable 저장한 뒤에만 ID 를 커밋한다.

        save_partial_results 가 실패하면 예외를 그대로 전파하며, 그 경우
        collected_ids(공유 집합) 는 이 호출 이전 상태 그대로 남는다 — 실패한
        체크포인트에 대응하는 ID 가 절대 먼저 커밋되지 않도록 보장한다.
        """
        if records:
            self.storage.save_partial_results(records, label)
        collected_ids.update(newly_collected)
        self.storage.save_collected_ids(collected_ids)

    # ── 카테고리 1개 수집 ──────────────────────────────────────────
    def crawl_category(
        self, cat_plan: CategoryPlan, collected_ids: set[str]
    ) -> tuple[list[dict], CrawlStats, bool, str | None]:
        """확정된 CategoryPlan.target_codes 를 그대로 실행한다(재필터링하지 않음).

        (records, stats, cancelled, error) 반환. 취소/오류 발생 시에도 그때까지
        수집한 records/stats 는 보존하여 반환한다(부분 결과 유실 방지, WORK_ORDER §7.5).
        """
        stats = CrawlStats()
        records: list[dict] = []
        newly_collected: set[str] = set()
        cancelled = False
        error: str | None = None

        targets = list(cat_plan.target_codes)
        stats.total = len(targets)

        if not targets:
            return records, stats, cancelled, error

        self.on_log(f"[{cat_plan.category_name}] 계획된 {len(targets)}건 수집 시작...")

        try:
            for i, code in enumerate(targets, start=1):
                self.control.checkpoint()  # 건 경계 안전 중단/일시정지
                self.on_item(code, i, len(targets))

                result = self.fetch_seller_info(code)

                if result.outcome == OUTCOME_OK and result.record is not None:
                    result.record["source"] = cat_plan.category_name
                    records.append(result.record)
                    newly_collected.add(code)  # 로컬에만 반영 — 아직 durable 아님
                    stats.success += 1
                    filled = sum(1 for f in CORE_CONTENT_FIELDS if result.record.get(f))
                    self._safe_callback(self.on_collected, result.record)
                    self.on_log(
                        f"  [{cat_plan.category_name}] {code} -> OK ({filled}/6) "
                        f"{result.record.get('store_name', '')}"
                    )
                elif result.outcome == OUTCOME_SKIP:
                    stats.skip += 1
                    self.on_log(f"  [{cat_plan.category_name}] {code} -> 건너뜀(302)")
                elif result.outcome == OUTCOME_FAIL:
                    stats.fail += 1
                    self.on_log(f"  [{cat_plan.category_name}] {code} -> 실패")
                else:
                    stats.miss += 1
                    self.on_log(f"  [{cat_plan.category_name}] {code} -> MISS")

                # 50건마다 중간 저장 (§5.6). 결과를 먼저 durable 저장한 뒤에만 ID 커밋.
                if i % config.SAVE_INTERVAL == 0:
                    self._commit_checkpoint(records, collected_ids, newly_collected, cat_plan.category_name)
                    self.on_log(f"  --- 중간 저장: 누적 성공 {stats.success}건 ---")

                # 요청 간격: 0.5초 + random(0~0.3초)
                time.sleep(self.delay + random.uniform(0, config.DELAY_JITTER))
        except CancelledError:
            # 취소되어도 지금까지의 records/stats 는 그대로 보존하여 반환한다.
            cancelled = True
        except Exception as e:  # noqa: BLE001 - 예상치 못한 오류도 부분 결과를 보존
            error = f"{type(e).__name__}: {e}"
            self.on_log(f"[{cat_plan.category_name}] 처리 중 예상치 못한 오류로 중단: {type(e).__name__}: {e}")
            self.on_log(traceback.format_exc())
            self.on_error(f"{cat_plan.category_name}: {e}")
        finally:
            # 항상 결과를 먼저 durable 저장한 뒤에만 ID 를 커밋한다.
            # 이 커밋 자체가 실패하면(예: 디스크 오류) collected_ids 는 손대지
            # 않은 채 오류로 기록한다 — ID 없는 결과 유실도, 결과 없는 ID
            # 오커밋도 발생시키지 않는다.
            try:
                self._commit_checkpoint(records, collected_ids, newly_collected, cat_plan.category_name)
            except Exception as e:  # noqa: BLE001
                msg = f"체크포인트 저장 실패: {e}"
                error = f"{error} | {msg}" if error else msg
                self.on_log(f"[{cat_plan.category_name}] {msg}")

        return records, stats, cancelled, error

    def _safe_callback(self, fn, *args) -> None:
        """UI/콜백 측 예외가 수집 루프 전체를 중단시키지 않도록 격리."""
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001
            self.on_log(f"  [경고] 콜백 처리 중 오류(무시하고 계속): {e}")

    # ── 전체 수집 (Phase 2 + 저장) ─────────────────────────────────
    def crawl(self, plan: CrawlPlan) -> CrawlSummary:
        """확정된 CrawlPlan 을 그대로 실행 + 저장. CrawlSummary 반환.

        취소 시 지금까지 수집분을 저장하고 summary.cancelled=True 로 반환한다.
        세션( self._session ) 정리는 최외곽 finally 에서 실행 경로와 무관하게
        항상 수행한다 — 3차 리뷰 MEDIUM 회귀 방지(예외 처리 코드 자체가 실패해
        중간에 함수를 벗어나도 세션이 새어나가지 않는다).
        """
        summary = CrawlSummary()
        try:
            # CrawlPlan 이 가리키는 출력 경로와 실제 Storage 가 쓰는 경로가
            # 일치하는지 확인한다(4차 리뷰 MEDIUM) — 둘이 어긋나면 감사용
            # plan_hash/plan.output_dir 는 A 를 가리키는데 실제 파일은 B 에
            # 조용히 저장되는 위험한 불일치가 생긴다. 프로그래밍 실수를 조기에
            # 잡기 위해 하드 실패시킨다. plan.categories 가 비어 있어도 검사한다
            # (5차 리뷰 MEDIUM) — 빈 계획이라고 봐주면, 다른 Storage 경로의
            # 체크포인트가 이 plan.output_dir 확인 없이 조용히 승격될 수 있다.
            plan_dir = Path(plan.output_dir).resolve()
            storage_dir = self.storage.output_dir.resolve()
            if plan_dir != storage_dir:
                summary.error = (
                    f"CrawlPlan.output_dir({plan_dir})가 실제 저장 경로"
                    f"({storage_dir})와 일치하지 않아 실행을 중단합니다."
                )
                self.on_log(f"[수집] {summary.error}")
                return summary

            # collected_ids.json 은 중복 방지의 단일 진실 공급원이다. 격리
            # (백업)조차 실패해 원본이 위험한 상태로 남아있는데(QUARANTINE_FAILED)
            # 그냥 빈 집합으로 계속 진행하면, ①대량 재수집이 발생하고 ②이후
            # save_collected_ids() 가 백업 없는 원본을 조용히 덮어써 영구히
            # 잃는다(6차 리뷰 HIGH-4). 이 상태에서는 아예 시작하지 않는다.
            ids_status, collected_ids = self.storage.load_collected_ids_status()
            if ids_status == LoadStatus.QUARANTINE_FAILED:
                summary.error = (
                    "collected_ids.json 이 손상됐고 격리(백업)에도 실패해 원본이 "
                    "위험한 상태로 남아 있어 실행을 중단합니다. 저장 경로를 "
                    "직접 확인해 수동으로 백업하세요."
                )
                self.on_log(f"[수집] {summary.error}")
                return summary

            # 계획 실행 이전에 무조건 잔여 체크포인트를 승격한다(HIGH-3).
            # fail-closed: 승격(결과 저장) 실패 또는 손상 체크포인트가 있으면
            # 어떤 카테고리도 실행하지 않는다 — 그렇지 않으면 실패한 카테고리를
            # 이어서 새로 수집할 때 고정 경로의 체크포인트가 미승격 데이터를
            # 덮어써 영구 유실된다(3차 리뷰 HIGH-1). main_window.on_start() 가
            # CrawlPlan 확정 이전에 이미 이 함수를 호출하므로 정상 경로에서는
            # 여기서 다시 실행해도 대개 아무것도 발견되지 않는다 — 이 호출은
            # crawler.crawl() 을 UI 를 거치지 않고 직접 쓰는 경우를 위한
            # 방어선이다.
            result = reconcile_leftover_checkpoints(self.storage, collected_ids, self.on_log)
            if result.blocking:
                problems = []
                if result.failed_labels:
                    problems.append("저장 실패: " + ", ".join(result.failed_labels))
                if result.corrupt_labels:
                    problems.append(
                        "손상되어 격리된 체크포인트(백업에서 수동 확인 필요): "
                        + ", ".join(result.corrupt_labels)
                    )
                if result.quarantine_failed_labels:
                    problems.append(
                        "손상됐지만 격리(백업)에도 실패해 원본이 위험한 체크포인트: "
                        + ", ".join(result.quarantine_failed_labels)
                    )
                summary.error = "체크포인트 문제로 신규 수집을 시작하지 않았습니다 — " + " / ".join(problems)
                self.on_log(f"[수집] {summary.error}")
                return summary

            # UI 를 거치지 않고 미리 만들어둔 plan 을 직접 crawl() 에 넘기는
            # 호출자를 위한 방어선(4차 리뷰 아키텍처 BLOCK-1): reconcile 이
            # collected_ids 를 바꿨는데 그 계획이 이미 커밋된 코드를 여전히
            # target_codes 로 담고 있다면, 그대로 실행 시 같은 goodscode 가
            # 두 번(승격분 + 신규 재수집분) 저장된다. main_window.on_start() 는
            # 계획을 만들기 전에 이미 reconcile 을 실행하므로 정상 UI 경로에서는
            # 이 조건이 절대 발생하지 않는다.
            planned_codes = {c for cat in plan.categories for c in cat.target_codes}
            stale_codes = planned_codes & collected_ids
            if stale_codes:
                summary.replan_required = True
                preview = ", ".join(sorted(stale_codes)[:10])
                more = "..." if len(stale_codes) > 10 else ""
                summary.error = (
                    "체크포인트 복구로 이미 수집된 코드가 계획에 포함되어 있어 "
                    f"이 계획을 실행할 수 없습니다(재계획 필요): {preview}{more}"
                )
                self.on_log(f"[수집] {summary.error}")
                return summary

            total_cats = len(plan.categories)
            if total_cats == 0:
                self.on_log("[수집] 계획에 포함된 수집 대상이 없습니다.")
                return summary

            return self._run_plan(plan, collected_ids, summary)
        finally:
            self.close()

    def _run_plan(
        self, plan: CrawlPlan, collected_ids: set[str], summary: CrawlSummary
    ) -> CrawlSummary:
        total_cats = len(plan.categories)
        try:
            for c_idx, cat_plan in enumerate(plan.categories, start=1):
                self.control.checkpoint()
                self.on_category(cat_plan.category_name, c_idx, total_cats)
                self.on_log(f"\n{'='*50}\n[{c_idx}/{total_cats}] {cat_plan.category_name}\n{'='*50}")

                records, stats, cancelled, cat_error = self.crawl_category(cat_plan, collected_ids)

                # 취소/오류 여부와 무관하게 부분 결과까지 항상 누적/저장 (유실 방지)
                summary.per_category[cat_plan.category_name] = stats
                summary.total_success += stats.success
                summary.total_failed += stats.failed
                summary.all_records.extend(records)

                # 카테고리 결과 최종 저장 (§9.1). 성공하면 체크포인트를 정리하고
                # 이 레코드들의 ID 를 확실히 커밋한다(크래시로 인해 중간 체크포인트
                # 커밋이 누락됐더라도, 최종 저장 성공은 그 자체로 durable 하므로
                # 여기서 한 번 더 확정 커밋한다 — 멱등이라 안전하다).
                save_error: str | None = None
                if records:
                    try:
                        paths = self.storage.save_results(records, label=cat_plan.category_name)
                        summary.files.append(paths)

                        # 이 저장 내용을 승격-완료 manifest 에 기록한다(5차/6차
                        # 리뷰 HIGH-2/3 회귀 방지) — 이 카테고리의 체크포인트가
                        # 아래에서 삭제에 실패해 다음 실행까지 남더라도, reconcile
                        # 이 이 경로가 실제로 존재함을 확인하면 promote_partial 로
                        # 별도의 _recovered_hash 파일을 또 만들지 않고 정리만
                        # 시도한다 — 정상 저장(타임스탬프 파일명)과 체크포인트
                        # 승격(결정적 파일명)이 서로 다른 이름 체계를 쓰기 때문에
                        # 이 manifest 없이는 서로의 존재를 알 수 없었다.
                        try:
                            self.storage.mark_promoted(
                                _checkpoint_content_hash(records), paths[0], paths[1]
                            )
                        except Exception as e:  # noqa: BLE001
                            self.on_log(f"[{cat_plan.category_name}] manifest 기록 실패(무시): {e}")

                        for rec in records:
                            code = rec.get("goodscode")
                            if code:
                                collected_ids.add(code)
                        # ID 커밋이 실패하면 예외가 그대로 밖의 except 로 전파되어
                        # 아래 clear_partial() 에 도달하지 않는다 — 체크포인트가
                        # 보존된다(6차 리뷰 HIGH-1: ID 커밋 실패에도 체크포인트가
                        # 지워지면 재시작 후 collected_ids.json 에도, 체크포인트
                        # 에도 없는 상태가 되어 다음 조사가 "신규"로 오판한다).
                        self.storage.save_collected_ids(collected_ids)

                        # ID 커밋까지 끝난 뒤 마지막으로 체크포인트를 정리한다
                        # (5차 리뷰 MEDIUM 순서 변경 — 예전에는 저장 직후 바로
                        # 지웠는데, manifest 덕분에 이제는 늦게 지워도 중복 승격
                        # 위험이 없으므로 ID 커밋을 먼저 끝내는 편이 더 안전하다).
                        if not self.storage.clear_partial(cat_plan.category_name):
                            self.on_log(
                                f"[{cat_plan.category_name}] 체크포인트 삭제 실패"
                                f"(결과/ID 는 이미 안전하게 저장됨, 다음 실행에서 "
                                f"중복 없이 자동 정리)."
                            )
                    except Exception as e:  # noqa: BLE001
                        save_error = f"최종 결과 저장 실패: {e}"
                        self.on_log(f"[{cat_plan.category_name}] {save_error} — 체크포인트를 보존합니다.")

                combined_error = cat_error
                if save_error:
                    combined_error = f"{combined_error} | {save_error}" if combined_error else save_error

                # 완료 처리(§8.2)는 정상 완료 + 최종 저장 성공 + max_items 로
                # 잘리지 않은 카테고리에만. 그 외에는 미완료로 남겨 재개 시
                # 다시 다뤄지게 한다(collected_ids 로 중복 수집은 여전히 방지됨).
                if combined_error is not None:
                    summary.error = combined_error
                    summary.failed_category = cat_plan.category_name
                    self.on_log(
                        f"[수집] {cat_plan.category_name} 처리 중 오류로 전체 수집을 중단합니다."
                    )
                    break

                if cancelled:
                    summary.cancelled = True
                    self.on_log("[수집] 사용자 취소 — 중간 저장 후 종료합니다.")
                    break

                if cat_plan.capped:
                    self.on_log(
                        f"  [{cat_plan.category_name}] max_items 제한으로 일부만 수집 — "
                        f"'완료' 처리하지 않습니다(다음 실행에서 이어서 수집 가능)."
                    )
                else:
                    self.storage.mark_completed(cat_plan.category_name, stats.success)

                self.on_log(
                    f"  [결과] {cat_plan.category_name}: 성공 {stats.success} / "
                    f"미스 {stats.miss} / 스킵 {stats.skip} / 실패 {stats.fail}"
                )

        except CancelledError:
            # 카테고리 경계(다음 카테고리 시작 전)에서 취소된 경우
            summary.cancelled = True
            self.on_log("[수집] 사용자 취소 — 중간 저장 후 종료합니다.")
            self._safe_save_collected_ids(collected_ids)
        except Exception as e:  # noqa: BLE001 - 카테고리 경계 밖의 예상치 못한 오류
            summary.error = f"{type(e).__name__}: {e}"
            self.on_log(f"[수집] 예상치 못한 오류로 전체 수집을 중단합니다: {type(e).__name__}: {e}")
            self.on_log(traceback.format_exc())
            self.on_error(str(e))
            self._safe_save_collected_ids(collected_ids)

        # 전체 통합 파일 저장 (§9.2). 이 저장 자체가 실패해도 summary 는
        # 반드시 반환해야 한다(그래야 워커/UI 가 "완료 0건"으로 오표시하지 않음).
        if summary.all_records:
            try:
                summary.all_files = self.storage.save_results(summary.all_records, label=config.ALL_LABEL)
            except Exception as e:  # noqa: BLE001
                msg = f"전체 통합 파일 저장 실패: {e}"
                summary.error = f"{summary.error} | {msg}" if summary.error else msg
                self.on_log(f"[수집] {msg}")

        return summary

    def _safe_save_collected_ids(self, collected_ids: set[str]) -> None:
        """collected_ids 저장 실패가 crawl() 전체를 벗어나 세션 정리(close)를
        건너뛰게 만들지 않도록 예외를 흡수한다(3차 리뷰 MEDIUM 회귀 방지).
        실패해도 데이터(레코드)는 이미 다른 경로로 저장되어 있으므로 최악의
        경우 해당 ID 들이 다음 조사에서 신규로 재수집될 뿐이다."""
        try:
            self.storage.save_collected_ids(collected_ids)
        except Exception as e:  # noqa: BLE001
            self.on_log(f"[수집] collected_ids 저장 실패(다음 조사에서 재수집될 수 있음): {e}")

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001, S110 - 세션 정리 실패는 조용히 넘어가도 안전하다
            pass
