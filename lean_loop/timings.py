from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from lean_loop.jsonutil import atomic_write_json, read_json, utc_now


class TimingRecorder:
    def __init__(self, path: Path, *, resume: bool = False) -> None:
        self.path = path
        self.started_at = utc_now()
        self.started_clock = time.perf_counter()
        self.spans: list[dict[str, Any]] = []
        self.previous_total_seconds = 0.0
        self.status = "running"
        self.completed_at: str | None = None
        if resume and path.is_file():
            previous = read_json(path)
            self.started_at = str(previous.get("started_at") or self.started_at)
            self.spans = list(previous.get("spans") or [])
            self.previous_total_seconds = float(previous.get("total_seconds") or 0.0)
        self._write()

    @contextmanager
    def measure(self, phase: str, attempt: int | None = None) -> Iterator[None]:
        started_at = utc_now()
        started = time.perf_counter()
        outcome = "ok"
        try:
            yield
        except BaseException:
            outcome = "error"
            raise
        finally:
            self.spans.append(
                {
                    "phase": phase,
                    "attempt": attempt,
                    "started_at": started_at,
                    "duration_seconds": round(time.perf_counter() - started, 6),
                    "outcome": outcome,
                }
            )
            self._write()

    def finish(self, status: str) -> dict[str, Any]:
        self.status = status
        self.completed_at = utc_now()
        self._write()
        return self.summary()

    def summary(self) -> dict[str, Any]:
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for span in self.spans:
            phase = str(span["phase"])
            totals[phase] = totals.get(phase, 0.0) + float(span["duration_seconds"])
            counts[phase] = counts.get(phase, 0) + 1
        return {
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_seconds": round(
                self.previous_total_seconds + time.perf_counter() - self.started_clock,
                6,
            ),
            "phase_seconds": {key: round(value, 6) for key, value in totals.items()},
            "phase_counts": counts,
        }

    def _write(self) -> None:
        atomic_write_json(
            self.path,
            {
                **self.summary(),
                "spans": self.spans,
            },
        )
