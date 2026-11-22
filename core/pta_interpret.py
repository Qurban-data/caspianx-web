"""Automatic engineering interpretation of one build-up (used by the app and the report).

Reads the Horner result (submitted values) and the Bourdet derivative and returns plain-language statements:
wellbore-storage duration, infinite-acting radial flow window, late-time / boundary signature, damage severity,
and a stimulation recommendation with the theoretical oil gain if skin is removed (PSS inflow, constant drawdown:
q_new / q = (ln(r_e/r_w) - 0.75 + s) / (ln(r_e/r_w) - 0.75)). Every number comes from the event's own data.
"""
from __future__ import annotations

import numpy as np

from . import pta_horner as H
from .intervention import productivity_gain


def damage_class(s: float) -> str:
    """Severity class of the skin factor."""
    if s >= 5:
        return "severe damage"
    if s >= 2:
        return "moderate damage"
    if s > 0:
        return "slight damage"
    if s > -2:
        return "undamaged / slightly stimulated"
    return "stimulated (e.g. acidised or fractured)"


def interpret_event(res: "H.PTAResult", dt_hr, pws, pwf: float, q_oil: float, pvt: dict,
                    re_ft: float | None = None) -> dict:
    """Return {'lines': [...], 'dq_stb_d': float|nan, 'class': str} for one build-up."""
    dt = np.asarray(dt_hr, float)
    p = np.asarray(pws, float)
    ok = np.isfinite(dt) & np.isfinite(p) & (dt > 0)
    dt, p = dt[ok], p[ok]
    o = np.argsort(dt)
    dt, p = H.truncate_at_reopen(dt[o], p[o])
    te = H.agarwal_time(res.tp_hr, dt)
    der = H.bourdet_derivative(te, p - pwf, L=0.1)
    der[-1] = np.nan
    plateau = res.horner_slope / np.log(10)
    lines = []
    # wellbore storage
    lines.append(f"Wellbore storage and skin transition dominate the first {res.window_dt_start_hr:.2f} h "
                 f"(derivative hump before the plateau).")
    # IARF
    lines.append(f"Infinite-acting radial flow from {res.window_dt_start_hr:.2f} to {res.window_dt_end_hr:.1f} h "
                 f"({res.window_cycles:.2f} log-cycles, flat derivative at {plateau:.1f} psi): "
                 f"m = {res.horner_slope:.2f} psi/cycle, k = {res.k_md:.1f} md, kh = {res.k_md * pvt['h_ft']:,.0f} md·ft.")
    if res.window_cycles < 1.0:
        lines.append("Radial-flow plateau is shorter than one log-cycle: k and skin carry higher uncertainty.")
    # late time / boundary
    late = (dt > res.window_dt_end_hr) & np.isfinite(der) & (der > 0)
    r_inv = 0.029 * np.sqrt(res.k_md * dt.max() / (pvt["phi"] * pvt["mu"] * pvt["ct"]))
    if late.sum() >= 3:
        ratio = float(np.median(der[late][-3:]) / plateau)
        if ratio < 0.7:
            lines.append(f"Late-time derivative falls to {ratio:.2f}× the plateau: pressure support or closed-drainage "
                         f"build-up signature (boundary felt after ~{res.window_dt_end_hr:.0f} h, r_inv ≈ {r_inv:,.0f} ft).")
        elif ratio > 1.3:
            lines.append(f"Late-time derivative rises to {ratio:.2f}× the plateau: no-flow boundary / fault or "
                         f"permeability reduction away from the well (r_inv ≈ {r_inv:,.0f} ft).")
        else:
            lines.append(f"No clear boundary within the test: radius of investigation ≈ {r_inv:,.0f} ft.")
    else:
        lines.append(f"Test ends during radial flow: no boundary seen, radius of investigation ≈ {r_inv:,.0f} ft.")
    # damage + recommendation
    cls = damage_class(res.skin)
    dps = 0.87 * res.horner_slope * res.skin
    fe = (res.p_star_psi - pwf - dps) / (res.p_star_psi - pwf) if res.p_star_psi > pwf else np.nan
    lines.append(f"Skin s = {res.skin:+.2f} ({cls}); ΔP_skin = {dps:+.0f} psi; flow efficiency ≈ {fe:.2f}.")
    dq = np.nan
    if re_ft and res.skin > 0:
        g = productivity_gain(q_oil, res.skin, 0.0, re_ft, pvt["rw_ft"])
        if g.get("valid"):
            dq = float(g["dq"])
    if res.skin >= 2:
        rec = (f"Recommendation: matrix acidizing / stimulation candidate - theoretical gain ≈ {dq:,.0f} STB/d "
               f"if skin is removed (upper bound, constant drawdown)." if np.isfinite(dq) else
               "Recommendation: stimulation candidate (drainage radius unavailable for a gain estimate).")
    elif res.skin > 0:
        rec = (f"Recommendation: monitor; slight damage (≈{dq:,.0f} STB/d theoretical upside) - check perforations "
               f"before any job." if np.isfinite(dq) else "Recommendation: monitor; slight damage.")
    else:
        rec = "Recommendation: no stimulation - completion is efficient; optimise choke / gas-lift instead."
    lines.append(rec)
    return {"lines": lines, "dq_stb_d": dq, "class": cls}
