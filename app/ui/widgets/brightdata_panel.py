"""Bright Data 계정 설정 탭 패널 — API 토큰·존 입력, 잔액 조회, 저장.

사용자가 자신의 Bright Data 계정 API 토큰을 입력하면 두 탭의 Bright Data
사용량(Web Unlocker 요청 과금 / ISP 프록시 대역폭 과금)이 그 계정 키에서
차감된다. 자격 증명은 저장소 밖 output/brightdata_settings.json 에만
저장된다(.gitignore) — 절대 저장소에 커밋되지 않는다.

참고 실측 문서:
- docs/gmarket/ACCESS_ROUTES_RESEARCH_20260908.md §6 (Web Unlocker)
- docs/coupang/BRIGHTDATA_AKAMAI_REVIEW_20260908.md §3·§7·§9 (ISP 프록시
  하이브리드 통과, 판매자정보 API getStoreReview 는 프록시 IP에서 403)
"""

from __future__ import annotations

from dataclasses import replace as dataclasses_replace

from PyQt6.QtCore import Qt
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


class BrightDataPanel(QWidget):
    """'설정' 탭 위젯 — Bright Data 계정 자격 증명 입력·검증·저장."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self.load_from_settings()

    # ── UI 구성 ─────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

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
        self.edit_token.setPlaceholderText("계정 Settings → API token")
        token_row.addWidget(self.edit_token, 1)
        self.chk_show_token = QCheckBox("표시")
        self.chk_show_token.stateChanged.connect(self._on_toggle_token)
        token_row.addWidget(self.chk_show_token)
        self.btn_balance = QPushButton("잔액 조회·토큰 검증")
        self.btn_balance.clicked.connect(self.on_check_balance)
        token_row.addWidget(self.btn_balance)
        acct_form.addRow("API 토큰", token_row)

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

    def on_check_balance(self) -> None:
        """토큰 검증을 겸한 잔액 조회 — 성공 시 입력 키의 잔액을 표시한다."""
        token = self.edit_token.text().strip()
        if not token:
            self._set_status("API 토큰을 입력한 뒤 조회하세요.", error=True)
            return
        try:
            data = brightdata.fetch_balance(token)
        except brightdata.BrightDataAPIError as e:
            self._set_status(f"잔액 조회 실패: {e}", error=True)
            return
        balance = data.get("balance")
        pending = data.get("pending_balance")
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
        try:
            password = brightdata.fetch_zone_password(token, zone)
        except brightdata.BrightDataAPIError as e:
            self._set_status(f"존 비밀번호 조회 실패: {e}", error=True)
            return
        self.edit_isp_password.setText(password)
        self._set_status(f"존 '{zone}' 의 비밀번호를 가져와 채웠습니다.", error=False)

    def on_save(self) -> None:
        settings = self.collect_settings_preserving_stored()
        path = brightdata.default_settings_path()
        try:
            path = brightdata.save_settings(settings)
        except OSError as e:
            QMessageBox.critical(self, "저장 실패",
                                 f"설정 파일을 쓸 수 없습니다:\n{path}\n\n{e}")
            return
        stored = brightdata.load_settings(path)
        log_line(f"[설정] Bright Data 계정 토큰 저장: "
                 f"{brightdata.masked_token(stored.api_token)} ({path})")
        self._set_status(
            f"저장됨: {path} — 토큰 {brightdata.masked_token(stored.api_token)}, "
            f"Unlocker 존 '{stored.unlocker_zone or '기본값'}'"
            f"{', ISP 프록시 사용' if stored.isp_enabled else ''}",
            error=False,
        )

    # ── 공개 API ─────────────────────────────────────────────────────────
    def collect_settings(self) -> brightdata.BrightDataSettings:
        """입력값 → BrightDataSettings (저장·주입 경로 모두 사용)."""
        return brightdata.BrightDataSettings(
            api_token=self.edit_token.text().strip(),
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
            self.edit_token.setPlaceholderText("계정 Settings → API token")
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
        for w in (self.edit_token, self.edit_unlocker_zone, self.edit_country,
                  self.chk_isp_enabled, self.edit_customer_id, self.edit_isp_zone,
                  self.edit_isp_password, self.btn_save, self.btn_balance,
                  self.btn_fetch_password, self.chk_show_token):
            w.setEnabled(not busy)

    # ── 내부 ─────────────────────────────────────────────────────────────
    def _set_status(self, text: str, error: bool) -> None:
        prefix = "⚠ " if error else ""
        self.lbl_status.setText(f"{prefix}{text}")
