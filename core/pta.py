"""
Pressure Transient Analysis (PTA) of build-up tests, single-phase oil, oilfield units
(psi, STB/d, rb/STB, cp, md, ft, hr, 1/psi).

Methods
-------
1. Straight line (classic): Horner  p_ws = p* - m log10((t_p+dt)/dt)  and MDH  p_ws vs log10(dt)
       k      = 162.6 q B mu / (m h)                                       [md]
       s      = 1.151 [ (p_1hr - p_wf)/m - log10(k/(phi mu c_t r_w^2)) + 3.2275 ]
       dP_skin = 0.869 m s = 141.2 q B mu s / (k h)                         [psi]
2. Log-log diagnostic: dP and Bourdet derivative d(dP)/d ln(dt_e) vs Agarwal time dt_e = dt t_p/(t_p+dt);
   unit slope = wellbore storage, flat derivative = infinite-acting radial flow (IARF, deriv = m/2.303).
3. Regression (official values): Laplace-space radial model with wellbore storage C, skin s and a
   closed circular drainage boundary r_e (from the drainage area, fitted within x3), inverted with
   Stehfest (N=12) and superposed for the build-up; multi-start least squares on log(dP).
   Reported m = 162.6 q B mu/(k h) consistent with the regression k (same convention as the reference).

q = VFM daily oil rate on the last full-uptime day before shut-in; t_p = N_p/q (Horner pseudo-time).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from math import factorial, log
from scipy.special import k0e, k1e, ive, kve
from scipy.optimize import least_squares

# ------------------------------------------------------------------ physics model
def total_compressibility(sw, c_o, c_w, c_f):
    return c_f + (1 - sw) * c_o + sw * c_w


def _stehfest(N=12):
    V = np.zeros(N)
    for i in range(1, N + 1):
        s = 0.0
        for k in range((i + 1) // 2, min(i, N // 2) + 1):
            s += (k ** (N // 2) * factorial(2 * k)) / (
                factorial(N // 2 - k) * factorial(k) * factorial(k - 1) * factorial(i - k) * factorial(2 * k - i))
        V[i - 1] = (-1) ** (N // 2 + i) * s
    return V


_V = _stehfest(12)


def _pd_lap(u, CD, s):
    sq = np.sqrt(u)
    k0, k1 = k0e(sq), k1e(sq)          # exp-scaled, ratio is exact
    num = k0 + s * sq * k1
    return num / (u * (sq * k1 + CD * u * num))


def _pd_lap_closed(u, CD, s, reD):
    """Closed circular reservoir (no-flow at reD) + skin + WBS, overflow-safe scaled Bessels."""
    x = np.sqrt(u)
    R = reD * x
    e = np.exp(np.clip(2 * x - 2 * R, -700, 0))
    num = kve(0, x) * ive(1, R) + ive(0, x) * kve(1, R) * e
    den = x * (ive(1, R) * kve(1, x) - kve(1, R) * ive(1, x) * e)
    F = num / den
    return (F + s) / (u * (1 + CD * u * (F + s)))


def pwd(tD, CD, s, reD=None):
    tD = np.atleast_1d(tD).astype(float)
    ln2 = log(2.0)
    out = np.zeros_like(tD)
    for i, v in enumerate(_V, start=1):
        u = i * ln2 / tD
        out += v * (_pd_lap(u, CD, s) if reD is None else _pd_lap_closed(u, CD, s, reD))
    return out * ln2 / tD


def _dimless(k, h, phi, mu, ct, rw, C):
    tD_per_hr = 0.0002637 * k / (phi * mu * ct * rw ** 2)
    CD = 0.8936 * C / (phi * ct * h * rw ** 2)
    return tD_per_hr, CD


def pwd_buildup(dt_hr, tp, q, B, mu, k, h, phi, ct, rw, s, C, drawdown_only=False, re=None):
    """Delta p (psi) for build-up (superposition) or drawdown. re (ft) -> closed circular boundary."""
    a, CD = _dimless(k, h, phi, mu, ct, rw, C)
    coef = 141.2 * q * B * mu / (k * h)
    dt = np.asarray(dt_hr, float)
    reD = None if re is None else re / rw
    if drawdown_only:
        return coef * pwd(dt * a, CD, s, reD)
    if reD is not None:
        t_pss_D = 0.1 * np.pi * reD ** 2           # tDA = 0.1 for circle
        if tp * a > 10 * t_pss_D:                  # long production -> PSS: d pD/d tD = 2/reD^2
            return coef * (pwd(dt * a, CD, s, reD) - 2 * dt * a / reD ** 2)
    return coef * (pwd(tp * a, CD, s, reD) - pwd((tp + dt) * a, CD, s, reD) + pwd(dt * a, CD, s, reD))


# ------------------------------------------------------------------ data handling
def detect_shutin(t_hr, p):
    """Shut-in = largest sustained pressure rise. Returns index of last flowing point."""
    p = np.asarray(p, float)
    dp = np.diff(p)
    win = max(3, len(p) // 2000)
    score = pd.Series(dp).rolling(win, min_periods=1).sum().shift(-win + 1).fillna(0).values
    return int(np.argmax(score))


def prepare_buildup(df, pressure_unit="bar", shutin_time=None, n_points=200):
    """df: columns [timestamp or elapsed_hr, pressure]. Returns log-sampled buildup + pwf."""
    d = df.dropna(subset=["pressure"]).copy()
    if "timestamp" in d:
        d["timestamp"] = pd.to_datetime(d["timestamp"])
        d = d.sort_values("timestamp").drop_duplicates("timestamp")
        t = (d["timestamp"] - d["timestamp"].iloc[0]).dt.total_seconds().values / 3600
    else:
        d = d.sort_values("elapsed_hr")
        t = d["elapsed_hr"].values.astype(float)
    p = d["pressure"].values.astype(float)
    good = p > 0                                   # drop -999.25 style null sentinels
    t, p = t[good], p[good]
    if pressure_unit == "bar":
        p = p * 14.5038
    # robust spike removal (Hampel)
    s = pd.Series(p)
    med = s.rolling(11, center=True, min_periods=1).median()
    mad = (s - med).abs().rolling(11, center=True, min_periods=1).median()
    bad = (s - med).abs() > 6 * 1.4826 * mad.clip(lower=0.05)
    p = np.where(bad, med, p)

    if shutin_time is not None:
        i0 = int(np.searchsorted(t, shutin_time)) - 1
    else:
        i0 = detect_shutin(t, p)
    pwf = float(np.median(p[max(0, i0 - 5):i0 + 1]))
    dt = t[i0 + 1:] - t[i0]
    pws = p[i0 + 1:]
    m = dt > 0
    dt, pws = dt[m], pws[m]
    # log-resample (median in log bins) -> uniform weight per log-cycle
    edges = np.logspace(np.log10(dt.min()), np.log10(dt.max()), n_points + 1)
    idx = np.digitize(dt, edges) - 1
    g = pd.DataFrame({"dt": dt, "p": pws, "b": idx}).groupby("b").median()
    return g["dt"].values, g["p"].values, pwf, int(bad.sum())


def bourdet_derivative(x, y, L=0.15):
    """d(y)/d(ln x) with Bourdet L-smoothing (x>0)."""
    lx = np.log(x)
    n = len(x)
    der = np.full(n, np.nan)
    for i in range(n):
        j = i - 1
        while j >= 0 and lx[i] - lx[j] < L:
            j -= 1
        k = i + 1
        while k < n and lx[k] - lx[i] < L:
            k += 1
        if j < 0 or k >= n:
            continue
        d1 = (y[i] - y[j]) / (lx[i] - lx[j])
        d2 = (y[k] - y[i]) / (lx[k] - lx[i])
        w1, w2 = lx[k] - lx[i], lx[i] - lx[j]
        der[i] = (d1 * w1 + d2 * w2) / (w1 + w2)
    return der


def find_radial_window(dte, der, min_cycles=0.5, tol=0.12):
    """Longest late segment where log-derivative is flat (IARF)."""
    ok = ~np.isnan(der) & (der > 0)
    ld, lt = np.log10(np.where(ok, der, np.nan)), np.log10(dte)
    slope = np.gradient(ld, lt)
    flat = ok & (np.abs(slope) < tol)
    best, cur = (0, 0), None
    for i, f in enumerate(flat):
        if f and cur is None:
            cur = i
        if (not f or i == len(flat) - 1) and cur is not None:
            end = i if f else i - 1
            if lt[end] - lt[cur] > lt[best[1]] - lt[best[0]]:
                best = (cur, end)
            cur = None
    i0, i1 = best
    if lt[i1] - lt[i0] < min_cycles:          # fallback: last 1.0 log cycle
        i1 = int(np.nanargmax(np.where(ok, lt, -np.inf)))
        i0 = int(np.searchsorted(lt, lt[i1] - 1.0))
    return i0, i1


# ------------------------------------------------------------------ main analysis
def analyze_buildup(dt, pws, pwf, tp, q, B, mu, h, phi, ct, rw, regression=True, L=0.15, re=None, fit_re=True):
    dp = pws - pwf
    dte = dt * tp / (tp + dt)                 # Agarwal equivalent time
    der = bourdet_derivative(dte, dp, L)
    i0, i1 = find_radial_window(dte, der)
    win = slice(i0, i1 + 1)

    # --- Horner straight line in radial window
    htime = (tp + dt) / dt
    X = np.log10(htime[win])
    A = np.vstack([X, np.ones_like(X)]).T
    slope, p_star = np.linalg.lstsq(A, pws[win], rcond=None)[0]
    m = -slope                                   # psi/cycle (positive)
    k = 162.6 * q * B * mu / (m * h)
    p1hr = p_star - m * np.log10(tp + 1.0)
    skin = 1.151 * ((p1hr - pwf) / m - np.log10(k / (phi * mu * ct * rw ** 2)) + 3.2275
                    + np.log10((tp + 1.0) / tp))
    dP_skin = 0.869 * m * skin

    # --- MDH (cross-check, valid for dt << tp)
    Xm = np.log10(dt[win])
    sm, im = np.linalg.lstsq(np.vstack([Xm, np.ones_like(Xm)]).T, pws[win], rcond=None)[0]
    k_mdh = 162.6 * q * B * mu / (sm * h)
    p1_mdh = im
    s_mdh = 1.151 * ((p1_mdh - pwf) / sm - np.log10(k_mdh / (phi * mu * ct * rw ** 2)) + 3.2275)

    # --- wellbore storage from unit slope
    lslope = np.gradient(np.log10(np.clip(dp, 1e-6, None)), np.log10(dte))
    early = (lslope > 0.85) & (np.arange(len(dte)) < i0)
    if early.sum() >= 2:
        C = float(np.median(q * B * dte[early] / (24 * dp[early])))
    else:
        C = float(q * B * dte[0] / (24 * max(dp[0], 1e-3)))
    t_radial_start = float(dt[i0])

    res = {"method": "Straight-line (Horner)", "k_md": k, "skin": skin, "dP_skin_psi": dP_skin,
           "C_bbl_psi": C, "m_psi_cycle": m, "p_star_psi": p_star, "p1hr_psi": p1hr,
           "k_mdh_md": k_mdh, "skin_mdh": s_mdh, "t_radial_start_hr": t_radial_start,
           "t_radial_end_hr": float(dt[i1])}

    # --- non-linear regression on analytical model (WBS + skin + IARF)
    model_dp = None
    if regression:
        use_re = re is not None
        def unpack(x):
            return np.exp(x[0]), x[1], np.exp(x[2]), (np.exp(x[3]) if (use_re and fit_re) else re)
        def resid(x):
            kk, ss, CC, rr = unpack(x)
            mod = pwd_buildup(dt, tp, q, B, mu, kk, h, phi, ct, rw, ss, CC, re=rr)
            return np.log(np.clip(mod, 1e-6, None)) - np.log(np.clip(dp, 1e-6, None))
        x0 = [np.log(max(k, 1e-3)), float(np.clip(skin, -4, 50)), np.log(max(C, 1e-5))]
        lb, ub = [np.log(1e-3), -6, np.log(1e-6)], [np.log(1e5), 100, np.log(10)]
        if use_re and fit_re:
            x0.append(np.log(re)); lb.append(np.log(re / 3)); ub.append(np.log(re * 3))
        try:
            best = None
            for k0 in [x0[0], np.log(max(k, 1e-3) * 0.5), np.log(max(k, 1e-3) * 2)]:   # multi-start
                xs = list(x0); xs[0] = k0
                sol = least_squares(resid, xs, bounds=(lb, ub))
                if best is None or sol.cost < best.cost:
                    best = sol
            kr, sr, Cr, rr = unpack(best.x)
            model_dp = pwd_buildup(dt, tp, q, B, mu, kr, h, phi, ct, rw, sr, Cr, re=rr)
            mr = 162.6 * q * B * mu / (kr * h)
            res.update({"k_reg_md": float(kr), "skin_reg": float(sr), "C_reg_bbl_psi": float(Cr),
                        "re_reg_ft": float(rr) if rr is not None else np.nan,
                        "m_reg_psi_cycle": mr, "dP_skin_reg_psi": 141.2 * q * B * mu / (kr * h) * sr,
                        "match_rmse_psi": float(np.sqrt(np.mean((model_dp - dp) ** 2)))})
        except Exception as e:  # keep straight-line results
            res["regression_error"] = str(e)

    # --- derived engineering quantities
    kf = res.get("k_reg_md", k)
    sf = res.get("skin_reg", skin)
    Cf = res.get("C_reg_bbl_psi", C)
    mf = 162.6 * q * B * mu / (kf * h)        # IARF semilog (Horner) slope consistent with k
    dPs = 141.2 * q * B * mu / (kf * h) * sf  # = 0.869 m s
    _, CD = _dimless(kf, h, phi, mu, ct, rw, Cf)
    res.update({
        "final_m_psi_cycle": mf, "final_k_md": kf, "final_skin": sf, "final_dP_skin_psi": dPs, "final_C_bbl_psi": Cf,
        "CD": CD,
        "t_wbs_end_hr": (200000 + 12000 * max(sf, 0)) * Cf * mu / (kf * h),  # Chen-Brigham
        "flow_efficiency": (p_star - pwf - dPs) / (p_star - pwf),
        "radius_investigation_ft": float(np.sqrt(kf * dt.max() / (948 * phi * mu * ct))),
        "p_avg_est_psi": float(pws[-1]) if re is not None else np.nan,
        "productivity_index_stb_d_psi": q / (p_star - pwf),
        "ideal_q_no_skin_stb_d": q / ((p_star - pwf - dPs) / (p_star - pwf)),
    })
    res["extra_oil_if_skin_removed_stb_d"] = res["ideal_q_no_skin_stb_d"] - q

    # --- IARF window consistent with the model slope (for Horner/MDH display & p*)
    if "k_reg_md" in res:
        near = np.abs(der * 2.303 - mf) / mf < 0.15
        near &= dt > min(res["t_wbs_end_hr"], dt.max() / 10)
        idx = np.where(near)[0]
        if len(idx) >= 3:
            i0, i1 = int(idx[0]), int(idx[-1])
            w2 = slice(i0, i1 + 1)
            p_star_m = float(np.mean(pws[w2] + mf * np.log10(htime[w2])))
            res.update({"p_star_psi": p_star_m, "m_psi_cycle_window": res["m_psi_cycle"], "m_psi_cycle": mf,
                        "t_radial_start_hr": float(dt[i0]), "t_radial_end_hr": float(dt[i1])})
    plots = pd.DataFrame({"dt_hr": dt, "dte_hr": dte, "pws_psi": pws, "dp_psi": dp, "deriv_psi": der,
                          "horner_time": htime, "in_radial": False})
    plots.loc[i0:i1, "in_radial"] = True
    if model_dp is not None:
        plots["model_dp_psi"] = model_dp
        plots["model_deriv_psi"] = bourdet_derivative(dte, model_dp, L)
    return res, plots


# ------------------------------------------------------------------ SOCAR event workflow (VFM -> PTA)
def pvt_for(pvt: pd.DataFrame, well: str) -> dict:
    """Lab / reservoir inputs for one well (h ft, phi, c_t 1/psi, r_w ft, B_o rb/STB, mu cp, S_w, area acres)."""
    r = pvt[pvt["well_name"] == well].iloc[0]
    return {"h_ft": r["net_pay_thickness_ft"], "phi": r["porosity_frac"], "ct": r["total_compressibility_1_psi"],
            "rw_ft": r["wellbore_radius_ft"], "Bo": r["oil_fvf_rb_stb"], "mu": r["oil_viscosity_cp"],
            "sw": r.get("water_saturation_frac", np.nan), "area_acres": r.get("estimated_drainage_area_acres", np.nan)}


def np_coverage(P: pd.DataFrame, completion: pd.DataFrame, well: str, t_shut) -> dict:
    """Cumulative production N_p up to the shut-in and how it was obtained.

    N_p = cum oil at the completion-table reference date (column found by name, e.g. cum_oil_at_2025_01_01_stb)
          + VFM oil on every day between that date and the shut-in.
    Days in that interval without a VFM row (data starts later, or gaps) are filled with the mean VFM oil rate of the
    covered days - a documented approximation that is reported (method, filled days) and never hidden.
    Without a cumulative column N_p is the VFM sum of the loaded period only (t_p is then a lower bound)."""
    import re as _re
    d = P[(P["well_name"] == well) & (pd.to_datetime(P["date"]) < pd.Timestamp(t_shut).normalize())].sort_values("date")
    cols = [c for c in completion.columns if str(c).lower().startswith("cum_oil")]
    cum0, since = 0.0, None
    if cols:
        row = completion.loc[completion["well_name"] == well, cols[0]]
        cum0 = float(row.iloc[0]) if len(row) and pd.notna(row.iloc[0]) else 0.0
        mt = _re.search(r"(\d{4})_(\d{2})_(\d{2})", str(cols[0]))
        since = pd.Timestamp(f"{mt.group(1)}-{mt.group(2)}-{mt.group(3)}") if mt else None
    dd = d if since is None else d[pd.to_datetime(d["date"]) >= since]
    vfm_sum = float(dd["oil_rate_stbd"].sum())
    info = {"cum0_stb": cum0, "cum_date": since, "vfm_sum_stb": vfm_sum, "fill_days": 0, "fill_stb": 0.0,
            "method": "cum + VFM, full daily coverage"}
    if since is None:
        info["method"] = ("no cumulative-production column: N_p = VFM sum over the loaded period only "
                          "(t_p is a lower bound)") if not cols else "cumulative column without a date: cum + all VFM days"
    else:
        need = pd.date_range(since, pd.Timestamp(t_shut).normalize() - pd.Timedelta(days=1), freq="D")
        have = set(pd.to_datetime(dd["date"]).dt.normalize())
        miss = [x for x in need if x not in have]
        if miss and len(dd):
            rate = float(dd["oil_rate_stbd"].mean())
            info.update(fill_days=len(miss), fill_stb=rate * len(miss),
                        method=f"cum + VFM; {len(miss)} of {len(need)} days since {since:%Y-%m-%d} had no VFM row and were "
                               f"filled with the mean VFM rate {rate:,.0f} STB/d (approximation)")
        elif miss:
            info["method"] = "no VFM day between the cumulative date and the shut-in: N_p = cumulative only"
    info["Np_stb"] = cum0 + vfm_sum + info["fill_stb"]
    info["share_filled"] = info["fill_stb"] / info["Np_stb"] if info["Np_stb"] else float("nan")
    return info


def q_tp_from_vfm(P: pd.DataFrame, completion: pd.DataFrame, well: str, t_shut) -> tuple[float, float, float, pd.Timestamp]:
    """q = VFM oil rate on the last full-uptime (>95 %) day before shut-in; N_p from ``np_coverage``;
    t_p = 24 N_p / q [hr]. Returns (q, t_p, N_p, source date)."""
    d = P[(P["well_name"] == well) & (pd.to_datetime(P["date"]) < pd.Timestamp(t_shut).normalize())].sort_values("date")
    if d.empty:
        raise ValueError(f"no VFM rate for {well} before the shut-in {pd.Timestamp(t_shut):%Y-%m-%d %H:%M}: "
                         "q and t_p cannot be computed")
    full = d[d["uptime"] > 0.95]
    src = (full if len(full) else d).iloc[-1]
    q = float(src["oil_rate_stbd"])
    if not q > 0:
        raise ValueError(f"VFM oil rate before the shut-in of {well} is {q:.1f} STB/d: k cannot be computed")
    Np = np_coverage(P, completion, well, t_shut)["Np_stb"]
    return q, Np / q * 24.0, Np, pd.Timestamp(src["date"])


def truncate_at_reopen(dt: np.ndarray, p: np.ndarray, drop_psi: float = 5.0) -> tuple[np.ndarray, np.ndarray]:
    """The build-up ends at re-opening: first point more than ``drop_psi`` below the running maximum."""
    bad = np.where(np.maximum.accumulate(p) - p > drop_psi)[0]
    return (dt[:bad[0]], p[:bad[0]]) if len(bad) else (dt, p)


def pta_event(dt: np.ndarray, p: np.ndarray, pwf: float, well: str, t_shut, P: pd.DataFrame, pvt: pd.DataFrame,
              completion: pd.DataFrame, q: float | None = None, fit_re: bool = True) -> tuple[dict, pd.DataFrame]:
    """Analyse one shut-in. dt in HOURS since shut-in, p = downhole pressure [psia], pwf = last flowing BHP.
    Per-minute data (>400 points) is log-binned (200 bins, median) so every log cycle has equal weight."""
    dt, p = np.asarray(dt, float), np.asarray(p, float)
    ok = (dt > 0) & np.isfinite(p) & (p > 0)
    dt, p = truncate_at_reopen(dt[ok], p[ok])
    if len(dt) > 400:
        edges = np.logspace(np.log10(dt.min()), np.log10(dt.max()) + 1e-9, 200)
        tab = pd.DataFrame({"dt": dt, "p": p, "b": np.digitize(dt, edges)}).groupby("b").median()
    else:
        tab = pd.DataFrame({"dt": dt, "p": p})
    pv = pvt_for(pvt, well)
    qv, tp, Np, src = q_tp_from_vfm(P, completion, well, t_shut)
    np_info = np_coverage(P, completion, well, t_shut)
    q = qv if q is None else q
    re = np.sqrt(pv["area_acres"] * 43560 / np.pi) if np.isfinite(pv["area_acres"]) else None
    res, plots = analyze_buildup(tab["dt"].values, tab["p"].values, pwf, tp, q, pv["Bo"], pv["mu"], pv["h_ft"],
                                 pv["phi"], pv["ct"], pv["rw_ft"], re=re, fit_re=fit_re, regression=True)
    res.update({"well_name": well, "t_shut": pd.Timestamp(t_shut), "duration_hr": float(dt.max()),
                "q_oil_stbd": q, "q_source_date": src, "tp_hr": tp, "Np_stb": Np, "pwf_psi": pwf,
                "np_method": np_info["method"], "np_filled_share": np_info["share_filled"],
                "q_source_lag_days": (pd.Timestamp(t_shut).normalize() - pd.Timestamp(src)).days})
    return res, plots


def method_comparison(results: list[dict]) -> pd.DataFrame:
    """Classic Horner straight line vs closed-boundary regression for every event (sensitivity / QC)."""
    rows = []
    for r in results:
        rows.append({"well_name": r["well_name"], "t_shut": r["t_shut"], "duration_hr": r["duration_hr"],
                     "m_horner": r.get("m_psi_cycle_window", r.get("m_psi_cycle")), "m_regression": r["final_m_psi_cycle"],
                     "k_horner_md": r["k_md"], "k_regression_md": r["final_k_md"],
                     "skin_horner": r["skin"], "skin_regression": r["final_skin"],
                     "k_diff_%": (r["k_md"] / r["final_k_md"] - 1) * 100,
                     "match_rmse_psi": r.get("match_rmse_psi", np.nan),
                     "re_ft": r.get("re_reg_ft", np.nan), "r_inv_ft": r.get("radius_investigation_ft", np.nan)})
    return pd.DataFrame(rows)


def diagnosis(r: dict) -> str:
    """Plain-language engineering reading of one PTA result (skin, k, boundary, method bias)."""
    s, k, dps = r["final_skin"], r["final_k_md"], r["final_dP_skin_psi"]
    txt = []
    if s > 3:
        txt.append(f"High positive skin (+{s:.1f}) with ΔP_skin = {dps:.0f} psi indicates near-wellbore damage; "
                   f"matrix acidizing / stimulation recommended (≈{r['extra_oil_if_skin_removed_stb_d']:.0f} STB/d upside).")
    elif s > 1:
        txt.append(f"Moderate skin (+{s:.1f}, ΔP_skin = {dps:.0f} psi): mild damage; review completion before intervening.")
    elif s < -1:
        txt.append(f"Negative skin ({s:.1f}): stimulated / well-connected completion, no intervention needed.")
    else:
        txt.append(f"Near-zero skin ({s:.1f}): undamaged completion.")
    txt.append(f"Permeability {k:.0f} md → {'high' if k > 150 else 'moderate' if k > 30 else 'low'} transmissibility.")
    if np.isfinite(r.get("re_reg_ft", np.nan)):
        txt.append(f"Late-time derivative matched with a closed drainage boundary at r_e ≈ {r['re_reg_ft']:.0f} ft.")
    if abs(r.get("k_md", k) / k - 1) > 0.25:
        txt.append(f"Classic straight-line Horner gives k = {r['k_md']:.0f} md, biased because the boundary is felt "
                   f"before a clean semilog line develops; the model-based value is reported.")
    return " ".join(txt)


def recommendation(r: dict) -> str:
    """One-line action for the executive report."""
    s = r["final_skin"]
    if s > 3:
        return f"Stimulation (acid wash) candidate: +{r.get('extra_oil_if_skin_removed_stb_d', 0):.0f} STB/d if skin removed."
    if s > 1:
        return "Monitor; review completion / perforation efficiency before intervention."
    return "No intervention needed; optimise gas-lift and choke instead."
