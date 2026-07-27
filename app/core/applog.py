"""세션 밖에서도 진단 가능하도록 UI 로그를 회전 파일에 병행 기록한다.

- 로그 위치: Windows %LOCALAPPDATA%/SellerCollector/logs, 그 외 ~/.local/share/SellerCollector/logs
- setup_file_logging() 은 실패해도 앱 기동을 막지 않는다(파일 로그는 부가 기능).
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "seller_collector"

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_file_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "SellerCollector" / "logs"
    return Path.home() / ".local" / "share" / "SellerCollector" / "logs"


def setup_file_logging() -> Path | None:
    """회전 파일 핸들러를 설치하고 로그 파일 경로를 반환한다. 실패 시 None."""
    logger = get_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        for h in logger.handlers:
            if isinstance(h, RotatingFileHandler):
                return Path(h.baseFilename)
    try:
        log_dir = log_file_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "seller_collector.log"
        handler = RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
        return path
    except OSError:
        return None


def log_line(msg: str) -> None:
    """UI 로그 한 줄을 파일에도 기록한다. 핸들러 미설치 시 조용히 무시된다."""
    get_logger().info(msg)
