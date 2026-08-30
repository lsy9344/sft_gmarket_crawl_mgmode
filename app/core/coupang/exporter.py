"""Coupang 결과 저장: JSON/CSV 원자적 쓰기 (WORK_ORDER §8)."""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.models.coupang_records import RECORD_FIELDS, CoupangRunConfig

_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")
_EXPORT_LOCK_FILENAME = ".coupang_export.lock"
_save_guard = threading.Lock()


@contextmanager
def _exclusive_export_lock(out_dir: Path):
    """JSON/CSV 경로 선택과 저장을 프로세스 간에도 직렬화한다."""
    lock_path = out_dir / _EXPORT_LOCK_FILENAME
    try:
        lock_file = open(lock_path, "a+b")  # noqa: SIM115 - 잠금 획득 실패와 저장 중 오류를 구분해 닫는다
    except OSError as e:
        raise OSError(f"Coupang 결과 잠금 파일을 만들 수 없습니다: {lock_path}: {e}") from e

    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        lock_file.close()
        raise OSError(
            f"다른 프로세스가 '{out_dir}'에 Coupang 결과를 저장 중입니다. "
            "저장이 끝난 뒤 다시 시도하세요."
        ) from e
    try:
        yield
    finally:
        lock_file.close()


class CsvWriteError(Exception):
    """CSV 쓰기 실패 — JSON은 이미 저장됨."""

    def __init__(self, json_path: str, cause: Exception) -> None:
        super().__init__(f"CSV 쓰기 실패: {type(cause).__name__}: {cause}")
        self.json_path = json_path
        self.cause = cause


def _csv_safe(value: object) -> str:
    text = "" if value is None else str(value)
    if text and text[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + text
    return text


class CoupangExporter:
    def __init__(self, config: CoupangRunConfig) -> None:
        self.config = config

    def _prefix(self) -> str:
        if self.config.output_prefix:
            return self.config.output_prefix
        local_now = datetime.now(timezone.utc).astimezone()
        return f"coupang_omp_sellers_{local_now.strftime('%Y%m%d_%H%M%S')}"

    @staticmethod
    def _available_paths(out_dir: Path, prefix: str, partial: bool) -> tuple[str, str]:
        """기존 결과를 덮어쓰지 않는 JSON/CSV 한 쌍의 경로를 고른다."""
        suffix = "_partial" if partial else ""
        index = 1
        while True:
            numbered = "" if index == 1 else f"_{index}"
            stem = f"{prefix}{numbered}{suffix}"
            json_path = out_dir / f"{stem}.json"
            csv_path = out_dir / f"{stem}.csv"
            if not json_path.exists() and not csv_path.exists():
                return str(json_path), str(csv_path)
            index += 1

    def save(self, records: list[dict], partial: bool = False) -> tuple[str | None, str | None]:
        if not records and partial:
            return None, None

        out_dir = Path(self.config.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = self._prefix()
        # 파일명 선택부터 JSON/CSV 승격까지 같은 프로세스의 복수 스레드를
        # 직렬화한다. 프로세스 간 경쟁은 crawler 시작 시 출력 디렉터리 잠금으로
        # 차단하고, Exporter를 직접 쓰는 경로도 파일 잠금으로 한 번 더 보호한다.
        with _save_guard, _exclusive_export_lock(out_dir):
            json_path, csv_path = self._available_paths(out_dir, prefix, partial)
            self._atomic_write_json(json_path, records)
            try:
                self._atomic_write_csv(csv_path, records)
            except Exception as e:
                raise CsvWriteError(json_path, e) from e
            return json_path, csv_path

    def _atomic_write_json(self, path: str, records: list[dict]) -> None:
        dir_path = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _atomic_write_csv(self, path: str, records: list[dict]) -> None:
        dir_path = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(RECORD_FIELDS))
                writer.writeheader()
                for rec in records:
                    writer.writerow({k: _csv_safe(v) for k, v in rec.items()})
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
