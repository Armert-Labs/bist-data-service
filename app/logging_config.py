"""Yapisal (JSON) loglama + istek kimligi (request-id) baglami.

Uretimde JSON loglar (Loki/ELK/CloudWatch dostu); LOG_JSON=false ile
gelistirmede okunabilir metin. Her HTTP istegine bir request-id atanir ve
tum log satirlarina eklenir (dagitik izleme icin).
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar

from .config import settings

# JsonFormatter konumu python-json-logger surumune gore degisir.
try:  # v3+
    from pythonjsonlogger.json import JsonFormatter
except ImportError:  # v2
    from pythonjsonlogger.jsonlogger import JsonFormatter

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def setup_logging() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())

    if settings.log_json:
        formatter: logging.Formatter = JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(request_id)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s"
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Gurultu kaynaklarini kis.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def set_request_id(value: str) -> None:
    request_id_ctx.set(value)


def get_request_id() -> str:
    return request_id_ctx.get()
