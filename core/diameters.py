"""Choke / tubing / casing / wellbore / flowline diameters: detection, unit normalisation, documented defaults,
wellbore-storage and tubing-hydraulics diagnostics.

Detection: any table with ``well_name`` is scanned; a column is recognised by its name and its unit by the suffix
(_in / _inch, _mm, _ft, _64ths / _64). Everything is normalised to inches (r_w to ft). A missing diameter gets the
documented default below and the flag "assumed" (shown on screen and in exports). The SOCAR files carry tubing OD / ID,
the adjustable-choke maximum bean size (64ths) and r_w; they carry no casing or flowline diameter and no choke bean
size per event (only the opening in %).

Role in the models (qa/choke_tubing_ablation.py): bean-area / Gilbert / dP-ratio proxies are linear combinations of the
log features the VFM already uses and changed forward / LOWO MAPE by <= 0.1 pp; tubing ID + flowing gradient worsened
LOWO by 1.7 pp. Diameters are therefore engineering diagnostics here (erosional velocity, storage), not VFM inputs.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

DEFAULTS = {          # documented defaults when a well has no value (flagged "assumed")
    "tubing_id_in": (2.992, "3-1/2 in 9.3 lb/ft API tubing"),
    "tubing_od_in": (3.5, "3-1/2 in API tubing"),
    "casing_id_in": (np.nan, "not provided; not used by any calculation"),
    "bean_max_in": (2.0, "2 in adjustable choke (128/64)"),
    "rw_ft": (0.328, "7-7/8 in open hole"),
    "flowline_id_in": (np.nan, "not used by any calculation"),
}
PATTERNS = {          # canonical -> regex on lower-case column names (unit decided by suffix)
    "tubing_id_in": r"^(tubing|tbg)_(id|inner_diameter|inside_diameter)(_(in|inch|mm|64ths?))?$",
    "tubing_od_in": r"^(tubing|tbg)_(od|outer_diameter|outside_diameter)(_(in|inch|mm))?$",
    "casing_id_in": r"^(casing|csg)_(id|inner_diameter)(_(in|inch|mm))?$",
    "bean_max_in": r"^(choke|bean)_(max_size|size_max|max)(_(in|inch|mm|64ths?))?$",
    "rw_ft": r"^(wellbore_radius|rw)(_(ft|in|inch|mm))?$",
    "hole_d": r"^(wellbore_diameter|hole_size|hole_diameter|bit_size)(_(in|inch|mm|ft))?$",
    "flowline_id_in": r"^(flowline|flow_line)_(id|diameter)(_(in|inch|mm))?$",
}
BEAN_EVENT_COLS = r"^(choke|bean)_(size|diameter)(_(in|inch|mm|64ths?))?$"   # per-event bean size instead of %


def _to_in(v: pd.Series, col: str) -> pd.Series:
    v = pd.to_numeric(v, errors="coerce")
    c = col.lower()
    if c.endswith("_mm"):
        return v / 25.4
    if c.endswith("_64ths") or c.endswith("_64th") or c.endswith("_64"):
        return v / 64.0
    if c.endswith("_ft"):
        return v * 12.0
    return v


def detect(tables: dict) -> pd.DataFrame:
    """Per-well diameters in inches (r_w in ft) with a <name>_source column: 'data: <column>' or 'assumed: <default>'."""
    wells = sorted(set().union(*[set(t["well_name"]) for t in tables.values() if t is not None and "well_name" in t]))
    out = pd.DataFrame({"well_name": wells})
    for canon in DEFAULTS:
        out[canon], out[canon + "_source"] = np.nan, ""
    for kind, t in tables.items():
        if t is None or "well_name" not in t or kind in ("daily_features", "high_freq", "choke_events", "well_tests"):
            continue
        g = t.drop_duplicates("well_name").set_index("well_name")
        for col in g.columns:
            c = str(col).lower()
            for canon, pat in PATTERNS.items():
                if not re.match(pat, c):
                    continue
                vals = _to_in(g[col], c)
                if canon == "rw_ft":                     # radius: ft unless the suffix says in / mm
                    raw_v = pd.to_numeric(g[col], errors="coerce")
                    vals = raw_v / 12.0 if c.endswith(("_in", "_inch")) else raw_v / 304.8 if c.endswith("_mm") else raw_v
                    target = "rw_ft"
                elif canon == "hole_d":
                    vals, target = _to_in(g[col], c) / 24.0, "rw_ft"            # diameter in -> radius ft
                else:
                    target = canon
                m = out.well_name.map(vals)
                fill = out[target].isna() & m.notna()
                out.loc[fill, target] = m[fill]
                out.loc[fill, target + "_source"] = f"data: {kind}.{col}"
    for canon, (val, why) in DEFAULTS.items():
        miss = out[canon].isna()
        if np.isfinite(val):
            out.loc[miss, canon] = val
            out.loc[miss, canon + "_source"] = f"assumed: {why}"
        else:
            out.loc[miss, canon + "_source"] = "absent"
    out["assumed"] = out[[c for c in out.columns if c.endswith("_source")]].apply(
        lambda r: ", ".join(k.replace("_source", "") for k, v in r.items() if str(v).startswith("assumed")), axis=1)
    rw_d = out.rw_ft * 24
    out["rw_check"] = np.where(rw_d >= out.tubing_od_in, "ok", "r_w smaller than the tubing OD - check r_w")
    return out


def bean_events_to_pct(choke: pd.DataFrame, diam: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Choke events given as a bean SIZE (64ths / mm / in) instead of an opening % -> opening % of the maximum bean."""
    col = next((c for c in choke.columns if re.match(BEAN_EVENT_COLS, str(c).lower())), None)
    if col is None or "choke_opening_pct" in choke:
        return choke, ""
    size_in = _to_in(choke[col], str(col).lower())
    mx = choke["well_name"].map(diam.set_index("well_name")["bean_max_in"])
    out = choke.copy()
    out["choke_opening_pct"] = (size_in / mx * 100).clip(0, 100)
    return out, f"choke events given as bean size ({col}) converted to % of the maximum bean (per-well max size)"


def effective_bean_in(opening_pct, bean_max_in):
    """Effective bean diameter, assuming the opening % refers to the maximum bean diameter (stated assumption)."""
    return np.asarray(opening_pct, float) / 100.0 * np.asarray(bean_max_in, float)


# ------------------------------------------------------------------ wellbore storage (PTA)
def storage_from_volume(tubing_id_in: float, depth_ft: float, c_wb: float = 1.0e-5) -> dict:
    """C = V_wb * c_wb [bbl/psi]; V_wb = tubing volume to the gauge. c_wb = 1e-5 1/psi (liquid-filled tubing, assumed;
    a gas-lifted tubing with gas holds far more compressibility, so C from the data is the reference)."""
    v_bbl = np.pi / 4 * (tubing_id_in / 12) ** 2 * depth_ft / 5.615
    return {"V_wb_bbl": v_bbl, "c_wb_1_psi": c_wb, "C_bbl_psi": v_bbl * c_wb}


def storage_from_data(dt_hr, dp_psi, q_stbd: float, Bo: float, n: int = 3) -> float:
    """Unit-slope storage from the first build-up points: C = q B dt / (24 dP) [bbl/psi] (median of the first n)."""
    dt, dp = np.asarray(dt_hr, float), np.asarray(dp_psi, float)
    ok = (dt > 0) & (dp > 0)
    dt, dp = dt[ok][:n], dp[ok][:n]
    return float(np.median(q_stbd * Bo * dt / (24 * dp))) if len(dt) else float("nan")


def storage_end_hr(C: float, s: float, mu: float, k: float, h: float) -> float:
    """End of storage-dominated flow, t_wbs = (200 000 + 12 000 s) C mu / (k h) [hr] (Earlougher)."""
    return float((200000 + 12000 * s) * C * mu / (k * h)) if k > 0 and h > 0 else float("nan")


# ------------------------------------------------------------------ tubing hydraulics (diagnostic)
def tubing_hydraulics(q_oil, q_water, q_gas_form, q_gl, whp_psia, tubing_id_in, api=35.0, sg_w=1.05,
                      sg_g=0.75, t_f=100.0, z=0.9, c_erosional=100.0, f_darcy=0.015) -> pd.DataFrame:
    """Wellhead no-slip mixture velocity, density, API RP 14E erosional velocity v_e = C / sqrt(rho_m) (C = 100,
    continuous service), ratio v_m / v_e and the friction gradient (Darcy f = 0.015, assumed). Diagnostic only."""
    q_oil, q_water = np.asarray(q_oil, float), np.asarray(q_water, float)
    qg = (np.asarray(q_gas_form, float) + np.asarray(q_gl, float)) * 1000.0                 # scf/d
    p = np.asarray(whp_psia, float)
    A = np.pi / 4 * (np.asarray(tubing_id_in, float) / 12) ** 2                              # ft2
    bg = 0.02827 * z * (t_f + 460) / np.clip(p, 15, None)                                  # ft3/scf
    ql = (q_oil + q_water) * 5.615 / 86400                                                  # ft3/s
    qgv = qg * bg / 86400
    rho_o = 141.5 / (131.5 + api) * 62.4
    rho_l = (rho_o * q_oil + sg_w * 62.4 * q_water) / np.clip(q_oil + q_water, 1e-9, None)
    rho_g = 2.7 * sg_g * p / (z * (t_f + 460))                                              # lb/ft3, real gas
    lam = ql / np.clip(ql + qgv, 1e-12, None)
    rho_m = lam * rho_l + (1 - lam) * rho_g
    v_m = (ql + qgv) / A
    v_e = c_erosional / np.sqrt(rho_m)
    dpdz_f = f_darcy * rho_m * v_m ** 2 / (2 * 32.174 * np.asarray(tubing_id_in, float) / 12) / 144
    return pd.DataFrame({"v_liquid_ft_s": ql / A, "v_mixture_ft_s": v_m, "rho_mix_lb_ft3": rho_m,
                         "v_erosional_ft_s": v_e, "erosional_ratio": v_m / v_e, "friction_psi_ft": dpdz_f,
                         "liquid_holdup_noslip": lam})
