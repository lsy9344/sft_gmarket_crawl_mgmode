"""Coupang 결과 저장: JSON/CSV 원자적 쓰기 (WORK_ORDER §8)."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.models.coupang_records import RECORD_FIELDS, CoupangRunConfig

_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")


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

    def save(self, records: list[dict], partial: bool = False) -> tuple[str | None, str | None]:
        if not records and not partial:
            out_dir = Path(self.config.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            prefix = self._prefix()
            json_path = str(out_dir / f"{prefix}.json")
            csv_path = str(out_dir / f"{prefix}.csv")
            self._atomic_write_json(json_path, [])
            try:
                self._atomic_write_csv(csv_path, [])
            except Exception as e:
                raise CsvWriteError(json_path, e) from e
            return json_path, csv_path

        if not records:
            return None, None

        out_dir = Path(self.config.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = self._prefix()
        suffix = "_partial" if partial else ""
        json_path = str(out_dir / f"{prefix}{suffix}.json")
        csv_path = str(out_dir / f"{prefix}{suffix}.csv")

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
