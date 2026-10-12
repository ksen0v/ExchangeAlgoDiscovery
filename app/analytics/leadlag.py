"""Who moves first: two price series on a common 1-second grid (М8; the base of М11 later)."""
import math


def returns(prices: list[float | None]) -> list[float]:
    out = []
    prev = None
    for p in prices:
        out.append(math.log(p / prev) if p and prev else 0.0)
        prev = p or prev
    return out


def xcorr_lead(a: list[float | None], b: list[float | None], max_lag: int) -> tuple[int, float | None]:
    """Lag (seconds) at which a's returns best match b's later returns, and that correlation.

    A positive lag means `a` moves first: a[t] correlates with b[t + lag].
    """
    ra, rb = returns(a), returns(b)
    n = len(ra)
    best, best_c = 0, None
    for lag in range(-max_lag, max_lag + 1):
        xs = ra[max(0, -lag): n - max(0, lag)]
        ys = rb[max(0, lag): n - max(0, -lag)]
        m = len(xs)
        if m < 30:
            continue
        mx, my = sum(xs) / m, sum(ys) / m
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        if vx <= 0 or vy <= 0:
            continue
        c = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(vx * vy)
        if best_c is None or c > best_c:
            best, best_c = lag, c
    return best, best_c


def half_cross(series: list[tuple[float, float]], p0: float, p1: float) -> float | None:
    """When the series first went half of the way from p0 to p1."""
    if not p0 or p1 == p0:
        return None
    mid = p0 + (p1 - p0) / 2
    up = p1 > p0
    for t, p in series:
        if p and (p >= mid if up else p <= mid):
            return t
    return None
