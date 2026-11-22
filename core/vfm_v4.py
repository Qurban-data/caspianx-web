"""VFM v4 candidates (experiment only; the deployed model in core/vfm.py + core/vfm_direct.py is unchanged).

C1 PurePhysics      sub-critical choke physics, per-well C_w, WC/GOR trends (GasLiftVFM without residual).
C2 PhysicsML        C1 + three small residual layers, each with its regularisation chosen on an inner time split
                    of the TRAINING tests (option "off" always included, so the layer can stay dead):
                      liquid: Ridge / Huber on ln(q_test / q_phys) with ln WHP, ln dP_choke, ln choke, ln q_gl, P_bh;
                      GOR   : within-well Ridge on ln GOR deviations (P_bh, ln q_gl, ln dP, ln WHP; free gas and
                              lift-gas separation effects), added to the trend;
                      WC    : within-well Ridge on logit WC deviations (same features).
                    All residuals clipped to +-0.3 in log / logit space.
C3 ConstrainedB     direct log-rate regression per phase (oil, water, gas) with hard physical bounds:
                      d ln q / d ln choke in [1.0, 2.2], d ln q / d ln dP_choke >= 0, d ln q / d ln q_gl >= 0,
                      d ln q / d (P_bh/1000) <= 0 (higher flowing BHP = smaller drawdown); per-well intercepts and
                      a time term (clipped to the training range +- 180 d) are free; ridge penalty on the
                      standardised slopes, alpha on an inner time split. Log target = relative-error loss.
C4 Hybrid           exponential recency weights (tau on the inner split, "none" included) for C2 and C3, a
                    per-phase convex blend of C2 and C3 (weight on the inner split), smooth shrinkage to C1 on
                    rows whose inputs leave the well's tested envelope, exact 0.0 on shut-in rows.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import lsq_linear
from sklearn.linear_model import HuberRegressor, Ridge

from .vfm import GasLiftVFM, RATE, subcrit_base

PH = ["oil", "water", "gas"]
CLIP = 0.3
INNER_DAYS = 45
ENV_COLS = ["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd", "choke_opening_pct"]


def inner_split(T: pd.DataFrame, days: int = INNER_DAYS):
    """Last ``days`` of the training tests as inner validation (wells must also appear in the inner train part)."""
    t = pd.to_datetime(T["test_mid"])
    end = t.max()
    tr, va = T[t < end - pd.Timedelta(days=days)], T[t >= end - pd.Timedelta(days=days)]
    va = va[va.well_name.isin(tr.well_name.unique())]
    return (tr, va) if len(tr) >= 10 and len(va) >= 3 else (None, None)


def recency_weights(T: pd.DataFrame, tau: float | None) -> np.ndarray:
    if tau is None:
        return np.ones(len(T))
    t = pd.to_datetime(T["test_mid"])
    return np.exp(-((t.max() - t).dt.total_seconds().values / 86400) / tau)


def _logratio_err(P: pd.DataFrame, V: pd.DataFrame, phases=PH) -> float:
    """Mean absolute log error over phases (relative-error loss used for every inner choice)."""
    return float(np.mean([np.mean(np.abs(np.log(P[RATE[p]].values / V[RATE[p]].values))) for p in phases]))


def _feat(T: pd.DataFrame) -> np.ndarray:
    return np.c_[np.log(T["whp_psia"]), np.log(np.clip(T["whp_psia"] - T["whp_downstream_psia"], 0.5, None)),
                 np.log(np.clip(T["choke_opening_pct"], 0.1, None)), np.log(np.clip(T["gas_lift_rate_mscfd"], 1, None)),
                 T["dh_pressure_psia"] / 1000]


def _ratio_feat(T: pd.DataFrame) -> np.ndarray:
    return np.c_[T["dh_pressure_psia"] / 1000, np.log(np.clip(T["gas_lift_rate_mscfd"], 1, None)),
                 np.log(np.clip(T["whp_psia"] - T["whp_downstream_psia"], 0.5, None)), np.log(T["whp_psia"])]


# ======================================================================= C1
class PurePhysics:
    name = "C1 pure physics"

    def __init__(self, tau: float | None = None):
        self.tau = tau

    def fit(self, T: pd.DataFrame):
        T = T.reset_index(drop=True)
        self.base = GasLiftVFM([("subcrit", None)]).fit(T, uncertainty=False, loo=False)
        if self.tau is not None:          # recency-weighted C_w (weighted mean of the log ratio)
            w = recency_weights(T, self.tau)
            lr = np.log(T["liquid_rate_stbd"].values) - np.log(self.base._phys(T, "subcrit"))
            self.base.M["Cw"] = {k: float(np.average(lr[g.index], weights=w[g.index])) for k, g in T.groupby("well_name")}
        self.env = T.groupby("well_name")[ENV_COLS].agg(["min", "max"])
        return self

    def predict_daily(self, D: pd.DataFrame) -> pd.DataFrame:
        return self.base.predict_daily(D)


# ======================================================================= C2
class PhysicsML(PurePhysics):
    name = "C2 physics + ML residual"
    LIQ = [("off", None), ("ridge", 0.1), ("ridge", 1.0), ("ridge", 10.0), ("ridge", 100.0), ("huber", 1.0)]
    RAT = [None, 0.3, 1.0, 3.0, 10.0, 30.0]          # None = layer off

    def __init__(self, tau: float | None = None, liq=None, gor="auto", wc="auto"):
        super().__init__(tau)
        self.liq, self.gor_a, self.wc_a = liq, gor, wc

    def _fit_layers(self, T, w):
        Tm = T.reset_index(drop=True)
        X = _feat(Tm)
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        Xs = (X - self.mu) / self.sd
        lr = np.log(Tm["liquid_rate_stbd"].values) - np.log(self.base._phys(Tm, "subcrit"))
        r = lr - Tm["well_name"].map(self.base.M["Cw"]).values
        kind, a = self.liq
        self.liq_m = None if kind == "off" else (Ridge(alpha=a) if kind == "ridge" else HuberRegressor(alpha=a, epsilon=1.35)).fit(Xs, r, sample_weight=w)
        Z = pd.DataFrame(_ratio_feat(Tm))
        g = Tm["well_name"].values
        self.zmean = Z.groupby(g).mean()
        Zd = (Z - Z.groupby(g).transform("mean")).values
        self.zsd = Zd.std(0) + 1e-9
        Zd = Zd / self.zsd
        ygor = np.log(Tm["gor"].values)
        ygor = ygor - pd.Series(ygor).groupby(g).transform("mean").values
        wc = np.clip(Tm["wc"].values, 1e-3, 0.999)
        ywc = np.log(wc / (1 - wc))
        ywc = ywc - pd.Series(ywc).groupby(g).transform("mean").values
        self.gor_m = None if self.gor_a is None else Ridge(alpha=self.gor_a, fit_intercept=False).fit(Zd, ygor, sample_weight=w)
        self.wc_m = None if self.wc_a is None else Ridge(alpha=self.wc_a, fit_intercept=False).fit(Zd, ywc, sample_weight=w)

    def fit(self, T: pd.DataFrame):
        T = T.reset_index(drop=True)
        if self.liq is None or self.gor_a == "auto" or self.wc_a == "auto":
            tr, va = inner_split(T)
            if tr is None:
                self.liq, self.gor_a, self.wc_a = ("ridge", 10.0), None, None
            else:
                V = va.assign(date=va["test_mid"], uptime=1.0)
                best = {}
                for liq in self.LIQ:          # liquid layer chosen on liquid error (gor/wc off)
                    m = PhysicsML(self.tau, liq, None, None).fit(tr)
                    best[liq] = _logratio_err(m.predict_daily(V).assign(liq=0), V, ["oil", "water"])
                self.liq = min(best, key=best.get)
                sg = {a: _logratio_err(PhysicsML(self.tau, self.liq, a, None).fit(tr).predict_daily(V), V, ["gas"]) for a in self.RAT}
                self.gor_a = min(sg, key=sg.get)
                sw = {a: _logratio_err(PhysicsML(self.tau, self.liq, self.gor_a, a).fit(tr).predict_daily(V), V, ["water"]) for a in self.RAT}
                self.wc_a = min(sw, key=sw.get)
                self.inner = {"liq": best, "gor": sg, "wc": sw}
        super().fit(T)
        self._fit_layers(T, recency_weights(T, self.tau))
        return self

    def predict_daily(self, D: pd.DataFrame) -> pd.DataFrame:
        P = super().predict_daily(D).reset_index(drop=True)
        Dd = D.reset_index(drop=True)
        up = Dd["uptime"].values if "uptime" in Dd else 1.0
        X = _feat(Dd)
        ok = np.isfinite(X).all(1)
        rl = np.zeros(len(Dd))
        if self.liq_m is not None and ok.any():
            rl[ok] = self.liq_m.predict((X[ok] - self.mu) / self.sd)
        Z = _ratio_feat(Dd)
        zm = self.zmean.reindex(Dd["well_name"].values).values
        Zd = (Z - zm) / self.zsd
        okz = np.isfinite(Zd).all(1)
        dg, dw = np.zeros(len(Dd)), np.zeros(len(Dd))
        if self.gor_m is not None and okz.any():
            dg[okz] = self.gor_m.predict(Zd[okz])
        if self.wc_m is not None and okz.any():
            dw[okz] = self.wc_m.predict(Zd[okz])
        q = P["liquid_rate_stbd"].values * np.exp(np.clip(rl, -CLIP, CLIP))
        wc = np.clip(P["wc"].values, 1e-3, 0.999)
        wc = 1 / (1 + np.exp(-(np.log(wc / (1 - wc)) + np.clip(dw, -CLIP, CLIP))))
        gor = P["gor"].values * np.exp(np.clip(dg, -CLIP, CLIP))
        P["liquid_rate_stbd"], P["wc"], P["gor"] = q, wc, gor
        P["oil_rate_stbd"], P["water_rate_stbd"] = q * (1 - wc), q * wc
        P["gas_rate_mscfd"] = P["oil_rate_stbd"] * gor / 1000
        z = np.asarray(up) <= 0 if np.ndim(up) else np.zeros(len(P), bool)
        for c in ["liquid_rate_stbd", "oil_rate_stbd", "water_rate_stbd", "gas_rate_mscfd"]:
            P.loc[z, c] = 0.0
        return P


# ======================================================================= C3
# slope columns: ln choke, ln dP, ln q_gl, ln WHP, P_bh/1000, t/100 ; bounds per column (raw units)
B_LO = np.array([1.0, 0.0, 0.0, -np.inf, -np.inf, -np.inf])
B_HI = np.array([2.2, np.inf, np.inf, np.inf, 0.0, np.inf])


class ConstrainedB:
    name = "C3 physics-constrained B"
    ALPHAS = (0.01, 0.1, 1.0, 10.0)
    H = 180

    def __init__(self, alpha: float | None = None, tau: float | None = None):
        self.alpha, self.tau = alpha, tau

    def _num(self, T):
        t = (pd.to_datetime(T["date"] if "date" in T else T["test_mid"]) - pd.Timestamp("2025-01-01")).dt.days.values.astype(float)
        if getattr(self, "t_range", None) is not None:
            t = np.clip(t, self.t_range[0] - self.H, self.t_range[1] + self.H)
        return np.c_[np.log(np.clip(T["choke_opening_pct"], 0.1, None)), np.log(np.clip(T["whp_psia"] - T["whp_downstream_psia"], 0.5, None)),
                     np.log(np.clip(T["gas_lift_rate_mscfd"], 1, None)), np.log(T["whp_psia"]), T["dh_pressure_psia"] / 1000, t / 100]

    def fit(self, T: pd.DataFrame):
        T = T.reset_index(drop=True)
        if self.alpha is None:
            tr, va = inner_split(T)
            if tr is None:
                self.alpha = 1.0
            else:
                V = va.assign(date=va["test_mid"], uptime=1.0)
                s = {a: _logratio_err(ConstrainedB(a, self.tau).fit(tr).predict_daily(V), V) for a in self.ALPHAS}
                self.alpha_scores, self.alpha = s, min(s, key=s.get)
        self.t_range = None
        self.wells = sorted(T.well_name.unique())
        Xn = self._num(T)
        self.mu, self.sd = Xn.mean(0), Xn.std(0) + 1e-9
        Xs = (Xn - self.mu) / self.sd
        W = (np.asarray(T["well_name"], dtype=object)[:, None] == np.array(self.wells, dtype=object)[None, :]).astype(float)
        A = np.c_[Xs, W]
        k = Xs.shape[1]
        sw = np.sqrt(recency_weights(T, self.tau))
        # bounds on standardised slopes: raw = std_coef / sd ; intercepts free
        lo = np.r_[B_LO * self.sd, np.full(W.shape[1], -np.inf)]
        hi = np.r_[B_HI * self.sd, np.full(W.shape[1], np.inf)]
        lo, hi = np.where(np.isfinite(lo), lo, -np.inf), np.where(np.isfinite(hi), hi, np.inf)
        pen = np.c_[np.sqrt(self.alpha) * np.eye(k), np.zeros((k, W.shape[1]))]
        self.coef = {}
        for ph in PH:
            y = np.log(T[RATE[ph]].values)
            # centre intercepts: the well intercept absorbs the level, mean of slopes over standardised X is zero
            res = lsq_linear(np.r_[A * sw[:, None], pen], np.r_[y * sw, np.zeros(k)], bounds=(lo, hi), method="bvls")
            self.coef[ph] = res.x
        self.t_range = ((pd.to_datetime(T["test_mid"]) - pd.Timestamp("2025-01-01")).dt.days.min(),
                        (pd.to_datetime(T["test_mid"]) - pd.Timestamp("2025-01-01")).dt.days.max())
        self.env = T.groupby("well_name")[ENV_COLS].agg(["min", "max"])
        return self

    def raw_slopes(self) -> pd.DataFrame:
        k = len(self.mu)
        return pd.DataFrame({ph: self.coef[ph][:k] / self.sd for ph in PH},
                            index=["ln choke", "ln dP_choke", "ln q_gl", "ln WHP", "P_bh/1000", "t/100"])

    def predict_daily(self, D: pd.DataFrame) -> pd.DataFrame:
        D = D.copy().reset_index(drop=True)
        Xs = (self._num(D) - self.mu) / self.sd
        W = (np.asarray(D["well_name"], dtype=object)[:, None] == np.array(self.wells, dtype=object)[None, :]).astype(float)
        A = np.c_[Xs, W]
        up = D["uptime"].values if "uptime" in D else 1.0
        for ph in PH:
            D[RATE[ph]] = np.exp(A @ self.coef[ph]) * up
        D["liquid_rate_stbd"] = D["oil_rate_stbd"] + D["water_rate_stbd"]
        D["wc"] = D["water_rate_stbd"] / D["liquid_rate_stbd"].replace(0, np.nan)
        return D


# ======================================================================= C4
def ood_score(D: pd.DataFrame, env: pd.DataFrame, tol: float = 0.05, ramp: float = 0.20) -> np.ndarray:
    """0 inside the well's tested envelope (+-tol), rising linearly to 1 at ``ramp`` relative exceedance."""
    s = np.zeros(len(D))
    for c in ENV_COLS:
        lo = D["well_name"].map(env[(c, "min")]).values * (1 - tol)
        hi = D["well_name"].map(env[(c, "max")]).values * (1 + tol)
        x = D[c].values.astype(float)
        with np.errstate(invalid="ignore", divide="ignore"):
            ex = np.where(x < lo, (lo - x) / np.abs(lo), np.where(x > hi, (x - hi) / np.abs(hi), 0.0))
        s = np.maximum(s, np.nan_to_num(ex / ramp, nan=0.0))
    return np.clip(s, 0, 1)


class Hybrid:
    name = "C4 OOD-safe recency hybrid"
    TAUS = (None, 180.0, 90.0)
    WGRID = (0.0, 0.25, 0.5, 0.75, 1.0)

    def __init__(self, tau="auto", w=None):
        self.tau, self.w = tau, w

    def fit(self, T: pd.DataFrame):
        T = T.reset_index(drop=True)
        if self.tau == "auto" or self.w is None:
            tr, va = inner_split(T)
            if tr is None:
                self.tau, self.w = None, {ph: 0.5 for ph in PH}
            else:
                V = va.assign(date=va["test_mid"], uptime=1.0)
                sc = {}
                for tau in self.TAUS:
                    a = PhysicsML(tau).fit(tr).predict_daily(V)
                    b = ConstrainedB(None, tau).fit(tr).predict_daily(V)
                    wbest, err = {}, []
                    for ph in PH:
                        e = {w: np.mean(np.abs(w * np.log(a[RATE[ph]].values) + (1 - w) * np.log(b[RATE[ph]].values)
                                               - np.log(V[RATE[ph]].values))) for w in self.WGRID}
                        wbest[ph] = min(e, key=e.get)
                        err.append(e[wbest[ph]])
                    sc[tau] = (float(np.mean(err)), wbest)
                self.inner = {str(k): v[0] for k, v in sc.items()}
                self.tau = min(sc, key=lambda k: sc[k][0])
                self.w = sc[self.tau][1]
        self.a = PhysicsML(self.tau).fit(T)
        self.b = ConstrainedB(None, self.tau).fit(T)
        self.p = PurePhysics().fit(T)
        self.env = self.p.env
        return self

    def predict_daily(self, D: pd.DataFrame, ood_shrink: bool = True) -> pd.DataFrame:
        D = D.reset_index(drop=True)
        A, B, C = self.a.predict_daily(D), self.b.predict_daily(D), self.p.predict_daily(D).reset_index(drop=True)
        s = ood_score(D, self.env) if ood_shrink else np.zeros(len(D))
        P = A.copy()
        up = D["uptime"].values if "uptime" in D else np.ones(len(D))
        for ph in PH:
            c = RATE[ph]
            a, b, p = (np.clip(x[c].values.astype(float), 1e-9, None) for x in (A, B, C))
            h = self.w[ph] * np.log(a) + (1 - self.w[ph]) * np.log(b)
            P[c] = np.exp((1 - s) * h + s * np.log(p))
            P.loc[np.asarray(up) <= 0, c] = 0.0
        P["liquid_rate_stbd"] = P["oil_rate_stbd"] + P["water_rate_stbd"]
        P["ood_score"] = s
        return P
