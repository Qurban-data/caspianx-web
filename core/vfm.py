"""
Virtual Flow Meter for gas-lifted wells (physics first, built for ~9 well tests per well).

Liquid rate (sub-critical choke, Bernoulli across the restriction)
    q_liq = C_w * (choke/100)^2 * sqrt(P_wh - P_ds) * exp(r(x))          [STB/d]
    C_w   : per-well discharge calibration = median of log(q_test / physics) (1 parameter per well)
    r(x)  : optional pooled residual on log-rate, clipped to +-0.3, features
            ln P_wh, ln(P_wh-P_ds), ln choke, ln q_gl, P_bh/1000.
            Residual learners: none | Ridge | shallow GBDT | Ridge+GBDT average ("ens").
    The Gilbert critical-flow correlation q = C P_wh S^1.89 / GLR^0.546 is kept as a candidate;
    every candidate is scored by leave-one-test-out (LOO) and the best is used.

Phase split (per well)
    WC(t), GOR(t): constant / Theil-Sen linear / interpolation, chosen by LOO.
    After the last test (hidden period): WC = mean(last 3) + Theil-Sen slope, GOR = mean(last 3)
    (rolling-origin backtest: WC error 3.9 % vs 4.4 % for a frozen value).
    q_o = q_liq (1-WC),  q_w = q_liq WC,  q_g = q_o GOR / 1000 [Mscf/d, formation gas].
    Daily rate = steady rate x uptime (fraction of the day the well flowed).

Uncertainty
    P10/P90 bands = point prediction x exp(empirical 10/90 % quantiles of out-of-sample log errors):
    LOO errors inside the tested period, rolling-origin (forward) errors after the last test.
"""
from __future__ import annotations

from typing import Callable, Sequence

import numpy as np
import pandas as pd
from scipy.stats import theilslopes
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .preprocessing import qc_tests, prepare_daily

PHASES = ["oil", "water", "gas"]
RATE = {"liq": "liquid_rate_stbd", "oil": "oil_rate_stbd", "water": "water_rate_stbd", "gas": "gas_rate_mscfd"}
RES_CLIP = 0.3
DEFAULT_CANDIDATES: list[tuple[str, str | None]] = [("gilbert", None), ("gilbert", "ridge"),
                                                    ("subcrit", None), ("subcrit", "ridge")]
# D1 (2026-11-21): the deployed model is pinned to sub-critical physics + Ridge residual. With all 85 tests the LOO
# selection already chose it; pinning removes the small-sample switch to Gilbert seen in the frozen rolling-origin
# protocol (evaluate.py: oil MAPE 7.08 % auto vs 4.79 % pinned). DEFAULT_CANDIDATES stays for validation reports.
DEPLOYED_CANDIDATES: list[tuple[str, str | None]] = [("subcrit", "ridge")]
# D3 (2026-11-21): P10/P90 widening for rows AFTER the last well test (the hidden-period regime). The empirical
# forward band under-covered at a 90-day horizon (protocol coverage ~72 %). Factors = 80 % quantile of the
# band-normalised forward errors of the frozen protocol (evaluate.py prints "band_scale_suggested"; a test keeps
# these constants in sync). Leave-one-cutoff-out coverage with these factors: oil 81.8, water 75.1, gas 75.2 %.
BAND_SCALE_FWD: dict[str, float] = {"liq": 1.55, "oil": 1.55, "water": 1.21, "gas": 1.19}


# ---------------------------------------------------------------- physics baselines
def gilbert_base(whp: np.ndarray, choke_pct: np.ndarray, glr_scf_bbl: np.ndarray, choke_max_64: float = 128):
    """Gilbert (1954) critical choke flow, unit discharge coefficient: (P_wh-14.7) S^1.89 / (10 GLR^0.546),
    S = bean size in 1/64 in, GLR in Mscf/STB."""
    S = np.clip(choke_pct, 0.1, 100) / 100 * choke_max_64
    return (whp - 14.7) * S ** 1.89 / (10 * np.clip(glr_scf_bbl / 1000, 1e-3, None) ** 0.546)


def subcrit_base(whp: np.ndarray, pds: np.ndarray, choke_pct: np.ndarray, b: float = 2.0):
    """Sub-critical choke: (A/A_max)^b * sqrt(P_wh - P_ds); b = 2 (area ~ opening^2), dP floored at 0.5 psi."""
    return (np.clip(choke_pct, 0.1, 100) / 100) ** b * np.sqrt(np.clip(whp - pds, 0.5, None))


# ---------------------------------------------------------------- ratio models (WC, GOR)
TREND_HORIZON_DAYS = 180     # trend frozen beyond last training test + 180 d: forward CV supports the trend up to
                             # ~180 d (oil MAPE 4.07 % at 91-180 d vs 4.64 % frozen at 90 d), no evidence beyond


class TrendModel:
    """y(t) = constant (median) | Theil-Sen line | linear interpolation between tests."""

    def __init__(self, kind: str):
        self.kind = kind

    def fit(self, t, y) -> "TrendModel":
        """Fit the trend on test times t (days) and values y (WC fraction or GOR scf/stb)."""
        t, y = np.asarray(t, float), np.asarray(y, float)
        self.c = float(np.median(y)) if len(y) else float("nan")   # no test -> NaN (filled by the caller)
        if self.kind == "linear" and len(t) >= 3:
            self.s, self.i, _, _ = theilslopes(y, t)
        elif self.kind == "interp" and len(t) >= 2:
            o = np.argsort(t)
            self.t, self.y = t[o], y[o]
        return self

    def predict(self, t) -> np.ndarray:
        """Trend value at days t; after the last test the frozen last-3-tests level + slope is used."""
        t = np.asarray(t, float)
        if self.kind == "linear" and hasattr(self, "s"):
            return self.i + self.s * t
        if self.kind == "interp" and hasattr(self, "t"):
            return np.interp(t, self.t, self.y)
        return np.full(len(t), self.c)


def select_trend(t: np.ndarray, y: np.ndarray, kinds: Sequence[str] = ("const", "linear", "interp")):
    """Pick the trend kind with the lowest LOO RMSE; returns (fitted model, scores).
    Fewer than 3 tests: leave-one-out cannot rank trends -> constant (median) trend."""
    if len(t) < 3:
        return TrendModel("const").fit(t, y), {}
    scores = {}
    for k in kinds:
        e = [TrendModel(k).fit(t[np.arange(len(t)) != i], y[np.arange(len(t)) != i]).predict(t[[i]])[0] - y[i]
             for i in range(len(t))]
        scores[k] = float(np.sqrt(np.mean(np.square(e))))
    best = min(scores, key=scores.get)
    return TrendModel(best).fit(t, y), scores


# ---------------------------------------------------------------- residual learners
def _make_residual(kind: str):
    """Residual learner factory: "ridge" (standardised Ridge, alpha 10), "gbdt", "ens" (average)."""
    if kind == "ridge":
        return make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    if kind == "gbdt":
        return GradientBoostingRegressor(n_estimators=150, max_depth=2, learning_rate=0.05, subsample=0.8,
                                         min_samples_leaf=6, random_state=0)
    raise ValueError(kind)


class _Ensemble:
    """Average of Ridge and GBDT residual predictions (variance reduction on small samples)."""

    def __init__(self):
        self.m = [_make_residual("ridge"), _make_residual("gbdt")]

    def fit(self, X, y):
        """Fit both members of the Ridge/GBDT average."""
        for m in self.m:
            m.fit(X, y)
        return self

    def predict(self, X):
        """Mean of the Ridge and GBDT residual predictions."""
        return np.mean([m.predict(X) for m in self.m], axis=0)


def metrics(a: np.ndarray, p: np.ndarray) -> dict[str, float]:
    """MAE, RMSE, MAPE (%), R^2 of predictions p against actuals a."""
    a, p = np.asarray(a, float), np.asarray(p, float)
    ok = np.isfinite(a) & np.isfinite(p) & (a > 0)
    a, p = a[ok], p[ok]
    return {"MAE": float(np.mean(np.abs(p - a))), "RMSE": float(np.sqrt(np.mean((p - a) ** 2))),
            "MAPE_%": float(np.mean(np.abs(p - a) / a) * 100),
            "R2": float(1 - np.sum((p - a) ** 2) / np.sum((a - a.mean()) ** 2)), "n": int(ok.sum())}


# ---------------------------------------------------------------- VFM
class GasLiftVFM:
    """Hybrid physics + residual VFM. ``fit(tests)`` then ``predict_daily(daily)``.

    Parameters
    ----------
    candidates : list of (physics, residual) pairs scored by LOO; residual in {None, 'ridge', 'gbdt', 'ens'}.
    t0         : time origin for trend models.
    """

    def __init__(self, candidates: Sequence[tuple[str, str | None]] | None = None, t0: str = "2025-01-01"):
        self.candidates = list(candidates or DEPLOYED_CANDIDATES)
        self.t0 = pd.Timestamp(t0)

    # -- helpers
    def _days(self, ts) -> np.ndarray:
        """Days since the model time origin (first training test) - used only as a trend coordinate."""
        return (pd.to_datetime(pd.Series(ts)) - self.t0).dt.total_seconds().values / 86400

    def train_period(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        """First and last well-test time the model was trained on."""
        t = pd.to_datetime(self.T["test_mid"])
        return t.min(), t.max()

    def _clip_days(self, td: np.ndarray) -> np.ndarray:
        """Trend coordinate limited to [first test - H, last test + H], H = ``trend_horizon_days`` (default 180 d,
        the longest horizon the forward study covers). Beyond it WC/GOR trends are frozen instead of extrapolated linearly in time.
        Rows inside the training period +- H are unchanged (portal files byte-identical)."""
        H = getattr(self, "trend_horizon_days", TREND_HORIZON_DAYS)
        if H is None or not hasattr(self, "T"):
            return td
        t0, t1 = self.train_period()
        lo, hi = self._days([t0])[0] - H, self._days([t1])[0] + H
        return np.clip(td, lo, hi)

    def _phys(self, df: pd.DataFrame, kind: str) -> np.ndarray:
        """Physics liquid-rate base (no C_w): sub-critical choke (choke/100)^2 * sqrt(WHP - P_ds) or Gilbert."""
        if kind == "gilbert":
            return gilbert_base(df["whp_psia"].values, df["choke_opening_pct"].values, df["glr_total"].values)
        return subcrit_base(df["whp_psia"].values, df["whp_downstream_psia"].values, df["choke_opening_pct"].values)

    @staticmethod
    def _resid_X(T: pd.DataFrame) -> np.ndarray:
        """Residual design: ln WHP, ln dP_choke, ln choke, ln lift gas, P_bh/1000 (same columns for tests and daily rows)."""
        return np.c_[np.log(T["whp_psia"]), np.log(np.clip(T["whp_psia"] - T["whp_downstream_psia"], 0.5, None)),
                     np.log(np.clip(T["choke_opening_pct"], 0.1, None)),
                     np.log(np.clip(T["gas_lift_rate_mscfd"], 1, None)), T["dh_pressure_psia"] / 1000]

    def _fit_core(self, T: pd.DataFrame, kind: str, residual: str | None) -> dict:
        """Fit per-well C_w (median log ratio test/physics) and the optional pooled residual on the training tests only."""
        lr = np.log(T["liquid_rate_stbd"].values) - np.log(self._phys(T, kind))
        Cw = pd.Series(lr).groupby(T["well_name"].values).median().to_dict()
        res_m = None
        if residual:
            r = lr - T["well_name"].map(Cw).values
            res_m = (_Ensemble() if residual == "ens" else _make_residual(residual)).fit(self._resid_X(T), r)
        return {"kind": kind, "residual": residual, "Cw": Cw, "res": res_m}

    def _pred_core(self, M: dict, T: pd.DataFrame) -> np.ndarray:
        """Liquid rate = C_w * physics * exp(clip(residual, +-0.3)); unknown wells get NaN (handled by inference adapters)."""
        lr = np.log(self._phys(T, M["kind"])) + T["well_name"].map(M["Cw"]).values
        if M["res"] is not None:
            X = self._resid_X(T)
            ok = np.isfinite(X).all(axis=1)
            r = np.zeros(len(T))
            if ok.any():
                r[ok] = M["res"].predict(X[ok])
            lr = lr + np.clip(r, -RES_CLIP, RES_CLIP)
        return np.exp(lr)

    @staticmethod
    def _label(c) -> str:
        """Readable candidate name, e.g. "subcrit+ridge"."""
        return f"{c[0]}{'+' + c[1] if c[1] else ''}"

    # -- training
    def fit(self, T: pd.DataFrame, uncertainty: bool = True, loo: bool = True) -> "GasLiftVFM":
        """Fit on QC-ed tests. LOO selects the liquid model; WC/GOR trends per well; optional P10/P90.
        ``loo=False`` (single candidate only) skips the internal LOO; used inside outer CV folds."""
        T = T.reset_index(drop=True)
        self.T = T
        y = T["liquid_rate_stbd"].values
        n = len(T)
        if not loo and len(self.candidates) == 1:
            self.choice = self.candidates[0]
            self.M = self._fit_core(T, *self.choice)
            self._fit_ratios(T)
            return self
        loo = {c: np.zeros(n) for c in self.candidates}
        for i in range(n):
            m = np.arange(n) != i
            for c in self.candidates:
                loo[c][i] = self._pred_core(self._fit_core(T[m], *c), T.iloc[[i]])[0]
        self.liq_scores = {self._label(c): float(np.mean(np.abs(v - y) / y) * 100) for c, v in loo.items()}
        self.choice = min(self.candidates, key=lambda c: np.mean(np.abs(loo[c] - y) / y))
        self.M = self._fit_core(T, *self.choice)
        wc_loo, gor_loo = self._fit_ratios(T)
        ql = loo[self.choice]
        self.loo = pd.DataFrame({"well_name": T["well_name"], "test_mid": T["test_mid"],
                                 "liq_test": y, "liq_loo": ql,
                                 "oil_test": T["oil_rate_stbd"], "oil_loo": ql * (1 - wc_loo),
                                 "water_test": T["water_rate_stbd"], "water_loo": ql * wc_loo,
                                 "gas_test": T["gas_rate_mscfd"], "gas_loo": ql * (1 - wc_loo) * gor_loo / 1000})
        self.scores = {ph: metrics(self.loo[f"{ph}_test"], self.loo[f"{ph}_loo"]) for ph in ["liq"] + PHASES}
        if uncertainty:
            self._fit_uncertainty()
        return self

    def _fit_ratios(self, T: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Per-well WC/GOR trend selection + forward rule; returns LOO WC and GOR at each test."""
        n = len(T)
        self.wc, self.gor, self.ratio_scores = {}, {}, {}
        tt = self._days(T["test_mid"])
        wc_loo, gor_loo = np.zeros(n), np.zeros(n)
        for w, g in T.groupby("well_name"):
            idx = g.index.values
            self.wc[w], _ = select_trend(tt[idx], g["wc"].values)
            self.gor[w], _ = select_trend(tt[idx], g["gor"].values)
            self.ratio_scores[w] = {"wc": self.wc[w].kind, "gor": self.gor[w].kind}
            for i in idx:
                m = idx != i
                wc_loo[i] = TrendModel(self.wc[w].kind).fit(tt[idx][m], g["wc"].values[m]).predict(tt[[i]])[0]
                gor_loo[i] = TrendModel(self.gor[w].kind).fit(tt[idx][m], g["gor"].values[m]).predict(tt[[i]])[0]
        # a well with a single test has no leave-one-out estimate: use the median of the other wells' tests
        for arr, col in ((wc_loo, "wc"), (gor_loo, "gor")):
            bad = ~np.isfinite(arr)
            if bad.any():
                arr[bad] = float(np.nanmedian(T[col].values))
        self.fwd = {}
        for w, g in T.groupby("well_name"):
            tw = tt[g.index.values]
            sl = theilslopes(g["wc"].values, tw)[0] if len(g) >= 4 else 0.0
            self.fwd[w] = {"t_last": float(tw[-1]), "t_anchor": float(np.mean(tw[-3:])),
                           "wc0": float(np.mean(g["wc"].values[-3:])), "wc_slope": float(sl),
                           "gor0": float(np.mean(g["gor"].values[-3:]))}
        return wc_loo, gor_loo

    def _fit_uncertainty(self, cutoffs: Sequence[str] | None = None):
        """Empirical log-error quantiles: LOO (interpolation) and rolling-origin forward (extrapolation).

        D3 (2026-11-21): inner forward cutoffs are RELATIVE to the training data (month starts 4, 3 and 2 months
        before the last test) instead of fixed 2025 dates. With fixed dates a model trained on few tests had no
        inner forward fold and silently fell back to the narrower LOO band (90-day coverage 46-67 %)."""
        if cutoffs is None:
            last = pd.Timestamp(self.T["test_mid"].max())
            first = pd.Timestamp(self.T["test_mid"].min())
            cand = [(last - pd.DateOffset(months=k)).to_period("M").to_timestamp() for k in (4, 3, 2)]
            cutoffs = [str(c.date()) for c in cand if c > first + pd.Timedelta(days=30)]
        def q(e):
            """Empirical 10/90 % log-error quantiles (P10/P90 band half-widths)."""
            e = np.asarray(e, float)
            e = e[np.isfinite(e)]
            return (float(np.quantile(e, 0.1)), float(np.quantile(e, 0.9))) if len(e) >= 8 else (-0.1, 0.1)
        self.band_loo = {ph: q(np.log(self.loo[f"{ph}_test"] / self.loo[f"{ph}_loo"])) for ph in ["liq"] + PHASES}
        fw = forward_predictions(self.T, lambda: GasLiftVFM([self.choice]), cutoffs)
        self.band_fwd = {ph: q(np.log(fw[f"{ph}_test"] / fw[f"{ph}_pred"])) for ph in ["liq"] + PHASES} \
            if len(fw) else self.band_loo

    # -- inference
    def predict_daily(self, D: pd.DataFrame) -> pd.DataFrame:
        """D: one row per well-day with well_name, date, whp_psia, whp_downstream_psia, dh_pressure_psia,
        gas_lift_rate_mscfd, choke_opening_pct, uptime (0-1). Adds liquid/oil/water/gas rates, WC, GOR and
        P10/P90 columns ``<phase>_p10`` / ``<phase>_p90`` when uncertainty was fitted."""
        D = D.copy().reset_index(drop=True)
        td = self._clip_days(self._days(D["date"]))
        wc = np.array([self.wc[w].predict([x])[0] if w in self.wc else np.nan for w, x in zip(D["well_name"], td)])
        gor = np.array([self.gor[w].predict([x])[0] if w in self.gor else np.nan for w, x in zip(D["well_name"], td)])
        after = np.zeros(len(D), bool)
        for i, (w, x) in enumerate(zip(D["well_name"], td)):
            f = self.fwd.get(w)
            if f is not None and x > f["t_last"]:
                wc[i] = f["wc0"] + f["wc_slope"] * (x - f["t_anchor"])
                gor[i] = f["gor0"]
                after[i] = True
        wc, gor = np.clip(wc, 0, 0.99), np.clip(gor, 0, None)
        if self.choice[0] == "gilbert":       # GLR depends on q: damped fixed-point iteration
            q0 = np.full(len(D), 1000.0)
            for _ in range(30):
                glr = (gor * (1 - wc) * q0 / 1000 + D["gas_lift_rate_mscfd"].values) * 1000 / np.clip(q0, 1, None)
                q1 = self._pred_core(self.M, D.assign(glr_total=glr))
                if np.nanmax(np.abs(q1 - q0) / np.clip(q0, 1, None)) < 1e-6:
                    break
                q0 = 0.5 * q0 + 0.5 * q1
            q = q1
        else:
            q = self._pred_core(self.M, D)
        up = D["uptime"].values if "uptime" in D else 1.0
        D["liquid_rate_stbd"] = q * up
        D["oil_rate_stbd"] = D["liquid_rate_stbd"] * (1 - wc)
        D["water_rate_stbd"] = D["liquid_rate_stbd"] * wc
        D["gas_rate_mscfd"] = D["oil_rate_stbd"] * gor / 1000
        D["wc"], D["gor"], D["after_last_test"] = wc, gor, after
        if hasattr(self, "band_loo"):
            for ph in PHASES:
                sc = getattr(self, "band_scale", BAND_SCALE_FWD).get(ph, 1.0)
                lo = np.where(after, self.band_fwd[ph][0] * sc, self.band_loo[ph][0])
                hi = np.where(after, self.band_fwd[ph][1] * sc, self.band_loo[ph][1])
                D[f"{ph}_p10"] = D[RATE[ph]] * np.exp(lo)
                D[f"{ph}_p90"] = D[RATE[ph]] * np.exp(hi)
        return D

    def predict_tests(self, T: pd.DataFrame) -> pd.DataFrame:
        """Predict well-test rows at their test conditions (uptime = 1, date = test mid-time)."""
        P = self.predict_daily(T.assign(date=T["test_mid"], uptime=1.0))
        return pd.DataFrame({f"{ph}_pred": P[RATE[ph]].values for ph in ["liq"] + PHASES}
                            | {f"{ph}_test": T[RATE[ph]].values for ph in ["liq"] + PHASES}
                            | {"well_name": T["well_name"].values, "test_mid": T["test_mid"].values})


# ---------------------------------------------------------------- out-of-sample validation
def forward_predictions(T: pd.DataFrame, factory: Callable[[], GasLiftVFM],
                        cutoffs: Sequence[str] = ("2025-06-01", "2025-07-01", "2025-08-01")) -> pd.DataFrame:
    """Rolling-origin: train on tests before each cutoff, predict ALL later tests (mimics the hidden Oct-Dec set)."""
    out = []
    for c in cutoffs:
        tr, te = T[T.test_mid < pd.Timestamp(c)], T[T.test_mid >= pd.Timestamp(c)]
        if len(te) == 0 or tr.well_name.nunique() < T.well_name.nunique():
            continue
        out.append(factory().fit(tr, uncertainty=False, loo=False).predict_tests(te).assign(cutoff=c))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def blocked_predictions(T: pd.DataFrame, factory: Callable[[], GasLiftVFM], scheme: str = "loo") -> pd.DataFrame:
    """'loo' = leave-one-test-out; 'month' = all wells' tests of one calendar month held out together
    (blocks leakage through simultaneous field conditions in the pooled residual)."""
    T = T.reset_index(drop=True)
    groups = np.arange(len(T)) if scheme == "loo" else T["test_mid"].dt.to_period("M").astype(str).values
    out = []
    for g in pd.unique(groups):
        te = groups == g
        out.append(factory().fit(T[~te], uncertainty=False, loo=False).predict_tests(T[te]))
    return pd.concat(out, ignore_index=True)


def validation_report(T: pd.DataFrame, models: dict[str, Callable[[], GasLiftVFM]],
                      schemes: Sequence[str] = ("loo", "month", "forward")) -> pd.DataFrame:
    """Metrics table: model x scheme x phase -> MAE, RMSE, MAPE, R^2."""
    rows = []
    for name, fac in models.items():
        for s in schemes:
            P = forward_predictions(T, fac) if s == "forward" else blocked_predictions(T, fac, s)
            for ph in ["liq"] + PHASES:
                rows.append({"model": name, "scheme": s, "phase": ph, **metrics(P[f"{ph}_test"], P[f"{ph}_pred"])})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- end-to-end
def run_vfm(daily: pd.DataFrame, tests_raw: pd.DataFrame, choke: pd.DataFrame,
            candidates: Sequence[tuple[str, str | None]] | None = None, model: GasLiftVFM | None = None,
            completion: pd.DataFrame | None = None) -> dict:
    """QC tests -> fit (or reuse ``model``) -> daily rates for every well-day; zero rate on full shut-in days.

    Unseen-data adapters (no effect on wells that have tests and choke data):
    * well without any well test -> field-median C_w / WC, GOR = solution GOR R_s from ``completion`` when given
      (undersaturated oil), flagged ``calibrated = False``;
    * well without any choke record -> field-median daily choke opening, flagged ``imputed`` and logged."""
    T, qclog = qc_tests(tests_raw, choke)
    m = model if model is not None else GasLiftVFM(candidates).fit(T)
    D = prepare_daily(daily, choke)
    log = [] if qclog is None or len(qclog) == 0 else qclog.to_dict("records")
    nochoke = [w for w, g in D.groupby("well_name") if g["choke_opening_pct"].isna().all()]
    if nochoke:
        med = D[~D.well_name.isin(nochoke)].groupby("date")["choke_opening_pct"].median()
        fill = D["date"].map(med).fillna(D["choke_opening_pct"].median())
        msk = D.well_name.isin(nochoke)
        D.loc[msk, "choke_opening_pct"] = fill[msk].fillna(50.0)
        D.loc[msk, "imputed"] = True
        log += [{"well": w, "test_start": "all", "column": "choke_opening_pct",
                 "issue": "no choke data: field-median opening used (flagged imputed)", "old": None, "new": None} for w in nochoke]
    new = sorted(set(D.well_name) - set(m.M["Cw"]))
    mp = m
    if new:
        from .inference import _with_new_wells
        rs = None
        if completion is not None and "solution_gor_at_pb_scf_stb" in completion:
            rs = completion.set_index("well_name")["solution_gor_at_pb_scf_stb"].to_dict()
        mp, _ = _with_new_wells(m, new, rs)
        log += [{"well": w, "test_start": "all", "column": "calibration",
                 "issue": "no well test: field-median C_w / WC, GOR = R_s if known (uncalibrated)", "old": None, "new": None}
                for w in new]
    P = mp.predict_daily(D)
    P["calibrated"] = ~P["well_name"].isin(new)
    qclog = pd.DataFrame(log)
    zero = [c for c in P.columns if c.startswith(("liquid_rate", "oil_", "water_", "gas_rate", "gas_p"))]
    P.loc[P["uptime"] <= 0, zero] = 0.0
    return {"model": m, "tests": T, "qc_log": qclog, "daily": P}
