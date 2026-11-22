"""VFM v5 candidates (experiment; the deployed v2 blend in core/vfm.py + core/vfm_direct.py is unchanged).

Motivation (qa/VFM_V4_DECISION.md): well tests flow to the test separator at a lower downstream pressure than normal
production, so the sub-critical choke term sqrt(WHP - P_ds) is extrapolated on almost every production day, and three
wells (GL-04, GL-06, GL-10) run at P_ds/WHP < 0.55, i.e. critical or near-critical choke flow where P_ds has no effect.

V5-A  REF v2 blend, liquid re-anchored on the inflow relation q_liq = J_w (P_r - P_wf) for wells with a build-up that
      ENDED before the training cut (P_r = Horner p*). J_w = median over the training tests. Phase rates of REF are
      scaled by q_IPR / q_liq,REF (WC and GOR of REF unchanged). Wells without such a build-up: REF unchanged.
V5-B  P_ds-free blend: model A with the critical-flow base (choke/100)^2 * WHP and a residual without ln dP_choke;
      model B (direct 3-target) without the ln dP_choke column and with the critical-flow base. Same blend weights
      as REF (not re-tuned).
V5-C  per-phase convex log-blend of REF, V5-A, V5-B; weights on a 0.25 simplex grid chosen on the inner time split of
      the training tests only.
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from .vfm import GasLiftVFM, DEPLOYED_CANDIDATES, RATE
from .vfm_direct import Direct3VFM, BLEND_WA, TREND_HORIZON_DAYS, train_day_range
from .vfm_v4 import inner_split

PH = ["oil", "water", "gas"]
BUILDUP_DAYS = 3.0          # a build-up is usable once it has ended (shut-in + 3 d covers every build-up here)


def _blend(A: pd.DataFrame, B: pd.DataFrame, w: dict | None = None) -> pd.DataFrame:
    P = A.copy().reset_index(drop=True)
    A, B = A.reset_index(drop=True), B.reset_index(drop=True)
    for ph in PH:
        c = RATE[ph]
        wa = (w or BLEND_WA)[c]
        a, b = A[c].values.astype(float), B[c].values.astype(float)
        z = (a <= 0) | (b <= 0)
        P[c] = np.where(z, 0.0, np.exp(wa * np.log(np.where(z, 1, a)) + (1 - wa) * np.log(np.where(z, 1, b))))
    P["liquid_rate_stbd"] = P["oil_rate_stbd"] + P["water_rate_stbd"]
    return P


# ------------------------------------------------------------------------------------------------ REF
class RefBlend:
    """The submitted v2 model: A = subcrit + Ridge, B = Direct3VFM, geometric blend BLEND_WA."""
    name = "REF v2 blend"

    def fit(self, T):
        self.a = GasLiftVFM(DEPLOYED_CANDIDATES).fit(T, uncertainty=False, loo=False)
        self.b = Direct3VFM().fit(T)
        return self

    def predict_daily(self, D):
        return _blend(self.a.predict_daily(D), self.b.predict_daily(D))


# ------------------------------------------------------------------------------------------------ V5-B
class CriticalA(GasLiftVFM):
    """Model A with the critical-flow base (choke/100)^2 * WHP and a P_ds-free residual."""

    def _phys(self, df, kind):
        return (np.clip(df["choke_opening_pct"].values, 0.1, 100) / 100) ** 2 * np.clip(df["whp_psia"].values, 1, None)

    @staticmethod
    def _resid_X(T):
        return np.c_[np.log(T["whp_psia"]), np.log(np.clip(T["choke_opening_pct"], 0.1, None)),
                     np.log(np.clip(T["gas_lift_rate_mscfd"], 1, None)), T["dh_pressure_psia"] / 1000]


class CriticalB(Direct3VFM):
    """Direct 3-target Ridge without ln dP_choke; base = ln[(choke/100)^2 WHP]."""
    NNUM = 6

    def _X(self, T):
        c = np.clip(T["choke_opening_pct"].values, 0.1, 100)
        base = (c / 100) ** 2 * np.clip(T["whp_psia"].values, 1, None)
        t = (pd.to_datetime(T["date"] if "date" in T else T["test_mid"]) - pd.Timestamp("2025-01-01")).dt.days.values
        rng = getattr(self, "t_range", None)
        if rng is not None:
            t = np.clip(t, rng[0] - TREND_HORIZON_DAYS, rng[1] + TREND_HORIZON_DAYS)
        num = np.c_[np.log(base), np.log(T["whp_psia"]), np.log(c), np.log(np.clip(T["gas_lift_rate_mscfd"], 1, None)),
                    T["dh_pressure_psia"] / 1000, t / 100.0]
        W = (np.asarray(T["well_name"], dtype=object)[:, None] == np.array(self.wells, dtype=object)[None, :]).astype(float)
        return np.c_[num, W]

    def fit(self, T, uncertainty=False, **kw):
        if self.alpha is None:
            end = pd.to_datetime(T["test_mid"]).max()
            tr = T[pd.to_datetime(T["test_mid"]) < end - pd.Timedelta(days=self.inner_days)]
            va = T[pd.to_datetime(T["test_mid"]) >= end - pd.Timedelta(days=self.inner_days)]
            va = va[va.well_name.isin(tr.well_name.unique())]
            if len(tr) < 10 or len(va) < 3:
                self.alpha = 0.1
            else:
                s = {}
                for a in self.ALPHAS:
                    P = CriticalB(a).fit(tr).predict_daily(va.assign(date=va["test_mid"], uptime=1.0))
                    s[a] = float(np.mean([np.mean(np.abs(np.log(P[RATE[p]].values / va[RATE[p]].values))) for p in PH]))
                self.alpha_scores, self.alpha = s, min(s, key=s.get)
        k = self.NNUM
        self.wells = sorted(T["well_name"].unique())
        self.t_range = None
        X = self._X(T)
        self.sc = StandardScaler().fit(X[:, :k])
        Xs = np.c_[self.sc.transform(X[:, :k]), X[:, k:]]
        self.m = {ph: Ridge(alpha=self.alpha).fit(Xs, np.log(T[RATE[ph]].values)) for ph in PH}
        self.t_range = train_day_range(T)
        return self

    def predict_daily(self, D):
        D = D.copy()
        k = self.NNUM
        X = self._X(D)
        Xs = np.c_[self.sc.transform(X[:, :k]), X[:, k:]]
        up = D["uptime"].values if "uptime" in D else 1.0
        for ph in PH:
            D[RATE[ph]] = np.exp(self.m[ph].predict(Xs)) * up
        D["liquid_rate_stbd"] = D["oil_rate_stbd"] + D["water_rate_stbd"]
        D["wc"] = D["water_rate_stbd"] / D["liquid_rate_stbd"].replace(0, np.nan)
        return D


class PdsFreeBlend(RefBlend):
    name = "V5-B P_ds-free blend"

    def fit(self, T):
        self.a = CriticalA([("subcrit", "ridge")]).fit(T, uncertainty=False, loo=False)
        self.b = CriticalB().fit(T)
        return self


# ------------------------------------------------------------------------------------------------ V5-A
class IPRAnchored(RefBlend):
    """REF blend with the liquid rate re-anchored on q = J_w (P_r - P_wf) for wells with a usable build-up."""
    name = "V5-A inflow-anchored"

    def __init__(self, buildups: dict | None = None):
        self.buildups = buildups or {}          # well -> (t_shut Timestamp, p_star psia)

    def fit(self, T):
        super().fit(T)
        cut = pd.to_datetime(T["test_mid"]).max()
        self.pr = {w: float(ps) for w, (ts, ps) in self.buildups.items()
                   if pd.Timestamp(ts) + pd.Timedelta(days=BUILDUP_DAYS) <= cut and np.isfinite(ps)}
        self.J = {}
        for w, pr in self.pr.items():
            g = T[T.well_name == w]
            dd = pr - g["dh_pressure_psia"].values
            ok = dd > 10
            if ok.sum() >= 2:
                self.J[w] = float(np.median(g["liquid_rate_stbd"].values[ok] / dd[ok]))
        return self

    def predict_daily(self, D):
        P = super().predict_daily(D)
        Dd = D.reset_index(drop=True)
        up = Dd["uptime"].values if "uptime" in Dd else np.ones(len(Dd))
        f = np.ones(len(Dd))
        for w, J in self.J.items():
            m = (Dd["well_name"].values == w) & (np.asarray(up) > 0)
            if m.any():
                q_ipr = J * np.clip(self.pr[w] - Dd.loc[m, "dh_pressure_psia"].values, 0, None) * np.asarray(up)[m]
                q_ref = P.loc[m, "liquid_rate_stbd"].values
                f[m] = np.where(q_ref > 0, q_ipr / q_ref, 1.0)
        for ph in PH:
            P[RATE[ph]] = P[RATE[ph]].values * f
        P["liquid_rate_stbd"] = P["oil_rate_stbd"] + P["water_rate_stbd"]
        P["ipr_factor"] = f
        return P


# ------------------------------------------------------------------------------------------------ V5-C
SIMPLEX = [w for w in itertools.product([0, 0.25, 0.5, 0.75, 1.0], repeat=3) if abs(sum(w) - 1) < 1e-9]


class Hybrid5:
    """Per-phase convex log-blend of REF, V5-A, V5-B with weights from the inner time split (training tests only)."""
    name = "V5-C IPR + P_ds-free hybrid"

    def __init__(self, buildups: dict | None = None, w: dict | None = None):
        self.buildups, self.w = buildups or {}, w

    def _members(self, T):
        return [RefBlend().fit(T), IPRAnchored(self.buildups).fit(T), PdsFreeBlend().fit(T)]

    def fit(self, T):
        T = T.reset_index(drop=True)
        if self.w is None:
            tr, va = inner_split(T)
            if tr is None:
                self.w = {ph: (1.0, 0.0, 0.0) for ph in PH}
            else:
                V = va.assign(date=va["test_mid"], uptime=1.0)
                Ps = [m.predict_daily(V) for m in self._members(tr)]
                self.w, self.inner = {}, {}
                for ph in PH:
                    L = np.c_[[np.log(np.clip(P[RATE[ph]].values, 1e-9, None)) for P in Ps]].T
                    y = np.log(V[RATE[ph]].values)
                    e = {w: float(np.mean(np.abs(L @ np.array(w) - y))) for w in SIMPLEX}
                    # ties broken toward REF (first member), the simpler, already-validated model
                    best = min(e.values())
                    cand = [w for w in SIMPLEX if e[w] <= best + 1e-12]
                    self.w[ph] = max(cand, key=lambda w: w[0])
                    self.inner[ph] = e[self.w[ph]]
        self.members = self._members(T)
        return self

    def predict_daily(self, D):
        Ps = [m.predict_daily(D).reset_index(drop=True) for m in self.members]
        P = Ps[0].copy()
        for ph in PH:
            c = RATE[ph]
            z = np.any([p[c].values <= 0 for p in Ps], axis=0)
            L = sum(wi * np.log(np.clip(p[c].values, 1e-9, None)) for wi, p in zip(self.w[ph], Ps))
            P[c] = np.where(z, 0.0, np.exp(L))
        P["liquid_rate_stbd"] = P["oil_rate_stbd"] + P["water_rate_stbd"]
        return P


def buildups_from_pipeline(R: dict) -> dict:
    """{well: (t_shut, p*)} from core.app_pipeline.run output (read-only use of the locked PTA results)."""
    out = {}
    for r, _ in R.get("pta", []):
        ps = getattr(r.get("horner"), "p_star_psi", np.nan)
        if r.get("t_shut") is not None and np.isfinite(ps):
            out[r["well_name"]] = (pd.Timestamp(r["t_shut"]), float(ps))
    return out
