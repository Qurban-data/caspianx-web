"""Publication-grade dual-panel PTA diagnostic figure (one per shut-in).

Left : log-log diagnostic - pressure change dP = p_ws - p_wf and Bourdet derivative dP' = d(dP)/d ln(dt_e)
       versus Agarwal equivalent time dt_e = dt * t_p / (t_p + dt); regions shaded from the AUTOMATIC radial window:
       wellbore storage / transition (before the window), radial flow (MTR, the window), late time / boundary (after).
Right: Horner semilog - p_ws versus (t_p + dt)/dt (reversed log axis), the least-squares line fitted ONLY on the
       MTR points, slope m [psi/cycle], p_1hr (line at dt = 1 h), p*, and the derived k, skin, dP_skin.
The Part-1 VFM oil rate q_o (and its source day) that feeds k = 162.6 q B mu / (m h) is printed on the figure.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from . import pta_horner as H  # noqa: E402

C_DP, C_DER, C_MTR, C_WBS, C_BND, C_FIT = "#1f4e79", "#c0504d", "#2e7d32", "#9e9e9e", "#ef6c00", "#000000"


def dual_figure(well: str, t_shut, dt_hr, pws, pwf: float, res: "H.PTAResult", q_oil: float, q_date,
                pvt: dict, path: str, L: float = 0.1, theme: str = "light") -> str:
    """Draw and save the dual-panel figure. ``res`` is the Horner result used for the submission."""
    dt = np.asarray(dt_hr, float)
    p = np.asarray(pws, float)
    m_ = np.isfinite(dt) & np.isfinite(p) & (dt > 0)
    dt, p = dt[m_], p[m_]
    o = np.argsort(dt)
    dt, p = H.truncate_at_reopen(dt[o], p[o])
    tp = res.tp_hr
    dp = p - pwf
    te = H.agarwal_time(tp, dt)
    der = H.bourdet_derivative(te, dp, L=L)
    der[-1] = np.nan
    w0, w1 = res.window_dt_start_hr, res.window_dt_end_hr
    te0, te1 = tp * w0 / (tp + w0), tp * w1 / (tp + w1)
    win = (dt >= w0) & (dt <= w1)

    dark = theme == "dark"
    if dark:                                   # SOCAR app palette (colours only, same content)
        C_DP, C_DER, C_MTR, C_WBS, C_BND, C_FIT = "#4f8ff0", "#f2a446", "#ffc861", "#b3a38c", "#e0503a", "#f6efe4"
        C_MEAS, BOX_FC, BOX_EC = "#a8d8ff", "#18110a", "#6b5a44"
        rc = {"figure.facecolor": "#07080b", "axes.facecolor": "#120d08", "savefig.facecolor": "#07080b",
              "axes.edgecolor": "#6b5a44", "axes.labelcolor": "#f6efe4", "text.color": "#f6efe4",
              "xtick.color": "#b3a38c", "ytick.color": "#b3a38c", "grid.color": "#b3a38c",
              "legend.facecolor": "#18110a", "legend.edgecolor": "#6b5a44", "axes.titlecolor": "#f6efe4"}
    else:
        C_DP, C_DER, C_MTR, C_WBS, C_BND, C_FIT = globals()["C_DP"], globals()["C_DER"], globals()["C_MTR"], \
            globals()["C_WBS"], globals()["C_BND"], globals()["C_FIT"]
        C_MEAS, BOX_FC, BOX_EC, rc = "#7f9fbf", "#f7f9fb", "#8aa0b4", {}
    with plt.rc_context(rc):
        return _draw(well, t_shut, dt, p, pwf, res, q_oil, q_date, pvt, path, L, te, dp, der, te0, te1, tp, win, w0, w1,
                     C_DP, C_DER, C_MTR, C_WBS, C_BND, C_FIT, C_MEAS, BOX_FC, BOX_EC)


def _draw(well, t_shut, dt, p, pwf, res, q_oil, q_date, pvt, path, L, te, dp, der, te0, te1, tp, win, w0, w1,
          C_DP, C_DER, C_MTR, C_WBS, C_BND, C_FIT, C_MEAS, BOX_FC, BOX_EC):
    fig = plt.figure(figsize=(15.5, 7.6))
    gs = fig.add_gridspec(2, 2, height_ratios=[5.6, 1.0], hspace=0.28, wspace=0.18,
                          left=0.06, right=0.985, top=0.94, bottom=0.04)
    a1, a2, at = fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1]), fig.add_subplot(gs[1, :])
    at.axis("off")
    # ---------------- log-log
    ok = dp > 0
    a1.loglog(te[ok], dp[ok], "o", ms=3.2, color=C_DP, label="ΔP = p_ws − p_wf")
    okd = np.isfinite(der) & (der > 0)
    a1.loglog(te[okd], der[okd], "s", ms=3.0, color=C_DER, label="Bourdet derivative  t·dΔP/dt  (L = %.1f)" % L)
    xlo, xhi = te[ok].min() * 0.8, te[ok].max() * 1.25
    a1.axvspan(xlo, te0, color=C_WBS, alpha=0.13, lw=0, label="wellbore storage / transition")
    a1.axvspan(te0, te1, color=C_MTR, alpha=0.16, lw=0, label="radial flow (MTR) – fit window")
    if te1 < xhi:
        a1.axvspan(te1, xhi, color=C_BND, alpha=0.12, lw=0, label="late time / boundary")
    plateau = res.horner_slope / np.log(10)
    a1.hlines(plateau, te0, te1, colors=C_MTR, linestyles="--", lw=1.8,
              label=f"IARF level m/2.303 = {plateau:.2f} psi")
    a1.set_xlim(xlo, xhi)
    a1.set(xlabel="equivalent (Agarwal) time Δt_e, hr", ylabel="ΔP and derivative, psi",
           title=f"{well} – log-log diagnostic (shut-in {t_shut:%Y-%m-%d %H:%M})")
    a1.grid(True, which="both", alpha=0.25)
    a1.legend(fontsize=8.2, loc="upper left", framealpha=0.85)

    # ---------------- Horner
    X = (tp + dt) / dt
    a2.semilogx(X, p, "o", ms=3.0, color=C_MEAS, label="measured p_ws")
    a2.semilogx(X[win], p[win], "o", ms=4.2, color=C_MTR, label="MTR points used in the fit")
    xs = np.logspace(np.log10(X.min() * 0.9), np.log10(X.max() * 1.1), 200)
    a2.semilogx(xs, res.p_star_psi - res.horner_slope * np.log10(xs), "-", color=C_FIT, lw=1.4,
                label=f"Horner line (MTR fit), m = {res.horner_slope:.2f} psi/cycle")
    x1 = tp + 1.0
    a2.semilogx([x1], [res.p1hr_psi], "D", ms=7, color=C_DER, label=f"p_1hr = {res.p1hr_psi:.1f} psia (Δt = 1 h)")
    a2.semilogx([], [], " ", label=f"p* (extrapolated to ratio 1) = {res.p_star_psi:.1f} psia")
    a2.semilogx([], [], " ", label=f"p_wf (Δt = 0) = {pwf:.1f} psia")
    a2.set_xlim(X.max() * 1.1, X.min() * 0.9)
    lo = min(p.min(), res.p1hr_psi)
    hi = max(p.max(), res.p1hr_psi)
    a2.set_ylim(lo - 0.05 * (hi - lo), hi + 0.08 * (hi - lo))
    a2.set(xlabel="Horner time ratio (t_p + Δt)/Δt", ylabel="bottom-hole pressure p_ws, psia",
           title="Horner semilog – straight line fitted on the MTR only")
    a2.grid(True, which="both", alpha=0.25)
    a2.legend(fontsize=8.6, loc="upper left", framealpha=0.9)
    txt = (f"VFM → PTA:  q_o = {q_oil:.1f} STB/d  (Part-1 VFM, {q_date:%Y-%m-%d}),  t_p = {tp:,.0f} h\n"
           f"k = 162.6·q·B·μ/(m·h) = {res.k_md:.1f} md      skin s = {res.skin:+.2f}      "
           f"ΔP_skin = 0.87·m·s = {0.87 * res.horner_slope * res.skin:+.1f} psi\n"
           f"B = {pvt['Bo']:.3f} rb/STB, μ = {pvt['mu']:.2f} cp, h = {pvt['h_ft']:.0f} ft, φ = {pvt['phi']:.3f}, "
           f"c_t = {pvt['ct']:.2e} 1/psi, r_w = {pvt['rw_ft']:.3f} ft   |   MTR {w0:.2f}–{w1:.1f} h "
           f"({res.window_cycles:.2f} log-cycles), R² = {res.r2_horner:.4f}")
    at.text(0.5, 0.5, txt, transform=at.transAxes, ha="center", va="center", fontsize=10, family="monospace",
            bbox=dict(boxstyle="round,pad=0.5", fc=BOX_FC, ec=BOX_EC))
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path
