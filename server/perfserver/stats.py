"""Per-metric statistics computed once at upload time."""
from collections import defaultdict


def percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def slope_per_hour(ts_ms, vals):
    n = len(vals)
    # Trend over a few minutes is noise; only report for runs >= 5 min
    if n < 3 or ts_ms[-1] - ts_ms[0] < 5 * 60 * 1000:
        return None
    xs = [(t - ts_ms[0]) / 3_600_000 for t in ts_ms]
    mx, my = sum(xs) / n, sum(vals) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, vals)) / den


def compute(points):
    """points: [[ts, metric, value], ...] -> {metric: {...stats}}"""
    by = defaultdict(list)
    for ts, m, v in points:
        if v is not None:
            by[m].append((ts, float(v)))
    out = {}
    for m, rows in by.items():
        rows.sort()
        vals = [v for _, v in rows]
        s = sorted(vals)
        r = lambda x: None if x is None else round(x, 3)
        out[m] = {
            "count": len(vals), "avg": r(sum(vals) / len(vals)), "min": r(s[0]), "max": r(s[-1]),
            "p50": r(percentile(s, .5)), "p90": r(percentile(s, .9)), "p99": r(percentile(s, .99)),
            "first": r(vals[0]), "last": r(vals[-1]),
            "slope_per_hour": r(slope_per_hour([t for t, _ in rows], vals)),
        }
    return out
