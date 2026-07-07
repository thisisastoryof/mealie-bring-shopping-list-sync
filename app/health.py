"""Lightweight in-process health state for the /health endpoint and Docker healthcheck."""
from dataclasses import dataclass, field
from datetime import datetime

from app.utils import utcnow


@dataclass
class HealthState:
    started_at: datetime = field(default_factory=utcnow)
    last_cycle_at: datetime | None = None
    last_cycle_ok: bool = False
    last_error: str | None = None
    cycles: int = 0

    def record_success(self) -> None:
        self.last_cycle_at = utcnow()
        self.last_cycle_ok = True
        self.last_error = None
        self.cycles += 1

    def record_failure(self, error: str) -> None:
        self.last_cycle_at = utcnow()
        self.last_cycle_ok = False
        self.last_error = error
        self.cycles += 1

    def as_dict(self) -> dict:
        return {
            "status": "ok" if self.last_cycle_ok or self.last_cycle_at is None else "degraded",
            "started_at": self.started_at.isoformat(),
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "last_cycle_ok": self.last_cycle_ok,
            "last_error": self.last_error,
            "cycles": self.cycles,
        }


health = HealthState()
