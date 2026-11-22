"""Experiment B (S2, 2026-11-21): direct 3-target VFM (oil, water, gas regressed separately).

Not the submitted model. Kept as a documented alternative: on the frozen protocol it beats the deployed
liquid + WC + GOR model on oil and gas (paired CI excludes 0) but its fitted elasticities are less physical
(effective choke exponent ~3, negative lift-gas elasticity). See reports/vfm_model_comparison.md.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .vfm import RATE

PH = ["oil", "water", "gas"]


TREND_HORIZON_DAYS = 180     # same rule as core.vfm: days/100 frozen beyond last training test + 180 d


def train_day_range(T: pd.DataFrame) -> tuple[float, float]:
    """(first, last) training-test day on the model's time axis (days since 2025-01-01)."""
    d = (pd.to_datetime(T["test_mid"]) - pd.Timestamp("2025-01-01")).dt.days
    return float(d.min()), float(d.max())


def ensure_train_range(model_b, tests: pd.DataFrame) -> None:
    """.pkl files saved before the horizon clip carry no training range: attach it from the training tests
    (model A's tests = the same QC-ed tests B was fitted on)."""
    if getattr(model_b, "t_range", None) is None and tests is not None and len(tests):
        model_b.t_range = train_day_range(tests)


class Direct3VFM:
    """Experiment B (S2): direct regression of each phase rate, no liquid/WC/GOR decomposition.
    log q_ph = per-well intercept + Ridge(ln choke-physics base, ln WHP, ln dP_choke, ln choke, ln q_gl, P_bh/1000,
    days since t0). Same inputs as the deployed model; the time term lets each phase follow its own trend."""

    choice = ("direct3", None)

    ALPHAS = (0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0)

    def __init__(self, alpha: float | None = None, inner_days: int = 45):
        self.alpha = alpha            # None -> chosen INSIDE fit by an inner time split (nested, no test leakage)
        self.inner_days = inner_days

    def _X(self, T: pd.DataFrame) -> np.ndarray:
        """Design: ln physics base, ln WHP, ln dP_choke, ln choke, ln lift gas, P_bh/1000, days/100 (standardised) + well one-hot (unseen well = all zeros)."""
        from core.vfm import subcrit_base
        base = subcrit_base(T["whp_psia"].values, T["whp_downstream_psia"].values, T["choke_opening_pct"].values)
        t = (pd.to_datetime(T["date"] if "date" in T else T["test_mid"]) - pd.Timestamp("2025-01-01")).dt.days.values
        rng = getattr(self, "t_range", None)        # (first, last) training day; set in fit or attached for old .pkl
        H = getattr(self, "trend_horizon_days", TREND_HORIZON_DAYS)
        if rng is not None and H is not None:
            t = np.clip(t, rng[0] - H, rng[1] + H)
        num = np.c_[np.log(base), np.log(T["whp_psia"]), np.log(np.clip(T["whp_psia"] - T["whp_downstream_psia"], 0.5, None)),
                    np.log(np.clip(T["choke_opening_pct"], 0.1, None)), np.log(np.clip(T["gas_lift_rate_mscfd"], 1, None)),
                    T["dh_pressure_psia"] / 1000, t / 100.0]
        W = (np.asarray(T["well_name"], dtype=object)[:, None] == np.array(self.wells, dtype=object)[None, :]).astype(float)
        return np.c_[num, W]

    def fit(self, T: pd.DataFrame, uncertainty: bool = False, **kw):
        """Ridge per phase on log-rate; alpha chosen on an inner time split of the TRAINING tests only (no leakage)."""
        if self.alpha is None:
            end = pd.to_datetime(T["test_mid"]).max()
            tr = T[pd.to_datetime(T["test_mid"]) < end - pd.Timedelta(days=self.inner_days)]
            va = T[pd.to_datetime(T["test_mid"]) >= end - pd.Timedelta(days=self.inner_days)]
            va = va[va.well_name.isin(tr.well_name.unique())]
            if len(tr) < 10 or len(va) < 3:            # too few tests for an inner split: moderate default
                self.alpha = 0.1
                return self.fit(T)
            scores = {}
            for a in self.ALPHAS:
                m = Direct3VFM(a).fit(tr)
                P = m.predict_daily(va.assign(date=va["test_mid"], uptime=1.0))
                scores[a] = float(np.mean([np.mean(np.abs(np.log(P[RATE[ph]].values / va[RATE[ph]].values))) for ph in PH]))
            self.alpha_scores = scores
            self.alpha = min(scores, key=scores.get)
        from sklearn.linear_model import Ridge
        from sklearn.preprocessing import StandardScaler
        self.wells = sorted(T["well_name"].unique())
        self.t_range = None                          # training rows are never clipped
        X = self._X(T)
        self.sc = StandardScaler().fit(X[:, :7])
        Xs = np.c_[self.sc.transform(X[:, :7]), X[:, 7:]]
        self.m = {ph: Ridge(alpha=self.alpha).fit(Xs, np.log(T[RATE[ph]].values)) for ph in PH}
        self.t_range = train_day_range(T)
        return self

    def predict_daily(self, D: pd.DataFrame) -> pd.DataFrame:
        """Daily phase rates = exp(prediction) x uptime (exact 0 when uptime is 0)."""
        D = D.copy()
        X = self._X(D)
        Xs = np.c_[self.sc.transform(X[:, :7]), X[:, 7:]]
        up = D["uptime"].values if "uptime" in D else 1.0
        for ph in PH:
            D[RATE[ph]] = np.exp(self.m[ph].predict(Xs)) * up
        D["liquid_rate_stbd"] = D["oil_rate_stbd"] + D["water_rate_stbd"]
        D["wc"] = D["water_rate_stbd"] / D["liquid_rate_stbd"]
        return D




# ------------------------------------------------------------------ submitted VFM = per-phase A/B geometric blend
BLEND_WA = {"oil_rate_stbd": 0.2, "water_rate_stbd": 0.5, "gas_rate_mscfd": 0.3}


def blend_rates(A: pd.DataFrame, B: pd.DataFrame, key: list, tested: set, rs: dict | None = None) -> pd.DataFrame:
    """Per-phase geometric blend exp(w ln A + (1-w) ln B) of two daily-rate tables (A = choke physics + Ridge,
    B = direct 3-target), both rounded to 0.01 like the submission files. Rules (validated, see qa/):
    * B missing (e.g. a sensor gap) -> A;  * either model 0 (confirmed shut-in day) -> exact 0.0;
    * well without any well test: gas = oil x solution GOR R_s (undersaturated oil) when R_s is known.
    Used by run_all.py (official file) and by the app, so both produce identical numbers."""
    M = A[key + list(BLEND_WA)].merge(B[key + list(BLEND_WA)], on=key, suffixes=("_A", "_B"), validate="one_to_one")
    out = M[key].copy()
    for c, w in BLEND_WA.items():
        a, b = M[c + "_A"].values.astype(float), M[c + "_B"].values.astype(float)
        b = np.where(np.isfinite(b), b, a)
        z = (a <= 0) | (b <= 0)
        v = np.exp(w * np.log(np.where(z, 1, a)) + (1 - w) * np.log(np.where(z, 1, b)))
        out[c] = np.where(z, 0.0, v).round(2)
    if rs:
        rsm = out["well_name"].map(rs)
        un = ~out["well_name"].isin(tested) & rsm.gt(0)
        out.loc[un, "gas_rate_mscfd"] = (out.loc[un, "oil_rate_stbd"] * rsm[un] / 1000).round(2)
    return out
