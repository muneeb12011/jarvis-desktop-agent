"""
Logging: timestamped lines to stdout and to a rotating file in backend/logs.
"""

import logging
import logging.handlers

from . import config

LOG_DIR = config.DATA_DIR / "logs"
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_jarvis_configured", False):
        return
    LOG_DIR.mkdir(exist_ok=True)
    root.setLevel(config.LOG_LEVEL)
    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "jarvis.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if config.LOG_LEVEL != "DEBUG":
        for noisy in ("uvicorn.access", "urllib3", "httpx", "comtypes", "PIL", "faster_whisper"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    root._jarvis_configured = True  # type: ignore[attr-defined]
