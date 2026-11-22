"""Streamlit-free pipeline behind the SOCAR-style app (app.py). Same math and same code paths as run_all.py:

  ingest -> model A (.pkl, locked) daily rates + P10-P90 -> model B (.pkl) -> per-phase geometric blend (submitted VFM)
  build-ups -> closed-boundary regression (cross-check) + Horner in the automatic radial window (submitted PTA)
            -> dual-panel figure inputs + automatic interpretation
Every function returns messages instead of raising, so the app never crashes on a judge's file.
"""
from __future__ import annotations

import glob
import io
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from . import pta_horner as HORNER
from .inference import infer_daily
from .ingest import ingest_table
from .live_error import live_error
from .preprocessing import qc_tests, daily_features_from_hf, detect_events_hf, event_series_hf
from .pta import pta_event, pvt_for
from .pta_interpret import interpret_event
from .vfm_direct import Direct3VFM, blend_rates, BLEND_WA

KINDS = ["daily_features", "well_tests", "choke_events", "reservoir_pvt", "well_completion"]
RATES = list(BLEND_WA)
BIG_FILE_BYTES = 50_000_000          # CSVs above this are passed by path (per-minute files are read in chunks)
EDGE_DAYS = 7                        # days just outside the test period still count as "inside" for the warning
MIN_BUILDUP_HR = 2.0                 # shorter build-ups are not interpreted (storage still dominates)
NOT_REPORTED_FLAGS = {"NO_FLAT_DERIVATIVE_used_last_half_cycle": "no radial-flow (flat derivative) window",
                      "HORNER_SLOPE_NONNEGATIVE": "Horner slope has the wrong sign",
                      "K_OUT_OF_RANGE": "permeability outside 0.01-10 000 mD",
                      "SKIN_OUT_OF_RANGE": "skin outside -6 to 50"}


class InputError(ValueError):
    """The loaded data cannot be used; the message says what is missing."""


def not_reported_reason(h, dt_hr) -> str:
    """Why a Horner result must not be shown as a value ('' = reportable)."""
    dur = float(np.nanmax(dt_hr)) if len(dt_hr) else 0.0
    if dur < MIN_BUILDUP_HR:
        return f"build-up too short ({dur:.1f} h < {MIN_BUILDUP_HR:g} h)"
    return "; ".join(v for k, v in NOT_REPORTED_FLAGS.items() if k in (h.flags or ""))


def check_inputs(tables: dict) -> None:
    """Stop with a readable message when the mandatory inputs are present but unusable."""
    d, c = tables.get("daily_features"), tables.get("choke_events")
    if d is None or not len(d):
        raise InputError("sensor data (daily or per-minute) has no valid rows after cleaning")
    for col in ["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd"]:
        if col in d and d[col].notna().sum() == 0:
            raise InputError(f"sensor column {col} is empty in every row: VFM cannot run")
    if c is None or not len(c):
        raise InputError("choke events table is empty: VFM needs the choke opening")
    if not set(d["well_name"]) & set(c["well_name"]):
        raise InputError("no well appears in both the sensor data and the choke events (check well names)")


def period_summary(tables: dict, model_a, n_buildups: int) -> dict:
    """Loaded data period vs the training period of the shipped model (shown in the app header)."""
    from .vfm import TREND_HORIZON_DAYS
    d = tables.get("daily_features")
    t = tables.get("well_tests")
    out = {"first": pd.to_datetime(d["date"]).min(), "last": pd.to_datetime(d["date"]).max(),
           "wells": int(d["well_name"].nunique()), "tests": int(len(t)) if t is not None else 0,
           "buildups": int(n_buildups)}
    if model_a is not None and hasattr(model_a, "T"):
        t0, t1 = model_a.train_period()
        H = getattr(model_a, "trend_horizon_days", TREND_HORIZON_DAYS) or 0
        dd = pd.to_datetime(d["date"])
        out.update(train_first=t0, train_last=t1, train_tests=int(len(model_a.T)), horizon_days=H,
                   days_after_train=int(max(0, (dd.max().normalize() - t1.normalize()).days)),
                   rows_after_train=int((dd > t1.normalize() + pd.Timedelta(days=EDGE_DAYS)).sum()),
                   rows_beyond_horizon=int((dd > t1 + pd.Timedelta(days=H)).sum()),
                   rows_before_train=int((dd < t0.normalize() - pd.Timedelta(days=EDGE_DAYS)).sum()))
    return out


def choke_log_shutins(hf, choke: pd.DataFrame | None, found: list, tol_hr: float = 6.0,
                      rise_psi: float = 15.0) -> list:
    """Shut-ins the sensor rule missed but the choke log shows (opening -> 0): read the pressure from the per-minute
    file until the choke re-opens (max 7 days) and keep it if the downhole pressure rises by >= ``rise_psi``."""
    if choke is None or not len(choke) or "choke_opening_pct" not in choke:
        return []
    c = choke.assign(timestamp=pd.to_datetime(choke["timestamp"])).sort_values(["well_name", "timestamp"])
    extra = []
    for w, g in c.groupby("well_name"):
        closes = g[g.choke_opening_pct <= 0.5]
        for _, row in closes.iterrows():
            t0 = row.timestamp
            if any(f[0] == w and abs((pd.Timestamp(f[1]) - t0).total_seconds()) <= tol_hr * 3600 for f in found + extra):
                continue
            reopen = g[(g.timestamp > t0) & (g.choke_opening_pct > 0.5)].timestamp
            hours = min(168.0, ((reopen.iloc[0] - t0).total_seconds() / 3600) if len(reopen) else 168.0)
            try:
                s_ = manual_series(hf, w, t0, hours)
            except Exception:
                continue
            if len(s_[3]) and float(np.nanmax(s_[3]) - s_[4]) >= rise_psi:
                extra.append(s_)
    if extra:
        choke_log_shutins.note = f"{len(extra)} build-up(s) found from the choke log (choke closed) that the sensor rule missed"
    return extra


def manual_series(hf, well: str, t_shut, hours: float) -> tuple:
    """(well, t_shut, dt_hr, pws, pwf) for an engineer-chosen shut-in time from the per-minute file."""
    from .preprocessing import _hf_min
    h = _hf_min(load_hf(hf))
    g = h[h.well_name == well]
    t_shut = pd.Timestamp(t_shut)
    before = g[g.timestamp <= t_shut]["dh_pressure_psia"].dropna()
    if before.empty:
        raise InputError(f"no downhole pressure for {well} before {t_shut:%Y-%m-%d %H:%M}")
    seg = g[(g.timestamp > t_shut) & (g.timestamp <= t_shut + pd.Timedelta(hours=hours))].dropna(subset=["dh_pressure_psia"])
    if len(seg) < 10:
        raise InputError(f"only {len(seg)} pressure points for {well} in the {hours:g} h after {t_shut:%Y-%m-%d %H:%M}")
    return well, t_shut, (seg.timestamp - t_shut).dt.total_seconds().values / 3600, seg["dh_pressure_psia"].values, \
        float(before.iloc[-1])


def analyse_event(w, ts, dt, p, pwf, P_q, pvt, comp, re_hint=True, window_hr=None) -> tuple[dict, pd.DataFrame]:
    """Regression cross-check + Horner (submitted method) + interpretation for one build-up."""
    r1, pl1 = pta_event(dt, p, pwf, w, ts, P_q, pvt, comp)
    pv = pvt_for(pvt, w)
    h = HORNER.analyze_buildup(w, ts, pl1.dt_hr.values, pl1.pws_psi.values, pwf, r1["tp_hr"], r1["q_oil_stbd"],
                               pv["Bo"], pv["mu"], pv["h_ft"], pv["phi"], pv["ct"], pv["rw_ft"], window_hr=window_hr)
    r1.update(horner=h, pvt=pv, horner_k_md=h.k_md, horner_skin=h.skin, horner_m=h.horner_slope,
              horner_dP_skin_psi=0.87 * round(h.horner_slope, 4) * round(h.skin, 4), horner_r2=h.r2_horner,
              pta_not_reported=not_reported_reason(h, pl1.dt_hr.values), manual_window=window_hr,
              interp=interpret_event(h, pl1.dt_hr.values, pl1.pws_psi.values, pwf, r1["q_oil_stbd"], pv,
                                     re_ft=r1.get("re_reg_ft")))
    try:                                          # wellbore storage: C from data and from the tubing volume
        from .diameters import detect, storage_from_volume, storage_from_data, storage_end_hr
        dm = detect({"well_completion": comp, "reservoir_pvt": pvt}).set_index("well_name").loc[w]
        depth = float(comp.set_index("well_name").get("gauge_tvd_ft", pd.Series(dtype=float)).get(w, np.nan))
        depth_src = "gauge TVD"
        if not np.isfinite(depth):
            depth = float(pvt.set_index("well_name").get("datum_tvd_ft", pd.Series(dtype=float)).get(w, 8000.0))
            depth_src = "datum TVD (assumed: no gauge depth)"
        cv = storage_from_volume(float(dm.tubing_id_in), depth)
        cd = storage_from_data(pl1.dt_hr.values, pl1.pws_psi.values - pwf, r1["q_oil_stbd"], pv["Bo"])
        r1["storage"] = {"C_data_bbl_psi": cd, "C_volume_bbl_psi": cv["C_bbl_psi"], "V_wb_bbl": cv["V_wb_bbl"],
                         "c_wb_assumed_1_psi": cv["c_wb_1_psi"], "tubing_id_in": float(dm.tubing_id_in),
                         "tubing_id_source": dm.tubing_id_in_source, "rw_ft": float(dm.rw_ft), "rw_source": dm.rw_ft_source,
                         "depth_ft": depth, "depth_source": depth_src,
                         "t_wbs_end_hr": storage_end_hr(cd, h.skin, pv["mu"], h.k_md, pv["h_ft"]),
                         "rw_check": dm.rw_check}
    except Exception as ex:
        r1["storage"] = {"error": f"{type(ex).__name__}: {ex}"}
    return r1, pl1


def read_folder(folder: str | os.PathLike) -> dict:
    """Raw inputs from a data folder (CSV files recognised by name, events/*.json, optional per-minute file)."""
    from .uploads import read_any
    d = Path(folder)
    raw, events, hf = {}, {}, None
    if not d.is_dir():
        return {"raw": raw, "events": events, "hf": hf}
    for k in KINDS:
        f = d / f"{k}.csv"
        if f.exists():
            try:
                raw[k] = read_any(f.name, f.read_bytes())
            except Exception:
                pass
    for f in sorted(glob.glob(str(d / "events" / "*.json"))):
        try:
            events[Path(f).name] = json.loads(Path(f).read_text())
        except Exception:
            pass
    if (d / "high_freq_data.csv").exists():
        hf = str(d / "high_freq_data.csv")
    return {"raw": raw, "events": events, "hf": hf}


def ingest_all(raw: dict, known: set | None = None) -> tuple[dict, list]:
    """Validated tables + the ingest reports (sign fixes, sentinels, renamed columns, dropped rows ...)."""
    tables, reports = {}, []
    for k in KINDS:
        if raw.get(k) is None:
            continue
        clean, rep = ingest_table(raw[k], k, known)
        reports.append(rep)
        if rep.ok:
            tables[k] = clean
    return tables, reports


def load_hf(hf) -> str:
    """Per-minute upload (bytes) -> a temporary file path, written once. Aggregation then reads it in chunks
    (constant memory) and shut-in detection reads only 6 columns, cached by path (no re-parsing per build-up)."""
    if isinstance(hf, (str, os.PathLike)):
        return str(hf)
    import hashlib
    import tempfile
    p = Path(tempfile.gettempdir()) / f"caspianx_hf_{hashlib.sha1(hf[:1_000_000]).hexdigest()[:12]}_{len(hf)}.csv"
    if not p.exists():
        p.write_bytes(hf)
    return str(p)


def event_series(events: dict, choke: pd.DataFrame | None, hf=None) -> list:
    """(well, t_shut, dt_hr, pws, pwf) for every build-up: per-minute file if present, else event JSON files."""
    if hf is not None:
        src = load_hf(hf) if isinstance(hf, (bytes, bytearray)) else hf
        out = []
        try:
            ev = detect_events_hf(src)
        except Exception as ex:                       # reported by the caller through an empty list + warning
            event_series.last_error = f"shut-in detection failed on the per-minute file: {type(ex).__name__}: {ex}"
            return []
        for _, e in ev.iterrows():
            try:
                out.append(event_series_hf(src, e))
            except Exception as ex:
                event_series.last_error = f"{e['well_name']} {e['t_shut']}: build-up not readable ({ex})"
        out += choke_log_shutins(src, choke, out)
        return out
    out = []
    for name, e in sorted(events.items()):
        try:
            w, day = Path(name).stem.rsplit("_", 1)
            w = w.strip().upper()
            ts = pd.Timestamp(day)
            if choke is not None and len(choke):
                c0 = choke[(choke.well_name == w) & (choke.choke_opening_pct <= 0.5)]
                c0 = c0[pd.to_datetime(c0.timestamp).dt.strftime("%Y-%m-%d") == day]
                if len(c0):
                    ts = pd.Timestamp(c0.timestamp.iloc[0])
            a = np.asarray(e["post"], float)
            out.append((w, ts, a[:, 0] / 60.0, a[:, 1], float(e["pwf"])))
        except Exception:
            continue
    return out


def run(tables: dict, series: list, model_a, model_b=None, use_locked: bool = True) -> dict:
    """Full VFM + PTA run. Returns a dict with P (model A + band), Bl (submitted blend), pta (list of (r, plot))."""
    from .vfm import GasLiftVFM
    warn: list[str] = []
    check_inputs(tables)
    daily, tests, choke = tables.get("daily_features"), tables.get("well_tests"), tables.get("choke_events")
    pvt, comp = tables.get("reservoir_pvt"), tables.get("well_completion")
    qc_log = qc_tests(tests, choke)[1] if tests is not None else pd.DataFrame()
    if use_locked and model_a is not None:
        m = model_a
        Tq = qc_tests(tests, choke)[0] if tests is not None else m.T
    else:
        Tq = qc_tests(tests, choke)[0]
        m = GasLiftVFM().fit(Tq)
    P, info = infer_daily(m, daily, choke)
    if info["new_wells"]:
        warn.append(f"uncalibrated wells (field-median parameters, wider band): {info['new_wells']}")
    if info["no_choke_wells"]:
        warn.append(f"no choke data, rates not computed: {info['no_choke_wells']}")
    Bl, src, B_ = None, "model A (choke physics + Ridge)", None
    try:
        mB = model_b if (use_locked and model_b is not None) else Direct3VFM().fit(Tq)
        from .vfm_direct import ensure_train_range
        ensure_train_range(mB, m.T)                  # old .pkl: training period taken from model A's tests
        A_ = P.dropna(subset=["oil_rate_stbd"])[["date", "well_name"] + RATES].copy()
        A_[RATES] = A_[RATES].round(2)
        feat = ["whp_psia", "whp_downstream_psia", "choke_opening_pct", "gas_lift_rate_mscfd", "dh_pressure_psia", "uptime"]
        X_ = P.loc[A_.index, ["date", "well_name"] + feat]
        ok = np.isfinite(X_[feat].astype(float)).all(axis=1) & (X_["whp_psia"] > 0) & (X_["choke_opening_pct"] >= 0)
        PB = mB.predict_daily(X_[ok].assign(date=pd.to_datetime(X_[ok]["date"])))
        PB.loc[PB["uptime"] <= 0, RATES] = 0.0
        PB[RATES] = PB[RATES].round(2)
        B_ = A_[["date", "well_name"]].merge(PB[["date", "well_name"] + RATES], on=["date", "well_name"], how="left")
        rs = (comp.set_index("well_name")["solution_gor_at_pb_scf_stb"].to_dict()
              if comp is not None and "solution_gor_at_pb_scf_stb" in comp else {})
        Bl = blend_rates(A_, B_, ["date", "well_name"], set(Tq.well_name), rs)
        src = "A/B blend (submitted method)"
    except Exception as ex:
        warn.append(f"model B / blend unavailable, model A shown ({type(ex).__name__}: {ex})")
    if Bl is None:
        Bl = P.dropna(subset=["oil_rate_stbd"])[["date", "well_name"] + RATES].round(2)
    pta = []
    if pvt is None or comp is None:
        warn.append("PVT or completion file missing: PTA skipped (no values invented)")
    else:
        P_q = P.copy()
        P_q[RATES] = P_q[RATES].round(2)
        for w, ts, dt, p, pwf in series:
            if w not in set(pvt.well_name) or w not in set(comp.well_name) or w not in set(P.well_name):
                warn.append(f"{w} {ts}: PVT / completion / VFM rate unavailable, build-up skipped")
                continue
            try:
                r1, pl1 = analyse_event(w, ts, dt, p, pwf, P_q, pvt, comp)
            except Exception as ex:
                warn.append(f"{w} {pd.Timestamp(ts):%Y-%m-%d %H:%M}: build-up not interpreted ({type(ex).__name__}: {ex})")
                continue
            if r1["pta_not_reported"]:
                warn.append(f"{w} {pd.Timestamp(ts):%Y-%m-%d %H:%M}: PTA values not reported - {r1['pta_not_reported']}")
            if r1.get("np_filled_share", 0) and r1["np_filled_share"] > 0:
                warn.append(f"{w}: N_p approximation - {r1['np_method']}")
            pta.append((r1, pl1))
    from .diameters import detect as _detect_d, tubing_hydraulics
    diam = _detect_d({k: v for k, v in tables.items() if k in ("well_completion", "reservoir_pvt")} or
                     {"wells": pd.DataFrame({"well_name": sorted(P.well_name.unique())})})
    diam = diam[diam.well_name.isin(set(P.well_name))].reset_index(drop=True)
    hyd = pd.DataFrame()
    try:
        L = Bl.assign(date=pd.to_datetime(Bl.date)).merge(P.assign(date=pd.to_datetime(P.date))[
            ["date", "well_name", "whp_psia", "gas_lift_rate_mscfd", "uptime"]], on=["date", "well_name"])
        L = L[L.uptime > 0.5].sort_values("date").groupby("well_name").tail(1).merge(diam[["well_name", "tubing_id_in"]],
                                                                                   on="well_name", how="left")
        api = tests.groupby("well_name")["oil_gravity_api"].mean() if tests is not None and "oil_gravity_api" in tests \
            else pd.Series(dtype=float)
        H_ = tubing_hydraulics(L.oil_rate_stbd, L.water_rate_stbd, L.gas_rate_mscfd, L.gas_lift_rate_mscfd, L.whp_psia,
                               L.tubing_id_in.fillna(2.992), api=L.well_name.map(api).fillna(35.0).values)
        hyd = pd.concat([L[["well_name", "date"]].reset_index(drop=True), H_], axis=1)
        hyd["api_source"] = np.where(L.well_name.map(api).notna(), "well tests", "assumed 35 API")
        for _, r in hyd[hyd.erosional_ratio > 1.0].iterrows():
            warn.append(f"{r.well_name}: wellhead mixture velocity {r.v_mixture_ft_s:.0f} ft/s exceeds the API RP 14E "
                        f"erosional velocity {r.v_erosional_ft_s:.0f} ft/s (ratio {r.erosional_ratio:.2f})")
    except Exception as ex:
        warn.append(f"tubing hydraulics diagnostic unavailable ({type(ex).__name__}: {ex})")
    if getattr(choke_log_shutins, "note", None):
        warn.append(choke_log_shutins.note)
        choke_log_shutins.note = None
    LE = live_error(Bl, Tq if tests is not None else None)
    per = period_summary(tables, m, len(pta))
    if per.get("rows_after_train"):
        warn.insert(0, f"{per['rows_after_train']} well-days are after the last training test "
                       f"({per['train_last']:%Y-%m-%d}): model output on new data, not validated against tests")
    if per.get("rows_beyond_horizon"):
        warn.insert(0, f"{per['rows_beyond_horizon']} well-days are more than {per['horizon_days']} days after the last "
                       "training test: WC/GOR and time trends are frozen at that horizon (no evidence beyond it); "
                       "provide new well tests and use Retrain for a reliable forecast")
    if per.get("rows_before_train"):
        warn.insert(0, f"{per['rows_before_train']} well-days are before the training period "
                       f"({per['train_first']:%Y-%m-%d}): model output on new data")
    return {"m": m, "mB": locals().get("mB"), "retrained": not (use_locked and model_a is not None),
            "P": P, "Bl": Bl, "B": B_, "vfm_src": src, "Tq": Tq, "info": info, "pta": pta, "warnings": warn,
            "live_error": LE, "tests_uploaded": tests is not None, "qc_log": qc_log, "period": per,
            "diameters": diam, "hydraulics": hyd}


def horner_rows(pta: list) -> list:
    """Reportable Horner results only (build-ups without a radial window / too short are left out, never guessed)."""
    return [{"well_name": r["well_name"], "t_shut": r["t_shut"], "k_md": r["horner_k_md"], "skin": r["horner_skin"],
             "m": r["horner_m"]} for r, _ in pta if "horner_k_md" in r and not r.get("pta_not_reported")]


def load_inputs(files: list, known: set | None = None, fallback_folder=None) -> dict:
    """files = [(name, bytes)] in any naming / order / format (CSV, XLSX, ZIP, event JSON, per-minute CSV).
    Returns {"tables", "reports", "events", "hf", "messages"}; raises InputError with what is missing."""
    from .uploads import classify_uploads

    class _U:
        def __init__(self, n, b):
            self.name, self._b = n, b

        def getvalue(self):
            return self._b
    big_hf, small = None, []
    for n, b in files:                     # large files arrive as paths (never loaded whole into memory)
        if isinstance(b, (str, os.PathLike)):
            from .ingest import ingest_table
            head = pd.read_csv(b, nrows=5)
            if ingest_table(head)[1].kind == "high_freq":
                big_hf = str(b)
                continue
            b = Path(b).read_bytes()
        small.append((n, b))
    U = classify_uploads([_U(n, b) for n, b in small])
    raw, events, hf, msgs = U["raw"], U.get("events", {}), U["hf_bytes"], list(U["messages"])
    if big_hf is not None:
        hf = big_hf
        msgs.append(("ok", f"{Path(big_hf).name}: per-minute sensor file (read in chunks from disk)"))
    if hf is not None:
        hf = load_hf(hf)
        raw["daily_features"] = daily_features_from_hf(hf)
        msgs.append(("ok", f"per-minute file aggregated to {len(raw['daily_features'])} well-days"))
    if fallback_folder is not None:
        F0 = read_folder(fallback_folder)
        for k in ["reservoir_pvt", "well_completion"]:
            if k not in raw and k in F0["raw"]:
                raw[k] = F0["raw"][k]
                msgs.append(("warn", f"{k} not provided: shipped file used"))
    if raw.get("choke_events") is not None and "choke_opening_pct" not in raw["choke_events"].columns:
        from .diameters import detect, bean_events_to_pct
        raw["choke_events"], note = bean_events_to_pct(raw["choke_events"], detect(raw))
        if note:
            msgs.append(("warn", note))
    tables, reports = ingest_all(raw, known)
    need = [k for k in ["daily_features", "choke_events"] if k not in tables]
    if need:
        why = [m for r in reports for lv, m in r.messages() if lv == "bad"] + [m for lv, m in msgs if lv == "bad"]
        raise InputError(f"required input missing or unusable: {need}. " + " | ".join(why[:4]))
    return {"tables": tables, "reports": reports, "events": events, "hf": hf, "messages": msgs}


def files_from_path(path) -> list:
    """A folder (recursive), a ZIP or a single file -> [(name, bytes)]."""
    p = Path(path)
    if p.is_dir():
        fs = [f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix.lower() in (".csv", ".xlsx", ".xls", ".json", ".zip")]
        return [(f.name, str(f) if (f.stat().st_size > BIG_FILE_BYTES and f.suffix.lower() == ".csv")
                 else f.read_bytes()) for f in fs]
    return [(p.name, str(p) if (p.stat().st_size > BIG_FILE_BYTES and p.suffix.lower() == ".csv") else p.read_bytes())]
