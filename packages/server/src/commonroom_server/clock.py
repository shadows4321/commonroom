"""The single world clock. World time is counted in minutes since day 1, 00:00."""

from __future__ import annotations

import math

PHASES = (
    (0, "night"),
    (5 * 60, "dawn"),
    (7 * 60, "morning"),
    (12 * 60, "afternoon"),
    (17 * 60, "evening"),
    (21 * 60, "night"),
)


def phase_of(minute_of_day: int) -> str:
    current = PHASES[0][1]
    for start, name in PHASES:
        if minute_of_day >= start:
            current = name
    return current


def clock_view(minutes: float) -> dict:
    whole = int(minutes)
    day, minute_of_day = whole // 1440 + 1, whole % 1440
    hour, minute = divmod(minute_of_day, 60)
    phase = phase_of(minute_of_day)
    return {
        "minutes": whole,
        "day": day,
        "hour": hour,
        "minute": minute,
        "time": f"{hour:02d}:{minute:02d}",
        "phase": phase,
        "label": f"Day {day}, {hour:02d}:{minute:02d} ({phase})",
    }


def crossings(old: float, new: float, minute_of_day: int) -> bool:
    """True if a given minute of the day falls in the interval (old, new]."""
    if new <= old:
        return False
    first = math.floor((old - minute_of_day) / 1440 + 1) * 1440 + minute_of_day
    return first <= new


class WorldClock:
    def __init__(self, minutes: float, minutes_per_real_second: float, tick_minutes: int):
        self.minutes = float(minutes)
        self.scale = minutes_per_real_second
        self.tick_minutes = max(1, tick_minutes)

    def view(self) -> dict:
        return clock_view(self.minutes)

    def advance(self, real_seconds: float) -> tuple[float, float, bool]:
        """Move time forward. Returns (old, new, crossed_tick_boundary)."""
        old = self.minutes
        self.minutes = old + real_seconds * self.scale
        crossed = math.floor(self.minutes / self.tick_minutes) > math.floor(old / self.tick_minutes)
        return old, self.minutes, crossed
