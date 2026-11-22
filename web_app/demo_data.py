"""Synthetic demo field (NOT field data): 90 days after the training period for the wells the shipped model knows.

Sensor levels are drawn around each well's median well-test conditions stored inside the model; PVT and completion
values are generic textbook numbers labelled DEMO; two build-ups are analytic radial-flow responses with wellbore
storage. Used only by the "Load demo field data" button so the app can be previewed without the confidential files.
"""
from __future__ import annotations

import io
import json

import numpy as np
import pandas as pd

START = pd.Timestamp("2025-10-01")
DAYS = 90
SHUT_INS = {"GL-01": (START + pd.Timedelta(days=40, hours=8), 3.2e-5, 120.0, 4.0),   # well: (t_shut, -, k md, skin)
            "GL-04": (START + pd.Timedelta(days=62, hours=8), 3.2e-5, 60.0, -1.0)}


def _csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, lineterminator="\n").encode("utf-8")


def build(model) -> list[tuple[str, bytes]]:
    rng = np.random.default_rng(2026)
    T = model.a.T
    wells = sorted(T.well_name.unique())
    med = T.groupby("well_name")[["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd",
                                  "choke_opening_pct"]].median()
    days = pd.date_range(START, periods=DAYS, freq="D")
    rows, chk = [], []
    for w in wells:
        m = med.loc[w]
        ar = np.cumsum(rng.normal(0, 0.004, DAYS))
        whp = m.whp_psia * (1.02 + ar + rng.normal(0, 0.006, DAYS))
        pds = m.whp_downstream_psia * (1.12 + rng.normal(0, 0.01, DAYS))      # production manifold > test separator
        bhp = m.dh_pressure_psia * (1.003 + 0.4 * ar + rng.normal(0, 0.0008, DAYS))
        gl = m.gas_lift_rate_mscfd * (1 + rng.normal(0, 0.004, DAYS))
        up = np.ones(DAYS)
        if w in SHUT_INS:
            d = (SHUT_INS[w][0].normalize() - START).days
            up[d], up[d + 1], up[d + 2] = 8 / 24, 0.0, 16 / 24
        rows.append(pd.DataFrame({"well_name": w, "date": days.strftime("%Y-%m-%d"), "nf": 1440 * up,
                                  "whp_psia": whp.round(1), "whp_downstream_psia": pds.round(1), "dh_pressure_psia": bhp.round(0),
                                  "gas_lift_rate_mscfd": gl.round(0), "sensor_missing": False, "uptime": up.round(4),
                                  "n_rows": 1440, "logger_gap_min": 0}))
        c0 = float(m.choke_opening_pct)
        chk.append((START, w, c0))
        chk.append((START + pd.Timedelta(days=int(rng.integers(20, 70))), w, round(c0 * float(rng.choice([0.92, 1.08])), 1)))
        if w in SHUT_INS:
            ts = SHUT_INS[w][0]
            chk.append((ts, w, 0.0))
            chk.append((ts + pd.Timedelta(hours=72), w, c0))
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
    # analytic build-ups: radial flow (Horner) with a storage transition, q from the median test liquid x (1 - WC)
    for w, (ts, ct, k, s) in SHUT_INS.items():
        q = float(T[T.well_name == w]["oil_rate_stbd"].median())
        B, mu, h, phi, rw = 1.16, 1.05, 75.0, 0.20, 0.328
        m_ = 162.6 * q * B * mu / (k * h)
        tp = 24 * 365.0
        mins = np.unique(np.r_[np.arange(1, 120), np.geomspace(120, 72 * 60, 260)].round().astype(int))
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


def demo_zip(model) -> bytes:
    import zipfile
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in build(model):
            z.writestr(n, b)
    return bio.getvalue()
