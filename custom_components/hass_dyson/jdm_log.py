"""Flight recorder for the RB05 "jdm" MQTT protocol.

The robot's second protocol (``command/jdm`` / ``status/jdm``, discovered 30 sep
2026) carries setting writes, map-rule changes, voice-pack downloads and robot
broadcasts that the normal state stream does not show. This writes every such
message as one JSON line to a size-capped rotating file so it can be inspected
after the fact (what happened right before ``prefer_on`` flipped, which command
a new app screen sends, ...). The constant app polling is left out so unknown
messages stay easy to spot.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any

MAX_BYTES = 1_000_000
BACKUP_COUNT = 3
_MAX_STRING = 1500
_MAX_LIST = 200

# Request/response pairs the apps repeat every few seconds.
_NOISE_METHODS = frozenset(
    {
        "prop.get",
        "service.get_preference",
        "service.get_order",
        "service.get_map_list",
    }
)
# Broadcasts the robot repeats continuously (battery, dock drying timer).
_NOISE_POST_KEYS = frozenset({"quantity", "work_time"})

_recorders: dict[str, JdmFlightRecorder] = {}


def is_noise(data: dict[str, Any]) -> bool:
    """Whether a jdm message is routine polling or a repeating broadcast."""
    method = data.get("method")
    if method in _NOISE_METHODS:
        return True
    if method == "prop.post":
        params = data.get("params")
        return (
            isinstance(params, dict)
            and bool(params)
            and set(params) <= _NOISE_POST_KEYS
        )
    return False


def _shorten(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "..."
    if isinstance(value, dict):
        return {k: _shorten(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_shorten(v) for v in value[:_MAX_LIST]]
        if len(value) > _MAX_LIST:
            items.append(f"...+{len(value) - _MAX_LIST}")
        return items
    return value


class JdmFlightRecorder:
    """Appends jdm messages to a rotating JSON-lines file."""

    def __init__(
        self,
        path: str,
        max_bytes: int = MAX_BYTES,
        backup_count: int = BACKUP_COUNT,
    ) -> None:
        self._logger = logging.getLogger(f"hass_dyson.jdm_flight_recorder.{path}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def record(self, serial: str, topic: str, data: dict[str, Any]) -> bool:
        """Write one message; returns False when it was filtered as noise."""
        if is_noise(data):
            return False
        self._logger.info(
            json.dumps(
                {
                    "t": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "serial": serial,
                    "topic": topic,
                    "data": _shorten(data),
                },
                ensure_ascii=False,
                default=str,
            )
        )
        return True


def get_recorder(path: str) -> JdmFlightRecorder:
    """Return the shared recorder for a file (one per path, many robots)."""
    recorder = _recorders.get(path)
    if recorder is None:
        recorder = _recorders[path] = JdmFlightRecorder(path)
    return recorder
