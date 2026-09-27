"""Bright Data 계정 설정 탭 패널 — API 토큰·존 입력, 검증, 저장.

Gmarket 카테고리 탭의 Web Unlocker 사용량이 입력한 계정 키에서 차감된다.
Coupang 카테고리 탭은 Decodo 계정을 쓴다.
자격 증명은 저장소 밖 output/brightdata_settings.json 에만 저장된다
(.gitignore) — 절대 저장소에 커밋되지 않는다.

검증 경로(2026-09-09 보강):
- API 토큰 — "잔액 조회·토큰 검증" 버튼 + 저장 버튼이 새 토큰을 먼저 검증
  (401/403 거부 시 확인 대화상자, 네트워크 오류는 미검증 명시 후 저장).
- API 호출은 QThread 워커로 실행한다 — UI 스레드가 타임아웃(최대 90초) 동안
  멈추지 않고, 조회 중에는 버튼이 잠긴다.

참고 실측 문서:
- docs/gmarket/ACCESS_ROUTES_RESEARCH_20260908.md §6 (Web Unlocker)
- docs/coupang/DECODO_PROXY_METHODOLOGY_20260910.md (쿠팡 수집)
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as dataclasses_replace

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core import brightdata, decodo
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


@dataclass(frozen=True)
class _DecodoReadiness:
    connection_ok: bool
    connection_message: str
    runtime_errors: tuple[str, ...]


def _check_decodo_readiness(settings: decodo.DecodoSettings) -> _DecodoReadiness:
    """저장된 계정 회선과 수집 런타임을 별도로 확인한다(워커 스레드에서 실행)."""
    from app.core.coupang.preflight import PreflightStatus, check_runtime
    from app.core.gmarket_preflight import check_gmarket_runtime

    proxy = decodo.sticky_proxy_dict(settings)
    try:
        if proxy is None:
            raise decodo.DecodoError("저장된 Decodo 계정 정보를 읽을 수 없습니다.")
        info = decodo.fetch_exit_ip(proxy)
        connection_ok = bool(info.ip and info.is_korea)
        if connection_ok:
            connection_message = f"한국 회선 확인 ({info.ip})"
        elif not info.ip:
            connection_message = "회선 응답에 IP가 없어 한국 회선을 확인할 수 없습니다."
        else:
            country = info.country_code or info.country_name or "알 수 없음"
            connection_message = f"한국 회선이 아닙니다 ({country})."
    except decodo.DecodoError as e:
        connection_ok = False
        connection_message = str(e)

    runtime_errors = []
    try:
        coupang = check_runtime()
        if coupang.status != PreflightStatus.OK:
            runtime_errors.append(f"Camoufox/GeoIP: {coupang.message.splitlines()[0]}")
    except Exception:  # noqa: BLE001 - 설치 상태 확인 실패도 미준비로 표시
        runtime_errors.append("Camoufox/GeoIP 설치 상태를 확인할 수 없습니다.")
    try:
        gmarket = check_gmarket_runtime()
        if not gmarket.ok:
            runtime_errors.append(f"Patchright: {gmarket.message.splitlines()[0]}")
    except Exception:  # noqa: BLE001 - 설치 상태 확인 실패도 미준비로 표시
        runtime_errors.append("Patchright 설치 상태를 확인할 수 없습니다.")

    return _DecodoReadiness(
        connection_ok, connection_message, tuple(runtime_errors)
    )


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
        self._external_busy = False
        self._build_ui()
        self.load_from_settings()
        self.load_from_decodo()

    # ── UI 구성 ─────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        content = QWidget()
        content.setMaximumWidth(800)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        scroll.setWidget(content)
        root.addWidget(scroll)

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
            "Bright Data 토큰은 Gmarket 카테고리 탭에 쓰입니다. "
            "Coupang 카테고리 탭은 아래 Decodo 계정을 씁니다. "
            "자격 증명은 이 PC의 output 폴더에만 저장됩니다."
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

        # ── Decodo (Coupang 카테고리 탭) ────────────────────────────────
        decodo_box = QGroupBox("Decodo — Coupang 카테고리 탭")
        decodo_form = QFormLayout(decodo_box)
        decodo_note = QLabel(
            "쿠팡 카테고리 수집은 Decodo 한국 고정 회선이 필요합니다. "
            "대시보드 Authentication 의 사용자명·비밀번호를 넣으세요. "
            "앱이 세션 형식을 붙입니다."
        )
        decodo_note.setWordWrap(True)
        decodo_form.addRow(decodo_note)

        self.edit_decodo_user = QLineEdit()
        self.edit_decodo_user.setPlaceholderText("예: sp3xxxxx (user- 접두사는 빼도 됩니다)")
        decodo_form.addRow("Decodo 사용자명", self.edit_decodo_user)

        self.edit_decodo_password = QLineEdit()
        self.edit_decodo_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_decodo_password.setPlaceholderText("비밀번호")
        decodo_form.addRow("Decodo 비밀번호", self.edit_decodo_password)

        self.btn_save_decodo = QPushButton("Decodo 계정 저장")
        self.btn_save_decodo.clicked.connect(self.on_save_decodo)
        decodo_form.addRow(self.btn_save_decodo)
        self.lbl_decodo_saved = QLabel("")
        self.lbl_decodo_saved.setWordWrap(True)
        decodo_form.addRow(self.lbl_decodo_saved)
        self.lbl_decodo_ready = QLabel("")
        self.lbl_decodo_ready.setWordWrap(True)
        decodo_form.addRow(self.lbl_decodo_ready)
        layout.addWidget(decodo_box)

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
        self._set_status(
            f"저장됨: {alias_txt}"
            f"토큰 {brightdata.masked_token(stored.api_token)} — {token_note}, "
            f"Unlocker 존 '{stored.unlocker_zone or '기본값'}'",
            error=False,
        )
        self._update_banner()

    # ── 공개 API ─────────────────────────────────────────────────────────
    def collect_settings(self) -> brightdata.BrightDataSettings:
        """화면의 입력값 → BrightDataSettings."""
        return brightdata.BrightDataSettings(
            api_token=self.edit_token.text().strip(),
            account_name=self.edit_account_name.text().strip(),
            unlocker_zone=self.edit_unlocker_zone.text().strip(),
            country=self.edit_country.text().strip(),
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
        if s.saved_at:
            self._set_status(f"저장된 설정 불러옴 (마지막 저장: {s.saved_at})",
                             error=False)
        self._update_banner()

    def load_from_decodo(self) -> None:
        s = decodo.load_settings()
        self.edit_decodo_user.setText(s.username)
        self.edit_decodo_password.setPlaceholderText(
            f"저장됨 ({decodo.masked_secret(s.password)}) — 변경할 때만 입력"
            if s.password else "비밀번호"
        )
        self.lbl_decodo_saved.setText(
            f"저장된 계정: {s.username}" if decodo.credentials_ready(s)
            else "Decodo 계정이 저장되지 않았습니다."
        )
        self._set_decodo_ready("사용 준비는 계정 저장 버튼을 눌러 확인하세요.")

    def on_save_decodo(self) -> None:
        if self._call_busy() or self._external_busy:
            return
        stored = decodo.load_settings()
        password = self.edit_decodo_password.text().strip() or stored.password
        settings = decodo.DecodoSettings(
            username=self.edit_decodo_user.text().strip(),
            password=password,
            host=stored.host,
            port=stored.port,
            country=stored.country,
        ).sanitized()
        if not decodo.credentials_ready(settings):
            QMessageBox.warning(
                self, "Decodo 계정 필요",
                "사용자명과 비밀번호를 모두 입력하세요.",
            )
            self.lbl_decodo_saved.setText("저장 실패: 사용자명과 비밀번호를 입력하세요.")
            self._set_decodo_ready("수집 준비 안 됨 — 계정이 저장되지 않았습니다.",
                                   error=True)
            return
        try:
            path = decodo.save_settings(settings)
        except OSError as e:
            guide = "EXE를 쓰기 가능한 폴더로 옮기거나 해당 폴더의 쓰기 권한을 확인하세요."
            QMessageBox.critical(self, "저장 실패",
                                 f"Decodo 설정을 쓸 수 없습니다:\n{e}\n\n{guide}")
            self.lbl_decodo_saved.setText(f"저장 실패: {e} — {guide}")
            self._set_decodo_ready("수집 준비 안 됨 — 계정을 저장하지 못했습니다.",
                                   error=True)
            return
        self.edit_decodo_password.clear()
        self.load_from_decodo()
        log_line(f"[설정] Decodo 계정 저장: {settings.username} ({path})")
        self.lbl_decodo_saved.setText(f"계정 저장 완료: {settings.username}")
        self._set_decodo_ready("Decodo 연결과 수집 환경 확인 중...")
        saved = decodo.load_settings(path)
        self._start_call(
            lambda: _check_decodo_readiness(saved),
            on_ok=self._on_decodo_readiness,
            on_error=lambda _e: self._set_decodo_check_failed(),
            busy_text="",
        )

    def _on_decodo_readiness(self, result: _DecodoReadiness) -> None:
        runtime_ok = not result.runtime_errors
        runtime_message = (
            "Camoufox·GeoIP·Patchright 확인됨" if runtime_ok else
            "; ".join(result.runtime_errors) +
            " SellerCollector.exe --setup-runtime 을 실행하세요."
        )
        if result.connection_ok and runtime_ok:
            message = f"사용 준비 완료 — Decodo {result.connection_message}; {runtime_message}."
            error = False
        else:
            message = (
                f"수집 준비 안 됨 — 연결: {result.connection_message}; "
                f"실행 환경: {runtime_message}"
            )
            error = True
        self._set_decodo_ready(message, error=error)

    def _set_decodo_check_failed(self) -> None:
        message = "수집 준비 안 됨 — 사용 준비 검사 중 오류가 발생했습니다. 다시 저장해 확인하세요."
        self._set_decodo_ready(message, error=True)

    def _set_decodo_ready(self, message: str, error: bool | None = None) -> None:
        color = "#b3261e" if error else "#1b6e3c" if error is False else "#475569"
        self.lbl_decodo_ready.setStyleSheet(f"color: {color};")
        self.lbl_decodo_ready.setText(message)

    def collect_settings_preserving_stored(self) -> brightdata.BrightDataSettings:
        """빈 토큰과 화면에서 제거한 ISP 설정은 기존 저장값을 유지한다."""
        s = self.collect_settings()
        stored = brightdata.load_settings()
        return dataclasses_replace(
            stored,
            api_token=s.api_token or stored.api_token,
            account_name=s.account_name,
            unlocker_zone=s.unlocker_zone,
            country=s.country,
        )

    def set_external_busy(self, busy: bool) -> None:
        """수집 실행 중 자격 증명 변경 잠금 (다른 탭의 규율과 동일)."""
        self._external_busy = busy
        for w in (self.edit_token, self.edit_account_name, self.edit_unlocker_zone,
                  self.edit_country, self.chk_show_token, self.edit_decodo_user,
                  self.edit_decodo_password):
            w.setEnabled(not busy and not self._call_busy())
        for b in (self.btn_save, self.btn_balance, self.btn_open_token_page,
                  self.btn_save_decodo):
            b.setEnabled(not busy and not self._call_busy())

    # ── 비동기 호출 공통 ─────────────────────────────────────────────────
    def _start_call(self, fn, on_ok, on_error, busy_text: str) -> bool:
        """계정 API 호출을 워커 스레드로 실행한다. 시작 실패(이미 실행 중)면 False.

        성공/실패 콜백은 시그널(큐 연결)로 UI 스레드에서 실행된다. 실행 중에는
        모든 동작 버튼을 잠가 중복 호출과 입력 변경을 막는다.
        """
        if self._worker is not None and self._worker.isRunning():
            return False
        if busy_text:
            self._set_status(busy_text, error=False)
        for b in self._action_buttons():
            b.setEnabled(False)
        self.edit_decodo_user.setEnabled(False)
        self.edit_decodo_password.setEnabled(False)
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
            b.setEnabled(not self._external_busy)
        self.edit_decodo_user.setEnabled(not self._external_busy)
        self.edit_decodo_password.setEnabled(not self._external_busy)

    def _call_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _action_buttons(self) -> tuple[QPushButton, ...]:
        return (self.btn_save, self.btn_balance, self.btn_save_decodo)

    # ── 내부 ─────────────────────────────────────────────────────────────
    def _set_api_error_status(self, err: Exception) -> None:
        self._set_status(f"실패: {err}", error=True)

    def _set_status(self, text: str, error: bool) -> None:
        prefix = "⚠ " if error else ""
        color = "#b3261e" if error else "#1b6e3c"
        self.lbl_status.setStyleSheet(f"color: {color};")
        self.lbl_status.setText(f"{prefix}{text}")
