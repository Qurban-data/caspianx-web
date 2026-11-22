"""Deterministic Pressure Transient Analysis (buildup) engine - CaspianX, SOCAR Hackathon 2026.

Field units throughout:
    q   [STB/d]   B  [rb/STB]   mu [cp]   h [ft]   phi [fraction]
    ct  [1/psi]   rw [ft]       p  [psi]  t [hr]   k  [md]

Formulas (Horner buildup, semilog straight line in infinite-acting radial flow):
    m      = slope of Pws vs log10((tp+dt)/dt)   [psi/cycle]  (reported as |m|)
    k      = 162.6 q B mu / (|m| h)
    s      = 1.151 [ (P1hr - Pwf)/|m| - log10(k/(phi mu ct rw^2)) + 3.23 ]
    dP_skin= 0.87 |m| s
P1hr is read from the extrapolated Horner line at dt = 1 hr (i.e. x = log10(tp+1)).
Pwf is the flowing pressure at the moment of shut-in (dt = 0).

No reservoir/fluid parameter is ever invented here: every input must be passed explicitly,
and the caller is responsible for logging the source/assumption of each value.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

LN10 = np.log(10.0)


# --------------------------------------------------------------------------- formulas
def permeability(q: float, B: float, mu: float, m_abs: float, h: float) -> float:
    """k [md] = 162.6 q B mu / (|m| h)."""
    if m_abs <= 0 or h <= 0:
        raise ValueError("|m| and h must be positive")
    return 162.6 * q * B * mu / (m_abs * h)


def skin_factor(p1hr: float, pwf: float, m_abs: float, k: float, phi: float,
                mu: float, ct: float, rw: float) -> float:
    """Horner skin (buildup): s = 1.151[(P1hr-Pwf)/|m| - log10(k/(phi mu ct rw^2)) + 3.23]."""
    if min(m_abs, k, phi, mu, ct, rw) <= 0:
        raise ValueError("all parameters must be positive")
    return 1.151 * ((p1hr - pwf) / m_abs - np.log10(k / (phi * mu * ct * rw ** 2)) + 3.23)


def dp_skin(m_abs: float, s: float) -> float:
    """Additional pressure drop due to skin [psi]: 0.87 |m| s (negative for stimulated wells)."""
    return 0.87 * m_abs * s


# --------------------------------------------------------------------------- diagnostics
def horner_time(tp: float, dt: np.ndarray) -> np.ndarray:
    dt = np.asarray(dt, float)
    return (tp + dt) / dt


def agarwal_time(tp: float, dt: np.ndarray) -> np.ndarray:
    """Equivalent time te = tp*dt/(tp+dt) (used for the buildup derivative)."""
    dt = np.asarray(dt, float)
    return tp * dt / (tp + dt)


def bourdet_derivative(t: np.ndarray, dp: np.ndarray, L: float = 0.1) -> np.ndarray:
    """Bourdet (1989) weighted central derivative d(dp)/d(ln t) with smoothing window L (log10 cycles
    expressed in ln units internally). t must be strictly increasing and > 0. End points use the
    nearest available one-sided difference. Returns NaN where it cannot be computed."""
    t = np.asarray(t, float)
    dp = np.asarray(dp, float)
    n = len(t)
    out = np.full(n, np.nan)
    if n < 3:
        return out
    x = np.log(t)
    Lln = L * LN10
    for i in range(n):
        # left point: first j<i with x_i - x_j >= L ; right: first j>i with x_j - x_i >= L
        j = i - 1
        while j > 0 and x[i] - x[j] < Lln:
            j -= 1
        k = i + 1
        while k < n - 1 and x[k] - x[i] < Lln:
            k += 1
        has_left = j >= 0 and i > 0
        has_right = k <= n - 1 and i < n - 1
        if has_left and has_right:
            dxl, dxr = x[i] - x[j], x[k] - x[i]
            if dxl <= 0 or dxr <= 0:
                continue
            dl = (dp[i] - dp[j]) / dxl
            dr = (dp[k] - dp[i]) / dxr
            out[i] = (dl * dxr + dr * dxl) / (dxl + dxr)
        elif has_right:
            dxr = x[k] - x[i]
            if dxr > 0:
                out[i] = (dp[k] - dp[i]) / dxr
        elif has_left:
            dxl = x[i] - x[j]
            if dxl > 0:
                out[i] = (dp[i] - dp[j]) / dxl
    return out


def find_radial_window(te: np.ndarray, deriv: np.ndarray, min_cycles: float = 0.5,
                       skip_early_frac: float = 0.0, tol: float = 0.10) -> Optional[tuple]:
    """Radial-flow (IARF) window = the FLATTEST part of the Bourdet derivative after the wellbore-storage hump.
    1) restrict to points after the early derivative maximum (end of storage hump);
    2) slide windows of `min_cycles` log-cycles, score = |d log10(der)/d log10(t)| + CV(der);
       the best-scoring (flattest) window is the seed - this avoids picking the late, decaying derivative
       of a closed boundary even when that late part is longer;
    3) grow the seed left/right while points stay within +/- tol of the seed median.
    Returns (i_start, i_end) inclusive indices, or None if nothing reaches `min_cycles`."""
    te = np.asarray(te, float)
    d = np.asarray(deriv, float)
    ok = np.isfinite(d) & (d > 0) & (te > 0)
    idx = np.where(ok)[0]
    if len(idx) < 5:
        return None
    first = idx[int(len(idx) * skip_early_frac)]
    early = idx[: max(3, int(len(idx) * 0.6))]
    peak = early[np.argmax(d[early])]
    if peak > first and d[peak] > 1.2 * np.nanmedian(d[idx]):
        first = peak
    cand = idx[idx > first]
    if len(cand) < 5:
        return None
    lx, ld = np.log10(te), np.log10(np.where(d > 0, d, np.nan))
    best, best_score = None, np.inf
    for a_pos in range(len(cand)):
        b_pos = a_pos
        while b_pos < len(cand) - 1 and lx[cand[b_pos]] - lx[cand[a_pos]] < min_cycles:
            b_pos += 1
        if lx[cand[b_pos]] - lx[cand[a_pos]] < min_cycles or b_pos - a_pos < 4:
            break
        sel = cand[a_pos:b_pos + 1]
        slope = np.polyfit(lx[sel], ld[sel], 1)[0]
        cv = np.std(d[sel]) / np.mean(d[sel])
        score = abs(slope) + cv
        if score < best_score:
            best, best_score = (a_pos, b_pos), score
    if best is None:
        return None
    a_pos, b_pos = best
    med = np.median(d[cand[a_pos:b_pos + 1]])
    while a_pos > 0 and abs(d[cand[a_pos - 1]] - med) <= tol * med:
        a_pos -= 1
    while b_pos < len(cand) - 1 and abs(d[cand[b_pos + 1]] - med) <= tol * med:
        b_pos += 1
    return int(cand[a_pos]), int(cand[b_pos])


def truncate_at_reopen(dt: np.ndarray, p: np.ndarray, drop_psi: float = 5.0):
    """Cut the build-up where pressure falls more than `drop_psi` below its running maximum
    (well re-opened or gauge glitch). Returns truncated (dt, p)."""
    p = np.asarray(p, float)
    run = np.maximum.accumulate(np.nan_to_num(p, nan=-np.inf))
    bad = np.where(run - p > drop_psi)[0]
    n = bad[0] if len(bad) else len(p)
    return np.asarray(dt, float)[:n], p[:n]


@dataclass
class PTAResult:
    well: str
    shut_in_time: object
    horner_slope: float          # psi/cycle, reported as |m|
    k_md: float
    skin: float
    dp_skin_psi: float
    p1hr_psi: float
    pwf_psi: float
    p_star_psi: float
    tp_hr: float
    window_dt_start_hr: float
    window_dt_end_hr: float
    window_cycles: float
    r2_horner: float
    n_points: int
    flags: str

    def as_dict(self):
        return asdict(self)


def analyze_buildup(well: str, shut_in_time, dt_hr: np.ndarray, pws: np.ndarray, pwf: float,
                    tp_hr: float, q: float, B: float, mu: float, h: float, phi: float,
                    ct: float, rw: float, L: float = 0.1, tol: float = 0.10,
                    min_cycles: float = 0.5, window_hr: tuple | None = None) -> PTAResult:
    """Full automated buildup interpretation for one shut-in.
    ``window_hr`` = (start, end) in hours since shut-in forces the radial-flow window (engineer override);
    None = automatic window from the flattest Bourdet derivative (the submitted method)."""
    dt = np.asarray(dt_hr, float)
    p = np.asarray(pws, float)
    m = np.isfinite(dt) & np.isfinite(p) & (dt > 0)
    dt, p = dt[m], p[m]
    order = np.argsort(dt)
    dt, p = dt[order], p[order]
    flags = []
    n0 = len(dt)
    dt, p = truncate_at_reopen(dt, p)
    if len(dt) < n0:
        flags.append(f"TRUNCATED_{n0 - len(dt)}_pts_at_reopen")
    dp = p - pwf
    te = agarwal_time(tp_hr, dt)
    der = bourdet_derivative(te, dp, L=L)
    der[-1] = np.nan                         # one-sided end point is unreliable
    if window_hr is not None:
        sel = np.where((dt >= window_hr[0]) & (dt <= window_hr[1]))[0]
        if len(sel) < 5:
            raise ValueError(f"only {len(sel)} pressure points between {window_hr[0]:g} and {window_hr[1]:g} h: "
                             "choose a wider analysis window")
        win = (int(sel[0]), int(sel[-1]))
        flags.append("MANUAL_WINDOW")
    else:
        win = find_radial_window(te, der, min_cycles=min_cycles, tol=tol)
    if win is None:
        flags.append("NO_FLAT_DERIVATIVE_used_last_half_cycle")
        lx = np.log10(dt)
        sel = np.where(lx >= lx[-1] - 0.5)[0]
        a, b = sel[0], sel[-1]
    else:
        a, b = win
    X = np.log10(horner_time(tp_hr, dt[a:b + 1]))
    Y = p[a:b + 1]
    slope, icpt = np.polyfit(X, Y, 1)       # slope is negative for buildup on Horner axis
    yhat = slope * X + icpt
    ss_res = float(np.sum((Y - yhat) ** 2))
    ss_tot = float(np.sum((Y - Y.mean()) ** 2)) or np.nan
    r2 = 1 - ss_res / ss_tot if ss_tot == ss_tot else np.nan
    m_abs = abs(slope)
    if slope >= 0:
        flags.append("HORNER_SLOPE_NONNEGATIVE")
    p1hr = slope * np.log10(tp_hr + 1.0) + icpt
    k = permeability(q, B, mu, m_abs, h)
    s = skin_factor(p1hr, pwf, m_abs, k, phi, mu, ct, rw)
    if not (0.01 <= k <= 10000):
        flags.append("K_OUT_OF_RANGE")
    if not (-6 <= s <= 50):
        flags.append("SKIN_OUT_OF_RANGE")
    return PTAResult(well, shut_in_time, m_abs, k, s, dp_skin(m_abs, s), p1hr, pwf, icpt, tp_hr,
                     float(dt[a]), float(dt[b]), float(np.log10(dt[b] / dt[a])), float(r2),
                     int(b - a + 1), ";".join(flags))


# --------------------------------------------------------------------------- shut-in detection
def detect_shut_ins(rate: np.ndarray, pressure: np.ndarray, rate_eps: float = 1e-6,
                    min_len: int = 5, min_rise_psi: float = 1.0) -> list:
    """Return list of (i_start, i_end) for shut-in periods: rate <= eps for >= min_len samples AND
    pressure rises over the period by >= min_rise_psi. i_start is the first zero-rate sample."""
    r = np.asarray(rate, float)
    p = np.asarray(pressure, float)
    off = np.nan_to_num(r, nan=0.0) <= rate_eps
    out, i, n = [], 0, len(r)
    while i < n:
        if off[i] and (i == 0 or not off[i - 1]):
            j = i
            while j + 1 < n and off[j + 1]:
                j += 1
            if j - i + 1 >= min_len:
                seg = p[i:j + 1]
                seg = seg[np.isfinite(seg)]
                if len(seg) >= 2 and seg[-1] - seg[0] >= min_rise_psi:
                    out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out
