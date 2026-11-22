"""
Controlled inference interface around the validated VFM (adapter; ``core.vfm.GasLiftVFM`` is not modified).

``infer_daily`` = ingest-checked daily features -> ``prepare_daily`` -> ``GasLiftVFM.predict_daily`` and adds:

* **new wells** (no well test): a *copy* of the model gets the field-median calibration C_w and field-median
  WC / GOR for that well only; known wells keep their validated coefficients bit-for-bit. Rows are flagged
  ``calibrated = False`` and their P10-P90 band is widened by the between-well spread of ln C_w and WC.
  Policy ``"unavailable"`` returns NaN rates instead.
* **missing choke data** for a well: rates NaN, status ``invalid`` (choke drives q ~ opening^2; never guessed).
* **out-of-distribution flags** per row: inputs outside the well's tested envelope (+-5 %).
* **semantics**: ``data_status`` (flowing / partial shut-in / shut-in / imputed / invalid) and
  ``value_source = 'predicted'``; full shut-in days carry exactly zero rate and zero band.
"""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from .ingest import data_status
from .preprocessing import prepare_daily
from .vfm import GasLiftVFM, TrendModel, PHASES, RATE

Z90 = 1.2816                                    # standard-normal 90th percentile
RATE_COLS = ["liquid_rate_stbd", "oil_rate_stbd", "water_rate_stbd", "gas_rate_mscfd"]
ENVELOPE = ["choke_opening_pct", "gas_lift_rate_mscfd", "whp_psia"]
# P_ds is NOT an envelope variable: tests flow to the test separator at systematically lower downstream
# pressure than daily production, so daily P_ds is outside the test range by design (documented limitation).


def known_wells(model: GasLiftVFM) -> set:
    return set(model.M["Cw"])


def _with_new_wells(model: GasLiftVFM, wells: list[str], rs: dict | None = None) -> tuple[GasLiftVFM, dict]:
    """Shallow model copy where unseen wells get field-median parameters; returns (copy, spread info)."""
    m = copy.copy(model)
    m.M = dict(model.M, Cw=dict(model.M["Cw"]))
    m.wc, m.gor, m.fwd = dict(model.wc), dict(model.gor), dict(model.fwd)
    cw = np.array(list(model.M["Cw"].values()))
    T = model.T
    wc_med = float(T.groupby("well_name")["wc"].median().median())
    gor_med = float(T.groupby("well_name")["gor"].median().median())
    for w in wells:
        m.M["Cw"][w] = float(np.median(cw))
        m.wc[w] = TrendModel("const").fit([0.0], [wc_med])
        # undersaturated oil (P_res > P_b): producing GOR = solution GOR R_s from the completion file when known
        g0 = float(rs[w]) if rs and w in rs and np.isfinite(rs[w]) and rs[w] > 0 else gor_med
        m.gor[w] = TrendModel("const").fit([0.0], [g0])
    spread = {"sd_ln_cw": float(np.std(cw, ddof=1)),
              "sd_ln_wc": float(np.std(np.log(T.groupby("well_name")["wc"].median()), ddof=1)),
              "sd_ln_gor": float(np.std(np.log(T.groupby("well_name")["gor"].median()), ddof=1))}
    return m, spread


def envelope(model: GasLiftVFM) -> pd.DataFrame:
    """Per-well tested operating envelope (min / max of each input over the well tests)."""
    T = model.T
    return T.groupby("well_name")[ENVELOPE].agg(["min", "max"])


def ood_flags(P: pd.DataFrame, model: GasLiftVFM, tol: float = 0.05) -> pd.Series:
    """Comma-separated list of inputs outside the well's tested range (+-tol); '' when inside."""
    env = envelope(model)
    out = pd.Series("", index=P.index)
    for c in ENVELOPE:
        lo = P["well_name"].map(env[(c, "min")]) * (1 - tol)
        hi = P["well_name"].map(env[(c, "max")]) * (1 + tol)
        bad = (P["uptime"] > 0) & ((P[c] < lo) | (P[c] > hi)) & lo.notna()
        out[bad] = out[bad] + c + ","
    return out.str.rstrip(",")


def infer_daily(model: GasLiftVFM, daily: pd.DataFrame, choke: pd.DataFrame,
                new_well_policy: str = "field_median") -> tuple[pd.DataFrame, dict]:
    """Daily rates for every well-day with flags. Returns (predictions, info)."""
    info: dict = {"new_wells": [], "no_choke_wells": []}
    wells = sorted(daily["well_name"].unique())
    ch_wells = set(choke["well_name"].unique()) if len(choke) else set()
    info["no_choke_wells"] = [w for w in wells if w not in ch_wells and "choke_opening_pct" not in daily]
    info["new_wells"] = [w for w in wells if w not in known_wells(model)]
    use = daily[~daily["well_name"].isin(info["no_choke_wells"])]
    D = prepare_daily(use, choke) if len(use) else use.assign(choke_opening_pct=np.nan, imputed=False)
    m, spread = (_with_new_wells(model, info["new_wells"]) if info["new_wells"] else (model, {}))
    P = m.predict_daily(D) if len(D) else D
    if info["no_choke_wells"]:
        X = daily[daily["well_name"].isin(info["no_choke_wells"])].copy()
        X["date"] = pd.to_datetime(X["date"])
        X["choke_opening_pct"], X["imputed"] = np.nan, True
        for c in RATE_COLS + [f"{p}_{q}" for p in PHASES for q in ("p10", "p90")] + ["wc", "gor"]:
            X[c] = np.nan
        P = pd.concat([P, X], ignore_index=True).sort_values(["well_name", "date"]).reset_index(drop=True)
    P["calibrated"] = ~P["well_name"].isin(info["new_wells"])
    newm = ~P["calibrated"]
    if newm.any():
        if new_well_policy == "unavailable":
            P.loc[newm, RATE_COLS + ["wc", "gor"]] = np.nan
        elif hasattr(model, "band_loo"):
            add = {"oil": np.hypot(spread["sd_ln_cw"], spread["sd_ln_wc"] * 0.5),
                   "water": np.hypot(spread["sd_ln_cw"], spread["sd_ln_wc"]),
                   "gas": np.hypot(spread["sd_ln_cw"], spread["sd_ln_gor"])}
            for ph in PHASES:
                P.loc[newm, f"{ph}_p10"] = P.loc[newm, RATE[ph]] * np.exp(model.band_fwd[ph][0] - Z90 * add[ph])
                P.loc[newm, f"{ph}_p90"] = P.loc[newm, RATE[ph]] * np.exp(model.band_fwd[ph][1] + Z90 * add[ph])
    zero = [c for c in P.columns if c.startswith(("liquid_rate", "oil_", "water_", "gas_rate", "gas_p"))]
    P.loc[P["uptime"] <= 0, zero] = 0.0
    P["data_status"] = data_status(P)
    P["ood_inputs"] = ood_flags(P, model)
    P["value_source"] = "predicted"
    info["ood_share"] = float((P["ood_inputs"] != "").mean()) if len(P) else 0.0
    info["spread"] = spread
    return P, info
