"""
Model governance and audit trail (metadata only; no model or calculation is changed here).

* ``MODEL_CARD``: what the validated model is, how it was validated, its features and limitations. The metrics are
  HISTORICAL (forward validation on the Jan-Sep 2025 well tests), never presented as measured on new data.
* ``fingerprint``: SHA-256 of the input tables (content, not file names), used as the cache key and audit key.
* ``audit_record`` / ``append_audit``: one JSON line per processing run or scenario (outputs/audit/audit_log.jsonl).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os

import numpy as np
import pandas as pd

MODEL_VERSION = "caspianx-vfm-physics-ridge-v4 (baseline-v4)"
MODEL_CARD: dict = {
    "name": "CaspianX Gas-Lift VFM",
    "version": MODEL_VERSION,
    "architecture": "Sub-critical choke physics q = C_w (choke/100)^2 sqrt(P_wh - P_ds), per-well C_w, pooled "
                    "Ridge residual on log-rate clipped to +-0.3; per-well WC/GOR trends; daily = rate x uptime",
    "features": ["whp_psia", "whp_downstream_psia", "choke_opening_pct", "gas_lift_rate_mscfd", "dh_pressure_psia",
                 "uptime (flowing minutes / observed minutes)"],
    "training_data": "85 QC-ed well tests, 10 wells GL-01..GL-10, Jan-Sep 2025 (SOCAR Hackathon 2026 data)",
    "validation": "Rolling-origin forward: train on tests before 1 Jun / 1 Jul / 1 Aug 2025, predict all later tests; "
                  "also leave-one-test-out and month-blocked group CV",
    "validation_period": "Jun-Sep 2025 (forward), Jan-Sep 2025 (LOO, month CV)",
    "historical_forward_metrics": {"oil_MAPE_%": 3.81, "water_MAPE_%": 4.56, "gas_MAPE_%": 7.47,
                                   "R2_range": "0.990-0.998"},
    "frozen_protocol_v1_1": {"script": "evaluate.py", "cutoffs": "1 Apr / 1 May / 1 Jun / 1 Jul 2025, +90 d",
                             "n_predictions": 109,
                             "deployed_form_subcrit_ridge_oil_MAPE_%": 4.79, "physics_only_oil_MAPE_%": 4.81,
                             "auto_selection_oil_MAPE_%": 7.08,
                             "note": "auto LOO selection picks Gilbert when few tests exist (Apr/May cutoffs); "
                                     "with all 85 tests it selects subcrit+ridge (deployed)",
                             "p10_p90_pooled_coverage_%_oil": 67.0},
    "uncertainty": "P10/P90 from empirical out-of-sample log-error quantiles (LOO inside, forward after last test); "
                   "month-CV coverage 74-81 % vs nominal 80 %",
    "last_validation_date": "2026-11-21",
    "limitations": [
        "P10-P90 band under-covers at a 90-day horizon (pooled oil 67 % vs nominal 80 %, frozen protocol)",
        "Wells without a well test are uncalibrated (field-median C_w, WC, GOR; wider band)",
        "Daily downstream pressure is higher than at test conditions (test separator); ratio extrapolation",
        "After the last test WC follows mean(last 3) + Theil-Sen slope, GOR = mean(last 3)",
        "Gas-lift and choke what-if uses a calibrated operating-point solver; validated on real changes only",
    ],
}


def build_model_card(model_a=None, metrics: dict | None = None) -> dict:
    """Model card generated from the LOADED model (training tests, wells, period) and models/metrics_card.json,
    so the app never shows a stale hard-coded period or test count."""
    card = json.loads(json.dumps(MODEL_CARD, default=str))
    if model_a is not None and hasattr(model_a, "T") and len(model_a.T):
        T = model_a.T
        t = pd.to_datetime(T["test_mid"])
        wl = sorted(T["well_name"].unique())
        card["training_data"] = (f"{len(T)} QC-ed well tests, {len(wl)} wells ({', '.join(wl[:12])}"
                                 f"{', ...' if len(wl) > 12 else ''}), {t.min():%d %b %Y} - {t.max():%d %b %Y}")
        card["training_period"] = [f"{t.min():%Y-%m-%d}", f"{t.max():%Y-%m-%d}"]
        card["trend_horizon_days"] = getattr(model_a, "trend_horizon_days", None) or 180
    if metrics:
        f = metrics.get("vfm", {}).get("forward_90d", {})
        if f:
            card["historical_forward_metrics"] = {f"{ph}_MAPE_%": round(v["MAPE_%"], 2) for ph, v in f.items()}
            card["historical_forward_metrics"]["n_predictions"] = int(next(iter(f.values()))["n"])
            card["validation"] = ("Frozen rolling-origin protocol (evaluate.py): cutoffs 1 Apr / 1 May / 1 Jun / 1 Jul 2025, "
                                  "90-day horizon; plus leave-one-well-out CV. Submitted VFM = A/B blend.")
            card["validation_period"] = "historical validation on the training tests (not measured on new data)"
    return card


def fingerprint(*tables: pd.DataFrame | None) -> str:
    """Content hash of the input tables (order-sensitive). None entries are skipped."""
    h = hashlib.sha256()
    for t in tables:
        if t is None:
            continue
        h.update(str(list(t.columns)).encode())
        h.update(pd.util.hash_pandas_object(t, index=False).values.tobytes())
    return h.hexdigest()[:16]


def _clean(v):
    if isinstance(v, (np.floating, float)):
        return None if not np.isfinite(v) else round(float(v), 6)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (pd.Timestamp, _dt.datetime, _dt.date)):
        return str(v)
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def audit_record(kind: str, dataset: str, well: str | None = None, inputs: dict | None = None,
                 outputs: dict | None = None, warnings: list | None = None, status: str = "ok") -> dict:
    return _clean({"timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                   "kind": kind, "dataset_fingerprint": dataset, "model_version": MODEL_VERSION, "well": well,
                   "inputs": inputs or {}, "outputs": outputs or {}, "warnings": warnings or [],
                   "validation_status": status})


def append_audit(rec: dict, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_audit(path: str, last: int = 200) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()[-last:]
    return pd.json_normalize([json.loads(x) for x in lines])
