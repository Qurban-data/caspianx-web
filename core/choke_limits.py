"""Realistic choke bounds for every slider / What-If solver.

Rule: lower = 15 %, upper = min(65 %, observed_max + 5 %). A choke is never offered above 65 % unless the well's
own data already shows it operating there; in that case the ceiling is the observed maximum (no extrapolation
beyond what was recorded) and the setting is flagged. 90-100 % is never offered.
"""
from __future__ import annotations

import numpy as np

CHOKE_FLOOR = 15.0
CHOKE_CEIL = 65.0
MARGIN = 5.0


def choke_bounds(observed) -> tuple[float, float, str]:
    """(lo, hi, note) from the well's observed choke openings (shut-in zeros ignored)."""
    v = np.asarray(observed, float)
    v = v[np.isfinite(v) & (v > 0.5)]
    if not len(v):
        return CHOKE_FLOOR, CHOKE_CEIL, "no observed choke opening: generic 15-65 % range"
    mx = float(v.max())
    if mx > CHOKE_CEIL:
        return CHOKE_FLOOR, round(mx, 1), (f"well recorded at up to {mx:.1f} % (above the 65 % design cap): ceiling = "
                                           "observed maximum, no extrapolation")
    hi = min(CHOKE_CEIL, mx + MARGIN)
    return CHOKE_FLOOR, round(max(hi, CHOKE_FLOOR + 1), 1), f"ceiling = min(65 %, observed max {mx:.1f} % + 5 %)"


def flag(choke: float, observed) -> str:
    """Empty if inside the observed operating range, else a plain warning."""
    v = np.asarray(observed, float)
    v = v[np.isfinite(v) & (v > 0.5)]
    if len(v) and not (v.min() - 1e-9 <= choke <= v.max() + 1e-9):
        return f"choke {choke:.1f} % is outside the recorded range {v.min():.1f}-{v.max():.1f} %: extrapolation"
    return ""
