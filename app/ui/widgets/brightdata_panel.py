"""Bright Data 계정 설정 탭 패널 — API 토큰·존 입력, 검증, 저장.

사용자가 자신의 Bright Data 계정 API 토큰을 입력하면 두 탭의 Bright Data
사용량(Web Unlocker 요청 과금 / ISP 프록시 대역폭 과금)이 그 계정 키에서
차감된다. 자격 증명은 저장소 밖 output/brightdata_settings.json 에만
저장된다(.gitignore) — 절대 저장소에 커밋되지 않는다.

검증 경로(2026-09-09 보강):
- API 토큰 — "잔액 조회·토큰 검증" 버튼 + 저장 버튼이 새 토큰을 먼저 검증
  (401/403 거부 시 확인 대화상자, 네트워크 오류는 미검증 명시 후 저장).
- ISP 프록시 3요소(계정 ID/존/비밀번호) — "프록시 테스트" 버튼이 실제 프록시
  연결로 출발 IP 를 확인한다. 잔액 조회만으로는 이 자격을 검증할 수 없다.
- API 호출은 QThread 워커로 실행한다 — UI 스레드가 타임아웃(최대 90초) 동안
  멈추지 않고, 조회 중에는 버튼이 잠긴다.

참고 실측 문서:
- docs/gmarket/ACCESS_ROUTES_RESEARCH_20260908.md §6 (Web Unlocker)
- docs/coupang/BRIGHTDATA_AKAMAI_REVIEW_20260908.md §3·§7·§9 (ISP 프록시
  하이브리드 통과, 판매자정보 API getStoreReview 는 프록시 IP에서 403)
"""

from __future__ import annotations

from dataclasses import replace as dataclasses_replace

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core import brightdata
from app.core.applog import log_line

# Bright Data API 토큰이 있는 설정 페이지 — 일반 사용자가 발급 경로를
# 직접 찾지 않아도 되게 열어주는 링크(2026-09-09 사용성 개선).
TOKEN_PAGE_URL = "https://brightdata.com/cp/settings"

_BANNER_NEEDS_TOKEN = (
    "⚠ Bright Data API 토큰이 필요합니다 — 입력하지 않으면 Gmarket 카테고리 탭 "
    "수집이 시작되지 않습니다. 아래 순서대로 따라 하면 1분이면 설정됩니다."
)
_BANNER_STYLES = {
    "warn": ("background-color: #fff3cd; color: #7a5b00; border: 1px solid #e0c46c;"),
    "ok": ("background-color: #e2f3e7; color: #135a2e; border: 1px solid #9ed3ae;"),
}


class _BrightDataCallWorker(QThread):
    """계정 API 호출을 워커 스레드에서 실행하고 결과를 시그널로 전달한다.

    ``fn`` 은 인자 없는 callable — BrightDataAPIError 는 failed 로, 그 외
    예상치 못한 예외는 crashed(str) 로 구조화해 전달한다(패널이 같은
    오류 경로로 표시할 수 있게).
    """

    succeeded = pyqtSignal(object)   # fn 의 반환값
    failed = pyqtSignal(object)      # BrightDataAPIError
    crashed = pyqtSignal(str)        # 그 외 예외 메시지

    def __init__(self, fn, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except brightdata.BrightDataAPIError as e:
            self.failed.emit(e)
            return
        except Exception as e:  # noqa: BLE001 - 워커 최상위 예외 격리
            self.crashed.emit(f"{type(e).__name__}: {e}")
            return
        self.succeeded.emit(result)


class BrightDataPanel(QWidget):
    """'설정' 탭 위젯 — Bright Data 계정 자격 증명 입력·검증·저장."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._worker: _BrightDataCallWorker | None = None
        self._build_ui()
        self.load_from_settings()

    # ── UI 구성 ─────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ── 토큰 필수 배너 — 토큰 유무에 따라 눈에 띄게 상태를 바꾼다 ──
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("padding: 8px;")
        layout.addWidget(self.banner)

        # 발급 경로 안내 — 일반 사용자가 어디서 토큰을 얻는지 모르는 문제를
        # 링크 하나로 해결한다.
        guide_row = QHBoxLayout()
        guide = QLabel(
            "① brightdata.com 로그인 → ② 계정 설정(Settings) 페이지에서 API token 복사 "
            "→ ③ 아래 'API 토큰'에 붙여넣기 → '잔액 조회·토큰 검증' → '저장'")
        guide.setWordWrap(True)
        guide_row.addWidget(guide, 1)
        self.btn_open_token_page = QPushButton("토큰 발급 페이지 열기")
        self.btn_open_token_page.setToolTip(
            "브라우저에서 Bright Data 설정 페이지(brightdata.com/cp/settings)를 "
            "엽니다. 로그인 후 'API token' 항목의 값을 복사해 아래에 붙여넣으세요.")
        self.btn_open_token_page.clicked.connect(self._on_open_token_page)
        guide_row.addWidget(self.btn_open_token_page)
        layout.addLayout(guide_row)

        info = QLabel(
            "Bright Data(brightdata.com) 계정의 API 토큰을 입력하면 Gmarket 카테고리 탭의 "
            "Web Unlocker 요청과 Coupang 카테고리 탭의 ISP 프록시 사용량이 "
            "입력된 계정 키에서 차감됩니다. 토큰은 이 PC의 output 폴더에만 저장되며 "
            "저장소(소스 코드)에는 포함되지 않습니다."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # ── 계정 (Web Unlocker — Gmarket 카테고리 탭) ────────────────────
        acct_box = QGroupBox("계정 — Web Unlocker (Gmarket 카테고리 탭)")
        acct_form = QFormLayout(acct_box)

        token_row = QHBoxLayout()
        self.edit_token = QLineEdit()
        self.edit_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_token.setPlaceholderText("여기에 복사한 API 토큰을 붙여넣으세요")
        token_row.addWidget(self.edit_token, 1)
        self.chk_show_token = QCheckBox("표시")
        self.chk_show_token.stateChanged.connect(self._on_toggle_token)
        token_row.addWidget(self.chk_show_token)
        self.btn_balance = QPushButton("잔액 조회·토큰 검증")
        self.btn_balance.clicked.connect(self.on_check_balance)
        token_row.addWidget(self.btn_balance)
        acct_form.addRow("Bright Data API 토큰", token_row)

        self.edit_account_name = QLineEdit()
        self.edit_account_name.setPlaceholderText("예: 본사 계정 (선택 — 여러 키를 구분하는 이름)")
        self.edit_account_name.setToolTip(
            "여러 Bright Data 계정 키를 쓸 때 구분하기 위한 별칭입니다. "
            "저장하면 상태·수집 로그에 이 이름으로 표시됩니다.")
        acct_form.addRow("계정 이름", self.edit_account_name)
        # 토큰 입력이 끝날 때마다 배너 상태를 갱신한다(저장 전에도 반응).
        self.edit_token.editingFinished.connect(self._update_banner)

        self.edit_unlocker_zone = QLineEdit()
        self.edit_unlocker_zone.setPlaceholderText("gm_unlocker")
        acct_form.addRow("Unlocker 존", self.edit_unlocker_zone)

        self.edit_country = QLineEdit()
        self.edit_country.setPlaceholderText("kr")
        self.edit_country.setMaxLength(2)
        acct_form.addRow("출발 국가", self.edit_country)

        layout.addWidget(acct_box)

        # ── ISP 프록시 (Coupang 카테고리 탭 — 선택) ──────────────────────
        isp_box = QGroupBox("ISP 프록시 — Coupang 카테고리 탭 (선택)")
        isp_form = QFormLayout(isp_box)

        self.chk_isp_enabled = QCheckBox(
            "Coupang 카테고리 수집 시 Bright Data ISP 프록시(한국 고정 IP) 경유"
        )
        isp_form.addRow(self.chk_isp_enabled)

        self.edit_customer_id = QLineEdit()
        self.edit_customer_id.setPlaceholderText("예: hl_22fb0228 (/status 의 customer)")
        isp_form.addRow("계정 ID", self.edit_customer_id)

        self.edit_isp_zone = QLineEdit()
        self.edit_isp_zone.setPlaceholderText("예: gm_isp_kr3")
        isp_form.addRow("ISP 존", self.edit_isp_zone)

        isp_pw_row = QHBoxLayout()
        self.edit_isp_password = QLineEdit()
        self.edit_isp_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_isp_password.setPlaceholderText("존 비밀번호")
        isp_pw_row.addWidget(self.edit_isp_password, 1)
        self.btn_fetch_password = QPushButton("토큰으로 가져오기")
        self.btn_fetch_password.clicked.connect(self.on_fetch_zone_password)
        isp_pw_row.addWidget(self.btn_fetch_password)
        isp_form.addRow("ISP 존 비밀번호", isp_pw_row)

        self.btn_test_proxy = QPushButton("프록시 테스트 (출발 IP 확인)")
        self.btn_test_proxy.setToolTip(
            "입력한 계정 ID·존·비밀번호로 실제 프록시 연결을 시험합니다.\n"
            "성공 시 출발 IP 가 표시되고, 인증 실패(407)·시간 초과 등은\n"
            "원인과 함께 안내됩니다. 요청 1건 수 KB 의 대역폭이 소비됩니다.")
        self.btn_test_proxy.clicked.connect(self.on_test_isp_proxy)
        isp_form.addRow("자격 검증", self.btn_test_proxy)

        isp_note = QLabel(
            "프록시를 켜면 1차 목록 수집은 프록시 IP(회선 IP 보호)로 진행되고, "
            "2차 판매자정보는 회선 IP 세션으로 자동 전환됩니다 — 실측상 "
            "판매자정보 API(getStoreReview)는 프록시 IP에서 403 입니다. "
            "로그인 세션을 함께 쓰면 계정 보안 경보 가능성이 있으니 비로그인 "
            "수집에 사용하세요."
        )
        isp_note.setWordWrap(True)
        isp_form.addRow(isp_note)

        layout.addWidget(isp_box)

        # ── 저장 ─────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("저장")
        self.btn_save.setToolTip(
            "새 API 토큰이 입력된 상태로 저장하면 먼저 검증합니다 —\n"
            "거부된 토큰(401/403)은 확인 후에만 저장됩니다.")
        self.btn_save.clicked.connect(self.on_save)
        btn_row.addWidget(self.btn_save)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)
        layout.addStretch(1)

    # ── 시그널 핸들러 ──────────────────────────────────────────────────
    def _on_toggle_token(self, state: int) -> None:
        shown = state == Qt.CheckState.Checked.value
        self.edit_token.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)

    def _on_open_token_page(self) -> None:
        """브라우저로 Bright Data 설정(토큰 발급) 페이지를 연다."""
        QDesktopServices.openUrl(QUrl(TOKEN_PAGE_URL))

    def _update_banner(self) -> None:
        """토큰 유무에 따라 상단 배너를 갱신한다 — 눈에 띄는 경고/확인 표시."""
        stored = brightdata.load_settings()
        new_token = self.edit_token.text().strip()
        alias = self.edit_account_name.text().strip() or stored.account_name
        if stored.api_token or new_token:
            token_mask = brightdata.masked_token(new_token or stored.api_token)
            alias_txt = f"'{alias}' " if alias else ""
            self.banner.setText(
                f"✔ Bright Data 계정 {alias_txt}설정됨 — 토큰 {token_mask}. "
                "'잔액 조회·토큰 검증'으로 확인 후 저장하세요.")
            self.banner.setStyleSheet(
                f"{_BANNER_STYLES['ok']} padding: 8px; font-weight: bold;")
        else:
            self.banner.setText(_BANNER_NEEDS_TOKEN)
            self.banner.setStyleSheet(
                f"{_BANNER_STYLES['warn']} padding: 8px; font-weight: bold;")

    # ── 잔액 조회·토큰 검증 ──────────────────────────────────────────────
    def on_check_balance(self) -> None:
        """토큰 검증을 겸한 잔액 조회 — 성공 시 입력 키의 잔액을 표시한다."""
        token = self.edit_token.text().strip()
        if not token:
            self._set_status("API 토큰을 입력한 뒤 조회하세요.", error=True)
            return
        if not self._start_call(
                lambda: brightdata.fetch_balance(token),
                on_ok=self._on_balance_result,
                on_error=self._set_api_error_status,
                busy_text=f"토큰 {brightdata.masked_token(token)} 검증·잔액 조회 중..."):
            return

    def _on_balance_result(self, data: dict) -> None:
        balance = data.get("balance")
        pending = data.get("pending_balance")
        token = self.edit_token.text().strip()
        try:
            if balance is None:
                raise TypeError("balance 누락")
            balance_txt = f"${float(balance):,.2f}"
            pending_txt = f"${float(pending):,.2f}" if pending is not None else "-"
        except (TypeError, ValueError):
            balance_txt, pending_txt = str(balance), str(pending)
        self._set_status(
            f"토큰 {brightdata.masked_token(token)} 유효 — 잔액 {balance_txt} "
            f"(다음 청구 예정 {pending_txt}). 이 계정 키로 사용량이 차감됩니다.",
            error=False,
        )

    # ── 존 비밀번호 가져오기 ─────────────────────────────────────────────
    def on_fetch_zone_password(self) -> None:
        """입력된 토큰으로 ISP 존 비밀번호를 조회해 채워 넣는다."""
        token = self.edit_token.text().strip()
        zone = self.edit_isp_zone.text().strip()
        if not token:
            self._set_status("존 비밀번호를 가져오려면 먼저 API 토큰을 입력하세요.",
                             error=True)
            return
        if not zone:
            self._set_status("ISP 존 이름을 입력한 뒤 가져오세요.", error=True)
            return
        if not self._start_call(
                lambda: brightdata.fetch_zone_password(token, zone),
                on_ok=self._on_password_result,
                on_error=self._set_api_error_status,
                busy_text=f"존 '{zone}' 비밀번호 조회 중..."):
            return

    def _on_password_result(self, password: str) -> None:
        zone = self.edit_isp_zone.text().strip()
        self.edit_isp_password.setText(str(password))
        self._set_status(f"존 '{zone}' 의 비밀번호를 가져와 채웠습니다. "
                         "'프록시 테스트'로 자격을 확인한 뒤 저장하세요.", error=False)

    # ── ISP 프록시 연결 테스트 ───────────────────────────────────────────
    def on_test_isp_proxy(self) -> None:
        """프록시 3요소를 실제 연결로 검증한다 — 출발 IP 를 표시한다."""
        settings = self.collect_settings_preserving_stored()
        if not settings.isp_enabled:
            self._set_status("먼저 'ISP 프록시 경유'를 체크한 뒤 테스트하세요.",
                             error=True)
            return
        if not (settings.isp_customer_id and settings.isp_zone
                and settings.isp_password):
            self._set_status("계정 ID·ISP 존·비밀번호를 모두 입력한 뒤 테스트하세요. "
                             "(비밀번호는 '토큰으로 가져오기'로 채울 수 있습니다)",
                             error=True)
            return
        if not self._start_call(
                lambda: brightdata.test_isp_proxy(settings),
                on_ok=self._on_proxy_test_result,
                on_error=self._set_api_error_status,
                busy_text=f"ISP 프록시 '{settings.isp_zone}' 연결 테스트 중... "
                          "(최대 20초, 출발 IP 확인)"):
            return

    def _on_proxy_test_result(self, result: dict) -> None:
        ip = str(result.get("ip", "?"))
        country = str(result.get("country", "") or "").strip()
        country_txt = f" ({country.upper()})" if country else ""
        self._set_status(
            f"ISP 프록시 통과 — 출발 IP {ip}{country_txt}. 이 자격으로 Coupang "
            "1차 목록 수집이 진행됩니다. (2차 판매자정보는 자동으로 회선 IP 세션)",
            error=False,
        )

    # ── 저장 ─────────────────────────────────────────────────────────────
    def on_save(self) -> None:
        """저장 — 새 토큰이 입력된 경우 저장 전에 잔액 조회로 검증한다.

        - 검증 통과: 저장 + '토큰 유효' 표시
        - 401/403 거부(확정 실패): 확인 대화상자 — 아니오면 저장 취소
        - 네트워크 오류 등 불확실 실패: 저장은 진행하되 '미검증'으로 명시
        - 빈 토큰 위젯: 기존 저장값 유지(검증 없이 저장)
        """
        if self._call_busy():
            return
        new_token = self.edit_token.text().strip()
        if not new_token:
            self._do_save("토큰 변경 없음(기존 저장값 유지)")
            return
        self._set_status(f"토큰 {brightdata.masked_token(new_token)} 검증 중...",
                         error=False)
        if not self._start_call(
                lambda: brightdata.fetch_balance(new_token),
                on_ok=lambda _data: self._do_save(
                    f"토큰 {brightdata.masked_token(new_token)} 검증 완료"),
                on_error=lambda e: self._on_token_verify_failed(new_token, e),
                busy_text="저장 전 토큰 검증 중..."):
            return

    def _on_token_verify_failed(self, token: str, err: Exception) -> None:
        if getattr(err, "status", 0) in (401, 403):
            reply = QMessageBox.question(
                self, "토큰 거부",
                f"{err}\n\n이 토큰은 Bright Data 에서 거부된 상태라 수집 시에도 "
                "동일하게 실패합니다.\n그래도 저장할까요?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._set_status(
                    "저장 취소 — 토큰이 거부됐습니다. 계정 Settings → API token 값을 "
                    "다시 확인하세요.", error=True)
                return
            self._do_save(
                f"토큰 {brightdata.masked_token(token)} 거부 — 사용자 확인으로 저장")
        else:
            # 네트워크 오류 등 판정 불가 — 저장은 진행하되 미검증임을 명시한다
            self._do_save(
                f"토큰 미검증({err}) — 네트워크 복구 후 '잔액 조회·토큰 검증'으로 "
                "재확인 권장")

    def _do_save(self, token_note: str) -> None:
        """검증 흐름의 끝에서 실제 저장을 수행한다. token_note 는 검증 결과 표시."""
        settings = self.collect_settings_preserving_stored()
        path = brightdata.default_settings_path()
        try:
            path = brightdata.save_settings(settings)
        except OSError as e:
            QMessageBox.critical(self, "저장 실패",
                                 f"설정 파일을 쓸 수 없습니다:\n{path}\n\n{e}")
            self._set_status(f"저장 실패: {e}", error=True)
            return
        stored = brightdata.load_settings(path)
        alias = stored.account_name or self.edit_account_name.text().strip()
        alias_txt = f"계정 '{alias}', " if alias else ""
        log_line(f"[설정] Bright Data 계정 토큰 저장: "
                 f"계정 {alias or '(이름 없음)'} — "
                 f"{brightdata.masked_token(stored.api_token)} ({path})")
        isp_warn = ""
        if stored.isp_enabled and not (stored.isp_customer_id and stored.isp_zone
                                       and stored.isp_password):
            isp_warn = (" ⚠ ISP 프록시 사용 설정이지만 계정 ID/존/비밀번호가 비어 "
                        "있어 Coupang 탭에서는 프록시 없이(직접 접속) 진행됩니다 — "
                        "'프록시 테스트'로 자격을 완성하세요.")
        self._set_status(
            f"저장됨: {alias_txt}"
            f"토큰 {brightdata.masked_token(stored.api_token)} — {token_note}, "
            f"Unlocker 존 '{stored.unlocker_zone or '기본값'}'"
            f"{', ISP 프록시 사용' if stored.isp_enabled else ''}{isp_warn}",
            error=bool(isp_warn),
        )
        self._update_banner()

    # ── 공개 API ─────────────────────────────────────────────────────────
    def collect_settings(self) -> brightdata.BrightDataSettings:
        """입력값 → BrightDataSettings (저장·주입 경로 모두 사용)."""
        return brightdata.BrightDataSettings(
            api_token=self.edit_token.text().strip(),
            account_name=self.edit_account_name.text().strip(),
            unlocker_zone=self.edit_unlocker_zone.text().strip(),
            country=self.edit_country.text().strip(),
            isp_enabled=self.chk_isp_enabled.isChecked(),
            isp_customer_id=self.edit_customer_id.text().strip(),
            isp_zone=self.edit_isp_zone.text().strip(),
            isp_password=self.edit_isp_password.text().strip(),
        )

    def load_from_settings(self) -> None:
        """저장된 설정 → 입력 위젯. 마지막 4자리만 표시(전체 노출 방지)."""
        s = brightdata.load_settings()
        if s.api_token:
            # 파일의 토큰은 위젯에 그대로 복원하지 않고 마스크만 보여준다 —
            # 다른 사람 화면에서 앱을 열었을 때 전체 키가 노출되지 않게.
            self.edit_token.setPlaceholderText(
                f"저장된 토큰 {brightdata.masked_token(s.api_token)} — "
                "변경할 때만 새 키 입력")
        else:
            self.edit_token.setPlaceholderText("여기에 복사한 API 토큰을 붙여넣으세요")
        self.edit_account_name.setText(s.account_name)
        self.edit_unlocker_zone.setText(s.unlocker_zone)
        self.edit_country.setText(s.country)
        self.chk_isp_enabled.setChecked(s.isp_enabled)
        self.edit_customer_id.setText(s.isp_customer_id)
        self.edit_isp_zone.setText(s.isp_zone)
        # 비밀번호는 마스크 복원 — 실제 값은 설정 파일에 그대로 남아 있고,
        # 위젯을 건드리지 않으면(빈 값) 저장 시 기존 값이 유지된다.
        self.edit_isp_password.setPlaceholderText(
            f"저장됨 ({brightdata.masked_token(s.isp_password)}) — 변경할 때만 입력"
            if s.isp_password else "존 비밀번호")
        if s.saved_at:
            self._set_status(f"저장된 설정 불러옴 (마지막 저장: {s.saved_at})",
                             error=False)
        self._update_banner()

    def collect_settings_preserving_stored(self) -> brightdata.BrightDataSettings:
        """저장 버튼 경로 — 빈 토큰/비밀번호는 기존 저장값을 유지한다.

        토큰·비밀번호 위젯은 보안상 전체 값을 복원하지 않으므로(placeholder 마스크만
        표시), 사용자가 새 값을 입력하지 않았으면 저장 파일의 기존 값을 그대로 둔다.
        """
        s = self.collect_settings()
        stored = brightdata.load_settings()
        if not s.api_token and stored.api_token:
            s = dataclasses_replace(s, api_token=stored.api_token)
        if not s.isp_password and stored.isp_password:
            s = dataclasses_replace(s, isp_password=stored.isp_password)
        return s

    def set_external_busy(self, busy: bool) -> None:
        """수집 실행 중 자격 증명 변경 잠금 (다른 탭의 규율과 동일)."""
        for w in (self.edit_token, self.edit_account_name, self.edit_unlocker_zone,
                  self.edit_country, self.chk_isp_enabled, self.edit_customer_id,
                  self.edit_isp_zone, self.edit_isp_password, self.chk_show_token):
            w.setEnabled(not busy)
        for b in (self.btn_save, self.btn_balance, self.btn_fetch_password,
                  self.btn_test_proxy, self.btn_open_token_page):
            b.setEnabled(not busy)

    # ── 비동기 호출 공통 ─────────────────────────────────────────────────
    def _start_call(self, fn, on_ok, on_error, busy_text: str) -> bool:
        """계정 API 호출을 워커 스레드로 실행한다. 시작 실패(이미 실행 중)면 False.

        성공/실패 콜백은 시그널(큐 연결)로 UI 스레드에서 실행된다. 실행 중에는
        모든 동작 버튼을 잠가 중복 호출과 입력 변경을 막는다.
        """
        if self._worker is not None and self._worker.isRunning():
            return False
        self._set_status(busy_text, error=False)
        for b in self._action_buttons():
            b.setEnabled(False)
        worker = _BrightDataCallWorker(fn, self)
        worker.succeeded.connect(on_ok)
        worker.failed.connect(on_error)
        worker.crashed.connect(
            lambda msg: on_error(brightdata.BrightDataAPIError(
                f"예상치 못한 오류: {msg}")))
        worker.finished.connect(self._on_call_finished)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()
        return True

    def _on_call_finished(self) -> None:
        self._worker = None
        for b in self._action_buttons():
            b.setEnabled(True)

    def _call_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _action_buttons(self) -> tuple[QPushButton, ...]:
        return (self.btn_save, self.btn_balance, self.btn_fetch_password,
                self.btn_test_proxy)

    # ── 내부 ─────────────────────────────────────────────────────────────
    def _set_api_error_status(self, err: Exception) -> None:
        self._set_status(f"실패: {err}", error=True)

    def _set_status(self, text: str, error: bool) -> None:
        prefix = "⚠ " if error else ""
        color = "#b3261e" if error else "#1b6e3c"
        self.lbl_status.setStyleSheet(f"color: {color};")
        self.lbl_status.setText(f"{prefix}{text}")
