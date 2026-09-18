"""AliExpress 카테고리 수집 진행 저장·재개 (ResumeStore).

카테고리별 하위 폴더의 resume.sqlite3 에 목록 페이지·상품·판매자 정보·완료 결과를
트랜잭션 단위로 저장한다. 네트워크 끊김, 사용자 취소, 프로그램 재실행 시
저장된 지점부터 이어서 수집한다.
"""

from __future__ import annotations

import hashlib
import json
import re
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


def ali_category_run_dir(
    output_dir: Path | str, category_name: str = "", category_url: str = ""
) -> Path:
    """카테고리별 격리된 실행 디렉터리를 생성한다.

    category_url 에서 category/ID 를 추출하거나 URL 해시(8자리)를 사용하고,
    category_name 을 안전한 폴더명으로 정규화하여 output_dir/{safe_name}_{cid}를 반환한다.
    """
    m = re.search(r"category/(\d+)", category_url or "")
    cid = m.group(1) if m else hashlib.md5((category_url or "").encode()).hexdigest()[:8]
    raw = str(category_name or "").strip() or "ali_cat"
    safe_name = re.sub(r"[^\w가-힣]+", "_", raw).strip("._")[:40] or "ali_cat"
    return Path(output_dir) / f"{safe_name}_{cid}"


class AliexpressResumeStore:
    """AliExpress 카테고리 수집 진행 상태 저장소 (SQLite)."""

    def __init__(self, run_dir_or_path: Path | str) -> None:
        p = Path(run_dir_or_path)
        if p.suffix == ".sqlite3" or p.name.endswith(".sqlite3"):
            self.path = p
        else:
            self.path = p / "resume.sqlite3"
        self._conn: sqlite3.Connection | None = None

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
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                page_no INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sellers (
                vendor_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
                item_id TEXT PRIMARY KEY,
                vendor_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS item_failures (
                item_id TEXT PRIMARY KEY,
                failures INTEGER NOT NULL,
                updated_at TEXT NOT NULL
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

    # ── 설정 지문 및 상태 ────────────────────────────────────────────────
    def check_config(
        self, category_name: str, category_url: str, max_pages: int
    ) -> str | None:
        """설정 불일치 사유를 반환한다. None 이면 재개 가능(또는 신규 실행)."""
        stored_url = self._get_meta("category_url")
        if not stored_url:
            self._set_meta("category_name", str(category_name or "").strip())
            self._set_meta("category_url", str(category_url or "").strip())
            self._set_meta("max_pages", int(max_pages))
            self._set_meta("schema_version", SCHEMA_VERSION)
            self._set_meta("status", STATUS_LISTING)
            return None

        if stored_url != str(category_url or "").strip():
            return (
                f"저장된 카테고리 URL({stored_url})과 입력된 URL({category_url})이 다릅니다."
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
    def last_completed_page(self) -> int:
        try:
            row = self.conn.execute("SELECT MAX(page_no) FROM pages").fetchone()
            return int(row[0] or 0)
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    @property
    def last_item_page(self) -> int:
        """상품이 확인된 마지막 페이지 — 재개 경계(다시 확인할 페이지).

        0건으로 기록된 페이지(소프트 차단·렌더 지연)는 경계에 포함하지 않는다.
        재개 시 이 페이지를 다시 확인한 뒤 다음 페이지부터 이어간다
        (쿠팡 resume_store.last_item_page 규율 동일).
        """
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
    def item_result_count(self) -> int:
        try:
            row = self.conn.execute("SELECT COUNT(*) FROM items").fetchone()
            return int(row[0] or 0)
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    @property
    def empty_streak(self) -> int:
        return int(self._get_meta("empty_streak", "0") or 0)

    def reset_empty_streak(self) -> None:
        self._set_meta("empty_streak", "0")

    def has_state(self) -> bool:
        try:
            row = self.conn.execute(
                "SELECT (SELECT COUNT(*) FROM pages) + (SELECT COUNT(*) FROM sellers) + (SELECT COUNT(*) FROM items)"
            ).fetchone()
            return int(row[0] or 0) > 0
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    def preview_text(self) -> str:
        """시작 전 안내문."""
        return (
            f"마지막 확인 페이지 {self.last_completed_page} / "
            f"상품 {self.product_count:,}개 / "
            f"확인된 판매자 {self.confirmed_seller_count:,}명"
        )

    # ── Phase 1: 페이지 단위 원자적 저장 ─────────────────────────────────
    def record_page(self, page_no: int, items: list[dict], new_count: int | None = None) -> None:
        """페이지 1건을 단일 트랜잭션으로 저장.

        ``new_count`` 를 넘기면 빈 페이지 판정을 크롤러와 같은 기준
        (신규 상품 0개면 빈 페이지)으로 맞춘다. 재개 시 이 값이 없으면
        페이지에 상품만 있는지로 판정하는 구버전 기준을 유지한다.
        """
        empty_streak = self.empty_streak
        if new_count is None:
            new_streak = 0 if items else empty_streak + 1
        else:
            new_streak = 0 if (items and new_count > 0) else empty_streak + 1
        try:
            with self.conn:
                for item in items:
                    item_id = str(item.get("id") or "")
                    if not item_id:
                        continue
                    payload = json.dumps(item, ensure_ascii=False)
                    self.conn.execute(
                        "INSERT INTO products (id, payload, page_no) VALUES (?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
                        "page_no = excluded.page_no",
                        (item_id, payload, int(page_no)),
                    )
                self.conn.execute(
                    "INSERT INTO pages (page_no, item_count, completed_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(page_no) DO UPDATE SET item_count = excluded.item_count, "
                    "completed_at = excluded.completed_at",
                    (int(page_no), len(items), _now()),
                )
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

    def load_products(self) -> list[dict]:
        """저장된 상품 목록 복원."""
        try:
            rows = self.conn.execute(
                "SELECT payload FROM products ORDER BY page_no, id"
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

    # ── Phase 2: 판매자 및 상품 결과 실시간 저장 ────────────────────────
    def record_seller(self, vendor_id: str, seller_payload: dict) -> None:
        """판매자 1개사의 사업자 정보 캐시 저장."""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO sellers (vendor_id, status, payload, completed_at) "
                    "VALUES (?, 'confirmed', ?, ?) "
                    "ON CONFLICT(vendor_id) DO UPDATE SET status = 'confirmed', "
                    "payload = excluded.payload, completed_at = excluded.completed_at",
                    (str(vendor_id), json.dumps(seller_payload, ensure_ascii=False), _now()),
                )
        except sqlite3.Error as e:
            raise ResumeStoreError(f"판매자 {vendor_id} 저장 실패: {e}") from e

    def record_item_result(self, item_id: str, vendor_id: str, record: dict) -> None:
        """상품 1건의 최종 수집 결과 저장."""
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO items (item_id, vendor_id, payload, completed_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(item_id) DO UPDATE SET vendor_id = excluded.vendor_id, "
                    "payload = excluded.payload, completed_at = excluded.completed_at",
                    (str(item_id), str(vendor_id), json.dumps(record, ensure_ascii=False), _now()),
                )
        except sqlite3.Error as e:
            raise ResumeStoreError(f"상품 {item_id} 결과 저장 실패: {e}") from e

    def confirmed_sellers(self) -> dict[str, dict]:
        """확인된 판매자 dict (vendor_id -> seller_info_dict)."""
        try:
            rows = self.conn.execute(
                "SELECT vendor_id, payload FROM sellers WHERE status = 'confirmed'"
            ).fetchall()
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e
        out = {}
        for vid, payload in rows:
            if payload:
                try:
                    out[str(vid)] = json.loads(payload)
                except ValueError as e:
                    raise ResumeStoreError(f"저장된 판매자 {vid} 해석 실패: {e}") from e
        return out

    def processed_item_ids(self) -> set[str]:
        """이미 수집 완료된 상품 ID 목록."""
        try:
            rows = self.conn.execute("SELECT item_id FROM items").fetchall()
            return {str(r[0]) for r in rows}
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e

    def load_item_results(self) -> list[dict]:
        """수집 완료된 전체 상품 레코드 복원."""
        try:
            rows = self.conn.execute(
                "SELECT payload FROM items ORDER BY completed_at, item_id"
            ).fetchall()
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e
        out = []
        for (payload,) in rows:
            if payload:
                try:
                    out.append(json.loads(payload))
                except ValueError as e:
                    raise ResumeStoreError(f"저장된 상품 결과 해석 실패: {e}") from e
        return out

    # ── 영구 실패 추적 (재시도 포기 판정) ────────────────────────────────
    def record_item_failure(self, item_id: str) -> int:
        """상품 1건의 실패 실행 수를 1 늘리고 누적값을 반환한다.

        데이터를 끝내 주지 않는 상품(삭제·변경 SKU)이 재개 대상에 영원히 남아
        완료 봉인이 불가능한 비수렴을 막기 위한 카운터다. 실행 1회(재시도 3회
        소진)당 1씩 늘어 크롤러의 ITEM_GIVE_UP_RUNS 판정에 쓰인다.
        """
        try:
            with self.conn:
                row = self.conn.execute(
                    "SELECT failures FROM item_failures WHERE item_id = ?", (str(item_id),)
                ).fetchone()
                total = int(row[0] if row else 0) + 1
                self.conn.execute(
                    "INSERT INTO item_failures (item_id, failures, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET "
                    "failures = excluded.failures, updated_at = excluded.updated_at",
                    (str(item_id), total, _now()),
                )
            return total
        except sqlite3.Error as e:
            raise ResumeStoreError(f"상품 {item_id} 실패 기록 저장 실패: {e}") from e

    def item_failure_counts(self) -> dict[str, int]:
        try:
            rows = self.conn.execute(
                "SELECT item_id, failures FROM item_failures"
            ).fetchall()
        except sqlite3.Error as e:
            raise ResumeStoreError(f"진행 기록을 읽을 수 없습니다: {e}") from e
        return {str(item_id): int(n) for item_id, n in rows}

    def give_up_item_ids(self, threshold: int) -> set[str]:
        """실행 누적 실패가 threshold 이상인 상품 — 더 이상 재시도하지 않는다."""
        limit = max(1, int(threshold))
        return {i for i, n in self.item_failure_counts().items() if n >= limit}

    def mark_finished(self) -> None:
        """전체 수집 완료 봉인."""
        self._set_meta("status", STATUS_FINISHED)

    # ── 아카이브 (처음부터 새로 수집) ───────────────────────────────────
    def archive(self) -> Path | None:
        """기존 resume.sqlite3 파일을 타임스탬프 아카이브로 보관."""
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
            raise ResumeStoreError(f"진행 파일 보관 실패: {e}") from e
        return target


ResumeStore = AliexpressResumeStore


def peek_resume(run_dir: Path | str) -> str | None:
    """폴더의 미완료 진행 안내문을 읽어온다. 없으면 None."""
    p = Path(run_dir)
    path = p if p.suffix == ".sqlite3" else p / "resume.sqlite3"
    if not path.exists():
        return None
    try:
        with AliexpressResumeStore(path) as store:
            if not store.has_state() or store.status == STATUS_FINISHED:
                return None
            return store.preview_text()
    except ResumeStoreError:
        return None
