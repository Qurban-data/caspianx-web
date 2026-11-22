"""
Intervention intelligence: rank wells by formation damage (skin) and theoretical oil gain (adapter over PTA).

Pseudo-steady-state radial inflow for a well in a closed circular drainage area (oilfield units):

    J = q / (p_avg - p_wf) = k h / (141.2 B mu [ln(r_e/r_w) - 0.75 + s])

With k, h, B, mu, r_e, r_w and the drawdown unchanged, removing damage from s to s_new changes only the
log term, so

    J_new / J = (C + s) / (C + s_new),        C = ln(r_e / r_w) - 0.75
    q_new     = q * J_new / J,                dq = q_new - q

r_e is the regression drainage radius from the PTA (or sqrt(A/pi) from the drainage area), s the regression
skin, q the VFM oil rate before shut-in. This is a THEORETICAL productivity gain: it assumes constant
drawdown, single-phase oil inflow and complete removal of damage down to s_new. It is not an achievable
forecast: lift capacity, choke and surface constraints, multiphase flow (WC) and pressure-dependent PI can cap it.

Technical confidence: High = build-up >= 24 h, model match RMSE <= 0.5 psi and a radial-flow window;
Medium = >= 8 h; Low otherwise (short build-ups: storage still decaying, skin +-0.5).
Economics only from user-supplied net oil price, intervention cost, decline and discount rate (none invented):

    payback [d] = cost / (dq * net_price)
    NPV = sum_{m=1..H} dq * 30.4 * net_price * exp(-D m/12) / (1 + r)^(m/12)  -  cost
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SKIN_UNCERTAINTY = {"High": 0.2, "Medium": 0.35, "Low": 0.5}     # +- skin used for the dq range
CONF_WEIGHT = {"High": 1.0, "Medium": 0.7, "Low": 0.4}


def confidence(r: dict) -> str:
    dur, rmse = r.get("duration_hr", 0.0), r.get("match_rmse_psi", np.inf)
    radial = np.isfinite(r.get("t_radial_start_hr", np.nan)) and r.get("t_radial_end_hr", 0) > r.get("t_radial_start_hr", 0)
    if dur >= 24 and rmse <= 0.5 and radial:
        return "High"
    return "Medium" if dur >= 8 else "Low"


def productivity_gain(q: float, s: float, s_new: float, re_ft: float, rw_ft: float) -> dict:
    """PSS productivity ratio and oil gain; returns NaN with a reason when assumptions are violated."""
    if not (np.isfinite(re_ft) and np.isfinite(rw_ft) and re_ft > rw_ft > 0):
        return {"valid": False, "reason": "drainage or wellbore radius unavailable"}
    C = np.log(re_ft / rw_ft) - 0.75
    if C + s <= 0 or C + s_new <= 0:
        return {"valid": False, "reason": "non-positive inflow resistance (C + s <= 0)"}
    ratio = (C + s) / (C + s_new)
    return {"valid": True, "C": C, "J_ratio": ratio, "q_new": q * ratio, "dq": q * (ratio - 1)}


def npv(dq: float, net_price: float, cost: float, months: int = 12, decline_per_year: float = 0.0,
        discount_per_year: float = 0.10) -> float:
    m = np.arange(1, months + 1)
    cash = dq * 30.4 * net_price * np.exp(-decline_per_year * m / 12) / (1 + discount_per_year) ** (m / 12)
    return float(cash.sum() - cost)


def rank_interventions(results: list[dict], pvt: pd.DataFrame | None = None, wells: list[str] | None = None,
                       s_new: float = 0.0,
                       net_price: float | None = None, cost: float | None = None, months: int = 12,
                       decline_per_year: float = 0.0, discount_per_year: float = 0.10) -> pd.DataFrame:
    """One row per well (latest build-up). Wells without a build-up are listed with status 'no PTA data'."""
    latest: dict[str, dict] = {}
    for r in results:
        if r["well_name"] not in latest or r["t_shut"] > latest[r["well_name"]]["t_shut"]:
            latest[r["well_name"]] = r
    rows = []
    for w, r in latest.items():
        s, q = float(r["final_skin"]), float(r["q_oil_stbd"])
        re = r.get("re_reg_ft", np.nan)
        rw = r.get("rw_ft", np.nan)
        if not np.isfinite(rw) and pvt is not None and (pvt["well_name"] == w).any():
            rw = float(pvt.loc[pvt["well_name"] == w, "wellbore_radius_ft"].iloc[0])
        if not np.isfinite(re) and pvt is not None and "estimated_drainage_area_acres" in pvt:
            a = pvt.loc[pvt["well_name"] == w, "estimated_drainage_area_acres"]
            re = float(np.sqrt(a.iloc[0] * 43560 / np.pi)) if len(a) else np.nan
        conf = confidence(r)
        row = {"well_name": w, "t_shut": r["t_shut"], "skin": s, "k_md": r["final_k_md"],
               "q_oil_stbd": q, "PI_stb_d_psi": r.get("productivity_index_stb_d_psi", np.nan),
               "confidence": conf}
        if s <= s_new:
            row.update({"status": "not damaged (skin <= target)", "dq_stbd": 0.0})
            rows.append(row)
            continue
        g = productivity_gain(q, s, s_new, re, rw)
        if not g["valid"]:
            row.update({"status": g["reason"]})
            rows.append(row)
            continue
        du = SKIN_UNCERTAINTY[conf]
        lo = productivity_gain(q, max(s - du, s_new), s_new, re, rw)
        hi = productivity_gain(q, s + du, s_new, re, rw)
        row.update({"status": "candidate", "C_ln_re_rw": g["C"], "PI_after_stb_d_psi": row["PI_stb_d_psi"] * g["J_ratio"],
                    "q_new_stbd": g["q_new"], "dq_stbd": g["dq"], "dq_low": lo.get("dq", np.nan),
                    "dq_high": hi.get("dq", np.nan), "gain_%": (g["J_ratio"] - 1) * 100,
                    "priority_score": g["dq"] * CONF_WEIGHT[conf]})
        if net_price is not None and cost is not None and net_price > 0:
            row["revenue_usd_d"] = g["dq"] * net_price
            row["payback_days"] = cost / row["revenue_usd_d"] if row["revenue_usd_d"] > 0 else np.inf
            row["NPV_usd"] = npv(g["dq"], net_price, cost, months, decline_per_year, discount_per_year)
            row["NPV_low_usd"] = npv(row["dq_low"], net_price, cost, months, decline_per_year, discount_per_year)
        rows.append(row)
    for w in sorted(set(wells or []) - set(latest)):
        rows.append({"well_name": w, "status": "no PTA data (no build-up in the dataset)"})
    R = pd.DataFrame(rows)
    if "priority_score" in R:
        R = R.sort_values("priority_score", ascending=False, na_position="last")
        R["priority"] = np.where(R["status"] == "candidate", np.arange(1, len(R) + 1), np.nan)
    return R.reset_index(drop=True)
