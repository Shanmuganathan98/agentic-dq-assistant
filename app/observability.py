"""Structured JSON logging + a tiny Prometheus-format metrics registry (stdlib only)."""
import json
import logging
import sys
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_counters: dict = {}
_log = logging.getLogger("dq")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "level": record.levelname, "event": record.getMessage()}
        out.update(getattr(record, "ctx", {}))
        return json.dumps(out, default=str)


def configure_logging() -> None:
    if _log.handlers:
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    _log.addHandler(h)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def log_event(event: str, **fields) -> None:
    _log.info(event, extra={"ctx": fields})


def inc(name: str, value: float = 1.0, **labels) -> None:
    key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
    with _lock:
        _counters[key] = _counters.get(key, 0.0) + value


def render() -> str:
    with _lock:
        items = sorted(_counters.items())
    lines = []
    for (name, labels), v in items:
        lab = "{" + ",".join(f'{k}="{val}"' for k, val in labels) + "}" if labels else ""
        lines.append(f"{name}{lab} {v}")
    return "\n".join(lines) + "\n"
