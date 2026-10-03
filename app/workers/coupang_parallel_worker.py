"""쿠팡 병렬 수집 QThread 워커 — 매니저 이벤트 ↔ Qt 시그널 브리지 (M3).

순수 코어 매니저(app.core.coupang.parallel_manager.ParallelCoupangManager)는
Qt 를 모르고 표준 이벤트 dict({"type", "instance", ...}) 만 내보낸다. 워커는

1. 매니저 생성 — run() 시작 시점에 만든다. 생성 중 이벤트(상태 복원·강등
   경고·배분 로그)도 시그널로 흘러야 하고 디스크 IO 를 GUI 스레드에서
   피하기 위해서다(시그널 연결은 start() 전 main_window 가 마친다).
2. 이벤트 → 시그널 변환(log/status/progress/exit_ip/blocked/complete/
   error/degraded).
3. 세션 스케줄 루프: control.checkpoint() → 매니저 all_done() 이면 종료 →
   다음 due 시각까지 control.sleep(초, poll 5초; due 즉시면 0) →
   run_due(time.time()).
4. 결과 표용 판매자 행 증분 방출 — 세션 요약마다 가족 폴더의
   top_sellers.csv 를 다시 읽어 새로 저장된(saved) 판매자만 보낸다.
5. 최종 요약 dict 방출(인스턴스별 상태·누적, 차단/오류 목록).

세션 실행은 순차다(매니저 run_due 가 due 인 인스턴스를 차례로 동기 실행).
엔진 세션이 짧고 인스턴스별 세션 간격(기본 80분)이 엇갈려 사실상 동시성이
확보되며, 한 시점에 브라우저가 1개만 열리므로 PC 자원·브라우저 프로필
경합을 피한다 — 설계 판단(스레드 병렬 대신 스케줄 interleaving).

정지: Control 가 엔진 전체에 침투한다. 세션 도중 취소하면 매니저가
cancelled 를 받아 대기로 복귀하고, 워커 루프의 checkpoint 가
CancelledError 를 던져 루프를 빠져나온다(종료 코드 의존 없음).
어떤 경로로 끝나도 finished_parallel 은 정확히 1회 emit 한다.
"""

from __future__ import annotations

import csv
import time
import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from app.core.base import CancelledError, Control
from app.core.coupang.parallel_manager import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_WAITING,
    ParallelCoupangManager,
    ParallelRunConfig,
)

# sellers_appended 행이 담는 키 — 결과 표(DISPLAY_COLUMNS 8열)와 동일한
# 계약이다. app.ui.category_panel.DISPLAY_COLUMNS 와 순서·키를 함께 유지한다.
SELLER_DISPLAY_FIELDS = (
    "vendor_id",
    "store_name",
    "company_name",
    "ceo_name",
    "business_number",
    "phone",
    "email",
    "power_seller",
)

# 판매자 저장소 파일명 — patchright_top_sellers.TopSellerStore 규약.
_SELLERS_FILENAME = "top_sellers.csv"
# 대기 폴링 주기(초) — 5초보다 급할 필요가 없다(세션 간격은 분 단위).
_SLEEP_POLL_SECONDS = 5.0


class CoupangParallelWorker(QThread):
    """병렬 인스턴스 수집 워커 — 매니저 소유·시그널 브리지·스케줄 루프."""

    log_message = pyqtSignal(str)
    # (인스턴스 id, 상태 문자, 요약 dict)
    instance_state_changed = pyqtSignal(str, str, dict)
    # 결과 표에 붙일 판매자 행 목록(키는 SELLER_DISPLAY_FIELDS)
    sellers_appended = pyqtSignal(list)
    warning_message = pyqtSignal(str)
    # 최종 요약 dict — main_window 가 결과 안내·잠금 해제에 쓴다
    finished_parallel = pyqtSignal(object)

    def __init__(
        self,
        run_config: ParallelRunConfig,
        control: Control,
        browser_scope_factory=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.run_config = run_config
        # Control 은 호출자(main_window) 소유 — 정지 요청은 caller 가
        # control.request_cancel() 로 지시하고 워커는 소유하지 않는다.
        self.control = control
        self.browser_scope_factory = browser_scope_factory
        # 매니저는 run() 에서 만든다(모듈 docstring 참조). 테스트는 이
        # 속성에 가짜 매니저를 넣어 루프를 검증한다.
        self.manager: ParallelCoupangManager | None = None
        # 가족별로 이미 결과 표에 방출한 판매자 id — 증분 방출용.
        self._emitted_sellers: dict[int, set[str]] = {}

    # ── 스케줄 루프 ─────────────────────────────────────────────────

    def run(self) -> None:
        cancelled = False
        error_text: str | None = None
        try:
            if self.manager is None:
                self.manager = ParallelCoupangManager(
                    self.run_config,
                    self.control,
                    on_event=self._on_manager_event,
                    browser_scope_factory=self.browser_scope_factory,
                )
            self.log_message.emit(
                f"병렬 수집 시작 — 가족 {len(self.run_config.families)}개, "
                f"인스턴스 {self.run_config.instance_count}개, "
                f"세션 간격 {self.run_config.interval_minutes}분"
            )
            # 재개 시작이면 이미 저장된 판매자를 결과 표에 먼저 띄운다.
            self._emit_initial_sellers()
            while True:
                self.control.checkpoint()
                if self.manager.all_done():
                    break
                wait_seconds = self._seconds_until_next_due(time.time())
                if wait_seconds > 0:
                    self.log_message.emit(
                        f"다음 세션까지 {wait_seconds / 60:.0f}분 대기합니다."
                    )
                    self.control.sleep(
                        wait_seconds, poll_interval=_SLEEP_POLL_SECONDS
                    )
                self.manager.run_due(time.time())
        except CancelledError:
            cancelled = True
            self.log_message.emit(
                "[제어] 정지 — 워커를 종료합니다. 같은 출력 폴더로 다시 시작하면 "
                "저장된 배분·누적부터 이어서 수집됩니다."
            )
        except Exception as error:  # noqa: BLE001 - 워커 최상위 예외 격리
            error_text = f"{type(error).__name__}: {error}"
            self.log_message.emit(traceback.format_exc())
            self.warning_message.emit(
                f"병렬 수집이 오류로 중단되었습니다: {error_text}"
            )
        finally:
            self.finished_parallel.emit(
                self._final_summary(cancelled=cancelled, error=error_text)
            )

    def _seconds_until_next_due(self, now: float) -> float:
        """대기 중인 인스턴스의 가장 빠른 다음 세션 시각까지 남은 초."""
        next_run_values = [
            state.next_run_at
            for state in self.manager.instances.values()
            if state.status == STATUS_WAITING
        ]
        if not next_run_values:
            return 0.0
        return max(0.0, min(next_run_values) - now)

    # ── 이벤트 → 시그널 브리지 ──────────────────────────────────────

    def _on_manager_event(self, event: dict) -> None:
        """매니저 표준 이벤트를 워커 시그널로 바꾼다(QThread 안에서 안전)."""
        event_type = str(event.get("type") or "")
        instance_id = str(event.get("instance") or "")

        if event_type == "log":
            self.log_message.emit(str(event.get("message") or ""))
        elif event_type == "status":
            summary = {
                "family_index": event.get("family_index"),
                "result": event.get("result") or "",
                "message": event.get("message") or "",
            }
            self.instance_state_changed.emit(
                instance_id, str(event.get("status") or STATUS_WAITING), summary
            )
            if event.get("message"):
                self.log_message.emit(str(event["message"]))
        elif event_type == "progress":
            self._on_progress_event(instance_id, event)
        elif event_type == "exit_ip":
            self._on_exit_ip_event(instance_id, event)
        elif event_type == "blocked":
            reason = str(event.get("reason") or "차단 신호를 확인했습니다.")
            self.warning_message.emit(
                f"인스턴스 {instance_id} 이(가) 차단으로 중단됩니다: {reason}"
                + (
                    f" (근거: {event['reference']})"
                    if event.get("reference")
                    else ""
                )
            )
            self.instance_state_changed.emit(
                instance_id,
                STATUS_BLOCKED,
                {
                    "family_index": event.get("family_index"),
                    "reason": reason,
                },
            )
        elif event_type == "complete":
            self._on_complete_event(instance_id, event)
        elif event_type == "error":
            reason = str(event.get("reason") or "원인 미상")
            self.warning_message.emit(
                f"인스턴스 {instance_id} 이(가) 오류로 정지합니다: {reason}"
            )
            self.instance_state_changed.emit(
                instance_id,
                STATUS_ERROR,
                {
                    "family_index": event.get("family_index"),
                    "reason": reason,
                },
            )
        elif event_type == "degraded":
            self.warning_message.emit(
                str(event.get("message") or "인스턴스 구성이 강등되었습니다.")
            )
        # all_done 등 나머지 — 스케줄 루프가 종료 조건으로 처리한다.

    def _on_progress_event(self, instance_id: str, event: dict) -> None:
        """진행 이벤트 — 세션 요약(result 있음)만 상태 갱신·판매자 조사."""
        if "result" not in event:
            # 목록 세션의 중간 스냅숏(runner 브리지) — 로그·표시 생략.
            return
        summary = {
            key: event.get(key)
            for key in (
                "family_index",
                "action",
                "result",
                "status",
                "next_run_at",
                "total_products",
                "total_sellers",
            )
        }
        self.instance_state_changed.emit(
            instance_id,
            str(event.get("status") or STATUS_WAITING),
            summary,
        )
        family_index = event.get("family_index")
        if isinstance(family_index, int):
            # 세션이 끝난 뒤 저장소에 새로 확보된 판매자를 표에 붙인다.
            self._emit_new_sellers(family_index)

    def _on_exit_ip_event(self, instance_id: str, event: dict) -> None:
        """Decodo 회선 점검 결과 — 성공은 로그, 실패는 뒤따르는 error 가 안내."""
        if not event.get("ok"):
            self.log_message.emit(
                f"인스턴스 {instance_id} 회선 점검 실패("
                + str(event.get("error_kind") or "원인 미상")
                + ") — 시도 sid: "
                + ", ".join(str(sid) for sid in (event.get("tried") or []))
            )
            return
        sid = str(event.get("proxy_session_id") or "")
        ip = str(event.get("ip") or "")
        if event.get("rotated"):
            self.log_message.emit(
                f"인스턴스 {instance_id} 스티키 세션이 교체되었습니다: "
                f"sid {sid} (IP {ip})"
            )
        else:
            self.log_message.emit(
                f"인스턴스 {instance_id} 회선 점검 통과: sid {sid} (IP {ip})"
            )

    def _on_complete_event(self, instance_id: str, event: dict) -> None:
        """가족 완주 — 최종 파일 안내 + 승계(waiting)/종료(done) 상태."""
        family_index = event.get("family_index")
        index_text = (
            f"가족 {family_index + 1}"
            if isinstance(family_index, int)
            else "가족"
        )
        finals = event.get("finals") or {}
        finals_text = ", ".join(str(path) for path in finals.values())
        self.log_message.emit(
            f"인스턴스 {instance_id} {index_text} 완주 — 최종 파일: {finals_text}"
        )
        if event.get("inherited_family") is not None:
            status = STATUS_WAITING
        else:
            status = STATUS_DONE
        self.instance_state_changed.emit(
            instance_id,
            status,
            {
                "family_index": event.get("inherited_family", family_index),
                "next_run_at": event.get("next_run_at", 0.0),
            },
        )

    # ── 판매자 행 증분 방출 ─────────────────────────────────────────

    def _emit_initial_sellers(self) -> None:
        """시작 시 저장소에 이미 확보된 판매자를 결과 표에 올린다(재개 화면)."""
        for family_index in range(len(self.run_config.families)):
            self._emit_new_sellers(family_index)

    def _emit_new_sellers(self, family_index: int) -> None:
        """가족 폴더의 top_sellers.csv 에서 아직 방출하지 않은 saved 행만 보낸다."""
        path = (
            self.run_config.output_dir
            / f"family_{family_index + 1:02d}"
            / _SELLERS_FILENAME
        )
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except OSError:
            return  # 아직 세션이 돌지 않은 가족 — 정상 상태
        emitted = self._emitted_sellers.setdefault(family_index, set())
        fresh: list[dict] = []
        for row in rows:
            if row.get("status") != "saved":
                continue
            vendor_id = str(row.get("vendor_id") or "")
            if not vendor_id or vendor_id in emitted:
                continue
            emitted.add(vendor_id)
            fresh.append(
                {key: str(row.get(key) or "") for key in SELLER_DISPLAY_FIELDS}
            )
        if fresh:
            self.sellers_appended.emit(fresh)

    # ── 최종 요약 ───────────────────────────────────────────────────

    def _final_summary(self, *, cancelled: bool, error: str | None) -> dict:
        """인스턴스별 최종 상태·누적을 모은 요약 dict — 종료 코드 의존 없음."""
        instances: list[dict] = []
        blocked: list[str] = []
        errors: list[str] = []
        total_products = 0
        total_sellers = 0
        for state in sorted(
            self.manager.instances.values() if self.manager else [],
            key=lambda state: str(state.config.instance_id),
        ):
            instance_id = str(state.config.instance_id)
            record = {
                "instance_id": instance_id,
                "name": str(state.config.name),
                "line": str(state.config.line),
                "status": str(state.status),
                "family_index": state.family_index,
                "next_run_at": state.next_run_at,
                "total_products": state.total_products,
                "total_sellers": state.total_sellers,
            }
            instances.append(record)
            if state.status == STATUS_BLOCKED:
                blocked.append(instance_id)
            elif state.status == STATUS_ERROR:
                errors.append(instance_id)
            total_products += int(state.total_products)
            total_sellers += int(state.total_sellers)
        return {
            "cancelled": cancelled,
            "error": error,
            "output_dir": str(self.run_config.output_dir),
            "family_count": len(self.run_config.families),
            "instance_count": self.run_config.instance_count,
            "instances": instances,
            "blocked_instances": blocked,
            "error_instances": errors,
            "total_products": total_products,
            "total_sellers": total_sellers,
        }
