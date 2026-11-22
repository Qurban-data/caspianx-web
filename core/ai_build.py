"""
Builds the grounded knowledge base and the anomaly results from the files on disk (no Streamlit needed).
Used by tools_build_ai.py (JSON export for desktop / mobile), the tests and the Streamlit page fallback.
"""
from __future__ import annotations

import glob
import json
import os
import pickle
import warnings

import numpy as np
import pandas as pd

from .ai_facts import Knowledge
from .anomaly import detect, events_table, validate, validate_tests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_inputs(root: str = HERE) -> dict:
    D = os.path.join(root, "data", "socar")
    return {"daily": pd.read_csv(os.path.join(D, "daily_features.csv"), parse_dates=["date"]),
            "tests": pd.read_csv(os.path.join(D, "well_tests.csv")),
            "choke": pd.read_csv(os.path.join(D, "choke_events.csv")),
            "pvt": pd.read_csv(os.path.join(D, "reservoir_pvt.csv")),
            "comp": pd.read_csv(os.path.join(D, "well_completion.csv")),
            "events_dir": os.path.join(D, "events")}


def run_anomaly(daily, comp, choke, tests, with_validation: bool = True) -> dict:
    A, model = detect(daily, comp, choke, tests=tests)
    E = events_table(A)
    v = validate(daily, comp, choke, tests=tests) if with_validation else {}
    tc = validate_tests(tests)
    return {"A": A, "E": E, "validation": v, "tests_check": tc, "model": model}


def build_knowledge(root: str = HERE, with_validation: bool = True) -> tuple[Knowledge, dict]:
    """Recompute everything from disk with the locked baseline model (identical numbers to the deliverables)."""
    warnings.filterwarnings("ignore")
    from .inference import infer_daily
    from .intervention import rank_interventions
    from .preprocessing import qc_tests
    from .pta import pta_event
    from .governance import MODEL_CARD

    I = load_inputs(root)
    with open(os.path.join(root, "outputs", "vfm_model.pkl"), "rb") as fh:
        m = pickle.load(fh)
    P, info = infer_daily(m, I["daily"], I["choke"])
    Tq, _ = qc_tests(I["tests"], I["choke"])
    pta = []
    choke = I["choke"]
    for fj in sorted(glob.glob(os.path.join(I["events_dir"], "*.json"))):
        with open(fj) as fh:
            e = json.load(fh)
        w, day = os.path.basename(fj)[:-5].split("_")
        c0 = choke[(choke.well_name == w) & (choke.choke_opening_pct <= 0.5)]
        c0 = c0[pd.to_datetime(c0.timestamp).dt.strftime("%Y-%m-%d") == day]
        ts = pd.Timestamp(c0.timestamp.iloc[0]) if len(c0) else pd.Timestamp(day)
        a = np.array(e["post"])
        pta.append(pta_event(a[:, 0] / 60, a[:, 1], e["pwf"], w, ts, P, I["pvt"], I["comp"])[0])
    R = rank_interventions(pta, I["pvt"], wells=sorted(P.well_name.unique()))
    an = run_anomaly(I["daily"], I["comp"], I["choke"], I["tests"], with_validation)
    K = Knowledge(P, pta, R, an["A"], an["E"], I["choke"], Tq, MODEL_CARD, an["validation"])
    return K, an
