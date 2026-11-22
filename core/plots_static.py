"""Static (PNG) plots for reports / presentation: PTA diagnostic panel and VFM overview."""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def pta_figure(pl: "pd.DataFrame", r: dict, path: str) -> None:
    """2x2 panel: Horner, MDH, log-log + Bourdet derivative, pressure history; radial window shaded.
    pl: DataFrame from analyze_buildup (dt_hr, dte_hr, pws_psi, dp_psi, deriv_psi, horner_time, in_radial, model_*)."""
    fig, axs = plt.subplots(2, 2, figsize=(15, 10.0))
    ax = axs.ravel()
    m, k, s = r["final_m_psi_cycle"], r["final_k_md"], r["final_skin"]
    pstar = r.get("p_star_psi", np.nan)
    rad = pl["in_radial"].values.astype(bool) if "in_radial" in pl else np.zeros(len(pl), bool)
    t0, t1 = r.get("t_radial_start_hr", np.nan), r.get("t_radial_end_hr", np.nan)
    has_win = np.isfinite(t0) and np.isfinite(t1) and t1 > t0
    tp = r.get("tp_hr", np.nan)
    win_lbl = f"radial window {t0:.2g}-{t1:.2g} h" if has_win else None
    # Horner
    ht = pl["horner_time"].values
    if has_win and np.isfinite(tp):
        ax[0].axvspan((tp + t1) / t1, (tp + t0) / t0, color="#d62728", alpha=0.10, lw=0, label=win_lbl)
    ax[0].semilogx(ht, pl["pws_psi"], "o", ms=3, color="#1f77b4", label="measured Pws")
    xx = np.logspace(0, np.log10(ht.max()), 50)
    if np.isfinite(pstar):
        ax[0].semilogx(xx, pstar - m * np.log10(xx), "-", color="#d62728", lw=1.4, label=f"semilog line, m={m:.1f} psi/cycle")
        ax[0].plot([1], [pstar], "D", color="#d62728", ms=6, label=f"p* = {pstar:.0f} psia")
    ax[0].invert_xaxis()
    ax[0].set(title="Horner plot", xlabel="Horner time (tp+Δt)/Δt", ylabel="Pws, psia")
    ax[0].legend(fontsize=8, loc="upper left")
    # MDH
    dt = pl["dt_hr"].values
    if has_win:
        ax[1].axvspan(t0, t1, color="#d62728", alpha=0.10, lw=0, label=win_lbl)
    ax[1].semilogx(dt, pl["pws_psi"], "o", ms=3, color="#2ca02c", label="measured Pws")
    if rad.sum() >= 3:
        sl, ic = np.polyfit(np.log10(dt[rad]), pl["pws_psi"].values[rad], 1)
        xm = np.logspace(np.log10(dt.min()), np.log10(dt.max()), 40)
        ax[1].semilogx(xm, ic + sl * np.log10(xm), "-", color="#d62728", lw=1.4, label=f"MDH line, slope {sl:.1f} psi/cycle")
    ax[1].set(title="MDH plot", xlabel="shut-in time Δt, hr", ylabel="Pws, psia")
    ax[1].legend(fontsize=8, loc="upper left")
    # log-log
    dte = pl["dte_hr"].values
    if has_win and np.isfinite(tp):
        ax[2].axvspan(t0 * tp / (tp + t0), t1 * tp / (tp + t1), color="#d62728", alpha=0.10, lw=0, label=win_lbl)
    ax[2].loglog(dte, pl["dp_psi"], "o", ms=3, color="#1f77b4", label="ΔP measured")
    ax[2].loglog(dte, np.clip(pl["deriv_psi"], 1e-3, None), "s", ms=3, color="#ff7f0e", label="Bourdet derivative")
    if "model_dp_psi" in pl and pl["model_dp_psi"].notna().any():
        ax[2].loglog(dte, pl["model_dp_psi"], "-", color="#0b3d91", lw=1.6, zorder=5, label="model ΔP")
        ax[2].loglog(dte, np.clip(pl["model_deriv_psi"], 1e-3, None), "-", color="#8c2d04", lw=1.6, zorder=5, label="model derivative")
    ax[2].axhline(m / 2.303, color="#d62728", ls="--", lw=1, label=f"IARF level m/2.303 = {m / 2.303:.1f} psi")
    ax[2].set(title="Log-log diagnostic", xlabel="Agarwal time Δte, hr", ylabel="ΔP and derivative, psi")
    ax[2].legend(fontsize=7.5, loc="lower left", framealpha=0.85)
    # pressure history (linear time): flowing pressure at shut-in, then the measured build-up
    pwf = r.get("pwf_psi", np.nan)
    span = max(float(dt.max()) * 0.15, 0.5)
    if np.isfinite(pwf):
        ax[3].plot([-span, 0], [pwf, pwf], "-", color="#7f7f7f", lw=2, label=f"flowing, p_wf = {pwf:.0f} psia (last value)")
        ax[3].plot([0, 0], [pwf, pl["pws_psi"].iloc[0]], ":", color="#7f7f7f", lw=1)
    ax[3].plot(dt, pl["pws_psi"], "o-", ms=2.5, lw=0.8, color="#1f77b4", label="measured Pws (build-up)")
    if has_win:
        ax[3].axvspan(t0, t1, color="#d62728", alpha=0.10, lw=0, label=win_lbl)
    if np.isfinite(pstar):
        ax[3].axhline(pstar, color="#d62728", ls="--", lw=1, label=f"p* = {pstar:.0f} psia")
    ax[3].axvline(0, color="k", lw=0.8)
    ax[3].set(title="Pressure history around shut-in", xlabel="time relative to shut-in, hr",
              ylabel="bottom-hole pressure, psia")
    ax[3].legend(fontsize=8, loc="lower right")
    fig.suptitle(f"{r['well_name']}  shut-in {r['t_shut']:%Y-%m-%d %H:%M}   q={r['q_oil_stbd']:.0f} STB/d (VFM)   "
                 f"m={m:.2f} psi/cycle   k={k:.1f} md   skin={s:.2f}   ΔPskin={r['final_dP_skin_psi']:.1f} psi",
                 fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def vfm_figure(P: "pd.DataFrame", T: "pd.DataFrame", path: str) -> None:
    """Daily oil/water VFM with P10-P90 bands and well-test points, one panel per well."""
    wells = sorted(P["well_name"].unique())
    n = len(wells)
    fig, axes = plt.subplots((n + 1) // 2, 2, figsize=(15, 2.4 * ((n + 1) // 2)), sharex=True)
    for ax, w in zip(axes.ravel(), wells):
        g = P[P.well_name == w]
        t = T[T.well_name == w]
        for ph, c in [("oil", "#2ca02c"), ("water", "#1f77b4")]:
            if f"{ph}_p10" in g:
                ax.fill_between(g["date"], g[f"{ph}_p10"], g[f"{ph}_p90"], color=c, alpha=0.18, lw=0)
        ax.plot(g["date"], g["oil_rate_stbd"], color="#2ca02c", lw=1, label="oil VFM")
        ax.plot(g["date"], g["water_rate_stbd"], color="#1f77b4", lw=1, label="water VFM")
        ax.scatter(t["test_mid"], t["oil_rate_stbd"], color="#2ca02c", edgecolor="k", s=25, zorder=3, label="oil test")
        ax.scatter(t["test_mid"], t["water_rate_stbd"], color="#1f77b4", edgecolor="k", s=25, zorder=3, label="water test")
        ax.set_title(w, fontsize=9)
        ax.set_ylabel("STB/d", fontsize=8)
    axes.ravel()[0].legend(fontsize=7, ncol=4)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
