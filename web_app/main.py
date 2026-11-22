"""CaspianX Well Intelligence - FastAPI backend (no Streamlit).

Wraps the existing core/ pipeline (core.app_pipeline: ingest -> VFM blend -> PTA Horner -> interpretation) and the
FINAL VFM model (web_app/models/vfm_model_FINAL.pkl = submitted REF v2 blend refitted on all 85 tests).

Run:  uvicorn web_app.main:app --host 0.0.0.0 --port 8000      (from the repository root)
"""
from __future__ import annotations

import base64
import shutil
import tempfile
import zipfile
import html
import io
import json
import math
import os
import pickle
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
for p in (str(ROOT), str(BASE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core import app_pipeline as AP                                   # noqa: E402
from core import pta_horner as H                                      # noqa: E402
from core.exporters import portal_pta_horner, portal_vfm_rates, to_excel_bytes, to_portal_csv  # noqa: E402
import demo_data                                                      # noqa: E402

PH = ["oil", "water", "gas"]
RATES = {"oil": "oil_rate_stbd", "water": "water_rate_stbd", "gas": "gas_rate_mscfd"}
MODEL_PATH = BASE / "models" / "vfm_model_FINAL.pkl"
CARD_PATH = BASE / "models" / "metrics_card.json"
MAX_SESSIONS = 8

app = FastAPI(title="CaspianX Well Intelligence", version="5.0", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.add_middleware(GZipMiddleware, minimum_size=1024)   # plotly.min.js 4.8 MB -> ~1.5 MB on the wire

_MODEL = None
_LOCK = threading.Lock()
SESSIONS: "OrderedDict[str, dict]" = OrderedDict()


def model():
    global _MODEL
    if _MODEL is None:
        _MODEL = pickle.loads(MODEL_PATH.read_bytes())
    return _MODEL


# ------------------------------------------------------------------------------------------------ helpers
def clean(x):
    """JSON-safe: numpy -> python, NaN/inf -> None, timestamps -> ISO strings."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, np.ndarray):
        return [clean(v) for v in x.tolist()]
    if isinstance(x, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(x).isoformat()
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        v = float(x)
        return None if not math.isfinite(v) else v
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return x


def ok(payload: dict) -> JSONResponse:
    return JSONResponse(clean({"ok": True, **payload}))


def session(sid: str) -> dict:
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(404, "Session not found or expired: load data again (Upload or Load demo).")
    SESSIONS.move_to_end(sid)
    return s


JOBS: dict = {}
WORK = Path(tempfile.gettempdir()) / "caspianx_web"
WORK.mkdir(parents=True, exist_ok=True)
DATA_EXT = (".csv", ".xlsx", ".xls", ".json", ".zip")
SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("CASPIANX_SYNC_UPLOAD"))


def _evict(sid: str, s: dict) -> None:
    d = s.get("dir")
    if d:
        shutil.rmtree(d, ignore_errors=True)


def run_pipeline(files: list, source: str, progress=None, work_dir: str | None = None) -> dict:
    step = progress or (lambda msg: None)
    m = model()
    step("reading and validating files" + (" (per-minute file is aggregated in chunks)" if any(isinstance(b, str) for _, b in files) else ""))
    I = AP.load_inputs(files, set(m.a.M["Cw"]))
    step("detecting shut-ins and build-ups")
    AP.event_series.last_error = None
    series = AP.event_series(I["events"], I["tables"].get("choke_events"), I["hf"])
    step(f"running VFM and PTA ({len(series)} build-up record(s))")
    R = AP.run(I["tables"], series, m.a, m.b)
    if getattr(AP.event_series, "last_error", None):
        R["warnings"].append(AP.event_series.last_error)
    sid = uuid.uuid4().hex
    with _LOCK:
        SESSIONS[sid] = {"I": I, "R": R, "source": source, "t": time.time(), "dir": work_dir}
        while len(SESSIONS) > MAX_SESSIONS:
            _evict(*SESSIONS.popitem(last=False))
    return {"sid": sid, **summary(SESSIONS[sid])}


def _safe_extract(zpath: Path, dest: Path) -> list[str]:
    """Extract only data files from a ZIP, flattening paths (no traversal outside dest)."""
    names = []
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            base = Path(info.filename).name
            if info.is_dir() or not base or base.startswith(("._", "~$")) or not base.lower().endswith(DATA_EXT):
                continue
            out = dest / base
            i = 1
            while out.exists():
                out = dest / f"{Path(base).stem}_{i}{Path(base).suffix}"
                i += 1
            with z.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            names.append(out.name)
            if out.suffix.lower() == ".zip":
                names += _safe_extract(out, dest)
                out.unlink()
    return names


def _job(jid: str, folder: Path, names: list[str]) -> None:
    J = JOBS[jid]
    try:
        for f in list(folder.iterdir()):
            if f.suffix.lower() == ".zip":
                J["stage"] = f"extracting {f.name}"
                _safe_extract(f, folder)
                f.unlink()
        files = AP.files_from_path(folder)
        if not files:
            raise AP.InputError("no CSV / Excel / JSON data file found in the upload")
        J["files"] = [n for n, _ in files]

        def step(msg):
            J["stage"] = msg
        res = run_pipeline(files, "uploaded files: " + ", ".join(names)[:200], step, str(folder))
        J.update(state="done", stage="done", result=res)
    except AP.InputError as ex:
        J.update(state="error", error=f"Input check failed: {ex}")
        shutil.rmtree(folder, ignore_errors=True)
    except MemoryError:
        J.update(state="error", error="The server ran out of memory while processing the upload. Close other programs "
                                      "or upload the per-minute file on its own (it is read in chunks from disk).")
        shutil.rmtree(folder, ignore_errors=True)
    except Exception as ex:
        J.update(state="error", error=f"Processing failed: {type(ex).__name__}: {ex}")
        shutil.rmtree(folder, ignore_errors=True)
    finally:
        J["t_end"] = time.time()


def blend_with_band(R: dict) -> pd.DataFrame:
    """Submitted blend rates + P10/P90 transferred from model A's empirical band (same relative width)."""
    V = R["Bl"].assign(date=lambda d: pd.to_datetime(d.date))
    P = R["P"].assign(date=lambda d: pd.to_datetime(d.date))
    cols = ["date", "well_name"] + [c for ph in PH for c in (RATES[ph], f"{ph}_p10", f"{ph}_p90") if c in P]
    M = V.merge(P[cols], on=["date", "well_name"], how="left", suffixes=("", "_A"))
    for ph in PH:
        r = RATES[ph]
        if f"{ph}_p10" in M:
            a = M[r + "_A"].replace(0, np.nan)
            M[f"{ph}_p10"] = (M[r] * M[f"{ph}_p10"] / a).fillna(0).round(2)
            M[f"{ph}_p90"] = (M[r] * M[f"{ph}_p90"] / a).fillna(0).round(2)
    return M[["date", "well_name"] + [RATES[p] for p in PH] + [c for ph in PH for c in (f"{ph}_p10", f"{ph}_p90") if c in M]]


def summary(s: dict) -> dict:
    R, I = s["R"], s["I"]
    V = R["Bl"].assign(date=lambda d: pd.to_datetime(d.date))
    last = V[V.date == V.date.max()]
    per = R.get("period", {})
    card = json.loads(CARD_PATH.read_text()) if CARD_PATH.exists() else {}
    fw = card.get("vfm", {}).get("forward_90d", {})
    fo, fw_, fg = (float(last[RATES[ph]].sum()) for ph in PH)
    al = alerts_table(R)
    return {"source": s["source"], "dataset_kind": "DEMO" if "synthetic" in s["source"] else "FIELD DATA",
            "wells": sorted(V.well_name.unique()),
            "last_day": V.date.max().strftime("%Y-%m-%d"),
            "derived": {"water_cut_pct": fw_ / (fo + fw_) * 100 if (fo + fw_) > 0 else None,
                        "gor_scf_stb": fg * 1000 / fo if fo > 0 else None,
                        "wells_flowing": int((last[[RATES[p] for p in PH]].sum(axis=1) > 0).sum()),
                        "n_alerts": int(sum(1 for a in al if a.get("severity") in ("alert", "warn")))},
            "period": [V.date.min().strftime("%Y-%m-%d"), V.date.max().strftime("%Y-%m-%d")], "well_days": int(len(V)),
            "n_buildups": len(R["pta"]), "vfm_source": R.get("vfm_src"),
            "field_last_day": {ph: float(last[RATES[ph]].sum()) for ph in PH},
            "train_period": [str(pd.Timestamp(per.get("train_first")).date()) if per.get("train_first") is not None else None,
                             str(pd.Timestamp(per.get("train_last")).date()) if per.get("train_last") is not None else None],
            "warnings": R["warnings"], "messages": [list(x) for x in I["messages"]],
            "model": {"name": "REF v2 blend (choke physics + Ridge, direct 3-target Ridge, geometric blend 0.2/0.5/0.3)",
                      "forward_cv_mape": {ph: fw.get(ph, {}).get("MAPE_%") for ph in PH}}}


def _horner_rmse(h, pl) -> float | None:
    """RMSE of the Horner line against the raw pressure points inside the radial window (same points as R²)."""
    try:
        dt, p = pl.dt_hr.values.astype(float), pl.pws_psi.values.astype(float)
        o = np.argsort(dt)
        dt, p = H.truncate_at_reopen(dt[o], p[o])
        sel = (dt >= h.window_dt_start_hr) & (dt <= h.window_dt_end_hr)
        if sel.sum() < 2:
            return None
        line = h.p_star_psi - h.horner_slope * np.log10((h.tp_hr + dt[sel]) / dt[sel])
        return float(np.sqrt(np.mean((p[sel] - line) ** 2)))
    except Exception:
        return None


def pta_rows(R: dict) -> list[dict]:
    out = []
    for i, (r, pl) in enumerate(R["pta"]):
        h = r["horner"]
        pl = pl if pl is not None else pd.DataFrame({"dt_hr": []})
        st = r.get("storage", {}) or {}
        out.append({"id": i, "well": r["well_name"], "t_shut": pd.Timestamp(r["t_shut"]).strftime("%Y-%m-%d %H:%M"),
                    "k_md": r.get("horner_k_md"), "skin": r.get("horner_skin"), "m_psi_cycle": r.get("horner_m"),
                    "dp_skin_psi": r.get("horner_dP_skin_psi"), "p_star_psi": getattr(h, "p_star_psi", None),
                    "pwf_psi": r.get("pwf_psi"), "r2": r.get("horner_r2"), "tp_hr": getattr(h, "tp_hr", None),
                    "window_hr": [getattr(h, "window_dt_start_hr", None), getattr(h, "window_dt_end_hr", None)],
                    "window_cycles": getattr(h, "window_cycles", None), "q_oil_stbd": r.get("q_oil_stbd"),
                    "q_source_date": str(r.get("q_source_date")) if r.get("q_source_date") is not None else None,
                    "C_bbl_psi": st.get("C_data_bbl_psi"), "t_wbs_end_hr": st.get("t_wbs_end_hr"),
                    "not_reported": r.get("pta_not_reported") or "", "flags": getattr(h, "flags", "") or "",
                    "class": (r.get("interp") or {}).get("class"), "p1hr_psi": getattr(h, "p1hr_psi", None),
                    "pvt": {k: (r.get("pvt") or {}).get(k) for k in ["Bo", "mu", "h_ft", "phi", "ct", "rw_ft"]},
                    "dq_stb_d": (r.get("interp") or {}).get("dq_stb_d"),
                    "duration_hr": float(np.nanmax(pl.dt_hr.values)) if len(pl) else None,
                    "n_points": getattr(h, "n_points", None), "rmse_psi": _horner_rmse(h, pl)})
    return out


def alerts_table(R: dict) -> list[dict]:
    """Engineer alerts from core.alerts without any monetary value (oil price 0 -> STB/d gains only)."""
    try:
        from core.alerts import build_alerts
        A = build_alerts(R, 0.0)
        return A.drop(columns=[c for c in ["rank", "value_usd_d"] if c in A]).to_dict("records")
    except Exception as ex:
        return [{"severity": "info", "well": "-", "type": "alerts", "message": f"alerts unavailable: {ex}"}]


def report_html(s: dict) -> str:
    R = s["R"]
    sm = summary(s)
    V = blend_with_band(R)
    last = V[V.date == V.date.max()]
    logos = ""
    for f in ["socar_logo.png", "caspian_ai_logo.png"]:
        p = BASE / "static" / "img" / f
        if p.exists():
            logos += f"<img src='data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}' style='height:28px;margin-right:16px'>"
    wt = "".join(f"<tr><td>{html.escape(r.well_name)}</td><td>{r.oil_rate_stbd:,.0f}</td><td>{r.water_rate_stbd:,.0f}</td>"
                 f"<td>{r.gas_rate_mscfd:,.0f}</td></tr>" for r in last.itertuples())
    pr = pta_rows(R)
    pt = "".join(f"<tr><td>{x['well']}</td><td>{x['t_shut']}</td><td>{x['k_md']:.1f}</td><td>{x['skin']:+.2f}</td>"
                 f"<td>{x['m_psi_cycle']:.2f}</td><td>{x['dp_skin_psi']:.1f}</td><td>{html.escape(x['class'] or '')}</td></tr>"
                 for x in pr if x["k_md"] is not None)
    it = "".join(f"<div class='ev'><b>{html.escape(r['well_name'])} · {pd.Timestamp(r['t_shut']):%Y-%m-%d %H:%M}</b><ul>"
                 + "".join(f"<li>{html.escape(str(l))}</li>" for l in (r.get('interp') or {}).get('lines', [])) + "</ul></div>"
                 for r, _ in R["pta"])
    warn = "".join(f"<li>{html.escape(w)}</li>" for w in R["warnings"])
    f = sm["field_last_day"]
    return f"""<!doctype html><html><head><meta charset='utf-8'><title>CaspianX engineering summary</title><style>
body{{font-family:Archivo,Segoe UI,Arial,sans-serif;background:#07080b;color:#f6efe4;margin:0;padding:28px 36px;line-height:1.45}}
h1{{font-size:22px;margin:12px 0 2px}}h2{{font-size:16px;color:#f2a446;border-bottom:1px solid rgba(255,206,140,.26);padding-bottom:4px;margin-top:26px}}
.k{{display:inline-block;background:#120d08;border:1px solid rgba(255,206,140,.14);border-radius:6px;padding:10px 16px;margin:4px 8px 4px 0}}
.k b{{font-size:20px;color:#f2a446}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border:1px solid rgba(255,206,140,.14);padding:4px 8px;text-align:right}}
td:first-child,th:first-child{{text-align:left}}th{{background:#1f160d}}.ev{{background:#120d08;border:1px solid rgba(255,206,140,.14);border-radius:6px;padding:8px 14px;margin:8px 0}}
.mut{{color:#b3a38c;font-size:12px}}@media print{{body{{background:#fff;color:#111}}.k,.ev,th{{background:#f3f4f6}}td,th,.k,.ev{{border-color:#ccc}}}}
</style></head><body>{logos}<h1>CaspianX · Well Intelligence engineering summary</h1>
<div class='mut'>Data: {html.escape(sm['source'])} · period {sm['period'][0]} to {sm['period'][1]} · {sm['well_days']} well-days ·
{sm['n_buildups']} build-ups · model: {html.escape(sm['model']['name'])}</div>
<h2>Field rates, last day</h2><div class='k'><b>{f['oil']:,.0f}</b> STB/d oil</div><div class='k'><b>{f['water']:,.0f}</b> STB/d water</div>
<div class='k'><b>{f['gas']:,.0f}</b> Mscf/d gas</div>
<h2>Daily rates by well (last day)</h2><table><tr><th>Well</th><th>Oil STB/d</th><th>Water STB/d</th><th>Gas Mscf/d</th></tr>{wt}</table>
<h2>Pressure-transient analysis (Horner, automatic Bourdet radial window)</h2>
<table><tr><th>Well</th><th>Shut-in</th><th>k, md</th><th>Skin</th><th>m, psi/cycle</th><th>ΔP skin, psi</th><th>Interpretation</th></tr>{pt}</table>
<h2>Interpretation</h2>{it or "<p class='mut'>No build-up in the loaded data.</p>"}
<h2>Notes and warnings</h2><ul>{warn or '<li>none</li>'}</ul>
<p class='mut'>Generated {pd.Timestamp.now():%Y-%m-%d %H:%M}. Use the browser's Print → Save as PDF for a PDF copy.</p></body></html>"""


# ------------------------------------------------------------------------------------------------ routes
@app.exception_handler(Exception)
async def _err(request, exc):
    return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=3)},
                        status_code=500)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE / "static" / "img" / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return ok({"model_loaded": MODEL_PATH.exists(), "model": MODEL_PATH.name, "sessions": len(SESSIONS)})


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    """Stream every file to disk in 1 MB chunks (never held whole in memory), then process in a background thread.
    Returns a job id; poll /api/job/{id}."""
    jid = uuid.uuid4().hex
    folder = WORK / jid
    folder.mkdir(parents=True)
    names, total = [], 0
    for f in files:
        base = Path(f.filename or "upload.csv").name
        if not base.lower().endswith(DATA_EXT):
            names.append(f"{base} (ignored: not CSV/Excel/ZIP/JSON)")
            continue
        out = folder / base
        with open(out, "wb") as fh:
            while chunk := await f.read(1 << 20):
                fh.write(chunk)
                total += len(chunk)
        names.append(base)
    if total == 0:
        shutil.rmtree(folder, ignore_errors=True)
        return JSONResponse({"ok": False, "error": "No file content received (empty or unsupported files)."}, status_code=400)
    JOBS[jid] = {"state": "running", "stage": "queued", "t0": time.time(), "bytes": total, "names": names}
    if SERVERLESS:   # serverless hosts freeze background threads after the response: process inside the request
        _job(jid, folder, names)
        return job(jid)
    threading.Thread(target=_job, args=(jid, folder, names), daemon=True).start()
    for k in [k for k, v in JOBS.items() if v.get("t_end") and time.time() - v["t_end"] > 3600]:
        JOBS.pop(k, None)
    return ok({"job": jid, "bytes": total, "files": names})


@app.get("/api/job/{jid}")
def job(jid: str):
    J = JOBS.get(jid)
    if J is None:
        raise HTTPException(404, "Upload job not found (server restarted?). Upload the files again.")
    out = {"job": jid, "state": J["state"], "stage": J.get("stage"), "elapsed_s": round((J.get("t_end") or time.time()) - J["t0"], 1),
           "bytes": J.get("bytes"), "files": J.get("names")}
    if J["state"] == "done":
        out["result"] = J["result"]
    if J["state"] == "error":
        out["error"] = J["error"]
    return ok(out)


@app.post("/api/demo")
def demo():
    return ok(run_pipeline(demo_data.build(model()), "synthetic demo field (not field data)"))


@app.get("/api/summary")
def get_summary(sid: str):
    return ok(summary(session(sid)))


@app.get("/api/measurements")
def measurements(sid: str, well: str):
    s = session(sid)
    P = s["R"]["P"].assign(date=lambda d: pd.to_datetime(d.date))
    g = P[P.well_name == well].sort_values("date")
    if g.empty:
        raise HTTPException(404, f"Well {well} not in the loaded data.")
    cols = {"whp_psia": "WHP, psia", "whp_downstream_psia": "Downstream P, psia", "dh_pressure_psia": "P_wf (downhole), psia",
            "choke_opening_pct": "Choke, %", "gas_lift_rate_mscfd": "Gas lift, Mscf/d", "uptime": "Uptime, fraction"}
    temp = [c for c in g.columns if "temperature" in c]
    series = {lab: g[c].values for c, lab in cols.items() if c in g}
    for c in temp:
        series[c] = g[c].values
    wins = []
    for r, pl in s["R"]["pta"]:
        if r["well_name"] == well:
            t0 = pd.Timestamp(r["t_shut"])
            dur = float(np.nanmax(pl.dt_hr.values)) if len(pl) else 0.0
            wins.append({"start": t0.isoformat(), "end": (t0 + pd.Timedelta(hours=dur)).isoformat(), "kind": "build-up"})
    shut = g[g.get("uptime", pd.Series(1, index=g.index)) <= 0].date.dt.strftime("%Y-%m-%d").tolist()
    units = {lab: lab.split(", ")[-1] if ", " in lab else "" for lab in series}
    return ok({"well": well, "dates": g.date.dt.strftime("%Y-%m-%d").tolist(), "series": series, "units": units,
               "temperature_available": bool(temp), "buildups": wins, "shutin_days": shut})


@app.get("/api/vfm")
def vfm(sid: str, well: str = Query("ALL")):
    s = session(sid)
    V = blend_with_band(s["R"])
    if well != "ALL":
        V = V[V.well_name == well]
        if V.empty:
            raise HTTPException(404, f"Well {well} not in the loaded data.")
    else:
        V = V.groupby("date", as_index=False)[[c for c in V.columns if c not in ("date", "well_name")]].sum()
    V = V.sort_values("date")
    Tq = s["R"].get("Tq") if s["R"].get("tests_uploaded") else None
    tests = []
    if Tq is not None and well != "ALL":
        t = Tq[Tq.well_name == well]
        tests = [{"date": pd.Timestamp(r.test_mid).strftime("%Y-%m-%d"), "oil": r.oil_rate_stbd, "water": r.water_rate_stbd,
                  "gas": r.gas_rate_mscfd} for r in t.itertuples()]
    shut = []
    if well != "ALL":
        P_ = s["R"]["P"].assign(date=lambda d: pd.to_datetime(d.date))
        shut = P_[(P_.well_name == well) & (P_.uptime <= 0)].date.dt.strftime("%Y-%m-%d").tolist()
    per = s["R"].get("period", {})
    resid = []
    if tests:
        idx = dict(zip(V.date.dt.strftime("%Y-%m-%d"), range(len(V))))
        for t in tests:
            i = idx.get(t["date"])
            if i is None:
                continue
            row = {"date": t["date"]}
            for ph in PH:
                pr, ac = float(V[RATES[ph]].iloc[i]), float(t[ph])
                row[ph] = {"pred": pr, "test": ac, "err_pct": (pr / ac - 1) * 100 if ac > 0 else None}
            resid.append(row)
    layers = {}
    for key, src in (("model_a", s["R"]["P"]), ("model_b", s["R"].get("B"))):
        if src is None or not len(src):
            continue
        D_ = src.assign(date=lambda d: pd.to_datetime(d.date))
        D_ = D_[D_.well_name == well] if well != "ALL" else D_.groupby("date", as_index=False)[[RATES[p] for p in PH]].sum(min_count=1)
        D_ = V[["date"]].merge(D_[["date"] + [RATES[p] for p in PH]], on="date", how="left")
        layers[key] = {ph: D_[RATES[ph]].values for ph in PH}
    out = {"well": well, "dates": V.date.dt.strftime("%Y-%m-%d").tolist(), "tests": tests, "shutin_days": shut, "layers": layers,
           "train_last": str(pd.Timestamp(per["train_last"]).date()) if per.get("train_last") is not None else None,
           "residuals": resid}
    for ph in PH:
        out[ph] = {"rate": V[RATES[ph]].values, "p10": V.get(f"{ph}_p10", pd.Series(dtype=float)).values,
                   "p90": V.get(f"{ph}_p90", pd.Series(dtype=float)).values}
    return ok(out)


@app.get("/api/pta")
def pta(sid: str):
    s = session(sid)
    ev = []
    for row, (r, pl) in zip(pta_rows(s["R"]), s["R"]["pta"]):
        h = r["horner"]
        dt, p = pl.dt_hr.values.astype(float), pl.pws_psi.values.astype(float)
        o = np.argsort(dt)
        dt, p = H.truncate_at_reopen(dt[o], p[o])
        okm = dt > 0
        dt, p = dt[okm], p[okm]
        pwf, tp = float(r["pwf_psi"]), float(h.tp_hr)
        te = H.agarwal_time(tp, dt)
        dp = p - pwf
        der = H.bourdet_derivative(te, dp, L=0.1)
        hr = (tp + dt) / dt
        line = h.p_star_psi - h.horner_slope * np.log10(hr)
        ev.append({**row, "dt_hr": dt, "pws": p, "te_hr": te, "dp": dp, "deriv": der, "horner_ratio": hr, "horner_line": line})
    return ok({"events": ev})


@app.get("/api/properties")
def properties(sid: str):
    return ok({"rows": pta_rows(session(sid)["R"])})


@app.get("/api/interpret")
def interpret(sid: str):
    s = session(sid)
    R = s["R"]
    rows = pta_rows(R)
    ev = [{"id": x["id"], "well": x["well"], "t_shut": x["t_shut"], "class": x["class"], "skin": x["skin"],
           "k_md": x["k_md"], "dp_skin_psi": x["dp_skin_psi"], "dq_stb_d": x["dq_stb_d"],
           "lines": (r.get("interp") or {}).get("lines", [])} for x, (r, _) in zip(rows, R["pta"])]
    hyd = {}
    H_ = R.get("hydraulics")
    if H_ is not None and len(H_) and "erosional_ratio" in H_:
        hyd = {r.well_name: {"erosional_ratio": r.erosional_ratio, "v_mixture_ft_s": getattr(r, "v_mixture_ft_s", None)}
               for r in H_.itertuples()}
    alerts = alerts_table(R)
    wells = sorted(set(R["Bl"].well_name))
    table = []
    for w in wells:
        e = [x for x in ev if x["well"] == w]
        e = max(e, key=lambda x: x["t_shut"]) if e else None
        table.append({"well": w, "event_id": e["id"] if e else None, "t_shut": e["t_shut"] if e else None,
                      "k_md": e["k_md"] if e else None, "skin": e["skin"] if e else None,
                      "dp_skin_psi": e["dp_skin_psi"] if e else None, "class": e["class"] if e else None,
                      "dq_stb_d": e["dq_stb_d"] if (e and e["skin"] is not None and e["skin"] > 0) else None,
                      "erosional_ratio": hyd.get(w, {}).get("erosional_ratio"),
                      "n_alerts": sum(1 for a in alerts if a.get("well") == w and a.get("severity") in ("alert", "warn"))})
    return ok({"events": ev, "wells": table, "alerts": alerts})


@app.get("/api/model")
def model_card():
    """Structured description of the deployed VFM, taken from the code and the stored validation card."""
    from core.vfm import BAND_SCALE_FWD, RES_CLIP, TREND_HORIZON_DAYS
    from core.vfm_direct import BLEND_WA
    m = model()
    card = json.loads(CARD_PATH.read_text()) if CARD_PATH.exists() else {}
    t0, t1 = m.a.train_period()
    return ok({
        "file": MODEL_PATH.name,
        "training": {"tests": int(len(m.a.T)), "wells": int(m.a.T.well_name.nunique()),
                     "first_test": str(pd.Timestamp(t0).date()), "last_test": str(pd.Timestamp(t1).date())},
        "model_a": {"name": "Choke physics + Ridge residual (liquid) with per-well WC / GOR trends",
                    "equation": "q_liq = C_w · (choke/100)² · √(WHP − P_ds) · exp(r(x)),  |r| ≤ %.1f" % RES_CLIP,
                    "split": "q_o = q_liq (1 − WC),  q_w = q_liq · WC,  q_g = q_o · GOR / 1000",
                    "features": ["ln WHP", "ln (WHP − P_ds)", "ln choke", "ln q_gl", "P_wf / 1000"],
                    "calibration": "C_w = per-well median of ln(q_test / physics); r(x) = standardised Ridge, alpha %s"
                                   % getattr(getattr(m.a.M.get("res"), "named_steps", {}).get("ridge", None), "alpha", "n/a")},
        "model_b": {"name": "Direct 3-target Ridge on log-rate (oil, water, gas fitted separately)",
                    "equation": "ln q_phase = well intercept + Ridge(features)",
                    "features": ["ln[(choke/100)² √(WHP − P_ds)]", "ln WHP", "ln (WHP − P_ds)", "ln choke", "ln q_gl",
                                 "P_wf / 1000", "days / 100 (clipped to training range ± %d d)" % TREND_HORIZON_DAYS],
                    "alpha": getattr(m.b, "alpha", None)},
        "blend": {"equation": "q = exp( w · ln q_A + (1 − w) · ln q_B )",
                  "w_A": {ph: BLEND_WA[RATES[ph]] for ph in PH}},
        "band": {"basis": "P10 / P90 = rate × exp(empirical 10 % / 90 % quantiles of out-of-sample log errors of model A): "
                          "leave-one-test-out inside the tested period, rolling-origin forward after the last test "
                          "(widened by the forward scale factors); transferred to the blend with the same relative width.",
                 "forward_scale": {ph: BAND_SCALE_FWD.get(ph) for ph in PH}},
        "validation": card.get("vfm", {}),
        "trend_horizon_days": TREND_HORIZON_DAYS})


@app.get("/api/export/{kind}")
def export(kind: str, sid: str):
    s = session(sid)
    R = s["R"]
    if kind == "vfm_csv":
        return Response(to_portal_csv(portal_vfm_rates(R["Bl"])), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=vfm_submission.csv"})
    if kind == "pta_csv":
        return Response(to_portal_csv(portal_pta_horner(AP.horner_rows(R["pta"]))), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=pta_submission.csv"})
    if kind == "xlsx":
        it = pd.DataFrame([{"well": r["well_name"], "t_shut": str(r["t_shut"]), "line": l}
                           for r, _ in R["pta"] for l in (r.get("interp") or {}).get("lines", [])])
        b = to_excel_bytes({"VFM": portal_vfm_rates(R["Bl"]), "VFM_P10_P90": blend_with_band(R).assign(date=lambda d: d.date.dt.date),
                            "PTA": portal_pta_horner(AP.horner_rows(R["pta"])), "PTA_detail": pd.DataFrame(pta_rows(R)),
                            "Interpretation": it, "Warnings": pd.DataFrame({"warning": R["warnings"]})})
        return Response(b, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": "attachment; filename=caspianx_results.xlsx"})
    if kind == "report_html":
        return Response(report_html(s), media_type="text/html",
                        headers={"Content-Disposition": "attachment; filename=caspianx_engineering_summary.html"})
    raise HTTPException(404, f"Unknown export '{kind}'. Use vfm_csv, pta_csv, xlsx or report_html.")
