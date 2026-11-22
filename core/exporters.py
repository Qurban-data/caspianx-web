"""
Official deliverables and the executive engineering report.

Phase 1 Excel (README):  date, well_name, oil_rate_stbd, water_rate_stbd, gas_rate_mscfd
Phase 2 Excel (README):  per shut-in event -> Horner plot slope [psi/cycle], skin [-], permeability(md),
                         delta P skin (psi)
"""
from __future__ import annotations

import datetime as _dt
import html
import os
import io

import numpy as np
import pandas as pd

VFM_COLS = ["date", "well_name", "oil_rate_stbd", "water_rate_stbd", "gas_rate_mscfd"]
PTA_COLS = ["well_name", "t_shut", "duration_hr", "m_psi_cycle", "k_md", "skin", "dP_skin_psi"]
PTA_OFFICIAL = {"m_psi_cycle": "Horner plot slope", "skin": "skin", "k_md": "permeability(md)",
                "dP_skin_psi": "delta P skin (psi)"}
PTA_OFFICIAL_COLS = ["Horner plot slope", "skin", "permeability(md)", "delta P skin (psi)"]


def vfm_excel_table(P: pd.DataFrame) -> pd.DataFrame:
    """Phase 1 table with exactly the five official columns (rates rounded to 0.01)."""
    t = P.copy()
    t["date"] = pd.to_datetime(t["date"]).dt.date
    for c in VFM_COLS[2:]:
        t[c] = t[c].round(2)
    return t[VFM_COLS]


def vfm_uncertainty_table(P: pd.DataFrame) -> pd.DataFrame:
    """Companion sheet: P10/P50/P90 per phase (P50 = point prediction)."""
    cols = ["date", "well_name"] + [c for ph in ["oil", "water", "gas"] for c in (f"{ph}_p10", f"{ph}_p90")
                                    if c in P]
    t = P[cols + VFM_COLS[2:]].copy()
    t["date"] = pd.to_datetime(t["date"]).dt.date
    return t.round(2)


def pta_excel_table(results: list[dict]) -> pd.DataFrame:
    """Per-event table with the regression (final) values under short names + traceability columns."""
    R = pd.DataFrame(results)
    R = R.drop(columns=[c for c in ["m_psi_cycle", "k_md", "skin", "dP_skin_psi"] if c in R])
    R = R.rename(columns={"final_m_psi_cycle": "m_psi_cycle", "final_k_md": "k_md", "final_skin": "skin",
                          "final_dP_skin_psi": "dP_skin_psi"})
    R = R.loc[:, ~R.columns.duplicated()]
    extra = [c for c in ["q_oil_stbd", "q_source_date", "tp_hr", "pwf_psi", "final_C_bbl_psi", "re_reg_ft",
                         "match_rmse_psi"] if c in R]
    return R[PTA_COLS + extra]


def pta_official_table(R: pd.DataFrame) -> pd.DataFrame:
    """Phase 2 table: event identifiers + the four official headers, in README order."""
    t = R[["well_name", "t_shut", "duration_hr", "m_psi_cycle", "skin", "k_md", "dP_skin_psi"]].copy()
    return t.rename(columns=PTA_OFFICIAL)


def to_excel_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    """Write {sheet name: DataFrame} to an in-memory .xlsx."""
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
    return bio.getvalue()


# ------------------------------------------------------------------ executive report
_CSS = """
:root{--bg:#0A0E14;--card:#111720;--line:#1E293B;--fg:#E2E8F0;--mut:#94A3B8;--amb:#F59E0B}
body{font-family:"IBM Plex Sans",Segoe UI,Arial,sans-serif;font-variant-numeric:tabular-nums;background:var(--bg);color:var(--fg);margin:0;padding:28px 36px;line-height:1.45}
h1{color:var(--amb);margin:0 0 4px}h2{color:var(--amb);border-bottom:1px solid var(--line);padding-bottom:4px;margin-top:28px}
.sub{color:var(--mut)}.grid{display:flex;flex-wrap:wrap;gap:10px}
.k{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--amb);border-radius:2px;padding:10px 16px;min-width:150px}
.k b{font-size:1.4rem;color:var(--amb)}table{border-collapse:collapse;margin:8px 0;font-size:.86rem;width:100%}
td,th{border:1px solid var(--line);padding:4px 8px;text-align:right;font-family:"JetBrains Mono",Consolas,monospace}th{background:var(--card)}td:first-child,th:first-child{text-align:left}
.ev{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:8px 0}
.note{color:var(--mut);font-size:.85rem}
@media print{:root{--bg:#fff;--card:#f3f4f6;--line:#d1d5db;--fg:#111;--mut:#555;--amb:#b45309}body{padding:10mm}.ev,.k{break-inside:avoid}}
"""


def executive_report_html(m, P: pd.DataFrame, results: list[dict], oil_price: float = 75.0,
                          validation: pd.DataFrame | None = None, diagnosis=None, recommendation=None,
                          extra_html: str = "", meta: dict | None = None) -> str:
    """Self-contained HTML (dark on screen, light when printed / saved to PDF).
    ``extra_html`` = additional sections (e.g. intervention ranking, model card); ``meta`` = provenance lines."""
    R = pta_excel_table(results) if results else pd.DataFrame(columns=PTA_COLS)
    last = P[P["date"] == P["date"].max()]
    flowing = P[P["uptime"] > 0.95]
    tot = {c: float(last[c].sum()) for c in VFM_COLS[2:]}
    kpi = [("Field oil (last day)", f"{tot['oil_rate_stbd']:,.0f} STB/d"),
           ("Field water", f"{tot['water_rate_stbd']:,.0f} STB/d"),
           ("Field formation gas", f"{tot['gas_rate_mscfd']:,.0f} Mscf/d"),
           ("Field WC", f"{tot['water_rate_stbd'] / max(tot['oil_rate_stbd'] + tot['water_rate_stbd'], 1) * 100:.1f} %")]
    kpi += [(f"{ph} LOO MAPE", f"{m.scores[ph]['MAPE_%']:.2f} %") for ph in ["oil", "water", "gas"]]
    kp = "".join(f"<div class='k'><b>{v}</b><br>{html.escape(k)}</div>" for k, v in kpi)

    acc = pd.DataFrame(m.scores).T.loc[["oil", "water", "gas", "liq"], ["MAE", "RMSE", "MAPE_%", "R2"]].round(3)
    val_html = ""
    if validation is not None and len(validation):
        v = validation.pivot_table(index=["scheme", "phase"], columns="model", values="MAPE_%").round(2)
        val_html = "<h3>Out-of-sample audit (MAPE %)</h3>" + v.to_html()

    well = flowing.groupby("well_name").agg(oil=("oil_rate_stbd", "mean"), water=("water_rate_stbd", "mean"),
                                            gas=("gas_rate_mscfd", "mean"), wc=("wc", "mean"),
                                            uptime=("uptime", "size"))
    well["uptime"] = (P.groupby("well_name")["uptime"].mean() * 100).round(1)
    well["wc"] = (well["wc"] * 100).round(1)
    if results:
        sk = pd.DataFrame([{"well_name": r["well_name"], "skin": r["final_skin"],
                            "extra_oil_if_skin_removed": max(r.get("extra_oil_if_skin_removed_stb_d", 0), 0)}
                           for r in results]).groupby("well_name").last()
        well = well.join(sk)
        well["upside_$k_per_month"] = (well["extra_oil_if_skin_removed"].fillna(0) * oil_price * 30 / 1000).round(0)
    well = well.rename(columns={"oil": "oil STB/d", "water": "water STB/d", "gas": "gas Mscf/d", "wc": "WC %",
                                "uptime": "uptime %"}).round(1)

    ev = ""
    for r in results:
        d = html.escape(diagnosis(r)) if diagnosis else ""
        rec = html.escape(recommendation(r)) if recommendation else ""
        ev += (f"<div class='ev'><b>{r['well_name']} · shut-in {pd.Timestamp(r['t_shut']):%Y-%m-%d %H:%M}</b> "
               f"({r['duration_hr']:.1f} h, q = {r['q_oil_stbd']:.0f} STB/d from VFM)<br>"
               f"Horner slope <b>{r['final_m_psi_cycle']:.2f}</b> psi/cycle · k <b>{r['final_k_md']:.1f}</b> md · "
               f"skin <b>{r['final_skin']:.2f}</b> · ΔP<sub>skin</sub> <b>{r['final_dP_skin_psi']:.1f}</b> psi · "
               f"FE {r.get('flow_efficiency', np.nan):.2f}<br>{d}<br><i>Action:</i> {rec}</div>")

    return f"""<!doctype html><html><head><meta charset='utf-8'><title>CaspianX Engineering Report</title>
<style>{_CSS}</style></head><body>
<h1>CaspianX · Executive Engineering Report</h1>
<div class='sub'>SOCAR Hackathon 2026 · generated {_dt.datetime.now():%Y-%m-%d %H:%M} · {P['well_name'].nunique()} wells ·
{len(P):,} well-days ({pd.to_datetime(P['date']).min():%d %b %Y} – {pd.to_datetime(P['date']).max():%d %b %Y}) ·
{len(results)} build-up tests</div>
<h2>1. Field KPIs</h2><div class='grid'>{kp}</div>
<h2>2. VFM accuracy</h2><p class='note'>Hybrid model: sub-critical choke equation q = C<sub>w</sub>(choke/100)<sup>2</sup>√(P<sub>wh</sub>−P<sub>ds</sub>)
with per-well calibration and a bounded pooled residual; WC/GOR per-well trends. Selected: <b>{m._label(m.choice)}</b>.
Leave-one-test-out metrics:</p>{acc.to_html()}{val_html}
<h2>3. Well-by-well production &amp; optimisation potential</h2>{well.to_html(na_rep="–")}
<p class='note'>Upside = extra oil if skin were removed (q·(1/FE − 1)), valued at {oil_price:.0f} $/bbl.</p>
<h2>4. Reservoir characterisation (PTA)</h2>{pta_official_table(R).pipe(lambda t: t.round({c: 3 for c in t.select_dtypes('number').columns})).to_html(index=False) if len(R) else '<p>No build-ups.</p>'}
{ev}
<p class='note'>PTA: Laplace-domain radial model (wellbore storage + skin + closed circular boundary), Stehfest inversion,
q from the VFM prediction on the last full-uptime day before shut-in. m = 162.6qBμ/(kh), ΔP<sub>skin</sub> = 141.2qBμs/(kh).
Print this page (Ctrl+P) to save as PDF.</p>{extra_html}
<p class='note'>{" · ".join(f"{html.escape(str(k))}: {html.escape(str(v))}" for k, v in (meta or {}).items())}</p></body></html>"""


# ------------------------------------------------------------------ portal template format (same as run_all.py)
_TPL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _tpl_cols(name: str, fallback: list) -> list:
    p = os.path.join(_TPL, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as fh:
            return [c.strip() for c in fh.readline().split(",")]
    return fallback


PORTAL_VFM_COLS = _tpl_cols("vfm_submission_template.csv",
                            ["timestamp", "well_name", "oil_rate_stbd", "water_rate_stbd", "gas_rate_mscfd"])
PORTAL_PTA_COLS = _tpl_cols("pta_submission_template.csv",
                            ["well_name", "shut_in_time", "permeability_md", "skin", "delta_p_skin_psi", "horner_slope"])


def portal_vfm(P: pd.DataFrame, ts_format: str = "%Y-%m-%d") -> pd.DataFrame:
    t = vfm_excel_table(P).rename(columns={"date": "timestamp"})
    t["timestamp"] = pd.to_datetime(t["timestamp"]).dt.strftime(ts_format)
    return t[PORTAL_VFM_COLS].sort_values(["well_name", "timestamp"]).reset_index(drop=True)


def portal_pta(R: pd.DataFrame) -> pd.DataFrame:
    """R = pta_excel_table(results) (regression values under short names)."""
    t = pd.DataFrame({"well_name": R["well_name"].astype(str).str.strip(),
                      "shut_in_time": pd.to_datetime(R["t_shut"]).dt.strftime("%Y-%m-%d %H:%M:%S"),
                      "permeability_md": R["k_md"].round(4), "skin": R["skin"].round(4),
                      "delta_p_skin_psi": R["dP_skin_psi"].round(4), "horner_slope": R["m_psi_cycle"].round(4)})
    return t[PORTAL_PTA_COLS].sort_values(["well_name", "shut_in_time"]).reset_index(drop=True)


def to_portal_csv(df: pd.DataFrame) -> bytes:
    """UTF-8 (no BOM), comma, LF, no index - the portal upload format."""
    return df.to_csv(index=False, lineterminator="\n").encode("utf-8")


def portal_vfm_rates(V: pd.DataFrame, ts_format: str = "%Y-%m-%d") -> pd.DataFrame:
    """Portal VFM table from any daily-rate table with date, well_name and the three rates (e.g. the A/B blend)."""
    t = V.rename(columns={"date": "timestamp"}).copy()
    t["timestamp"] = pd.to_datetime(t["timestamp"]).dt.strftime(ts_format)
    for c in PORTAL_VFM_COLS[2:]:
        t[c] = pd.to_numeric(t[c], errors="coerce").round(2)
    return t[PORTAL_VFM_COLS].sort_values(["well_name", "timestamp"]).reset_index(drop=True)


def portal_pta_horner(rows: list[dict]) -> pd.DataFrame:
    """Portal PTA table from Horner results: rows of {well_name, t_shut, k_md, skin, m}. Same rounding and
    dP_skin = 0.87 m s as run_all.pta_horner_table (the submitted file)."""
    if not rows:
        return pd.DataFrame({c: pd.Series(dtype="object" if c in ("well_name", "shut_in_time") else float)
                             for c in PORTAL_PTA_COLS})
    t = pd.DataFrame([{"well_name": str(r["well_name"]).strip(),
                       "shut_in_time": pd.Timestamp(r["t_shut"]).strftime("%Y-%m-%d %H:%M:%S"),
                       "permeability_md": round(float(r["k_md"]), 4), "skin": round(float(r["skin"]), 4),
                       "horner_slope": round(float(r["m"]), 4)} for r in rows])
    t["delta_p_skin_psi"] = (0.87 * t.horner_slope * t.skin).round(4)
    return t[PORTAL_PTA_COLS].sort_values(["well_name", "shut_in_time"]).reset_index(drop=True)
