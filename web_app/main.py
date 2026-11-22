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
import hashlib
import hmac
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
from fastapi import Form, FastAPI, File, HTTPException, Query, UploadFile
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
_DEF_LOCK = threading.Lock()
SESSIONS: "OrderedDict[str, dict]" = OrderedDict()


PRELOAD = BASE / "preload" / "field_session.pkl"   # built by tools/build_preload.py (derived results, no per-minute data)
ACCESS_CODE = os.environ.get("CASPIANX_ACCESS_CODE", "")
PUBLIC_HOST = bool(os.environ.get("VERCEL"))


def _default_sid(code: str) -> str:
    """Session id of the bundled field results. It is derived from the access code, so it cannot be guessed without it
    and is the same on every server instance."""
    return "f" + hashlib.sha256(("caspianx:" + code).encode()).hexdigest()[:30]


def _local_folder():
    """Local field folder loaded at start on an engineer's own machine (never on a public host): CASPIANX_DATA_DIR,
    or <repo>/data_autoload, or <repo>/data/socar."""
    if PUBLIC_HOST:
        return None
    for c in (os.environ.get("CASPIANX_DATA_DIR"), BASE.parent / "data_autoload", BASE.parent / "data" / "socar"):
        if c and Path(c).is_dir() and any(Path(c).glob("*.csv")):
            return Path(c)
    return None


def _load_default(sid: str | None = None):
    key = _default_sid(ACCESS_CODE)
    if sid is not None and sid != key:
        return None
    if key in SESSIONS:
        return SESSIONS[key]
    with _DEF_LOCK:
        if key in SESSIONS:
            return SESSIONS[key]
        try:
            if PRELOAD.exists() and not (PUBLIC_HOST and not ACCESS_CODE):
                SESSIONS[key] = pickle.loads(PRELOAD.read_bytes())
            elif _local_folder() is not None:
                j = run_pipeline(AP.files_from_path(_local_folder()), f"field data (local folder {_local_folder().name})")
                SESSIONS[key] = SESSIONS.pop(j["sid"])
        except Exception:
            traceback.print_exc()
    return SESSIONS.get(key)


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
    s = SESSIONS.get(sid) or _load_default(sid)
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
            old = next(k for k in SESSIONS if k != _default_sid(ACCESS_CODE))   # bundled field session is never evicted
            _evict(old, SESSIONS.pop(old))
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


@app.post("/api/default")
def default_dataset(code: str = Form("")):
    """Field results bundled with the app. With CASPIANX_ACCESS_CODE set, the code must be supplied."""
    has = (PRELOAD.exists() and not (PUBLIC_HOST and not ACCESS_CODE)) or _local_folder() is not None
    if not has:
        return ok({"available": False, "needs_code": False})
    if ACCESS_CODE and not hmac.compare_digest(code.strip().encode(), ACCESS_CODE.encode()):
        time.sleep(0.4)
        return ok({"available": False, "needs_code": True, "wrong": bool(code.strip())})
    d = _load_default()
    if d is None:
        return ok({"available": False, "needs_code": False})
    return ok({"available": True, "sid": _default_sid(ACCESS_CODE), **summary(d)})


@app.get("/api/monthly")
def monthly(sid: str, well: str = Query("ALL")):
    """Calendar-month aggregation of the submitted daily rates, month-over-month change, well x month matrix and the
    well x day oil surface. Everything is computed from the session's daily table."""
    s = session(sid)
    V = s["R"]["Bl"].assign(date=lambda d: pd.to_datetime(d.date))
    U = s["R"]["P"].assign(date=lambda d: pd.to_datetime(d.date))[["well_name", "date", "uptime"]]
    V = V.merge(U, on=["well_name", "date"], how="left")
    wells = sorted(V.well_name.unique())
    if well != "ALL":
        if well not in wells:
            raise HTTPException(404, f"Well {well} not in the loaded data.")
        F = V[V.well_name == well]
    else:
        F = V
    F = F.assign(month=F.date.dt.to_period("M").astype(str))
    rows = []
    for m, g in F.groupby("month"):
        days = g.date.nunique()
        o, w, gs = (float(g[RATES[ph]].sum()) for ph in PH)
        rows.append({"month": m, "days": int(days), "days_in_month": int(pd.Period(m).days_in_month),
                     "oil_stb": o, "water_stb": w, "gas_mscf": gs,
                     "oil_avg": o / days, "water_avg": w / days, "gas_avg": gs / days,
                     "wc_pct": w / (o + w) * 100 if o + w > 0 else None, "gor": gs * 1000 / o if o > 0 else None,
                     "uptime_pct": float(g.uptime.mean() * 100), "shutin_well_days": int((g.uptime <= 0).sum())})
    for i, r in enumerate(rows):
        for k in ("oil_avg", "water_avg", "gas_avg"):
            prev = rows[i - 1][k] if i else None
            r[k + "_mom_pct"] = (r[k] / prev - 1) * 100 if prev else None
        for k in ("wc_pct", "gor", "uptime_pct"):
            prev = rows[i - 1][k] if i else None
            r[k + "_mom"] = r[k] - prev if (prev is not None and r[k] is not None) else None
    mean_all = {k: float(np.mean([r[k] for r in rows])) for k in ("oil_avg", "water_avg", "gas_avg")} if rows else {}
    for r in rows:
        for k, v in mean_all.items():
            r[k + "_vs_mean_pct"] = (r[k] / v - 1) * 100 if v else None
    Vm = V.assign(month=V.date.dt.to_period("M").astype(str))
    piv = {ph: Vm.pivot_table(index="well_name", columns="month", values=RATES[ph], aggfunc="mean").reindex(wells) for ph in PH}
    daily = V.pivot_table(index="well_name", columns="date", values=RATES["oil"], aggfunc="sum").reindex(wells)
    return ok({"well": well, "months": rows,
               "matrix": {"wells": wells, "months": list(piv["oil"].columns), **{ph: piv[ph].round(1).values for ph in PH}},
               "surface": {"wells": wells, "dates": [d.strftime("%Y-%m-%d") for d in daily.columns], "oil": daily.round(1).values},
               "cumulative": {"oil_stb": float(F[RATES["oil"]].sum()), "water_stb": float(F[RATES["water"]].sum()),
                              "gas_mscf": float(F[RATES["gas"]].sum()), "days": int(F.date.nunique())}})


PROOF_PATH = BASE / "static" / "model_proof.json"   # built offline by experiments/v6_final_audit/model_proof.py
Y_CRIT = 0.55                                       # P_ds / WHP below this: critical (choked) flow, P_ds has no effect


def _proof() -> dict:
    return json.loads(PROOF_PATH.read_text()) if PROOF_PATH.exists() else {}


@app.get("/api/model-proof")
def model_proof():
    """Pure physics vs pure ML vs hybrid on the frozen validation protocol (pre-computed aggregates)."""
    d = _proof()
    return ok({"available": bool(d), **d})


def _regime(pds: float, whp: float) -> dict:
    y = pds / whp if whp > 0 else float("nan")
    return {"pds_over_whp": y, "regime": "critical" if y <= Y_CRIT else "sub-critical", "y_critical": Y_CRIT}


@app.get("/api/what-if")
def what_if(sid: str, well: str, choke: float | None = None, whp: float | None = None, pds: float | None = None,
            gaslift: float | None = None):
    """Rates from the deployed model for edited choke / WHP / P_ds / gas-lift, starting from the well's last flowing day."""
    s = session(sid)
    P = s["R"]["P"].assign(date=lambda d: pd.to_datetime(d.date))
    g = P[(P.well_name == well) & (P.uptime >= 0.99)].sort_values("date")
    if g.empty:
        raise HTTPException(404, f"Well {well} has no full-uptime day in the loaded data.")
    m = model()
    if well not in m.a.M["Cw"]:
        raise HTTPException(422, f"Well {well} has no calibration in the deployed model (no separator test): what-if is not available.")
    b = g.iloc[-1]
    base = {"choke": float(b.choke_opening_pct), "whp": float(b.whp_psia), "pds": float(b.whp_downstream_psia),
            "gaslift": float(b.gas_lift_rate_mscfd), "pbh": float(b.dh_pressure_psia), "date": b.date.strftime("%Y-%m-%d")}
    new = {"choke": base["choke"] if choke is None else float(np.clip(choke, 1, 100)),
           "whp": base["whp"] if whp is None else float(max(whp, 20)), "pds": base["pds"] if pds is None else float(max(pds, 1)),
           "gaslift": base["gaslift"] if gaslift is None else float(max(gaslift, 1))}
    if new["pds"] >= new["whp"]:
        raise HTTPException(422, "Downstream pressure must be below wellhead pressure: no flow through the choke otherwise.")

    def rates(v):
        D = pd.DataFrame([{"well_name": well, "date": b.date, "uptime": 1.0, "whp_psia": v["whp"], "whp_downstream_psia": v["pds"],
                           "dh_pressure_psia": base["pbh"], "gas_lift_rate_mscfd": v["gaslift"], "choke_opening_pct": v["choke"]}])
        r = m.predict_daily(D).iloc[0]
        return {ph: float(r[RATES[ph]]) for ph in PH}
    r0, r1 = rates(base), rates(new)
    tr = s["R"].get("Tq")
    lim = {}
    if tr is not None and len(tr):
        tw = tr[tr.well_name == well]
        for k, c in (("choke", "choke_opening_pct"), ("whp", "whp_psia"), ("pds", "whp_downstream_psia"), ("gaslift", "gas_lift_rate_mscfd")):
            if c in tw and len(tw):
                lim[k] = [float(tw[c].min()), float(tw[c].max())]
    outside = [k for k, (lo, hi) in lim.items() if not (lo * 0.9 <= new[k] <= hi * 1.1)]
    return ok({"well": well, "base": base, "inputs": new, "base_rates": r0, "rates": r1,
               "delta_pct": {ph: (r1[ph] / r0[ph] - 1) * 100 if r0[ph] > 0 else None for ph in PH},
               "regime": _regime(new["pds"], new["whp"]), "base_regime": _regime(base["pds"], base["whp"]),
               "tested_range": lim, "outside_tested_range": outside,
               "note": "Bottom-hole pressure is held at the base day value; the model is calibrated on separator tests, so inputs far "
                       "outside the tested range are extrapolation."})


COPILOT_Q = {"hybrid": ("Why is the hybrid model better than pure physics and pure ML?", "Hibrid model niyə təmiz fizikadan və təmiz ML-dən yaxşıdır?"),
             "overfit": ("How do you know the model is not overfitted?", "Modelin overfit olmadığını haradan bilirsiniz?"),
             "stim": ("Which well needs stimulation first?", "Hansı quyu birinci stimulyasiya olunmalıdır?"),
             "missing": ("How is missing sensor data handled?", "Çatışmayan sensor datası necə emal olunur?"),
             "gas": ("Why is the gas error higher than the oil error?", "Qaz xətası niyə neft xətasından böyükdür?"),
             "limits": ("When should the model not be trusted?", "Modelə nə vaxt etibar etmək olmaz?")}


@app.get("/api/copilot")
def copilot(q: str = "", sid: str = "", lang: str = "en"):
    """Deterministic engineering answers assembled from computed numbers only (no language model, works offline)."""
    az = lang == "az"
    T_ = lambda en, a: a if az else en
    if q not in COPILOT_Q:
        return ok({"questions": {k: T_(*v) for k, v in COPILOT_Q.items()}})
    d = _proof()
    M_ = {m["key"]: m for m in d.get("models", [])}
    f3 = lambda x, n=2: " / ".join(f"{x[p]:.{n}f}" for p in PH)
    owg = T_("oil / water / gas", "neft / su / qaz")
    PHN = {"oil": T_("oil", "neft"), "water": T_("water", "su"), "gas": T_("gas", "qaz")}
    lines, src = [], T_("validation summary (frozen forward CV)", "validasiya xülasəsi (dondurulmuş irəli CV)")
    out = lambda: ok({"q": q, "question": T_(*COPILOT_Q[q]), "lines": lines, "source": src})
    if q in ("hybrid", "overfit", "gas") and not M_:
        lines = [T_("Validation summary file is not available in this build.", "Validasiya xülasəsi faylı bu versiyada yoxdur.")]
        return out()
    if q == "hybrid":
        h, ph_ = M_["hybrid"], M_["physics"]
        lines = [T_(f"Forward-CV MAPE {owg}: hybrid {f3(h['oos_mape'])} %, pure physics {f3(ph_['oos_mape'])} %.",
                    f"İrəli CV MAPE {owg}: hibrid {f3(h['oos_mape'])} %, təmiz fizika {f3(ph_['oos_mape'])} %."),
                 T_(f"R² {owg}: hybrid {f3(h['oos_r2'], 3)}, pure physics {f3(ph_['oos_r2'], 3)}.",
                    f"R² {owg}: hibrid {f3(h['oos_r2'], 3)}, təmiz fizika {f3(ph_['oos_r2'], 3)}.")]
        for p in PH:
            v = ph_["vs_hybrid"][p]
            sig = v["ci_lo"] > 0 or v["ci_hi"] < 0
            lines.append(T_(f"{PHN[p]}: pure physics is {v['delta_pp']:+.2f} pp vs the hybrid (95 % CI {v['ci_lo']:+.2f} to {v['ci_hi']:+.2f}); "
                            + ("significant." if sig else "not statistically significant."),
                            f"{PHN[p]}: təmiz fizika hibriddən {v['delta_pp']:+.2f} f.b. fərqlənir (95 % etibarlılıq intervalı {v['ci_lo']:+.2f} … {v['ci_hi']:+.2f}); "
                            + ("fərq əhəmiyyətlidir." if sig else "fərq statistik əhəmiyyətli deyil.")))
        for k, nm in (("ml_ridge", T_("Pure ML (Ridge on raw sensors)", "Təmiz ML (xam sensorlarda Ridge)")), ("ml_gbdt", T_("Pure ML (boosted trees)", "Təmiz ML (boosting ağacları)"))):
            if k in M_:
                e = M_[k]["sanity"]["choke_elasticity_min"]
                lines.append(T_(f"{nm}: MAPE {f3(M_[k]['oos_mape'])} %, train-to-OOS gap {M_[k]['gap_pp']:+.2f} pp (hybrid {h['gap_pp']:+.2f} pp); "
                                f"minimum choke elasticity {e:+.2f}" + (" (unphysical: opening the choke lowers the rate)." if e <= 0 else "."),
                                f"{nm}: MAPE {f3(M_[k]['oos_mape'])} %, təlim–OOS fərqi {M_[k]['gap_pp']:+.2f} f.b. (hibrid {h['gap_pp']:+.2f} f.b.); "
                                f"minimum ştuser elastikliyi {e:+.2f}" + (" (qeyri-fiziki: ştuser açılanda debit azalır)." if e <= 0 else ".")))
    elif q == "overfit":
        h = M_["hybrid"]
        cov = " / ".join(f"{d['coverage_p10_p90_pct'].get(p, float('nan')):.1f}" for p in PH)
        lines = [T_(f"Train MAPE {f3(h['train_mape'])} % vs forward out-of-sample {f3(h['oos_mape'])} %: gap {h['gap_pp']:+.2f} pp.",
                    f"Təlim MAPE {f3(h['train_mape'])} %, irəli nümunədən kənar {f3(h['oos_mape'])} %: fərq {h['gap_pp']:+.2f} f.b."),
                 T_(f"Month-blocked CV mean MAPE {h['month_blocked_mean']:.2f} % (every calendar month held out in turn).",
                    f"Ay-bloklu CV orta MAPE {h['month_blocked_mean']:.2f} % (hər təqvim ayı növbə ilə kənarda saxlanır)."),
                 T_(f"Protocol: {d['protocol']['forward_cv']}; hyper-parameters are chosen inside each training fold only.",
                    "Protokol: 4 kəsim, 90 günlük üfüq, n = 109; hiperparametrlər yalnız hər təlim qatının daxilində seçilir."),
                 T_(f"P10–P90 coverage {owg}: {cov} % (nominal 80 %).", f"P10–P90 əhatəsi {owg}: {cov} % (nominal 80 %).")]
    elif q == "gas":
        h = M_["hybrid"]
        worst = ", ".join(f"{r['well']} {r['gas']:.1f} %" for r in sorted(h["per_well"], key=lambda r: -r["gas"])[:2])
        lines = [T_(f"Forward-CV MAPE: oil {h['oos_mape']['oil']:.2f} %, gas {h['oos_mape']['gas']:.2f} %.",
                    f"İrəli CV MAPE: neft {h['oos_mape']['oil']:.2f} %, qaz {h['oos_mape']['gas']:.2f} %."),
                 T_("Gas = oil rate × GOR, so the gas error contains the oil error plus the GOR-trend error between tests.",
                    "Qaz = neft debiti × GOR; ona görə qaz xətasına neft xətası və testlər arasındakı GOR trendinin xətası daxildir."),
                 T_(f"Largest gas errors: {worst}.", f"Ən böyük qaz xətaları: {worst}.")]
    elif q == "missing":
        lines = [T_(f"{m['sensor']}: missing on {m['prod_missing_pct']:.2f} % of production days, {m['test_missing_pct']:.0f} % of test days.",
                    f"{m['sensor']}: istehsal günlərinin {m['prod_missing_pct']:.2f} %-də, test günlərinin {m['test_missing_pct']:.0f} %-də yoxdur.") for m in d.get("missing", [])]
        lines += [T_(f"{d.get('imputed_production_rows', 0)} of {d.get('production_rows', 0)} production well-days had a sensor gap: the value is carried "
                     "forward (then backward) within the same well and the row is flagged 'imputed'; a full shut-in day is exactly 0.",
                     f"{d.get('production_rows', 0)} istehsal quyu-günündən {d.get('imputed_production_rows', 0)}-də sensor boşluğu var: dəyər eyni quyunun "
                     "əvvəlki (sonra növbəti) günündən götürülür və sətir 'imputed' kimi işarələnir; tam bağlanma günündə debit dəqiq 0-dır."),
                  T_("Sensors that exist only on test days (annulus pressure, downhole temperature) are not model inputs.",
                     "Yalnız test günlərində olan sensorlar (annulus təzyiqi, quyudibi temperaturu) modelin girişi deyil.")]
        src = T_("missing-data audit", "çatışmayan data auditi")
    elif q == "limits":
        lines = [T_("No separator test for the well: rates use field-median calibration and are flagged uncalibrated.",
                    "Quyunun separator testi yoxdursa: debit yataq medianı ilə kalibrlənir və 'kalibrlənməyib' kimi işarələnir."),
                 T_("More than 180 days after the last test: water-cut and GOR trends are frozen, not extrapolated.",
                    "Son testdən 180 gündən çox keçibsə: su payı və GOR trendləri dondurulur, ekstrapolyasiya edilmir."),
                 T_("Inputs far outside the tested choke / pressure range are extrapolation (the what-if panel flags this).",
                    "Sınaqdan keçmiş ştuser / təzyiq diapazonundan çox kənar girişlər ekstrapolyasiyadır (what-if paneli bunu göstərir).")]
        lo = json.loads(CARD_PATH.read_text()).get("vfm", {}).get("leave_one_well_out", {}) if CARD_PATH.exists() else {}
        if lo:
            v = " / ".join(f"{lo[p]['MAPE_%']:.1f}" for p in PH)
            lines.append(T_(f"Unseen-well (leave-one-well-out) MAPE {owg}: {v} %.", f"Görülməmiş quyu (leave-one-well-out) MAPE {owg}: {v} %."))
        src = T_("model card", "model kartı")
    elif q == "stim":
        if not sid:
            lines = [T_("Load a dataset first.", "Əvvəlcə data yükləyin.")]
            return out()
        rows = [x for x in pta_rows(session(sid)["R"]) if x.get("skin") is not None and x["skin"] > 0 and x.get("dq_stb_d")]
        rows.sort(key=lambda x: -x["dq_stb_d"])
        lines = [T_(f"{x['well']}: skin {x['skin']:+.2f}, Δp_skin {x['dp_skin_psi']:.0f} psi, theoretical gain {x['dq_stb_d']:.0f} STB/d if skin is removed.",
                    f"{x['well']}: skin {x['skin']:+.2f}, Δp_skin {x['dp_skin_psi']:.0f} psi, skin aradan qaldırılsa nəzəri artım {x['dq_stb_d']:.0f} STB/gün.")
                 for x in rows] or [T_("No build-up with positive skin in the loaded data.", "Yüklənmiş datada müsbət skinli bərpa yoxdur.")]
        if rows:
            lines.append(T_(f"First candidate: {rows[0]['well']}. The gain is an upper bound at constant drawdown, used for ranking.",
                            f"Birinci namizəd: {rows[0]['well']}. Artım sabit depressiyada yuxarı həddir və sıralama üçün istifadə olunur."))
        src = T_("PTA results of the loaded dataset", "yüklənmiş datanın PTA nəticələri")
    return out()


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
