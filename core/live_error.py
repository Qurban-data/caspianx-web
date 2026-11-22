"""Live error rate on an uploaded dataset that contains well tests: daily submitted VFM rate on the test day vs the
separator test rate. Only tests that fall on a predicted, flowing day are scored; nothing is filled."""
from __future__ import annotations

import numpy as np
import pandas as pd

RATES = {"oil": "oil_rate_stbd", "water": "water_rate_stbd", "gas": "gas_rate_mscfd"}


def live_error(V: pd.DataFrame, tests: pd.DataFrame | None) -> pd.DataFrame:
    """V: daily rates (date, well_name, three rates). tests: QC-ed tests with test_mid or test_start.
    Returns one row per phase: n, MAPE_%, WAPE_%, MAE, R2 (empty frame if nothing can be scored)."""
    if tests is None or V is None or not len(tests) or not len(V):
        return pd.DataFrame()
    t = tests.copy()
    tcol = "test_mid" if "test_mid" in t else "test_start"
    t["date"] = pd.to_datetime(t[tcol]).dt.normalize()
    v = V.assign(date=pd.to_datetime(V["date"]).dt.normalize())
    m = t.merge(v, on=["well_name", "date"], suffixes=("_test", "_pred"))
    rows = []
    for ph, c in RATES.items():
        if f"{c}_test" not in m or f"{c}_pred" not in m:
            continue
        a, p = m[f"{c}_test"].astype(float).values, m[f"{c}_pred"].astype(float).values
        ok = np.isfinite(a) & np.isfinite(p) & (a > 0) & (p > 0)
        a, p = a[ok], p[ok]
        if not len(a):
            continue
        e = p - a
        ss = ((a - a.mean()) ** 2).sum()
        rows.append({"phase": ph, "n": int(len(a)), "MAPE_%": float(np.mean(np.abs(e) / a) * 100),
                     "WAPE_%": float(np.abs(e).sum() / a.sum() * 100), "MAE": float(np.abs(e).mean()),
                     "R2": float(1 - (e ** 2).sum() / ss) if ss > 0 else float("nan")})
    return pd.DataFrame(rows)
