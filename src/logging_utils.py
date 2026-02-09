"""Shared logging utilities for Presence Tracker scripts."""

from __future__ import annotations

import logging
import os
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR_ENV = "LOG_DIR"
_LOG_MAX_LINES_ENV = "LOG_MAX_LINES"
_DEFAULT_MAX_LINES = 1000


def _to_absolute(path_str: str) -> Path:
    path = Path(path_str)
    if path.is_absolute():
        return path
    return (_PROJECT_ROOT / path).resolve()


def get_logs_dir() -> Path:
    """Return the configured logs directory, ensuring it exists."""

    configured = os.getenv(_LOG_DIR_ENV)
    target = _to_absolute(configured) if configured else _DEFAULT_LOG_DIR
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_log_path(filename: str | Path) -> Path:
    """Return the absolute path for a log file within the log directory."""

    filename_path = Path(filename)
    if filename_path.is_absolute():
        return filename_path
    return get_logs_dir() / filename_path.name


def _get_max_lines(max_lines: int | None = None) -> int:
    if max_lines is not None and max_lines > 0:
        return max_lines
    try:
        env_value = int(os.getenv(_LOG_MAX_LINES_ENV, ""))
        return env_value if env_value > 0 else _DEFAULT_MAX_LINES
    except ValueError:
        return _DEFAULT_MAX_LINES


class LineCappedFileHandler(logging.FileHandler):
    """File handler that restarts the log after a maximum line count."""

    def __init__(
        self,
        filename: str | Path,
        *,
        max_lines: int | None = None,
        mode: str = "a",
        encoding: str | None = "utf-8",
        delay: bool = False,
    ) -> None:
        log_path = get_log_path(filename)
        self._line_count = 0
        self._max_lines = _get_max_lines(max_lines)
        super().__init__(log_path, mode=mode, encoding=encoding, delay=delay)
        self._purge_backup_files()
        self._line_count = self._count_existing_lines()

    def _count_existing_lines(self) -> int:
        log_path = Path(self.baseFilename)
        if not log_path.exists():
            return 0
        try:
            with log_path.open("r", encoding=self.encoding or "utf-8", errors="ignore") as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0

    def _reset_log_file(self) -> None:
        try:
            self.close()
            Path(self.baseFilename).unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self._line_count = 0
            self.stream = self._open()

    def _purge_backup_files(self) -> None:
        log_path = Path(self.baseFilename)
        backup_pattern = f"{log_path.name}.*"
        for candidate in log_path.parent.glob(backup_pattern):
            try:
                candidate.unlink()
            except OSError:
                continue

    def emit(self, record: logging.LogRecord) -> None:
        if self._line_count >= self._max_lines:
            self._reset_log_file()

        super().emit(record)
        self._line_count += 1


def configure_logger(
    logger: logging.Logger,
    *,
    log_filename: str,
    level: int = logging.INFO,
    formatter: logging.Formatter | None = None,
) -> logging.Logger:
    """Attach a capped file handler and stream handler to the given logger."""

    if formatter is None:
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    if logger.handlers:
        return logger

    logger.setLevel(level)

    file_handler = LineCappedFileHandler(log_filename)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger


def configure_root_logger(log_filename: str, level: int = logging.INFO) -> None:
    """Ensure the root logger writes to the shared log directory."""

    configure_logger(logging.getLogger(), log_filename=log_filename, level=level)
