"""Engineer alert rail: deterministic, explainable alerts from one pipeline run.

  high skin      Horner skin >= 2 (damaged) / >= 5 (severe); theoretical oil gain dQ if s -> 0 (PSS inflow, constant
                 drawdown, from core.pta_interpret) and an ILLUSTRATIVE value dQ x oil price (price set by the user)
  sensor         per well over the last 30 loaded days: a jump in WHP, BHP or gas lift that is > 6 robust sigma
                 (median / MAD) AND >= 10 % from the median AND not within +-1 day of a choke change (operational),
                 a flat-lined sensor (identical value 7 days running), missing sensor days
  erosional      wellhead mixture velocity / API RP 14E erosional velocity >= 0.8 (warn) or >= 1.0 (alert)
  data quality   sign flips / unit conversions fixed in the well tests (e.g. GL-09 gas lift -1010.5)
Nothing is estimated beyond these rules; each alert says where its number comes from.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SENSORS = {"whp_psia": "WHP", "dh_pressure_psia": "BHP", "gas_lift_rate_mscfd": "gas lift"}


def _robust_z(x: pd.Series) -> pd.Series:
    med = x.median()
    mad = (x - med).abs().median() * 1.4826
    return (x - med) / mad if mad and np.isfinite(mad) else x * 0


def build_alerts(R: dict, oil_price: float = 0.0, window_days: int = 30) -> pd.DataFrame:
    rows = []
    for r, _ in R.get("pta", []):
        s = r.get("horner_skin")
        if s is None or r.get("pta_not_reported") or not np.isfinite(s) or s < 2:
            continue
        dq = float((r.get("interp") or {}).get("dq_stb_d", np.nan))
        val = dq * oil_price if (oil_price and np.isfinite(dq)) else np.nan
        rows.append({"severity": "alert" if s >= 5 else "warn", "well": r["well_name"], "type": "high skin",
                     "message": f"skin {s:+.2f} ({'severe damage' if s >= 5 else 'damaged'}); theoretical gain if s -> 0: "
                                f"{dq:,.0f} STB/d" + (f", illustrative {val:,.0f} $/d at {oil_price:,.0f} $/bbl" if np.isfinite(val) else ""),
                     "value_usd_d": val, "rank": s})
    P = R.get("P")
    if P is not None and len(P):
        P = P.assign(date=pd.to_datetime(P["date"]))
        recent = P[P.date > P.date.max() - pd.Timedelta(days=window_days)]
        for w, g in recent.groupby("well_name"):
            g = g.sort_values("date")
            flowing = g[g.uptime > 0.5]
            for c, lab in SENSORS.items():
                if c not in g:
                    continue
                miss = int(g[c].isna().sum())
                if miss:
                    rows.append({"severity": "warn", "well": w, "type": "sensor", "rank": miss,
                                 "message": f"{lab}: {miss} of the last {len(g)} days missing", "value_usd_d": np.nan})
                x = flowing[c].dropna()
                if len(x) >= 10:
                    z = _robust_z(x)
                    rel = (x / x.median() - 1).abs()
                    ch = flowing["choke_opening_pct"] if "choke_opening_pct" in flowing else pd.Series(0, index=flowing.index)
                    moved = ch.diff().abs().fillna(0).gt(0.5)
                    near_choke = moved | moved.shift(1, fill_value=False) | moved.shift(-1, fill_value=False)
                    cand = (z.abs() > 6) & (rel >= 0.10) & ~near_choke.reindex(x.index, fill_value=False)
                    if cand.any():
                        d = z[cand].abs().idxmax()
                        rows.append({"severity": "warn", "well": w, "type": "sensor", "rank": float(z.abs().max()),
                                     "message": f"{lab} jump on {flowing.loc[d, 'date']:%Y-%m-%d}: {x.loc[d]:,.1f} vs median "
                                                f"{x.median():,.1f} ({z.abs().max():.0f} robust sigma)", "value_usd_d": np.nan})
                    run = (x.diff() == 0).astype(int)
                    longest = int(run.groupby((run == 0).cumsum()).sum().max()) + 1 if len(run) else 0
                    if longest >= 7:
                        rows.append({"severity": "warn", "well": w, "type": "sensor", "rank": longest,
                                     "message": f"{lab} flat-lined for {longest} consecutive days (frozen transmitter?)",
                                     "value_usd_d": np.nan})
    H = R.get("hydraulics")
    if H is not None and len(H):
        for _, h in H[H.erosional_ratio >= 0.8].iterrows():
            rows.append({"severity": "alert" if h.erosional_ratio >= 1 else "warn", "well": h.well_name, "type": "erosional",
                         "rank": float(h.erosional_ratio), "value_usd_d": np.nan,
                         "message": f"wellhead mixture velocity {h.v_mixture_ft_s:.0f} ft/s = {h.erosional_ratio:.2f} x the "
                                    f"API RP 14E erosional velocity ({h.v_erosional_ft_s:.0f} ft/s)"})
    q = R.get("qc_log")
    if q is not None and len(q):
        for _, x in q.iterrows():
            rows.append({"severity": "info", "well": x.well, "type": "data quality", "rank": 0, "value_usd_d": np.nan,
                         "message": f"well test {x.column}: {x.issue} ({x.old:g} -> {x.new:.1f}) fixed before training"})
    A = pd.DataFrame(rows, columns=["severity", "well", "type", "message", "value_usd_d", "rank"])
    order = {"alert": 0, "warn": 1, "info": 2}
    return A.assign(o=A.severity.map(order)).sort_values(["o", "rank"], ascending=[True, False]).drop(columns=["o", "rank"])
