"""Robust statistics: median, MAD, percentiles, robust z-score (ТЗ 3.4)."""
from bisect import bisect_left, bisect_right
from statistics import median

MAD_SCALE = 1.4826  # MAD -> sigma for normally distributed data


def percentile(sorted_vals: list[float], q: float) -> float | None:
    """q in 0..100, linear interpolation between neighbours."""
    n = len(sorted_vals)
    if not n:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def robust_stats(values: list[float]) -> dict | None:
    """{n, median, mad, p10, p50, p90, p99} or None without data."""
    vals = sorted(v for v in values if v is not None and v == v)
    if not vals:
        return None
    med = median(vals)
    mad = median(sorted(abs(v - med) for v in vals))
    return {
        "n": len(vals),
        "median": med,
        "mad": mad,
        "p10": percentile(vals, 10),
        "p50": percentile(vals, 50),
        "p90": percentile(vals, 90),
        "p99": percentile(vals, 99),
        "sorted": vals,
    }


def robust_z(x: float | None, med: float | None, mad: float | None) -> float | None:
    """(x - median) / (1.4826 * MAD); None when the spread is zero (all values equal)."""
    if x is None or med is None or mad is None:
        return None
    scale = MAD_SCALE * mad
    if scale <= 1e-12:
        return None
    return (x - med) / scale


def pct_rank(x: float, sorted_vals: list[float]) -> float | None:
    """Share of the history below x, 0..100."""
    if not sorted_vals:
        return None
    lo, hi = bisect_left(sorted_vals, x), bisect_right(sorted_vals, x)
    return 100.0 * (lo + hi) / 2 / len(sorted_vals)


def z_of(x: float | None, st: dict | None) -> float | None:
    return robust_z(x, st["median"], st["mad"]) if st and x is not None else None
