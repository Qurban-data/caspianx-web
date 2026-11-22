"""
Operating-point what-if solver (adapter: the validated VFM is used as-is, never refitted here).

Why: the VFM computes rate from MEASURED wellhead pressure, q = C_w (choke/100)^2 sqrt(P_wh - P_ds) e^r.
Holding P_wh fixed while the choke or lift gas changes is unphysical (opening a choke lowers P_wh). A
consistent operating point needs the well's inflow and lift behaviour as well (nodal analysis, simplified):

    choke  (VFM)      q      = VFM(P_wh, P_ds, choke, q_gl, P_bh)
    inflow (IPR)      q      = J (P_r(t) - P_bh),          P_r(t) = (a + c t) / J
    lift   (tubing)   P_bh   = P_wh + b0 + b1 q + b2 ln q_gl

IPR and tubing coefficients are fitted per well by robust regression (Huber) on daily flowing days, with q
from the VFM on measured pressures. For given choke, q_gl and P_ds the solver finds q where the VFM curve
crosses the inflow/lift curve.

Validation gate (``backtest``): every real choke change (and every >10 % lift-gas step) in the data is
re-predicted using only days BEFORE the change. The solver must beat the fixed-P_wh assumption on both the
measured P_wh after the change and the rate; otherwise the UI labels results *exploratory*.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import HuberRegressor

from .vfm import GasLiftVFM

MIN_DAYS = 20


@dataclass
class WellResponse:
    well: str
    J: float            # STB/d/psi (liquid)
    a: float
    c: float            # P_r drift: q-intercept trend per day
    b0: float
    b1: float
    b2: float
    t0: pd.Timestamp
    n: int
    valid: bool
    reason: str = ""

    def pr(self, date) -> float:
        t = (pd.Timestamp(date) - self.t0).days
        return (self.a + self.c * t) / self.J


def _steady(P: pd.DataFrame) -> pd.DataFrame:
    F = P[(P["uptime"] > 0.95) & P["liquid_rate_stbd"].notna()].copy()
    F["q"] = F["liquid_rate_stbd"] / F["uptime"]
    return F


def calibrate_well(P: pd.DataFrame, well: str, until=None) -> WellResponse:
    """Fit IPR and lift relation for one well on flowing days (optionally only before ``until``)."""
    F = _steady(P[P["well_name"] == well])
    if until is not None:
        F = F[pd.to_datetime(F["date"]) < pd.Timestamp(until)]
    t0 = pd.to_datetime(F["date"]).min() if len(F) else pd.Timestamp("2025-01-01")   # data-driven time origin
    if len(F) < MIN_DAYS:
        return WellResponse(well, np.nan, np.nan, 0, np.nan, np.nan, np.nan, t0, len(F), False, "too few flowing days")
    t = (pd.to_datetime(F["date"]) - t0).dt.days.values.astype(float)
    X = np.c_[t / 100.0, F["dh_pressure_psia"].values / 100.0]
    ipr = HuberRegressor(max_iter=500).fit(X, F["q"].values)
    J = -ipr.coef_[1] / 100.0
    tub = HuberRegressor(max_iter=500).fit(
        np.c_[F["q"].values / 1000.0, np.log(np.clip(F["gas_lift_rate_mscfd"].values, 1, None))],
        (F["dh_pressure_psia"] - F["whp_psia"]).values)
    ok, why = True, ""
    if not (J > 0):
        ok, why = False, "inflow slope not identifiable (J <= 0)"
    return WellResponse(well, float(J), float(ipr.intercept_), float(ipr.coef_[0] / 100.0), float(tub.intercept_),
                        float(tub.coef_[0] / 1000.0), float(tub.coef_[1]), t0, len(F), ok, why)


def solve(model: GasLiftVFM, base: pd.Series, wr: WellResponse, choke: float, gl: float,
          pds: float | None = None, n: int = 240) -> pd.Series | None:
    """Operating point for one well-day: returns the VFM output row at the solved (q, P_wh, P_bh) or None."""
    if not wr.valid:
        return None
    pds = float(base["whp_downstream_psia"] if pds is None else pds)
    pr = wr.pr(base["date"])
    q_max = wr.J * (pr - pds)
    if not np.isfinite(q_max) or q_max <= 1:
        return None
    q = np.linspace(1.0, q_max, n)
    bhp = pr - q / wr.J
    whp = bhp - (wr.b0 + wr.b1 * q + wr.b2 * np.log(max(gl, 1.0)))
    rows = pd.DataFrame([base] * n).reset_index(drop=True)
    rows["choke_opening_pct"], rows["gas_lift_rate_mscfd"], rows["uptime"] = float(choke), float(gl), 1.0
    rows["whp_psia"], rows["dh_pressure_psia"], rows["whp_downstream_psia"] = whp, bhp, pds
    rows["date"] = pd.to_datetime(rows["date"])
    for c in ["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "choke_opening_pct", "gas_lift_rate_mscfd"]:
        rows[c] = rows[c].astype(float)
    ok = whp > pds + 0.5
    if ok.sum() < 3:
        return None
    q, rows = q[ok], rows[ok].reset_index(drop=True)
    out = model.predict_daily(rows)
    g = out["liquid_rate_stbd"].values - q
    s = np.where(np.isfinite(g[:-1]) & np.isfinite(g[1:]) & (np.sign(g[:-1]) != np.sign(g[1:])))[0]
    if not len(s):
        return None
    i = s[np.argmin(np.abs(q[s] - float(base.get("liquid_rate_stbd", q_max / 2))))]
    w = g[i] / (g[i] - g[i + 1])                       # linear interpolation of the crossing
    r = out.iloc[i].copy()
    for c in ["liquid_rate_stbd", "oil_rate_stbd", "water_rate_stbd", "gas_rate_mscfd", "whp_psia", "dh_pressure_psia"]:
        r[c] = out[c].iloc[i] + w * (out[c].iloc[i + 1] - out[c].iloc[i])
    return r


def _events(P: pd.DataFrame, choke: pd.DataFrame) -> pd.DataFrame:
    """Choke changes (both settings open, |d| >= 1 %) and lift-gas steps > 10 % between flowing days."""
    c = choke.copy()
    c["timestamp"] = pd.to_datetime(c["timestamp"])
    c = c.sort_values(["well_name", "timestamp"])
    c["prev"] = c.groupby("well_name")["choke_opening_pct"].shift()
    ch = c[(c.prev > 0.5) & (c.choke_opening_pct > 0.5) & ((c.choke_opening_pct - c.prev).abs() >= 1)]
    ev = [{"well_name": r.well_name, "day": r.timestamp.normalize(), "kind": "choke"} for r in ch.itertuples()]
    F = _steady(P).sort_values(["well_name", "date"])
    F["glc"] = F.groupby("well_name")["gas_lift_rate_mscfd"].pct_change().abs()
    ev += [{"well_name": r.well_name, "day": pd.Timestamp(r.date), "kind": "gas-lift"} for r in F[F.glc > 0.1].itertuples()]
    return pd.DataFrame(ev).drop_duplicates(["well_name", "day", "kind"])


def backtest(model: GasLiftVFM, P: pd.DataFrame, choke: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Re-predict the day after each control change with calibration on earlier days only.
    Returns (per-event table, summary by kind with MAE of P_wh and MAPE of liquid rate)."""
    F = _steady(P)
    F["date"] = pd.to_datetime(F["date"])
    rows = []
    for e in _events(P, choke).itertuples():
        g = F[F.well_name == e.well_name]
        before = g[g.date < e.day].tail(1)
        after = g[(g.date > e.day) & (g.date <= e.day + pd.Timedelta(days=3))].head(1) if e.kind == "choke" \
            else g[g.date == e.day]
        if before.empty or after.empty:
            continue
        b, a = before.iloc[0], after.iloc[0]
        wr = calibrate_well(P, e.well_name, until=b["date"] + pd.Timedelta(days=1))
        base = b.copy()
        base["date"] = a["date"]
        fixed = base.copy()
        fixed["choke_opening_pct"], fixed["gas_lift_rate_mscfd"] = a["choke_opening_pct"], a["gas_lift_rate_mscfd"]
        fixed["whp_downstream_psia"], fixed["uptime"] = a["whp_downstream_psia"], 1.0
        fx = model.predict_daily(fixed.to_frame().T.infer_objects().reset_index(drop=True)).iloc[0]
        op = solve(model, base, wr, a["choke_opening_pct"], a["gas_lift_rate_mscfd"], a["whp_downstream_psia"])
        rows.append({"well_name": e.well_name, "day": e.day, "kind": e.kind,
                     "d_choke": a["choke_opening_pct"] - b["choke_opening_pct"],
                     "d_gl": a["gas_lift_rate_mscfd"] - b["gas_lift_rate_mscfd"],
                     "whp_true": a["whp_psia"], "whp_fixed": b["whp_psia"],
                     "whp_op": np.nan if op is None else op["whp_psia"],
                     "q_true": a["q"], "q_fixed": fx["liquid_rate_stbd"],
                     "q_op": np.nan if op is None else op["liquid_rate_stbd"], "calibrated": wr.valid})
    E = pd.DataFrame(rows)
    if E.empty:
        return E, pd.DataFrame()
    S = []
    for k, g in E.groupby("kind"):
        g = g.dropna(subset=["whp_op", "q_op"])
        S.append({"kind": k, "events": len(g),
                  "whp_MAE_fixed": float((g.whp_fixed - g.whp_true).abs().mean()),
                  "whp_MAE_solver": float((g.whp_op - g.whp_true).abs().mean()),
                  "q_MAPE_fixed_%": float(((g.q_fixed - g.q_true).abs() / g.q_true).mean() * 100),
                  "q_MAPE_solver_%": float(((g.q_op - g.q_true).abs() / g.q_true).mean() * 100),
                  "solved_share": float(len(g) / max((E.kind == k).sum(), 1))})
    S = pd.DataFrame(S)
    S["validated"] = (S.whp_MAE_solver < S.whp_MAE_fixed) & (S["q_MAPE_solver_%"] < S["q_MAPE_fixed_%"]) & \
                     (S.solved_share >= 0.8) & (S.events >= 8)
    return E, S
