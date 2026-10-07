"""실제 앱 UI 를 사람처럼 조작하는 7-병렬 분할 수집 드라이버 (Windows 실행용).

`python -m app.main` 이 하는 것과 동일하게 QApplication+MainWindow 를
실제 디스플레이에 띄우고, 사용자가 하는 것과 같은 위젯 조작으로

  Coupang 카테고리 탭 → (대상이 없으면) 카테고리 새로고침 → 카테고리 선택
  → 배분 방식 '단일 카테고리 분할' → 인스턴스 7 → 수집 시작
  → 모니터링(스크린샷·상태 덤프) → 정지 → 최종 보고

를 수행한다. 버튼 .click()/콤보 setCurrentIndex/스핀 setValue/트리 setSelected
는 실제 마우스 조작과 동일한 Qt 시그널을 발생시킨다. 모달 대화상자는
'확인/OK'만 자동으로 눌러 준다(사용자가 확인을 누르는 것과 동일).

실행(Windows, 프로젝트 루트에서):
  python scripts\\prototypes\\ui_drive_7parallel.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton

# ── 설정 ────────────────────────────────────────────────────────────────
TARGET_CATEGORY = os.environ.get("UI7_CATEGORY", "채소")  # 수집 대상 카테고리 이름
TARGET_CATEGORY_ID = os.environ.get("UI7_CATEGORY_ID", "194432")  # URL id(linkCode)
# UI7_REFRESH=1 — 대상이 시드/캐시에 있어도 '카테고리 목록 새로고침'을 먼저
# 누른다. 2026-10-06 볼륨 인지 분할의 물량 소스(productCount)는 라이브
# category-list 응답에만 붙어 오므로, 새로 고친 트리에서 계획을 세우려면
# 필수다(없으면 라운드로빈 열화 호환으로 돈다).
FORCE_REFRESH = os.environ.get("UI7_REFRESH") == "1"
INSTANCE_COUNT = int(os.environ.get("UI7_INSTANCES", "7"))
# 첫 라운드(전 인스턴스 1세션씩) 관찰 예산 — 초과하면 정지 요청.
OBSERVE_SECONDS = int(os.environ.get("UI7_OBSERVE_SECONDS", "2400"))
# 정지 요청 후 진행 중 세션이 안전 경계에서 끝나기를 기다리는 최대 시간.
STOP_WAIT_SECONDS = int(os.environ.get("UI7_STOP_WAIT_SECONDS", "900"))

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
# UI7_OUTPUT_DIR 를 주면 같은 폴더로 재개(이어서 수집)한다.
OUTPUT_DIR = (
    Path(os.environ["UI7_OUTPUT_DIR"])
    if os.environ.get("UI7_OUTPUT_DIR")
    else PROJECT_ROOT / "output" / f"coupang_parallel_ui_7inst_win_{STAMP}"
)
# 무인 연속 운행 — 첫 라운드 관찰 뒤 멈추지 않고 계속 돈다(all_done/오류에만 종료).
CONTINUOUS = os.environ.get("UI7_CONTINUOUS") == "1"
# 재개 대화상자에서 '이어서 수집'을 자동으로 누른다(사용자 조작과 동일).
AUTO_RESUME = os.environ.get("UI7_RESUME") == "1"
# 모니터링 주기(초) — 연속 운행에서는 느리게.
STATUS_EVERY = int(os.environ.get("UI7_STATUS_SECONDS", "30"))
SHOT_EVERY = int(os.environ.get("UI7_SHOT_SECONDS", "150"))
SHOT_DIR = OUTPUT_DIR / "_ui_driver_shots"
REPORT_PATH = OUTPUT_DIR / "_ui_driver_report.json"

def _init_shot_counter() -> int:
    """기존 스크린샷 번호에 이어서 매긴다(같은 폴더 재개 시 덮어쓰기 방지)."""
    if not SHOT_DIR.exists():
        return 0
    numbers = []
    for path in SHOT_DIR.glob("*.png"):
        head = path.name.split("_", 1)[0]
        if head.isdigit():
            numbers.append(int(head))
    return max(numbers, default=0)


_state = {
    "phase": "boot",
    "start_clicked_at": None,
    "stop_clicked_at": None,
    "shots": _init_shot_counter(),
    "seen_status": {},      # instance_id -> set of seen status texts
    "dismissed_modals": [], # (title, text) 자동 확인 처리한 대화상자
}


def say(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def shot(window, tag: str) -> None:
    _state["shots"] += 1
    path = SHOT_DIR / f"{_state['shots']:02d}_{tag}.png"
    try:
        window.grab().save(str(path))
        say(f"스크린샷 저장: {path.name} ({tag})")
    except Exception as error:  # noqa: BLE001 - 스크린샷 실패는 치명 아님
        say(f"스크린샷 실패({tag}): {error}")


def find_category_item(tree, category_name: str, category_id: str = ""):
    """트리 전체(모든 깊이)에서 카테고리 항목을 찾는다.

    category_id 를 주면 id 로 먼저 찾는다(동명 노드·인코딩 무관하게
    확정적). 이름으로만 찾을 때는 정확히 일치하는 첫 항목. 찾은 항목의
    모든 조상을 펼쳐 눈에 보이게 만든 뒤 반환한다.
    """
    def walk(item):
        if category_id:
            if str(item.data(0, 0x0100) or "") == category_id:
                return item
        elif item.data(0, 0x0100) and item.text(0) == category_name:
            return item
        for index in range(item.childCount()):
            found = walk(item.child(index))
            if found is not None:
                return found
        return None

    for index in range(tree.topLevelItemCount()):
        found = walk(tree.topLevelItem(index))
        if found is not None:
            parent = found.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            return found
    return None


def card_states(panel) -> dict:
    """인스턴스 카드 현재 표시 상태."""
    states = {}
    for key, card in panel._instance_cards.items():
        if key == "header":
            continue
        states[key] = {
            "family": card.family_label.text(),
            "status": card.status_label.text(),
            "totals": card.totals_label.text(),
        }
    return states


def first_round_done(panel) -> bool:
    """전 인스턴스가 1세션 이상 마쳤는지 — 카드 상태 변화로 판정.

    첫 세션이 끝나면 상태가 '대기 [다음 HH:MM]'로 바뀌거나(정상 예약),
    완주/차단/오류로 종료 상태가 된다. 아직 한 번도 세션에 들어가지 않은
    인스턴스는 초기 문구 '대기'(괄호 없음) 그대로다.
    """
    states = card_states(panel)
    if len(states) < INSTANCE_COUNT:
        return False
    for info in states.values():
        text = info["status"]
        if text == "대기":
            return False
        if text == "수집중":
            return False
    return True


def load_state_file() -> dict | None:
    try:
        return json.loads(
            (OUTPUT_DIR / "coupang_parallel_state.json").read_text("utf-8")
        )
    except (OSError, ValueError):
        return None


class ModalResponder:
    """모달 대화상자 자동 응답 — '확인/OK' 계열 버튼만 누른다.

    재개 질문(이어서 수집/처음부터)은 자동으로 답하지 않는다 — 이 드라이버는
    항상 새 출력 폴더를 쓰므로 뜨지 않는다. 만약 뜨면 수동 판단을 위해
    그대로 둔다(드라이버 타임아웃으로 종료).
    """

    RESUME_MARKERS = ("이어서 수집", "처음부터", "재개")

    def __init__(self) -> None:
        self.timer = QTimer()
        self.timer.setInterval(700)
        self.timer.timeout.connect(self._respond)
        self.timer.start()

    def _respond(self) -> None:
        box = QApplication.activeModalWidget()
        if not isinstance(box, QMessageBox):
            return
        title = box.windowTitle()
        text = box.text()
        if title == "이어서 수집":
            # 재개 질문 — AUTO_RESUME 일 때만 '이어서 수집'을 누른다.
            # '처음부터 다시 수집'/'취소'는 절대 자동으로 누르지 않는다.
            if AUTO_RESUME:
                for button in box.buttons():
                    if button.text().strip() == "이어서 수집":
                        _state["dismissed_modals"].append(
                            {"title": title, "text": "자동 응답: 이어서 수집"}
                        )
                        say("재개 대화상자 자동 응답: [이어서 수집]")
                        button.click()
                        return
            return  # 자동 응답 금지 — 수동 판단 대기
        if any(marker in text for marker in self.RESUME_MARKERS):
            return  # 재개 관련 문구가 섞인 대화상자는 자동 응답 금지
        ok_button: QPushButton | None = None
        for button in box.buttons():
            label = button.text().strip()
            if label in ("OK", "확인", "네", "예", "예(Y)", "&OK", "&확인"):
                ok_button = button
                break
        if ok_button is None:
            return
        _state["dismissed_modals"].append({"title": title, "text": text[:300]})
        say(f"모달 자동 확인: [{title}] {text[:120]}")
        ok_button.click()


class Driver:
    """QTimer 상태 머신 — 사람의 조작 순서를 그대로 따라간다."""

    def __init__(self, app: QApplication, window) -> None:
        self.app = app
        self.window = window
        self.timer = QTimer()
        self.timer.setInterval(1_000)
        self.timer.timeout.connect(self._tick)
        self._wait_tree_deadline = 0.0
        self._last_shot_time = 0.0
        self._last_status_time = 0.0

    def panel(self):
        return self.window.coupang_parallel_panel

    def start(self) -> None:
        self.timer.start()
        say(f"출력 폴더: {OUTPUT_DIR}")
        say(f"스크린샷 폴더: {SHOT_DIR}")

    # ── 단계 ────────────────────────────────────────────────────────────

    def _phase_boot(self) -> None:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        shot(self.window, "boot")
        tab_index = self.window.tab_widget.indexOf(
            self.window.coupang_parallel_panel
        )
        say(f"'Coupang 카테고리' 탭(index {tab_index})으로 전환")
        self.window.tab_widget.setCurrentIndex(tab_index)
        if FORCE_REFRESH:
            say(
                "UI7_REFRESH=1 — 물량(productCount) 확보를 위해 "
                "'카테고리 목록 새로고침' 클릭(라이브 fetch)"
            )
            shot(self.window, "before_refresh")
            self._start_refresh()
        elif self.panel().category_tree.topLevelItemCount() > 0:
            say(
                "카테고리 트리 이미 로드됨(시작 시 캐시/기본 목록) — "
                f"{self.panel().cache_label.text()}"
            )
            _state["phase"] = "select"
        else:
            say("카테고리 트리 비어 있음 — '카테고리 목록 새로고침' 클릭")
            shot(self.window, "before_refresh")
            self._start_refresh()

    def _start_refresh(self) -> None:
        """사용자의 '카테고리 목록 새로고침' 클릭 — 라이브 fetch 대기로 전환."""
        self.panel().btn_refresh_categories.click()
        # 라이브 fetch는 약 30초 — 첫 실행 시 Camoufox 브라우저 다운로드가
        # 겹치면 수 분까지 걸릴 수 있어 여유 있게 잡는다.
        self._wait_tree_deadline = time.time() + 420
        _state["refresh_done"] = True
        _state["phase"] = "wait_tree"

    def _phase_wait_tree(self) -> None:
        panel = self.panel()
        # 새로고침 진행 여부 — 안내 문구 + 버튼 비활성(재입력 방지) 신호로 판정.
        # 로딩 중에는 구 트리 항목이 그대로 남아 있으므로 항목 수만으로는
        # 판정하지 않는다.
        loading = (
            "불러오는 중" in panel.cache_label.text()
            or not panel.btn_refresh_categories.isEnabled()
        )
        if not loading and panel.category_tree.topLevelItemCount() > 0:
            say(f"카테고리 로드 완료 — {panel.cache_label.text()}")
            shot(self.window, "tree_loaded")
            _state["phase"] = "select"
        elif time.time() > self._wait_tree_deadline:
            say("[중단] 카테고리 트리를 불러오지 못했습니다.")
            shot(self.window, "tree_timeout")
            self._finish()
        # else: 계속 대기 (모달 응답기가 오류 대화상자를 처리한다)

    def _phase_select(self) -> None:
        tree = self.panel().category_tree
        item = find_category_item(tree, TARGET_CATEGORY, TARGET_CATEGORY_ID)
        if item is None:
            if not _state.get("refresh_done"):
                # 캐시/시드 목록에 대상이 없다 — 사용자처럼 새로고침해서
                # 현재 쿠팡 트리를 받아온 뒤 다시 찾는다.
                say(
                    f"'{TARGET_CATEGORY}'(id={TARGET_CATEGORY_ID}) 항목이 "
                    "현재 목록에 없음 — '카테고리 목록 새로고침' 클릭(라이브 fetch)"
                )
                shot(self.window, "before_refresh")
                self._start_refresh()
                return
            say(
                f"[중단] 새로고침한 트리에서도 '{TARGET_CATEGORY}'"
                f"(id={TARGET_CATEGORY_ID}) 항목을 찾지 못했습니다."
            )
            shot(self.window, "category_not_found")
            self._finish()
            return
        tree.scrollToItem(item)
        found_id = str(item.data(0, 0x0100) or "")
        if found_id != TARGET_CATEGORY_ID:
            say(
                f"[중단] '{TARGET_CATEGORY}' 항목을 찾았지만 id가 다릅니다 — "
                f"기대 {TARGET_CATEGORY_ID}, 실제 {found_id}(동명 다른 노드)."
            )
            shot(self.window, "category_id_mismatch")
            self._finish()
            return
        tree.clearSelection()
        item.setSelected(True)
        say(f"카테고리 선택: {item.text(0)} (id={item.data(0, 0x0100)})")
        say(f"선택 라벨: {self.panel().selected_label.text()}")
        shot(self.window, "category_selected")
        _state["phase"] = "configure"

    def _phase_configure(self) -> None:
        panel = self.panel()
        panel.mode_combo.setCurrentIndex(1)  # 단일 카테고리 분할
        say(f"배분 방식: {panel.mode_combo.currentText()}")
        panel.spin_instances.setValue(INSTANCE_COUNT)
        say(f"인스턴스 수: {panel.spin_instances.value()}")
        say(f"선택 라벨: {panel.selected_label.text()}")
        if panel.decodo_hint_label.isVisible():
            say(f"[주의] Decodo 안내 표시 중: {panel.decodo_hint_label.text()}")
        panel.output_dir_edit.setText(str(OUTPUT_DIR))
        shot(self.window, "configured_7instances")
        _state["phase"] = "start"

    def _phase_start(self) -> None:
        say("'/수집 시작' 버튼 클릭")
        self.panel().btn_start.click()
        _state["start_clicked_at"] = time.time()
        _state["phase"] = "running"
        self._last_shot_time = time.time()
        shot(self.window, "started")

    def _phase_running(self) -> None:
        panel = self.panel()
        worker = self.window.parallel_worker
        now = time.time()
        # 카드 상태 추적
        for key, info in card_states(panel).items():
            seen = _state["seen_status"].setdefault(key, set())
            seen.add(info["status"])
        # 주기 상태 줄
        if now - self._last_status_time >= STATUS_EVERY:
            self._last_status_time = now
            for key in sorted(card_states(panel), key=int):
                info = card_states(panel)[key]
                say(
                    f"카드 {key}: {info['family']} | {info['status']} | {info['totals']}"
                )
            tail = panel.log_view.toPlainText().splitlines()[-3:]
            for line in tail:
                say(f"로그| {line}")
        # 주기 스크린샷
        if now - self._last_shot_time >= SHOT_EVERY:
            self._last_shot_time = now
            shot(self.window, "running")
        # 종료 조건
        if worker is None or not worker.isRunning():
            say("워커가 종료 상태가 됨(전체 종료/오류) — 최종 단계로.")
            _state["phase"] = "final"
            return
        if CONTINUOUS:
            return  # 무인 연속 운행 — 사용자 정지/완주까지만 계속 돈다
        if first_round_done(panel):
            say("전 인스턴스가 첫 세션을 마쳤습니다 — 정지 요청으로 전환.")
            shot(self.window, "first_round_done")
            self._request_stop()
            return
        if now - _state["start_clicked_at"] > OBSERVE_SECONDS:
            say(f"관찰 예산 {OBSERVE_SECONDS}초 초과 — 정지 요청으로 전환.")
            shot(self.window, "observe_budget")
            self._request_stop()
            return

    def _request_stop(self) -> None:
        if _state["stop_clicked_at"] is None:
            _state["stop_clicked_at"] = time.time()
            say("'정지' 버튼 클릭 — 진행 중 세션이 안전 경계에서 끝나기를 기다립니다.")
            self.panel().btn_stop.click()
            shot(self.window, "stop_requested")
        _state["phase"] = "stopping"

    def _phase_stopping(self) -> None:
        worker = self.window.parallel_worker
        if worker is None or not worker.isRunning():
            say("워커 종료 확인 — 정지 완료.")
            _state["phase"] = "final"
            return
        if time.time() - _state["stop_clicked_at"] > STOP_WAIT_SECONDS:
            say("[경고] 정지 대기 시간 초과 — 강제 종료하지 않고 보고만 남깁니다.")
            _state["phase"] = "final"
            return
        if (int(time.time() - _state["stop_clicked_at"]) % 60) == 0:
            say("정지 대기 중… (진행 중 세션 마무리 대기)")

    def _phase_final(self) -> None:
        shot(self.window, "final")
        state_file = load_state_file()
        report = {
            "stamp": STAMP,
            "category": TARGET_CATEGORY,
            "instance_count": INSTANCE_COUNT,
            "output_dir": str(OUTPUT_DIR),
            "cards": card_states(self.panel()),
            "dismissed_modals": _state["dismissed_modals"],
            "manager_state": state_file,
            "final_log_tail": self.panel().log_view.toPlainText().splitlines()[-80:],
        }
        REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        say(f"보고서 저장: {REPORT_PATH}")
        QTimer.singleShot(1500, self.app.quit)
        _state["phase"] = "done"

    def _finish(self) -> None:
        _state["phase"] = "final"

    # ── 루프 ────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        phase = _state["phase"]
        try:
            if phase == "boot":
                self._phase_boot()
            elif phase == "wait_tree":
                self._phase_wait_tree()
            elif phase == "select":
                self._phase_select()
            elif phase == "configure":
                self._phase_configure()
            elif phase == "start":
                self._phase_start()
            elif phase == "running":
                self._phase_running()
            elif phase == "stopping":
                self._phase_stopping()
            elif phase == "final":
                self._phase_final()
        except Exception as error:  # noqa: BLE001 - 드라이버 오류는 보고 후 종료
            say(f"[드라이버 오류] {type(error).__name__}: {error}")
            import traceback

            traceback.print_exc()
            _state["phase"] = "final"


def main() -> int:
    from app.core.applog import log_line, setup_file_logging

    setup_file_logging()
    log_line("=== UI 드라이버(7-병렬 분할 수집 시연) 시작 ===")

    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("판매자 정보 수집기")
    qss_path = PROJECT_ROOT / "app" / "ui" / "styles" / "theme.qss"
    try:
        qss = qss_path.read_text(encoding="utf-8")
        if qss:
            app.setStyleSheet(qss)
    except OSError:
        pass

    window = MainWindow()
    window.show()
    say("앱 메인 윈도우 표시 (실제 디스플레이)")

    responder = ModalResponder()  # noqa: F841 — QTimer 참조 유지
    driver = Driver(app, window)
    driver.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
