"""Durable request accounting helpers for historical evaluation runs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def atomic_json(path: Path, payload: Any) -> None:
    """Atomically replace a UTF-8 JSON file in its destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class TwelveDataRequestLedger:
    """Persist per-request Twelve Data usage, including requests in flight at interruption."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[dict[str, Any]] = []
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    self.rows = [row for row in payload if isinstance(row, dict)]
            except (OSError, ValueError):
                # Keep the old ledger untouched if it cannot be parsed; callers
                # can diagnose instead of silently replacing historical records.
                raise RuntimeError("TWELVE_DATA_LEDGER_INVALID")
        else:
            atomic_json(path, self.rows)

    def record(self, event: dict[str, Any]) -> None:
        """Append a start event or finalize the matching request row atomically."""
        if event.get("event") == "START":
            row = {key: value for key, value in event.items() if key != "event"}
            self.rows.append(row)
        elif event.get("event") == "FINISH":
            target = next((row for row in reversed(self.rows) if row.get("request_id") == event.get("request_id")), None)
            if target:
                target["result"] = event.get("result", "UNKNOWN")
                target["completed_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.path, self.rows)

    def totals(self) -> dict[str, int]:
        """Aggregate sent-request credits by historical date."""
        result: dict[str, int] = {}
        for row in self.rows:
            day = str(row.get("historical_date", ""))
            result[day] = result.get(day, 0) + int(row.get("credits", 0) or 0)
        return result
