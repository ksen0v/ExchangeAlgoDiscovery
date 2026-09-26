"""Tags tape prints that repeat an earlier size on the same stream and side.

A TWAP / iceberg bot sends many orders of nearly the same USD size. Every print
is put into a log-size bin; prints within one bin of an active group's anchor
join that group. Once a group has `min_count` prints inside `window` seconds,
its prints carry `rep` (how many so far) and `grp` (a stable id the UI turns
into a colour), so the algorithm stands out in the tape by eye.
"""
import math
from collections import deque
from dataclasses import dataclass, field

from app.models import Trade


@dataclass
class _Group:
    gid: int
    times: deque = field(default_factory=deque)


class RepeatTracker:
    def __init__(self, tolerance: float = 0.03, window: float = 60.0, min_count: int = 3, min_usd: float = 100.0):
        self.tolerance = tolerance
        self.window = window
        self.min_count = min_count
        self.min_usd = min_usd
        self._groups: dict[tuple[str, str], dict[int, _Group]] = {}  # (key, side) -> anchor bin -> group
        self._swept: dict[tuple[str, str], float] = {}
        self._next_id = 1

    def reset(self) -> None:
        self._groups = {}
        self._swept = {}

    def observe(self, key: str, t: Trade) -> tuple[int, int] | None:
        """Register a print; returns (grp, rep) when it belongs to a repeating group."""
        if t.side not in ("buy", "sell") or t.usd < max(self.min_usd, 1e-9):
            return None
        gk = (key, t.side)
        groups = self._groups.setdefault(gk, {})
        cutoff = t.ts - self.window
        if t.ts - self._swept.get(gk, 0.0) > 5:
            self._swept[gk] = t.ts
            for anchor in [a for a, g in groups.items() if not g.times or g.times[-1] < cutoff]:
                del groups[anchor]
        b = round(math.log(t.usd) / math.log1p(self.tolerance))
        near = [g for a in (b, b - 1, b + 1) if (g := groups.get(a)) and g.times and g.times[-1] >= cutoff]
        if near:
            g = max(near, key=lambda x: len(x.times))
        else:
            g = groups[b] = _Group(self._next_id)
            self._next_id += 1
        g.times.append(t.ts)
        while g.times and g.times[0] < cutoff:
            g.times.popleft()
        n = len(g.times)
        return (g.gid, n) if n >= self.min_count else None
