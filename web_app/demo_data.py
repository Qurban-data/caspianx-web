"""Synthetic demo field (NOT field data): 1 January to 30 September 2025 (273 days) for the wells the shipped model knows.

Sensor levels are drawn around each well's median well-test conditions stored inside the model; PVT and completion
values are generic textbook numbers labelled DEMO; the build-ups are analytic radial-flow responses with wellbore
storage. Used only by the "Load demo data" button so the app can be previewed without the confidential files.

What is built in on purpose, so the surveillance views have something to find:
  * natural decline (different per well) and a mild seasonal swing of the manifold back-pressure;
  * GL-03: formation damage from 6 July (drawdown across the choke falls over 10 days, rate about -20 %), confirmed by
    the build-up of 12 August (skin +8.5);
  * two more build-ups: GL-01 in March (skin +4, moderate damage) and GL-04 in May (skin -1, undamaged);
  * GL-06: unplanned two-day shut-in in February; GL-09: one partial day in June;
  * GL-08: wellhead-pressure transmitter gap of four days in May (filled and flagged by the pipeline).

The generator is deterministic (fixed seed). ``tools/build_demo_static.py`` writes its output to ``web_app/demo_static``;
when that folder exists ``build`` serves those files, so every server instance returns byte-identical demo data.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 2026
START = pd.Timestamp("2025-01-01")
DAYS = 273                                                   # 2025-01-01 .. 2025-09-30, nine complete months
BUILDUP_HOURS = 72
# well: (t_shut, total compressibility 1/psi, k md, skin)
SHUT_INS = {"GL-01": (pd.Timestamp("2025-03-18 08:00"), 3.2e-5, 120.0, 4.0),
            "GL-04": (pd.Timestamp("2025-05-20 08:00"), 3.2e-5, 60.0, -1.0),
            "GL-03": (pd.Timestamp("2025-08-12 08:00"), 3.2e-5, 95.0, 8.5)}
DAMAGE = {"GL-03": (pd.Timestamp("2025-07-06"), 10, 0.80)}   # well: (onset, ramp days, rate factor after the ramp)
# extra decline on top of the water-cut / GOR trends the model itself carries through the period
ANNUAL_DECLINE = {"GL-01": 0.03, "GL-02": 0.00, "GL-03": 0.02, "GL-04": 0.04, "GL-05": 0.00, "GL-06": 0.03,
                  "GL-07": 0.02, "GL-08": 0.06, "GL-09": 0.03, "GL-10": 0.01}
UNPLANNED = {"GL-06": [(pd.Timestamp("2025-02-11"), 0.0), (pd.Timestamp("2025-02-12"), 0.0)],
             "GL-09": [(pd.Timestamp("2025-06-17"), 0.55)]}
SENSOR_GAP = {"GL-08": ("whp_psia", pd.Timestamp("2025-05-09"), 4)}
STATIC_DIR = Path(__file__).resolve().parent / "demo_static"


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, lineterminator="\n").encode("utf-8")


def generate(model) -> list[tuple[str, bytes]]:
    """Run the seeded generator. Same model + same seed -> same bytes."""
    rng = np.random.default_rng(SEED)
    T = model.a.T
    wells = sorted(T.well_name.unique())
    med = T.groupby("well_name")[["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd",
                                  "choke_opening_pct"]].median()
    days = pd.date_range(START, periods=DAYS, freq="D")
    t = np.arange(DAYS)
    season = np.sin(2 * np.pi * (t - 15) / 365.0)            # +1 around mid-April .. -1 around mid-October
    rows, chk = [], []
    for iw, w in enumerate(wells):
        m = med.loc[w]
        ar = np.cumsum(rng.normal(0, 0.0025, DAYS))
        ar -= np.linspace(0, ar[-1], DAYS)                  # random walk pinned at both ends: noise, not trend
        f = (1 - ANNUAL_DECLINE.get(w, 0.06)) ** (t / 365.0)  # rate factor: natural decline
        if w in DAMAGE:
            t0, ramp, lvl = DAMAGE[w]
            f = f * (1 - (1 - lvl) * np.clip((t - (t0 - START).days) / ramp, 0, 1))
        pds = m.whp_downstream_psia * (1.12 + 0.015 * season + rng.normal(0, 0.008, DAYS))   # manifold > test separator
        dp0 = max(float(m.whp_psia - m.whp_downstream_psia * 1.12) * 1.02, 0.08 * float(m.whp_psia))
        whp = pds + dp0 * f ** 2 * (1 + 2 * ar + rng.normal(0, 0.010, DAYS))                  # q ~ sqrt(WHP - P_ds)
        bhp = m.dh_pressure_psia * (1.003 - 0.05 * (1 - f) + 0.4 * ar + rng.normal(0, 0.0008, DAYS))
        gl = m.gas_lift_rate_mscfd * (1 + rng.normal(0, 0.004, DAYS))
        up = np.ones(DAYS)
        if w in SHUT_INS:                                    # 72 h build-up from 08:00: 8 h + 0 + 0 + 16 h flowing
            d = (SHUT_INS[w][0].normalize() - START).days
            up[d], up[d + 1], up[d + 2], up[d + 3] = 8 / 24, 0.0, 0.0, 16 / 24
        for day, frac in UNPLANNED.get(w, []):
            up[(day - START).days] = frac
        df = pd.DataFrame({"well_name": w, "date": days.strftime("%Y-%m-%d"), "nf": (1440 * up).round(0),
                           "whp_psia": whp.round(1), "whp_downstream_psia": pds.round(1), "dh_pressure_psia": bhp.round(0),
                           "gas_lift_rate_mscfd": gl.round(0), "sensor_missing": False, "uptime": up.round(4),
                           "n_rows": 1440, "logger_gap_min": 0})
        if w in SENSOR_GAP:
            col, g0, n = SENSOR_GAP[w]
            i0 = (g0 - START).days
            df.loc[i0:i0 + n - 1, col] = np.nan
            df.loc[i0:i0 + n - 1, "sensor_missing"] = True
        rows.append(df)
        c0 = float(m.choke_opening_pct)
        chk.append((START, w, c0))
        c1 = c0 if w in DAMAGE else round(c0 * (1.04 if iw % 2 else 0.97), 1)        # damaged well: choke untouched
        tc = START + pd.Timedelta(days=int(rng.integers(40, 230)), hours=10)
        if c1 != c0:
            chk.append((tc, w, c1))
        if w in SHUT_INS:
            ts = SHUT_INS[w][0]
            chk.append((ts, w, 0.0))
            chk.append((ts + pd.Timedelta(hours=BUILDUP_HOURS), w, c1 if (c1 != c0 and tc < ts) else c0))
    daily = pd.concat(rows, ignore_index=True)
    choke = pd.DataFrame(sorted(chk), columns=["timestamp", "well_name", "choke_opening_pct"])
    choke["timestamp"] = pd.to_datetime(choke["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    pvt = pd.DataFrame({"well_name": wells, "net_pay_thickness_ft": 75.0, "porosity_frac": 0.20,
                        "total_compressibility_1_psi": 1.25e-05, "wellbore_radius_ft": 0.328, "oil_fvf_rb_stb": 1.16,
                        "oil_viscosity_cp": 1.05, "bubble_point_pressure_psia": 1300.0, "reservoir_temperature_degf": 200.0,
                        "datum_tvd_ft": 9000.0, "estimated_drainage_area_acres": 100.0, "water_saturation_frac": 0.33})
    comp = pd.DataFrame({"well_name": wells, "gauge_tvd_ft": 8800.0, "gl_valve_tvd_ft": 3800.0, "gl_valve_port_64ths": 14,
                         "tubing_od_in": 3.5, "tubing_id_in": 2.992, "production_manifold": "DEMO", "lift_gas_sg": 0.65,
                         "solution_gor_at_pb_scf_stb": 280.0, "water_fvf_rb_stb": 1.02, "choke_max_size_64ths": 128,
                         "first_production_date": "2012-01-01", "cum_oil_at_2025_01_01_stb": 8.0e6})
    files = [("daily_features.csv", _csv(daily)), ("choke_events.csv", _csv(choke)),
             ("reservoir_pvt.csv", _csv(pvt)), ("well_completion.csv", _csv(comp))]
    # analytic build-ups: radial flow (Horner) with a storage transition, q from the median test oil rate
    for w, (ts, ct, k, s) in SHUT_INS.items():
        if w not in wells:
            continue
        q = float(T[T.well_name == w]["oil_rate_stbd"].median())
        if w in DAMAGE and ts >= DAMAGE[w][0]:
            q *= DAMAGE[w][2]
        B, mu, h, phi, rw = 1.16, 1.05, 75.0, 0.20, 0.328
        m_ = 162.6 * q * B * mu / (k * h)
        tp = 24 * 365.0
        mins = np.unique(np.r_[np.arange(1, 120), np.geomspace(120, BUILDUP_HOURS * 60, 260)].round().astype(int))
        dt = mins / 60.0
        p_ideal_rise = m_ * np.log10((tp + dt) / dt)
        pstar = float(np.median(T[T.well_name == w].dh_pressure_psia)) + 420.0
        p1 = pstar - m_ * np.log10(tp + 1)
        pwf = p1 - m_ * (np.log10(k / (phi * mu * ct * rw ** 2)) - 3.23 + 0.869 * s)
        pws = pstar - p_ideal_rise
        tau = 0.25                                             # storage time constant, h
        pws = pwf + (pws - pwf) * (1 - np.exp(-dt / tau)) + rng.normal(0, 0.03, len(dt))
        ev = {"post": [[int(a), round(float(b), 2)] for a, b in zip(mins, pws)], "pwf": round(float(pwf), 2),
              "note": "synthetic demo build-up"}
        files.append((f"{w}_{ts:%Y-%m-%d}.json", json.dumps(ev).encode()))
    return files


def build(model) -> list[tuple[str, bytes]]:
    """Demo input files: the committed static copy when present (identical on every instance), else the generator."""
    if STATIC_DIR.is_dir() and not os.environ.get("CASPIANX_DEMO_REGENERATE"):
        man = STATIC_DIR / "manifest.json"
        if man.exists():
            names = json.loads(man.read_text())["files"]
            if all((STATIC_DIR / n).exists() for n in names):
                return [(n, (STATIC_DIR / n).read_bytes()) for n in names]
    return generate(model)


def write_static(model, folder: Path = STATIC_DIR) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    files = generate(model)
    for n, b in files:
        (folder / n).write_bytes(b)
    (folder / "manifest.json").write_text(json.dumps({
        "kind": "synthetic demo data (not field data)", "seed": SEED, "period": [str(START.date()), str((START + pd.Timedelta(days=DAYS - 1)).date())],
        "files": [n for n, _ in files]}, indent=1))
    return [n for n, _ in files]


def demo_zip(model) -> bytes:
    import zipfile
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in build(model):
            z.writestr(n, b)
    return bio.getvalue()
