from __future__ import annotations

import json
import logging
import sys
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "run_id": getattr(record, "run_id", ""),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    verbose: bool = False,
    log_dir: Path | None = None,
    json_format: bool = False,
    run_id: str | None = None,
) -> str:
    """Configure console + optional rotating-file logging.

    Returns the run correlation id, which is stamped on every log line so a
    server run's logs can be traced end to end. Safe to call more than once
    (handlers are reset each time).
    """
    run_id = run_id or uuid.uuid4().hex[:8]
    level = logging.DEBUG if verbose else logging.INFO

    if json_format:
        formatter: logging.Formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(run_id)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    run_filter = _RunIdFilter(run_id)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(run_filter)
    root.addHandler(console)

    if log_dir is not None:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path / "tag_creator.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(run_filter)
        root.addHandler(file_handler)

    return run_id
