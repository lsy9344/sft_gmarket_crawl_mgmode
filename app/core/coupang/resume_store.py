"""Coupang 카테고리 수집 진행 저장·재개 (WORK_ORDER_CATEGORY_RESUME_20260915).

카테고리 하위 폴더의 ``resume.sqlite3`` 에 목록 페이지·상품·판매자 매핑·
판매자정보 진행을 한 거래씩 저장한다. 회선 차단·브라우저 종료·앱 재실행 뒤
같은 카테고리 폴더로 시작하면 저장된 지점부터 이어서 수집한다.

설계 경계(작업지시서 §3.1):
- 프록시 비밀번호·쿠키·브라우저 프로필 내용은 저장하지 않는다.
- 쓰기가 실패하면 그 페이지·판매자를 완료로 기록하지 않는다 — 저장 예외는
  호출자로 그대로 올려 수집을 멈춘다(조용한 건너뛰기 금지).
- 진행 파일을 임의로 삭제하지 않는다. '처음부터 다시 수집'은 기존 파일을
  타임스탬프 아카이브로 보존한 뒤 새 진행 상태를 만든다.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import Self

SCHEMA_VERSION = 1
STATUS_LISTING = "listing"
STATUS_LISTING_DONE = "listing_done"
STATUS_FINISHED = "finished"


class ResumeStoreError(RuntimeError):
    """진행 파일 읽기·쓰기 실패 — 재개를 계속하면 안 되는 상태."""


class ResumeConfigMismatch(ResumeStoreError):
    """저장된 진행 기록과 현재 설정이 불일치 — 재개 중단 대상."""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class ResumeStore:
    """카테고리 1개 실행의 진행 상태 (SQLite, 같은 스레드에서만 사용)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    # ── 수명 ────────────────────────────────────────────────────────────
    def open(self) -> None:
        if self._conn is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path))
            conn.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 파일을 열 수 없습니다 ({self.path}): {e}") from e
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pages (
                page_no INTEGER PRIMARY KEY,
                item_count INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products (
                dedup_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                page_no INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mapping (
                vendor_item_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sellers (
                vendor_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT,
                completed_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise ResumeStoreError("진행 파일이 열려 있지 않습니다")
        return self._conn

    # ── 메타 ────────────────────────────────────────────────────────────
    def _get_meta(self, key: str, default: str = "") -> str:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다 ({key}): {e}") from e
        return str(row[0]) if row else default

    def _set_meta(self, key: str, value: Any) -> None:
        try:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 쓸 수 없습니다 ({key}): {e}") from e

    # ── 설정 지문 (재개 자격 검사) ──────────────────────────────────────
    def check_config(self, category_id: str, exclude_rocket: bool, max_pages: int) -> str | None:
        """설정 불일치 사유를 반환한다. None 이면 재개 가능(또는 새 실행).

        첫 실행에는 현재 값을 지문으로 저장한다. 이름 표시가 바뀌어도
        카테고리 ID 가 같으면 같은 실행으로 판단한다(작업지시서 §3.1).
        """
        cid = str(category_id or "").strip()
        stored_cid = self._get_meta("category_id")
        if not stored_cid:
            self._set_meta("category_id", cid)
            self._set_meta("exclude_rocket", "1" if exclude_rocket else "0")
            self._set_meta("max_pages", int(max_pages))
            self._set_meta("schema_version", SCHEMA_VERSION)
            self._set_meta("status", STATUS_LISTING)
            return None
        if stored_cid != cid:
            return (
                f"저장된 카테고리 ID({stored_cid or '없음'})와 선택한 카테고리 ID({cid or '없음'})가 다릅니다."
            )
        stored_rocket = self._get_meta("exclude_rocket", "1") == "1"
        if stored_rocket != bool(exclude_rocket):
            return (
                f"저장된 진행은 로켓 {'제외' if stored_rocket else '포함'}으로 수집됐는데 "
                f"지금은 {'포함' if exclude_rocket else '제외'}입니다."
            )
        stored_pages = int(self._get_meta("max_pages", "0") or 0)
        if stored_pages and int(max_pages) < stored_pages:
            return (
                f"저장된 진행은 최대 {stored_pages}페이지로 수집됐는데 "
                f"지금은 {max_pages}페이지입니다. {stored_pages} 이상으로 설정하거나 "
                "'처음부터 다시 수집'을 사용하세요."
            )
        return None

    @property
    def status(self) -> str:
        return self._get_meta("status", STATUS_LISTING)

    @property
    def category_id(self) -> str:
        return self._get_meta("category_id")

    @property
    def last_completed_page(self) -> int:
        try:
            row = self.conn.execute("SELECT MAX(page_no) FROM pages").fetchone()
            return int(row[0] or 0)
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    @property
    def last_item_page(self) -> int:
        """상품이 확인된 마지막 페이지 — 재개 경계(다시 읽을 페이지)."""
        try:
            row = self.conn.execute(
                "SELECT MAX(page_no) FROM pages WHERE item_count > 0"
            ).fetchone()
            return int(row[0] or 0)
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    @property
    def product_count(self) -> int:
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM products").fetchone()
            return int(row[0] or 0)
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    @property
    def confirmed_seller_count(self) -> int:
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM sellers WHERE status = 'confirmed'"
            ).fetchone()
            return int(row[0] or 0)
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    @property
    def listing_shifted(self) -> bool:
        return self._get_meta("listing_shifted", "0") == "1"

    def set_listing_shifted(self) -> None:
        self._set_meta("listing_shifted", "1")

    # ── 재개 가능 판정·안내 ─────────────────────────────────────────────
    def has_state(self) -> bool:
        try:
            row = self.conn.execute(
                "SELECT (SELECT COUNT(*) FROM pages) + (SELECT COUNT(*) FROM sellers)"
            ).fetchone()
            return int(row[0] or 0) > 0
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    def preview_text(self) -> str:
        """시작 전 안내 — '마지막 확인 페이지 N / 상품 M개 / 확인된 판매자 K명'."""
        return (
            f"마지막 확인 페이지 {self.last_completed_page} / "
            f"상품 {self.product_count:,}개 / "
            f"확인된 판매자 {self.confirmed_seller_count}명"
        )

    # ── 목록 페이지 (페이지 1건 = 1거래) ────────────────────────────────
    def record_page(self, page_no: int, items: list[Any]) -> None:
        """정상으로 읽은 페이지 1건을 상품·경계·빈 페이지 집계와 함께 저장.

        거래가 끝나기 전에는 다음 페이지로 넘어가지 않는다(작업지시서 §3.2).
        쓰기 실패 시 예외를 올려 호출자가 그 페이지를 미완료로 남긴다.
        """
        empty_streak = self.empty_streak
        try:
            with self.conn:
                for p in items:
                    if isinstance(p, dict):
                        key = p.get("dedup_key")
                        if not key:
                            raise ResumeStoreError(
                                "저장할 상품 항목에 dedup_key 가 없습니다"
                            )
                    else:
                        key = p.dedup_key
                    payload = json.dumps(_product_payload(p), ensure_ascii=False)
                    self.conn.execute(
                        "INSERT INTO products (dedup_key, payload, page_no) VALUES (?, ?, ?) "
                        "ON CONFLICT(dedup_key) DO UPDATE SET payload = excluded.payload, "
                        "page_no = excluded.page_no",
                        (key, payload, int(page_no)),
                    )
                self.conn.execute(
                    "INSERT INTO pages (page_no, item_count, completed_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(page_no) DO UPDATE SET item_count = excluded.item_count, "
                    "completed_at = excluded.completed_at",
                    (int(page_no), len(items), _now()),
                )
                new_streak = 0 if items else empty_streak + 1
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('empty_streak', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(new_streak),),
                )
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('updated_at', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_now(),),
                )
        except sqlite3.Error as e:
            raise ResumeStoreError(f"페이지 {page_no} 저장 실패: {e}") from e

    @property
    def empty_streak(self) -> int:
        return int(self._get_meta("empty_streak", "0") or 0)

    def reset_empty_streak(self) -> None:
        """새 회선(세션)에서 빈 페이지 판정을 다시 관찰하기 위해 0으로."""
        self._set_meta("empty_streak", "0")

    def load_products(self) -> list[dict]:
        try:
            rows = self.conn.execute(
                "SELECT payload FROM products ORDER BY page_no, dedup_key"
            ).fetchall()
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e
        out = []
        for (payload,) in rows:
            try:
                out.append(json.loads(payload))
            except ValueError as e:
                raise ResumeStoreError(f"저장된 상품을 해석할 수 없습니다: {e}") from e
        return out

    def mark_listing_done(self, end_reason: str = "empty") -> None:
        self._set_meta("status", STATUS_LISTING_DONE)
        self._set_meta("listing_end_reason", end_reason)

    @property
    def listing_end_reason(self) -> str:
        return self._get_meta("listing_end_reason", "")

    def mark_finished(self) -> None:
        """전체 파이프라인 완료 — 이 폴더는 더 이상 재개 대상이 아니다."""
        self._set_meta("status", STATUS_FINISHED)

    # ── 판매자 매핑 (완료 시 일괄 저장 — 도중 실패는 저장 없음) ──────────
    def save_mapping(self, vendors: dict[str, dict]) -> None:
        try:
            with self.conn:
                for viid, payload in vendors.items():
                    self.conn.execute(
                        "INSERT INTO mapping (vendor_item_id, payload) VALUES (?, ?) "
                        "ON CONFLICT(vendor_item_id) DO UPDATE SET payload = excluded.payload",
                        (str(viid), json.dumps(payload, ensure_ascii=False)),
                    )
        except sqlite3.Error as e:
            raise ResumeStoreError(f"매핑 저장 실패: {e}") from e

    def load_mapping(self) -> dict[str, dict]:
        try:
            rows = self.conn.execute("SELECT vendor_item_id, payload FROM mapping").fetchall()
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e
        out = {}
        for viid, payload in rows:
            try:
                out[str(viid)] = json.loads(payload)
            except ValueError as e:
                raise ResumeStoreError(f"저장된 매핑을 해석할 수 없습니다: {e}") from e
        return out

    # ── 판매자정보 (판매자 1명 = 1거래) ─────────────────────────────────
    def record_seller_confirmed(self, vendor_id: str, record: dict) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO sellers (vendor_id, status, payload, completed_at) "
                    "VALUES (?, 'confirmed', ?, ?) "
                    "ON CONFLICT(vendor_id) DO UPDATE SET status = 'confirmed', "
                    "payload = excluded.payload, completed_at = excluded.completed_at",
                    (str(vendor_id), json.dumps(record, ensure_ascii=False), _now()),
                )
        except sqlite3.Error as e:
            raise ResumeStoreError(f"판매자 {vendor_id} 저장 실패: {e}") from e

    def record_seller_brand(self, vendor_id: str) -> None:
        """브랜드 판매자 판정 저장 — payload 없이 상태만 (재처리 대상에서 제외)."""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO sellers (vendor_id, status, payload, completed_at) "
                    "VALUES (?, 'brand', NULL, ?) "
                    "ON CONFLICT(vendor_id) DO UPDATE SET status = 'brand', "
                    "payload = NULL, completed_at = excluded.completed_at",
                    (str(vendor_id), _now()),
                )
        except sqlite3.Error as e:
            raise ResumeStoreError(f"판매자 {vendor_id} 저장 실패: {e}") from e

    def confirmed_sellers(self) -> dict[str, tuple[str, dict | None]]:
        """vendor_id → (status, 레코드 dict|None). brand 는 payload 가 None."""
        try:
            rows = self.conn.execute(
                "SELECT vendor_id, status, payload FROM sellers"
            ).fetchall()
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e
        out = {}
        for vid, status, payload in rows:
            record = None
            if payload:
                try:
                    record = json.loads(payload)
                except ValueError as e:
                    raise ResumeStoreError(f"저장된 판매자 {vid}을 해석할 수 없습니다: {e}") from e
            out[str(vid)] = (str(status), record)
        return out

    # ── 처음부터 다시 수집 (기록 보존 + 새 상태) ────────────────────────
    def archive(self) -> Path | None:
        """기존 진행 파일을 타임스탬프 아카이브로 보존한다. 새 파일은 다음 open 에.

        기존 기록을 삭제·덮어쓰지 않는다(작업지시서 §3.4). 저장 파일이
        없으면 None.
        """
        self.close()
        if not self.path.exists():
            return None
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = self.path.with_name(f"{self.path.stem}_archive_{stamp}{self.path.suffix}")
        index = 1
        while target.exists():
            target = self.path.with_name(
                f"{self.path.stem}_archive_{stamp}_{index}{self.path.suffix}"
            )
            index += 1
        try:
            self.path.replace(target)
        except OSError as e:
            raise ResumeStoreError(f"진행 파일 보관에 실패했습니다: {e}") from e
        return target


def _product_payload(p: Any) -> dict:
    """SearchProduct(또는 이미 직렬화된 dict) → 저장용 dict.

    dedup_key 는 products 키이므로 payload 에는 넣지 않는다.
    """
    if isinstance(p, dict):
        return {k: v for k, v in p.items() if k != "dedup_key"}
    return {
        "item_id": p.item_id,
        "legacy_product_id": p.legacy_product_id,
        "vendor_item_id": p.vendor_item_id,
        "vendor_id": p.vendor_id,
        "title": p.title,
        "price": p.price,
        "rocket": p.rocket,
        "sponsored": p.sponsored,
        "url": p.url,
    }


def product_from_payload(payload: dict) -> Any:
    """저장된 dict → SearchProduct (search_parser 를 늦게 임포트해 순환 회피)."""
    from app.core.coupang.search_parser import SearchProduct

    known = {
        f: payload[f]
        for f in SearchProduct.__dataclass_fields__  # type: ignore[attr-defined]
        if f in payload
    }
    return SearchProduct(**known)


def peek_resume(run_dir: Path | str) -> str | None:
    """폴더의 미완료 진행 안내문을 읽어온다. 없으면 None.

    UI 시작 전 확인용 — 읽기 전용이며 파일 부재·손상은 조용히 None.
    완료된 실행(STATUS_FINISHED)은 재개 대상이 아니다.
    """
    path = Path(run_dir) / "resume.sqlite3"
    if not path.exists():
        return None
    try:
        with ResumeStore(path) as store:
            if not store.has_state() or store.status == STATUS_FINISHED:
                return None
            return store.preview_text()
    except ResumeStoreError:
        return None
