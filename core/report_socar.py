"""Self-contained HTML engineering report in the SOCAR challenge visual identity (one file, prints to PDF)."""
from __future__ import annotations

import base64
import datetime as _dt
import html

import pandas as pd

CSS = """
/* offline: no web fonts; Archivo / JetBrains Mono are used when installed, else system fonts */
:root{--bg:#07080b;--p1:#110d09;--p2:#17120c;--line:rgba(255,206,140,.16);--fg:#f6efe4;--mut:#b3a38c;
--oil:#f2a446;--oil2:#ffc861;--gas:#a8d8ff;--wat:#4f8ff0;--crit:#e0503a}
body{background:var(--bg);color:var(--fg);font-family:Archivo,'Segoe UI',system-ui,Arial,sans-serif;margin:0;padding:32px 40px;line-height:1.45}
h1{font-weight:800;letter-spacing:.02em;margin:0 0 4px}h2{font-weight:600;border-bottom:1px solid var(--line);padding-bottom:6px;margin-top:34px}
h3{font-weight:600;color:var(--oil2);margin:18px 0 6px}.mut{color:var(--mut)}
.k{display:inline-block;background:var(--p1);border:1px solid var(--line);padding:12px 18px;margin:0 10px 10px 0;min-width:150px}
.k b{font-family:'JetBrains Mono',Consolas,ui-monospace,monospace;font-variant-numeric:tabular-nums;font-size:1.5rem;display:block}
table{border-collapse:collapse;margin:8px 0;font-family:'JetBrains Mono',Consolas,ui-monospace,monospace;font-variant-numeric:tabular-nums;font-size:.85rem}
th,td{border:1px solid var(--line);padding:4px 9px;text-align:right}th{background:var(--p2);color:var(--mut);font-weight:600}
td:first-child,th:first-child{text-align:left}img{max-width:100%;border:1px solid var(--line);margin:8px 0}
ul{margin:4px 0 10px 18px}
@media print{body{background:#fff;color:#111}.k,th{background:#f4efe6}}
"""


def _t(df: pd.DataFrame, d: int = 2) -> str:
    return df.round(d).to_html(index=False, na_rep="-", border=0)


def build_report(kpi: dict, metrics: dict | None, vfm_last: pd.DataFrame, pta_table: pd.DataFrame,
                 interpretations: list[tuple[str, list]], integrity: list[str], meta: dict,
                 images: list[tuple[str, bytes]] | None = None, live: pd.DataFrame | None = None,
                 tables: list[tuple[str, pd.DataFrame]] | None = None) -> str:
    k = "".join(f"<div class='k'><span class='mut'>{html.escape(a)}</span><b>{html.escape(b)}</b></div>" for a, b in kpi.items())
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>CaspianX engineering report</title><style>{CSS}</style>"
           "</head><body>", "<h1>CASPIANX · Well Intelligence</h1>",
           f"<div class='mut'>SOCAR Hackathon 2026 · generated {_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d %H:%M} UTC · "
           + " · ".join(f"{html.escape(a)}: {html.escape(str(b))}" for a, b in meta.items()) + "</div>", f"<h2>Field today</h2>{k}"]
    if metrics:
        rows = []
        for sc, lab in [("forward_90d", "90-day forward CV"), ("leave_one_well_out", "leave-one-well-out CV")]:
            for ph, v in metrics["vfm"].get(sc, {}).items():
                rows.append({"validation": lab, "phase": ph, "n": v["n"], "MAPE %": v["MAPE_%"], "WAPE %": v["WAPE_%"],
                             "MAE": v["MAE"], "RMSE": v["RMSE"], "R2": v["R2"]})
        out.append("<h2>Model accuracy and error rate (out-of-sample)</h2>" + _t(pd.DataFrame(rows), 3))
        cov = metrics["vfm"].get("p10_p90_coverage_%")
        if cov:
            out.append(f"<p class='mut'>P10-P90 empirical coverage (nominal 80 %): oil {cov['oil']} %, water {cov['water']} %, "
                       f"gas {cov['gas']} % ({html.escape(metrics['vfm'].get('coverage_note', ''))}).</p>")
        if "pta" in metrics:
            p = metrics["pta"]
            out.append(f"<p class='mut'>PTA final check on {p['n_matched']} build-ups: mean |k error| "
                       f"{p['k_abs_err_mean_%']:.2f} %, mean |skin error| {p['skin_abs_err_mean']:.3f}.</p>")
    if live is not None and len(live):
        out.append("<h2>Live error rate on the loaded well tests</h2>" + _t(live, 3))
    out.append("<h2>Phase 1 · VFM, last day</h2>" + _t(vfm_last))
    out.append("<h2>Phase 2 · PTA (Horner, automatic radial window)</h2>"
               + (_t(pta_table, 4) if len(pta_table) else "<p>No build-up with pressure data in this dataset.</p>"))
    for title, lines in interpretations:
        out.append(f"<h3>{html.escape(title)}</h3><ul>" + "".join(f"<li>{html.escape(str(x))}</li>" for x in lines) + "</ul>")
    for title, png in images or []:
        out.append(f"<h3>{html.escape(title)}</h3><img src='data:image/png;base64,{base64.b64encode(png).decode()}'>")
    for title, df in tables or []:
        if df is not None and len(df):
            out.append(f"<h2>{html.escape(title)}</h2>" + _t(df, 3))
    if integrity:
        out.append("<h2>Data integrity</h2><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in integrity) + "</ul>")
    out.append("</body></html>")
    return "\n".join(out)
