"""
Physics-aware anomaly detection on well sensor data (adapter module: the protected VFM / PTA core is not touched).

Works on any regular resolution (per-minute, hourly or the daily aggregates in data/socar/daily_features.csv);
one row = one well-period. Three layers, each one explainable to an engineer:

1. Hard physics rules (always on, no training): sign errors, impossible pressure order
   (downstream > WHP while flowing, gauge <= WHP), missing sensors, stuck sensors (a run of identical values
   whose probability under the well's own repeat rate is < 1e-4).
2. Causal robust baseline per well: every feature is compared with the median / MAD of the previous
   ``window`` flowing periods only (no look-ahead, so a flag is raised the day it happens). Features are physical:
   WHP, downstream pressure, gauge pressure, lift gas, choke dP (WHP - downstream) and the tubing mixture gradient
   (gauge - WHP) / gauge TVD. The gradient residual after removing the lift-gas effect (more gas = lighter column)
   is the water-cut indicator: water makes the column heavier at the same lift gas.
3. Isolation Forest on the robust z-scores (pooled across wells, so it learns which *combinations* are rare).

Every flag is then checked against operational context: shut-in / restart days, logger gaps and choke changes
explain a process change, so these become "explained" events (shown, not alerted). Only unexplained flags are
alerts. Types: sensor_fault, water_cut_jump, pressure_anomaly, lift_gas_anomaly.

``validate()`` measures recall on injected faults (spike, sign flip, stuck, drift, water-cut step) and the
false-alarm rate on untouched days, plus sensitivity to real choke changes. ``validate_tests()`` runs the same
rules on the well-test table and must find the known QC issues (sign flips incl. GL-09, GL-06 unit error).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

SENSORS = ["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd"]
FEATURES = ["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd", "dp_choke_psi",
            "grad_psi_ft", "grad_resid"]
LABEL_AZ = {"sensor_fault": "Sensor nasazlığı", "water_cut_jump": "Su payı sıçrayışı",
            "pressure_anomaly": "Təzyiq anomaliyası", "lift_gas_anomaly": "Qazlift anomaliyası",
            "shut_in": "Dayanma", "restart": "İşə salınma", "choke_change": "Ştuser dəyişikliyi",
            "logger_gap": "Logger boşluğu", "well_test": "Quyu sınağı (test separatoru)", "gl_setpoint": "Qazlift tənzimləməsi (reaksiya normal)",
            "field_wide": "Mədən miqyaslı hadisə (kompressor / qaz kollektoru)"}


@dataclass
class AnomalyConfig:
    window: int = 21          # causal baseline length, flowing periods
    min_hist: int = 7         # periods of history before z-scores are trusted
    z_flag: float = 4.0       # robust z for a single-feature flag
    if_quantile: float = 0.99  # Isolation Forest score threshold (quantile of clean-history scores)
    z_if: float = 3.0         # minimum max|z| that must accompany an IF flag
    stuck_p: float = 1e-4     # probability threshold for a run of identical values
    choke_window_days: int = 1
    seed: int = 7


# ---------------------------------------------------------------------------------------------- helpers
def _choke_days(choke: pd.DataFrame | None) -> set:
    if choke is None or not len(choke):
        return set()
    c = choke.copy()
    c["timestamp"] = pd.to_datetime(c["timestamp"], errors="coerce")
    c = c.dropna(subset=["timestamp"]).sort_values(["well_name", "timestamp"])
    c["prev"] = c.groupby("well_name")["choke_opening_pct"].shift()
    c = c[c.prev.notna() & ((c.choke_opening_pct - c.prev).abs() > 0.05)]
    return {(w, d) for w, d in zip(c.well_name, c.timestamp.dt.normalize())}


def _robust_z(s: pd.Series, window: int, min_hist: int) -> pd.Series:
    """Causal robust z: (x - median(prev window)) / (1.4826 MAD(prev window)), floored scale."""
    prev = s.shift(1)
    med = prev.rolling(window, min_periods=min_hist).median()
    mad = (prev - med).abs().rolling(window, min_periods=min_hist).median()
    floor = 0.005 * s.abs().median() + 1e-6   # 0.5 % of level: sensor resolution floor
    scale = np.maximum(1.4826 * mad, floor)
    return (s - med) / scale


def _stuck_runs(s: pd.Series, p_thr: float) -> pd.Series:
    """True on periods inside a run of identical values that is improbable for this sensor."""
    v = s.to_numpy()
    same = np.r_[False, (v[1:] == v[:-1]) & np.isfinite(v[1:])]
    p = max(same.mean(), 1e-3)
    out = np.zeros(len(v), bool)
    i = 0
    while i < len(v):
        if same[i]:
            j = i
            while j < len(v) and same[j]:
                j += 1
            L = j - i + 1          # run length in values
            if p ** (L - 1) < p_thr:
                out[i - 1:j] = True
            i = j
        else:
            i += 1
    return pd.Series(out, index=s.index)


# ---------------------------------------------------------------------------------------------- features
def _test_days(tests: pd.DataFrame | None) -> set:
    if tests is None or not len(tests) or "test_start" not in tests:
        return set()
    a = pd.to_datetime(tests["test_start"], errors="coerce").dt.normalize()
    b = pd.to_datetime(tests.get("test_end", tests["test_start"]), errors="coerce").dt.normalize()
    out = set()
    for w, x, y in zip(tests.well_name, a, b):
        if pd.notna(x):
            for d in pd.date_range(x, y if pd.notna(y) else x):
                out.add((w, d))
    return out


def build_features(daily: pd.DataFrame, completion: pd.DataFrame | None = None,
                   choke: pd.DataFrame | None = None, cfg: AnomalyConfig | None = None,
                   tests: pd.DataFrame | None = None) -> pd.DataFrame:
    cfg = cfg or AnomalyConfig()
    D = daily.copy()
    D["date"] = pd.to_datetime(D["date"])
    D = D.sort_values(["well_name", "date"]).reset_index(drop=True)
    for c in SENSORS:
        if c not in D:
            D[c] = np.nan
        D[c] = pd.to_numeric(D[c], errors="coerce")
    if "uptime" not in D:
        D["uptime"] = 1.0
    if "logger_gap_min" not in D:
        D["logger_gap_min"] = 0
    tvd = {}
    if completion is not None and "gauge_tvd_ft" in completion:
        tvd = dict(zip(completion.well_name, completion.gauge_tvd_ft))
    D["gauge_tvd_ft"] = D.well_name.map(tvd).fillna(8000.0)
    D["dp_choke_psi"] = D.whp_psia - D.whp_downstream_psia
    D["grad_psi_ft"] = (D.dh_pressure_psia - D.whp_psia) / D.gauge_tvd_ft

    # operational context
    cd = _choke_days(choke)
    D["ctx_choke"] = False
    if cd:
        k = list(zip(D.well_name, D.date.dt.normalize()))
        for lag in range(-cfg.choke_window_days, cfg.choke_window_days + 1):
            D["ctx_choke"] |= pd.Series([(w, d - pd.Timedelta(days=lag)) in cd for w, d in k], index=D.index)
    D["ctx_shut"] = D.uptime < 0.95
    prev_shut = D.groupby("well_name")["ctx_shut"].transform(lambda s: s.shift(1, fill_value=False).rolling(3, min_periods=1).max().astype(bool))
    D["ctx_restart"] = (~D.ctx_shut) & prev_shut
    D["ctx_partial"] = (D.uptime >= 0.95) & (D.uptime < 0.999)
    D["ctx_gap"] = D.logger_gap_min > 60
    # well-test days: the well is routed to the test separator, downstream pressure drops (median z = -8 on
    # SOCAR data). A known operation, not an anomaly.
    td = _test_days(tests)
    D["ctx_test"] = pd.Series([(w, d) in td for w, d in zip(D.well_name, D.date.dt.normalize())], index=D.index)
    D["flowing"] = ~D.ctx_shut

    # operating segments: a new segment starts at every choke change and after every shut-in. The new
    # operating point is legitimate, so the baseline restarts and the first ``min_hist`` periods are "settling"
    # (only the physics rules apply there). Without this, every choke change would look anomalous for weeks.
    # lift-gas set-point changes are not logged, so they are recognised from the data: a step > 5 % that holds for
    # 3 periods. Physics check on the response: more gas must lighten the column (gauge down) and/or lift WHP.
    # A step with no response is kept as a "lift gas ineffective" alert (valve / bypass / beyond optimum).
    D["ctx_glstep"] = False
    D["gl_ineffective"] = False
    D["gl_step_txt"] = ""
    for w, g in D[~D.ctx_shut].groupby("well_name"):
        gl = g.gas_lift_rate_mscfd
        pre = gl.shift(1).rolling(7, min_periods=3).median()
        post = gl[::-1].rolling(3, min_periods=3).median()[::-1]
        rel = (post - pre) / pre.abs().clip(lower=1)
        step = (rel.abs() > 0.03) & ((gl - pre).abs() > 15) & ((post - pre).abs() > 15)
        step &= ~step.shift(1, fill_value=False)
        for ix in g.index[step.fillna(False).values]:
            i = g.index.get_loc(ix)
            a, b = g.iloc[max(0, i - 7):i], g.iloc[i:i + 3]
            d_whp = b.whp_psia.median() - a.whp_psia.median()
            d_dh = b.dh_pressure_psia.median() - a.dh_pressure_psia.median()
            sgn = np.sign(rel.loc[ix])
            ok = (sgn * d_whp > 1.0) or (-sgn * d_dh > 2.0)
            txt = (f"qazlift {a.gas_lift_rate_mscfd.median():.0f} -> {b.gas_lift_rate_mscfd.median():.0f} Mscf/d; "
                   f"WHP {d_whp:+.1f} psi, quyudibi {d_dh:+.1f} psi")
            win = g.index[i:i + 3]
            D.loc[win, "ctx_glstep"] = True
            D.loc[ix, "gl_step_txt"] = txt
            if not ok:
                D.loc[ix, "gl_ineffective"] = True
    ev = {(w, d) for w, d in cd} if cd else set()
    brk = pd.Series([(w, d) in ev for w, d in zip(D.well_name, D.date.dt.normalize())], index=D.index) | D.ctx_shut
    brk |= D.ctx_glstep & ~D.ctx_glstep.groupby(D.well_name).shift(1, fill_value=False)
    D["segment"] = brk.astype(int).groupby(D.well_name).cumsum()
    for c in FEATURES:
        D["z_" + c] = np.nan
    D["grad_resid"] = np.nan
    D["settling"] = False
    for (w, sg), g in D[D.flowing].groupby(["well_name", "segment"]):
        idx = g.index
        D.loc[idx[:cfg.min_hist], "settling"] = True
        x = g.gas_lift_rate_mscfd
        y = g.grad_psi_ft
        # causal regression of the mixture gradient on lift gas inside the segment
        resid = pd.Series(np.nan, index=idx)
        for i, ix in enumerate(idx):
            lo = max(0, i - 90)
            if i - lo < cfg.min_hist:
                continue
            xs, ys = x.iloc[lo:i].to_numpy(), y.iloc[lo:i].to_numpy()
            ok = np.isfinite(xs) & np.isfinite(ys)
            if ok.sum() < cfg.min_hist:
                continue
            if np.ptp(xs[ok]) > 1e-6:
                bb = np.polyfit(xs[ok], ys[ok], 1)
                bb[0] = min(bb[0], 0.0)          # physics: lift gas cannot make the column heavier
                bb[1] = np.median(ys[ok] - bb[0] * xs[ok])
            else:
                bb = np.array([0.0, np.median(ys[ok])])
            resid.loc[ix] = y.loc[ix] - (bb[0] * x.loc[ix] + bb[1])
        D.loc[idx, "grad_resid"] = resid
        for c in FEATURES:
            if c == "grad_resid":
                s_ = resid
                mad = s_.shift(1).abs().rolling(cfg.window, min_periods=cfg.min_hist).median()
                # floor 0.0008 psi/ft ~ 2 water-cut points: below that the daily gauge resolution dominates
                D.loc[idx, "z_" + c] = s_ / np.maximum(1.4826 * mad, 0.0008)
            else:
                # raw sensors are trusted after 3 periods of the new segment (resolution floor guards the scale)
                D.loc[idx, "z_" + c] = _robust_z(g[c], cfg.window, 3)
    for w, g in D[D.flowing].groupby("well_name"):
        for c in SENSORS:
            # lift gas is a controlled set-point with integer daily averages: repeats are normal, so much stricter
            D.loc[g.index, "stuck_" + c] = _stuck_runs(g[c], cfg.stuck_p if c != "gas_lift_rate_mscfd" else cfg.stuck_p ** 2).values
    for c in SENSORS:
        D["stuck_" + c] = D.get("stuck_" + c, False)
        D["stuck_" + c] = D["stuck_" + c].fillna(False).astype(bool)
    return D


# ---------------------------------------------------------------------------------------------- detector
def _rules(D: pd.DataFrame) -> pd.DataFrame:
    f = D.flowing
    r = pd.DataFrame(index=D.index)
    r["neg"] = f & (D[SENSORS] < 0).any(axis=1)
    r["missing"] = f & D[SENSORS].isna().any(axis=1)
    r["order_ds"] = f & (D.whp_downstream_psia > D.whp_psia + 1)
    r["order_dh"] = f & (D.dh_pressure_psia <= D.whp_psia)
    r["stuck"] = f & D[[c for c in D.columns if c.startswith("stuck_")]].any(axis=1)
    r["gl_ineffective"] = f & D.gl_ineffective
    return r


def detect(daily: pd.DataFrame, completion: pd.DataFrame | None = None, choke: pd.DataFrame | None = None,
           cfg: AnomalyConfig | None = None, model=None, tests: pd.DataFrame | None = None) -> tuple[pd.DataFrame, object]:
    """Return (scored frame, fitted IsolationForest). Columns added: if_score, anomaly, explained, type, severity, evidence."""
    from sklearn.ensemble import IsolationForest

    cfg = cfg or AnomalyConfig()
    D = build_features(daily, completion, choke, cfg, tests)
    Z = D[["z_" + c for c in FEATURES]].clip(-20, 20)
    usable = D.flowing & Z.notna().all(axis=1)          # full feature vector -> Isolation Forest
    partial = D.flowing & Z[["z_" + c for c in SENSORS]].notna().any(axis=1)   # raw sensors only (settling)
    D["if_score"] = np.nan
    if model is None and usable.sum() > 50:
        clean = usable & ~D.ctx_choke & ~D.ctx_restart & ~D.ctx_gap & ~D.ctx_partial & ~D.ctx_test & ~D.ctx_glstep
        model = IsolationForest(n_estimators=300, contamination="auto", random_state=cfg.seed)
        model.fit(Z[clean].abs())
        thr = float(np.quantile(-model.score_samples(Z[clean].abs()), cfg.if_quantile))
        model.caspianx_threshold = thr
    if model is not None and usable.any():
        D.loc[usable, "if_score"] = -model.score_samples(Z[usable].abs())
    thr = getattr(model, "caspianx_threshold", np.inf)

    R = _rules(D)
    zabs = Z.abs()
    D["max_z"] = zabs.max(axis=1)
    D["top_feature"] = zabs.fillna(-1).idxmax(axis=1).str[2:].where(zabs.notna().any(axis=1), "")
    stat_flag = (usable & ((D.max_z >= cfg.z_flag) | ((D.if_score >= thr) & (D.max_z >= cfg.z_if)))) | \
                (partial & ~usable & (D.max_z >= 2 * cfg.z_flag))
    rule_flag = R.any(axis=1)
    D["anomaly"] = stat_flag | rule_flag

    types, sev, evid, expl = [], [], [], []
    for i in D.index:
        if not D.at[i, "anomaly"]:
            if D.at[i, "ctx_shut"]:
                types.append("shut_in"); sev.append("info"); evid.append(f"uptime {D.at[i, 'uptime']*100:.0f} %"); expl.append("shut_in")
            else:
                types.append(""); sev.append(""); evid.append(""); expl.append("")
            continue
        z = {c: D.at[i, "z_" + c] for c in FEATURES}
        e = []
        if rule_flag.at[i] and R.loc[i].drop("gl_ineffective").sum() == 0:
            t = "lift_gas_anomaly"
            e.append("qazlift dəyişib, amma quyu reaksiya vermir (" + D.at[i, "gl_step_txt"] +
                     "): qaz klapana çatmır / bypass / optimumdan kənar ehtimalı, ya da sərfölçən xətası")
        elif rule_flag.at[i]:
            t = "sensor_fault"
            if R.at[i, "neg"]:
                bad = [c for c in SENSORS if D.at[i, c] < 0]
                e.append("mənfi dəyər (işarə xətası): " + ", ".join(f"{c}={D.at[i, c]:.1f}" for c in bad))
            if R.at[i, "missing"]:
                e.append("sensor dəyəri yoxdur: " + ", ".join(c for c in SENSORS if pd.isna(D.at[i, c])))
            if R.at[i, "order_ds"]:
                e.append(f"ştuserdən sonrakı təzyiq WHP-dən böyükdür ({D.at[i, 'whp_downstream_psia']:.0f} > {D.at[i, 'whp_psia']:.0f} psi): axın fiziki mümkün deyil")
            if R.at[i, "order_dh"]:
                e.append("quyudibi təzyiq ≤ WHP: fiziki mümkün deyil")
            if R.at[i, "stuck"]:
                e.append("sensor donub (eyni dəyər ardıcıl dövrlərdə): " + ", ".join(c[6:] for c in D.columns if c.startswith("stuck_") and D.at[i, c]))
        else:
            big = {c: v for c, v in z.items() if np.isfinite(v) and abs(v) >= 3}
            moved = {c: v for c, v in z.items() if np.isfinite(v) and abs(v) >= 1.5 and c in SENSORS}
            raw_big = [c for c in big if c in SENSORS]
            # single raw sensor jumps while every other sensor is quiet -> instrument, not process
            rz = sorted((abs(v) for c_, v in z.items() if c_ in SENSORS and np.isfinite(v)), reverse=True) + [0, 0]
            if len(raw_big) == 1 and rz[0] >= 6 and rz[1] < max(3.0, 0.15 * rz[0]) and raw_big[0] != "gas_lift_rate_mscfd":
                t = "sensor_fault"
                c = raw_big[0]
                e.append(f"yalnız {c} sıçrayıb (z={z[c]:+.1f}), digər sensorlar sabitdir: ölçü cihazı ehtimalı")
            elif len(raw_big) == 1 and raw_big[0] == "gas_lift_rate_mscfd" and rz[1] < 3.0:
                t = "lift_gas_anomaly"
                e.append(f"qazlift sərfi dəyişib (z={z['gas_lift_rate_mscfd']:+.1f}, {D.at[i, 'gas_lift_rate_mscfd']:.0f} Mscf/d), "
                         "təzyiqlər demək olar dəyişməyib: sərfölçəni və ya tənzimləyicini yoxlayın")
            elif (np.isfinite(z["grad_resid"]) and z["grad_resid"] >= 3 and z["gas_lift_rate_mscfd"] > -2
                  and (z["dh_pressure_psia"] >= 1.5 or (z["whp_psia"] <= -1.5 and z["dh_pressure_psia"] >= -0.5))):
                t = "water_cut_jump"
                e.append(f"lülədə maye sütunu ağırlaşıb (qradiyent qalığı z={z['grad_resid']:+.1f}) qazlift dəyişmədən: su payı artımı əlaməti")
            elif abs(z["gas_lift_rate_mscfd"]) >= 3 and abs(z["gas_lift_rate_mscfd"]) == max(abs(v) for v in big.values()):
                t = "lift_gas_anomaly"
                e.append(f"qazlift sərfi baza xəttindən kənar (z={z['gas_lift_rate_mscfd']:+.1f}, {D.at[i, 'gas_lift_rate_mscfd']:.0f} Mscf/d)")
            else:
                t = "pressure_anomaly"
                top = sorted(big.items(), key=lambda kv: -abs(kv[1]))[:3]
                e.append("təzyiq rejimi dəyişib: " + ", ".join(f"{c} z={v:+.1f}" for c, v in top))
            if np.isfinite(D.at[i, "if_score"]) and D.at[i, "if_score"] >= thr:
                e.append("Isolation Forest: kombinasiya nadirdir")
        ctx = [n for n, col in [("choke_change", "ctx_choke"), ("well_test", "ctx_test"), ("restart", "ctx_restart"),
                                    ("gl_setpoint", "ctx_glstep"), ("logger_gap", "ctx_gap")]
               if D.at[i, col]]
        # physics rules are never explained away by context; statistical flags are
        ex = "" if rule_flag.at[i] else (ctx[0] if ctx else "")
        hard = rule_flag.at[i] and (R.loc[i, ["neg", "order_ds", "order_dh"]].any())
        hi = hard or (t == "sensor_fault" and np.isfinite(D.at[i, "max_z"]) and D.at[i, "max_z"] >= 8) or \
            (t in ("water_cut_jump", "pressure_anomaly") and np.isfinite(D.at[i, "max_z"]) and D.at[i, "max_z"] >= 8)
        types.append(t); evid.append("; ".join(e)); expl.append(ex)
        sev.append("info" if ex else ("high" if hi else "medium"))
    D["type"], D["severity"], D["evidence"], D["explained_by"] = types, sev, evid, expl
    # field-wide: the same kind of flag on >= 3 wells (and >= 30 % of wells) on one day is a facility event
    # (compressor trip, lift-gas header, manifold), reported once at field level, not as N well alerts
    nw = D.well_name.nunique()
    cand = D[D.anomaly & (D.explained_by == "") & (D.type != "sensor_fault")]
    cnt = cand.groupby(["date", "type"]).well_name.nunique()
    fw = set(cnt[(cnt >= 3) & (cnt >= 0.3 * nw)].index)
    m_fw = pd.Series([(d, t) in fw for d, t in zip(D.date, D.type)], index=D.index) & D.anomaly & (D.explained_by == "")
    D.loc[m_fw, "explained_by"] = "field_wide"
    D.loc[m_fw, "severity"] = "info"
    D["alert"] = D.anomaly & (D.explained_by == "")
    D["event_id"] = _events(D)
    return D, model


def _events(D: pd.DataFrame) -> pd.Series:
    """Consecutive alert days of one well (gap <= 1 day) form one event: an engineer reviews events, not days."""
    eid = pd.Series(-1, index=D.index)
    n = 0
    for w, g in D[D.alert].groupby("well_name"):
        prev = None
        for i, d in zip(g.index, g.date):
            if prev is None or (d - prev).days > 2:
                n += 1
            eid.at[i] = n
            prev = d
    return eid


def events_table(D: pd.DataFrame) -> pd.DataFrame:
    A = D[D.event_id >= 0]
    if not len(A):
        return pd.DataFrame(columns=["event_id", "well", "start", "end", "days", "type", "severity", "peak_z", "evidence"])
    rows = []
    for e, g in A.groupby("event_id"):
        top = g.loc[g.max_z.fillna(0).idxmax()]
        t = g.type.value_counts().index[0] if "sensor_fault" not in set(g.type) else "sensor_fault"
        rows.append({"event_id": int(e), "well": g.well_name.iloc[0], "start": g.date.min(), "end": g.date.max(),
                     "days": int(len(g)), "type": t, "severity": "high" if (g.severity == "high").any() else "medium",
                     "peak_z": round(float(top.max_z), 1) if np.isfinite(top.max_z) else None,
                     "evidence": g[g.type == t].evidence.iloc[0]})
    return pd.DataFrame(rows).sort_values("start", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------------------------- validation
def _inject(D: pd.DataFrame, rng: np.random.Generator, n_per_type: int = 40,
            tvd: dict | None = None, kinds: list | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Inject faults into clean flowing days. Returns modified daily frame and the truth table."""
    X = D.copy()
    clean = X[(X.uptime >= 0.999) & (X.logger_gap_min <= 0)]
    clean = clean[clean.groupby("well_name").cumcount() >= 30]       # need history
    used: set = set()
    truth = []
    kinds = kinds or ["spike", "sign_flip", "stuck", "drift", "water_cut_step"]
    for kind in kinds:
        cand = clean.sample(frac=1.0, random_state=int(rng.integers(1e9)))
        n = 0
        for i, row in cand.iterrows():
            if n >= n_per_type:
                break
            w = row.well_name
            span = 7 if kind in ("stuck", "drift", "water_cut_step") else 1
            g = X[(X.well_name == w) & (X.date >= row.date) & (X.date < row.date + pd.Timedelta(days=span))]
            if len(g) < span or any((w, d) in used for d in g.date) or (X.loc[g.index, "uptime"] < 0.999).any():
                continue
            # keep a 10-day buffer so injections do not overlap
            for d in pd.date_range(row.date - pd.Timedelta(days=5), row.date + pd.Timedelta(days=span + 5)):
                used.add((w, d))
            # stuck: pressures only (a held lift-gas set-point is indistinguishable from a stuck meter at daily resolution)
            c = (rng.choice(SENSORS[:3]) if kind == "stuck" else rng.choice(SENSORS)) if kind in ("spike", "sign_flip", "stuck") else None
            if kind == "spike":
                X.loc[g.index, c] = X.loc[g.index, c] * (1 + rng.choice([-1, 1]) * rng.uniform(0.15, 0.30))
            elif kind == "sign_flip":
                X.loc[g.index, c] = -X.loc[g.index, c].abs()
            elif kind == "stuck":
                X.loc[g.index, c] = X.loc[g.index[0], c]
            elif kind == "drift":
                c = "dh_pressure_psia"
                X.loc[g.index, c] = X.loc[g.index, c] * (1 + np.linspace(0.01, 0.07, len(g)))
            elif kind == "water_cut_step":
                # +10 water-cut points: (0.44 water - 0.37 oil psi/ft) x 0.10 x liquid holdup ~0.6 = +0.0042 psi/ft;
                # the gauge rises by that x TVD at unchanged WHP and lift gas
                h = float((tvd or {}).get(w, 8000.0))
                X.loc[g.index, "dh_pressure_psia"] += 0.0042 * h
            truth.append({"kind": kind, "well_name": w, "start": row.date, "days": span, "sensor": c or ""})
            n += 1
    return X, pd.DataFrame(truth)


def validate(daily: pd.DataFrame, completion: pd.DataFrame | None = None, choke: pd.DataFrame | None = None,
             cfg: AnomalyConfig | None = None, seed: int = 11, n_per_type: int = 40,
             tests: pd.DataFrame | None = None) -> dict:
    cfg = cfg or AnomalyConfig()
    base, _ = detect(daily, completion, choke, cfg, tests=tests)
    tvd = dict(zip(completion.well_name, completion.gauge_tvd_ft)) if completion is not None and "gauge_tvd_ft" in completion else None
    rows = []
    Ts = []
    for k_i, kind in enumerate(["spike", "sign_flip", "stuck", "drift", "water_cut_step"]):
      rng = np.random.default_rng(seed + k_i)
      X, T = _inject(daily.assign(date=pd.to_datetime(daily["date"])), rng, n_per_type, tvd, kinds=[kind])
      Ts.append(T)
      Dx, _ = detect(X, completion, choke, cfg, tests=tests)
      key = Dx.set_index(["well_name", "date"])
      for t in T.itertuples():
        days = pd.date_range(t.start, periods=t.days)
        sub = key.loc[[(t.well_name, d) for d in days if (t.well_name, d) in key.index]]
        flagged = sub[sub.anomaly]
        det = len(flagged) > 0
        delay = int((flagged.index.get_level_values(1).min() - t.start).days) if det else None
        ok_types = {"sensor_fault"} if t.kind in ("spike", "sign_flip", "stuck") else (
            {"water_cut_jump"} if t.kind == "water_cut_step" else {"pressure_anomaly", "sensor_fault", "water_cut_jump"})
        if t.kind == "spike" and t.sensor == "gas_lift_rate_mscfd":
            ok_types = {"sensor_fault", "lift_gas_anomaly"}
        rows.append({"kind": t.kind, "detected": det, "delay_days": delay,
                     "typed_ok": bool(det and (ok_types & set(flagged.type)))})
    T = pd.concat(Ts, ignore_index=True)
    V = pd.DataFrame(rows)
    per = V.groupby("kind").agg(n=("detected", "size"), recall=("detected", "mean"), typed=("typed_ok", "mean"),
                                median_delay=("delay_days", "median")).reset_index()
    # alert burden on the real (un-injected) data: every alert an engineer would have to look at
    b = base[base.flowing]
    months = b.groupby("well_name").date.agg(lambda s: (s.max() - s.min()).days / 30.4).sum()
    fa = int(b.alert.sum())
    ch = base[base.ctx_choke & base.flowing]
    return {
        "per_fault": json.loads(per.round(3).to_json(orient="records")),
        "overall_recall": round(float(V.detected.mean()), 3),
        "alerts_real_data": fa,
        "alerts_per_well_month": round(fa / max(months, 1e-9), 3),
        "alert_events_real_data": int(b[b.alert].event_id.nunique()),
        "alert_events_per_well_month": round(b[b.alert].event_id.nunique() / max(months, 1e-9), 3),
        "well_test_days_explained": int((base.ctx_test & base.anomaly & (base.explained_by == "well_test")).sum()),
        "alert_rate_pct_of_well_days": round(100 * fa / max(len(b), 1), 2),
        "choke_change_days": int(len(ch)),
        "choke_change_sensitivity": round(float(ch.anomaly.mean()) if len(ch) else float("nan"), 3),
        "choke_change_explained": round(float((ch[ch.anomaly].explained_by != "").mean()) if ch.anomaly.any() else 1.0, 3),
        "shut_in_days_labelled": int(base.ctx_shut.sum()),
        "n_injected": int(len(T)),
        "seed": seed,
    }


def validate_tests(tests: pd.DataFrame) -> pd.DataFrame:
    """Same physics rules on the well-test table: sign errors and unit errors (ratio vs field)."""
    T = tests.copy()
    out = []
    num = [c for c in T.columns if c.endswith(("_psia", "_mscfd", "_stbd", "_degf"))]
    for i, r in T.iterrows():
        for c in num:
            v = pd.to_numeric(r[c], errors="coerce")
            if pd.notna(v) and v < 0:
                out.append({"well_name": r.well_name, "test_start": r.test_start, "column": c, "issue": "sign error",
                            "value": float(v)})
    if {"gas_rate_mscfd", "oil_rate_stbd"} <= set(T.columns):
        gor = (T.gas_rate_mscfd.abs() * 1000 / T.oil_rate_stbd.abs()).groupby(T.well_name).median()
        field = gor.median()
        for w, v in gor.items():
            ratio = v / field
            for f, unit in [(28.317, "sm3/d instead of Mscf/d"), (1 / 28.317, "Mscf/d instead of sm3/d"), (1000, "scf/d instead of Mscf/d")]:
                if abs(np.log(ratio / f)) < np.log(1.6):
                    out.append({"well_name": w, "test_start": "all", "column": "gas_rate_mscfd",
                                "issue": f"unit error: {unit} (GOR {v:.0f} vs field {field:.0f} scf/stb)", "value": float(v)})
    O = pd.DataFrame(out)
    if len(O):
        O["test_start"] = O["test_start"].astype(str)
    return O


# ---------------------------------------------------------------------------------------------- export
def to_json(D: pd.DataFrame, validation: dict | None = None, tests_check: pd.DataFrame | None = None,
            max_explained: int = 200) -> dict:
    """Compact JSON shared by Streamlit, desktop HTML and the mobile app."""
    ev = D[D.anomaly].copy()
    ev = ev.sort_values(["alert", "severity", "date"], ascending=[False, True, False])

    def rec(r):
        return {"well": r.well_name, "date": str(r.date.date()), "type": r.type, "type_az": LABEL_AZ.get(r.type, r.type),
                "severity": r.severity, "alert": bool(r.alert), "explained_by": r.explained_by,
                "explained_az": LABEL_AZ.get(r.explained_by, ""), "evidence": r.evidence,
                "max_z": None if not np.isfinite(r.max_z) else round(float(r.max_z), 1),
                "if_score": None if not np.isfinite(r.if_score) else round(float(r.if_score), 3),
                "top_feature": r.top_feature}

    alerts = [rec(r) for r in ev[ev.alert].itertuples()]
    explained = [rec(r) for r in ev[~ev.alert].itertuples()][:max_explained]
    summary = []
    last = D.date.max()
    for w, g in D.groupby("well_name"):
        a = g[g.alert]
        recent = a[a.date > last - pd.Timedelta(days=30)]
        summary.append({"well": w, "alerts": int(len(a)), "alerts_30d": int(len(recent)),
                        "last_alert": str(a.date.max().date()) if len(a) else None,
                        "types": a.type.value_counts().to_dict(),
                        "status": "red" if (recent.severity == "high").any() else ("amber" if len(recent) else "green")})
    series = {}
    for w, g in D.groupby("well_name"):
        series[w] = {"d": [str(x.date()) for x in g.date],
                     "score": [None if not np.isfinite(v) else round(float(v), 3) for v in g.if_score],
                     "max_z": [None if not np.isfinite(v) else round(float(v), 1) for v in g.max_z]}
    return {"meta": {"method": "physics rules + causal robust z (median/MAD) + Isolation Forest; context filter "
                               "(shut-in, restart, logger gap, choke change)",
                     "resolution": "daily" if D.groupby("well_name").date.diff().dt.days.median() >= 1 else "sub-daily",
                     "first": str(D.date.min().date()), "last": str(last.date()),
                     "features": FEATURES, "labels_az": LABEL_AZ},
            "validation": validation or {},
            "test_table_check": [] if tests_check is None else json.loads(tests_check.to_json(orient="records")),
            "summary": summary, "alerts": alerts, "explained": explained, "series": series}
