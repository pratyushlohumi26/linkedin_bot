#!/usr/bin/env python3
"""Simple JSONL telemetry logger for pipeline evolution."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TelemetryLogger:
    """Persists lightweight pipeline events for later optimization."""

    def __init__(self, path: str):
        self._path = Path(path)

    def record(self, event_type: str, **fields: Any) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event_type,
            **fields,
        }
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
