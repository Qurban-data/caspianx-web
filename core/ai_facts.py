"""
Grounded knowledge layer for the CaspianX engineer assistant (adapter: never changes VFM / PTA results).

``Knowledge`` wraps the outputs that the validated pipeline already computed (VFM daily rates with P10/P90,
PTA results, intervention ranking, anomaly events, choke events, well tests, model card) and exposes them as
small JSON-able *tools*. Both the online LLM assistant (core.assistant) and the offline fallback answer only
through these tools, so every number an engineer sees has a source and was computed by the validated models.

The one piece of arithmetic done here is bookkeeping, not modelling: ``explain_change`` splits an oil-rate change
into its exact multiplicative parts, oil = liquid x (1 - WC) x uptime, so
    dln(oil) = dln(liquid) + dln(1 - WC) + dln(uptime)
and the liquid part into the choke-physics terms of the VFM equation q ~ choke^2 sqrt(WHP - P_ds).
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from .anomaly import LABEL_AZ

LIFT = {"whp_psia": "WHP", "whp_downstream_psia": "ştuserdən sonra təzyiq", "dh_pressure_psia": "quyudibi təzyiq",
        "gas_lift_rate_mscfd": "qazlift sərfi", "choke_opening_pct": "ştuser açıqlığı"}


def _f(v, n=1):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, n)


def _d(x) -> str:
    return str(pd.Timestamp(x).date())


class Knowledge:
    def __init__(self, P: pd.DataFrame, pta: list[dict] | None = None, rank: pd.DataFrame | None = None,
                 anomalies: pd.DataFrame | None = None, events: pd.DataFrame | None = None,
                 choke: pd.DataFrame | None = None, tests: pd.DataFrame | None = None,
                 model_card: dict | None = None, anomaly_validation: dict | None = None,
                 vfm_validation: dict | None = None):
        self.P = P.copy()
        self.P["date"] = pd.to_datetime(self.P["date"])
        self.pta = pta or []
        self.rank = rank if rank is not None else pd.DataFrame()
        self.A = anomalies
        self.E = events if events is not None else pd.DataFrame()
        self.choke = None
        if choke is not None and len(choke):
            c = choke.copy()
            c["timestamp"] = pd.to_datetime(c["timestamp"], errors="coerce")
            self.choke = c.dropna(subset=["timestamp"]).sort_values(["well_name", "timestamp"])
        self.tests = tests
        self.card = model_card or {}
        self.av = anomaly_validation or {}
        self.vv = vfm_validation or (self.card.get("historical_forward_metrics") if self.card else {}) or {}
        self.wells = sorted(self.P.well_name.unique())
        self.last = self.P.date.max()

    # ------------------------------------------------------------------------------------------ tools
    def field_overview(self) -> dict:
        P, last = self.P, self.last
        L = P[P.date == last]
        prev = P[(P.date > last - pd.Timedelta(days=37)) & (P.date <= last - pd.Timedelta(days=30))]
        top = L.sort_values("oil_rate_stbd", ascending=False)
        ev = self.E
        return {
            "source": "VFM (Physics+Ridge, proqnoz); anomaliya modulu; PTA",
            "data_period": [_d(P.date.min()), _d(last)],
            "last_day": _d(last),
            "field_oil_stbd": _f(L.oil_rate_stbd.sum(), 0), "field_water_stbd": _f(L.water_rate_stbd.sum(), 0),
            "field_gas_mscfd": _f(L.gas_rate_mscfd.sum(), 0),
            "field_oil_30d_ago_stbd_weekavg": _f(prev.groupby("date").oil_rate_stbd.sum().mean(), 0),
            "wells_flowing": int((L.uptime > 0.95).sum()), "wells_total": len(self.wells),
            "top_producers": [{"well": r.well_name, "oil_stbd": _f(r.oil_rate_stbd, 0)} for r in top.head(3).itertuples()],
            "open_anomaly_events_30d": int((ev.end >= last - pd.Timedelta(days=30)).sum()) if len(ev) else 0,
            "anomaly_events_total": int(len(ev)),
            "pta_events": len(self.pta),
            "best_intervention": self._rank_records()[:1],
            "vfm_validation": self.vv,
        }

    def well_summary(self, well: str) -> dict:
        well = self._well(well)
        if well is None:
            return {"error": "naməlum quyu", "known_wells": self.wells}
        g = self.P[self.P.well_name == well].sort_values("date")
        L = g.iloc[-1]
        flow = g[g.uptime > 0.95]
        w30 = flow[flow.date > self.last - pd.Timedelta(days=30)]
        w90 = flow[(flow.date <= self.last - pd.Timedelta(days=30)) & (flow.date > self.last - pd.Timedelta(days=120))]
        out = {
            "well": well, "source": "VFM daily (proqnoz, P10-P90 ilə); sensorlar (ölçülmüş)",
            "last_day": _d(L.date), "status": L.data_status, "uptime": _f(L.uptime, 3),
            "oil_stbd": _f(L.oil_rate_stbd, 0), "oil_p10": _f(L.oil_p10, 0), "oil_p90": _f(L.oil_p90, 0),
            "water_stbd": _f(L.water_rate_stbd, 0), "gas_mscfd": _f(L.gas_rate_mscfd, 0),
            "water_cut_pct": _f(L.wc * 100, 1),
            "oil_avg_last30d": _f(w30.oil_rate_stbd.mean(), 0), "oil_avg_prev90d": _f(w90.oil_rate_stbd.mean(), 0),
            "wc_avg_last30d_pct": _f(w30.wc.mean() * 100, 1), "wc_avg_prev90d_pct": _f(w90.wc.mean() * 100, 1),
            "choke_pct": _f(L.choke_opening_pct, 1), "gas_lift_mscfd": _f(L.gas_lift_rate_mscfd, 0),
            "whp_psia": _f(L.whp_psia, 1), "downstream_psia": _f(L.whp_downstream_psia, 1),
            "bhp_psia": _f(L.dh_pressure_psia, 0),
            "calibrated": bool(L.get("calibrated", True)), "after_last_test": bool(L.get("after_last_test", False)),
            "out_of_envelope_inputs": L.get("ood_inputs", "") or "",
        }
        if self.tests is not None and len(self.tests):
            t = self.tests[self.tests.well_name == well]
            if len(t):
                tc = "test_mid" if "test_mid" in t else "test_start"
                tl = t.sort_values(tc).iloc[-1]
                out["last_well_test"] = {"date": _d(tl[tc]), "oil_stbd": _f(tl.oil_rate_stbd, 0),
                                         "water_stbd": _f(tl.water_rate_stbd, 0), "n_tests": int(len(t))}
        out["pta"] = self.pta_results(well).get("events", [])
        out["intervention"] = [r for r in self._rank_records() if r.get("well") == well]
        out["anomaly_events"] = self.anomalies(well).get("events", [])[:5]
        out["recent_choke_changes"] = self._choke_changes(well, self.last - pd.Timedelta(days=60), self.last)
        return out

    def explain_change(self, well: str, start: str | None = None, end: str | None = None,
                       ref_start: str | None = None, ref_end: str | None = None) -> dict:
        """Why did the oil rate change? Compares a recent window with a reference window (defaults: last 14
        flowing days vs the 30 flowing days before). Exact log decomposition + VFM choke-physics split."""
        well = self._well(well)
        if well is None:
            return {"error": "naməlum quyu", "known_wells": self.wells}
        g = self.P[self.P.well_name == well].sort_values("date").set_index("date")
        end_ = pd.Timestamp(end) if end else g.index.max()
        start_ = pd.Timestamp(start) if start else end_ - pd.Timedelta(days=13)
        rend = pd.Timestamp(ref_end) if ref_end else start_ - pd.Timedelta(days=1)
        rstart = pd.Timestamp(ref_start) if ref_start else rend - pd.Timedelta(days=29)
        if not start and not ref_start:
            # automatically move the recent window to the largest sustained drop in the last 120 days
            s = g.oil_rate_stbd.where(g.uptime > 0.95).rolling(7, min_periods=4).mean()
            d = (s - s.shift(14)).loc[end_ - pd.Timedelta(days=120):end_]
            if d.notna().any() and d.min() < -0.05 * float(s.median()):
                t0 = d.idxmin()
                start_, end_ = t0 - pd.Timedelta(days=6), min(t0 + pd.Timedelta(days=7), g.index.max())
                rend = t0 - pd.Timedelta(days=14)
                rstart = rend - pd.Timedelta(days=20)
        A = g.loc[rstart:rend]
        B = g.loc[start_:end_]
        if not len(A) or not len(B):
            return {"error": "pəncərədə məlumat yoxdur"}

        def m(df, c, flowing=True):
            x = df[df.uptime > 0.95][c] if flowing else df[c]
            return float(x.mean()) if len(x) else np.nan

        oa, ob = m(A, "oil_rate_stbd", False), m(B, "oil_rate_stbd", False)
        la, lb = m(A, "liquid_rate_stbd"), m(B, "liquid_rate_stbd")
        wa, wb = m(A, "wc"), m(B, "wc")
        ua, ub = float(A.uptime.mean()), float(B.uptime.mean())
        ca, cb = m(A, "choke_opening_pct"), m(B, "choke_opening_pct")
        dpa = m(A, "whp_psia") - m(A, "whp_downstream_psia")
        dpb = m(B, "whp_psia") - m(B, "whp_downstream_psia")
        parts = {}
        tot = np.log(ob / oa) if oa > 0 and ob > 0 else np.nan
        if np.isfinite(tot):
            parts = {"liquid": np.log(lb / la), "water_cut": np.log((1 - wb) / (1 - wa)), "uptime": np.log(ub / ua)}
            parts["other"] = tot - sum(parts.values())   # shut-in days inside window vs flowing mean
        liq = {}
        if la > 0 and lb > 0 and ca > 0 and cb > 0 and dpa > 0 and dpb > 0:
            liq = {"choke": 2 * np.log(cb / ca), "choke_dp": 0.5 * np.log(dpb / dpa)}
            liq["residual_model"] = np.log(lb / la) - liq["choke"] - liq["choke_dp"]
        dq = ob - oa

        def stb(x):  # share of the oil change in STB/d (log shares are additive)
            return _f(dq * x / tot, 0) if np.isfinite(tot) and abs(tot) > 1e-9 else None

        drivers = []
        for k, v in parts.items():
            drivers.append({"driver": k, "stbd": stb(v), "log_share": _f(v, 4)})
        for k, v in liq.items():
            drivers.append({"driver": "liquid:" + k, "stbd": stb(v), "log_share": _f(v, 4)})
        main = sorted([x for x in drivers if x["driver"] in ("liquid", "water_cut", "uptime", "other") and x["stbd"] is not None],
                      key=lambda x: abs(x["stbd"]))[::-1]
        sub = sorted([x for x in drivers if x["driver"].startswith("liquid:") and x["stbd"] is not None],
                     key=lambda x: abs(x["stbd"]))[::-1]
        return {
            "well": well, "source": "VFM daily (proqnoz) + sensorlar; dekompozisiya: oil = liquid x (1-WC) x uptime",
            "reference_window": [_d(A.index.min()), _d(A.index.max())], "recent_window": [_d(B.index.min()), _d(B.index.max())],
            "oil_ref_stbd": _f(oa, 0), "oil_recent_stbd": _f(ob, 0), "oil_change_stbd": _f(dq, 0),
            "oil_change_pct": _f(100 * dq / oa, 1) if oa else None,
            "liquid_ref": _f(la, 0), "liquid_recent": _f(lb, 0), "wc_ref_pct": _f(wa * 100, 1), "wc_recent_pct": _f(wb * 100, 1),
            "uptime_ref": _f(ua, 3), "uptime_recent": _f(ub, 3), "choke_ref_pct": _f(ca, 1), "choke_recent_pct": _f(cb, 1),
            "choke_dp_ref_psi": _f(dpa, 1), "choke_dp_recent_psi": _f(dpb, 1),
            "gas_lift_ref": _f(m(A, "gas_lift_rate_mscfd"), 0), "gas_lift_recent": _f(m(B, "gas_lift_rate_mscfd"), 0),
            "bhp_ref": _f(m(A, "dh_pressure_psia"), 0), "bhp_recent": _f(m(B, "dh_pressure_psia"), 0),
            "drivers_ranked": main, "liquid_drivers_ranked": sub,
            "choke_changes": self._choke_changes(well, A.index.min(), B.index.max()),
            "anomaly_events": [e for e in self.anomalies(well).get("events", [])
                               if e["end"] >= _d(A.index.min()) and e["start"] <= _d(B.index.max())],
            "shut_in_days_recent": int((B.uptime < 0.95).sum()),
        }

    def timeseries(self, well: str, start: str | None = None, end: str | None = None, freq: str = "W") -> dict:
        well = self._well(well)
        if well is None:
            return {"error": "naməlum quyu", "known_wells": self.wells}
        g = self.P[self.P.well_name == well].set_index("date").sort_index()
        g = g.loc[pd.Timestamp(start) if start else g.index.min(): pd.Timestamp(end) if end else g.index.max()]
        cols = ["oil_rate_stbd", "water_rate_stbd", "gas_rate_mscfd", "wc", "uptime", "choke_opening_pct",
                "whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd"]
        r = g[cols].resample(freq if freq in ("D", "W", "MS") else "W").mean()
        if len(r) > 60:
            r = g[cols].resample("MS").mean()
        r["wc"] = r.wc * 100
        return {"well": well, "freq": freq, "source": "VFM daily + sensorlar (orta)",
                "rows": [{"date": _d(i), **{k: _f(v, 1) for k, v in row.items()}} for i, row in r.iterrows()]}

    def pta_results(self, well: str | None = None) -> dict:
        ev = []
        for r in self.pta:
            if well and r.get("well_name") != self._well(well):
                continue
            ev.append({"well": r.get("well_name"), "t_shut": str(r.get("t_shut"))[:16],
                       "duration_hr": _f(r.get("duration_hr"), 1), "horner_slope_m_psi_cycle": _f(r.get("final_m_psi_cycle"), 2),
                       "k_md": _f(r.get("final_k_md"), 1), "skin": _f(r.get("final_skin"), 2),
                       "dP_skin_psi": _f(r.get("final_dP_skin_psi"), 0), "flow_efficiency": _f(r.get("flow_efficiency"), 2),
                       "q_oil_used_stbd": _f(r.get("q_oil_stbd"), 0), "p_star_psi": _f(r.get("p_star_psi"), 0)})
        return {"source": "PTA build-up (ölçülmüş təzyiq) + VFM sərfi; closed-boundary Laplace regressiya", "events": ev}

    def interventions(self) -> dict:
        return {"source": "PTA skin -> PI artımı (nəzəri, s_new = 0); prioritet sırası", "ranking": self._rank_records()}

    def anomalies(self, well: str | None = None) -> dict:
        E = self.E
        if len(E) and well:
            E = E[E.well == self._well(well)]
        evs = []
        for r in E.itertuples() if len(E) else []:
            evs.append({"well": r.well, "start": _d(r.start), "end": _d(r.end), "days": int(r.days), "type": r.type,
                        "severity": r.severity, "evidence": r.evidence})
        out = {"source": "anomaliya modulu (fizika qaydaları + robust baza xətti + Isolation Forest)", "events": evs,
               "validation": self.av}
        if self.A is not None and not well:
            fw = self.A[self.A.explained_by == "field_wide"]
            out["field_wide_days"] = sorted({_d(x) for x in fw.date})
        return out

    def model_card(self) -> dict:
        return {"vfm": self.card, "anomaly_validation": self.av}

    # ------------------------------------------------------------------------------------------ helpers
    def _well(self, w) -> str | None:
        if w is None:
            return None
        m = re.search(r"(\d{1,2})", str(w))
        if not m:
            return None
        c = f"GL-{int(m.group(1)):02d}"
        return c if c in self.wells else None

    def _rank_records(self) -> list[dict]:
        R = self.rank
        if R is None or not len(R):
            return []
        out = []
        for r in R.itertuples():
            d = r._asdict()
            out.append({"priority": d.get("priority"), "well": d.get("well_name"), "status": d.get("status"),
                        "confidence": d.get("confidence"), "skin": _f(d.get("skin"), 2), "k_md": _f(d.get("k_md"), 1),
                        "q_oil_stbd": _f(d.get("q_oil_stbd"), 0), "dq_stbd": _f(d.get("dq_stbd"), 0),
                        "dq_low": _f(d.get("dq_low"), 0), "dq_high": _f(d.get("dq_high"), 0),
                        "gain_pct": _f(d.get("gain_%"), 1)})
        return out

    def _choke_changes(self, well, a, b) -> list[dict]:
        if self.choke is None:
            return []
        c = self.choke[self.choke.well_name == well].copy()
        c["prev"] = c.choke_opening_pct.shift()
        c = c[(c.timestamp >= pd.Timestamp(a)) & (c.timestamp <= pd.Timestamp(b) + pd.Timedelta(days=1)) & c.prev.notna()]
        return [{"time": str(r.timestamp)[:16], "from_pct": _f(r.prev, 1), "to_pct": _f(r.choke_opening_pct, 1)}
                for r in c.itertuples()]

    # ------------------------------------------------------------------------------------------ tool registry
    def call(self, name: str, args: dict | None = None) -> dict:
        args = args or {}
        fn = {"field_overview": self.field_overview, "well_summary": self.well_summary,
              "explain_change": self.explain_change, "timeseries": self.timeseries, "pta_results": self.pta_results,
              "interventions": self.interventions, "anomalies": self.anomalies, "model_card": self.model_card}.get(name)
        if fn is None:
            return {"error": f"naməlum alət: {name}"}
        try:
            return fn(**args)
        except TypeError as ex:
            return {"error": f"yanlış arqument: {ex}"}

    def snapshot(self) -> dict:
        """Everything the desktop / mobile apps need for offline Q&A, in one JSON."""
        return {"field": self.field_overview(), "wells": {w: self.well_summary(w) for w in self.wells},
                "changes": {w: self.explain_change(w) for w in self.wells}, "interventions": self.interventions(),
                "anomalies": self.anomalies(), "pta": self.pta_results()}


TOOL_SPECS = [
    {"name": "field_overview", "description": "Field totals for the last day (oil/water/gas from VFM), flowing wells, top producers, open anomaly events, best intervention and VFM validation metrics. Use for any field-level question.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "well_summary", "description": "Everything known about one well: last-day VFM rates with P10-P90, 30/90-day averages, water cut, choke, lift gas, pressures, last well test, PTA results, intervention ranking entry, anomaly events, recent choke changes.",
     "input_schema": {"type": "object", "properties": {"well": {"type": "string", "description": "Well id, e.g. GL-04"}}, "required": ["well"], "additionalProperties": False}},
    {"name": "explain_change", "description": "Explains why a well's oil rate changed between a reference window and a recent window. Returns the exact decomposition oil = liquid x (1-WC) x uptime in STB/d, the liquid split into choke opening and choke dP (VFM physics), choke changes and anomaly events in the period. Without dates it finds the largest sustained drop in the last 120 days. Use for every 'why did X drop / rise' question.",
     "input_schema": {"type": "object", "properties": {"well": {"type": "string"},
                                                       "start": {"type": "string", "description": "recent window start YYYY-MM-DD (optional)"},
                                                       "end": {"type": "string", "description": "recent window end YYYY-MM-DD (optional)"},
                                                       "ref_start": {"type": "string", "description": "reference window start (optional)"},
                                                       "ref_end": {"type": "string", "description": "reference window end (optional)"}},
                      "required": ["well"], "additionalProperties": False}},
    {"name": "timeseries", "description": "Weekly (default) or monthly averages of VFM rates, water cut and sensors for one well; at most 60 rows.",
     "input_schema": {"type": "object", "properties": {"well": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
                                                       "freq": {"type": "string", "enum": ["D", "W", "MS"]}},
                      "required": ["well"], "additionalProperties": False}},
    {"name": "pta_results", "description": "Pressure build-up interpretation results: Horner slope m, permeability k, skin, dP skin, flow efficiency, for one well or all.",
     "input_schema": {"type": "object", "properties": {"well": {"type": "string"}}, "additionalProperties": False}},
    {"name": "interventions", "description": "Stimulation candidate ranking from PTA skin: theoretical oil gain dq (STB/d) with low/high range, confidence, priority.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "anomalies", "description": "Unexplained anomaly events (sensor faults, water-cut jumps, pressure and lift-gas anomalies) with evidence, for one well or all, plus detector validation metrics.",
     "input_schema": {"type": "object", "properties": {"well": {"type": "string"}}, "additionalProperties": False}},
    {"name": "model_card", "description": "VFM model architecture, inputs, validation results and limitations; anomaly detector validation.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


# ------------------------------------------------------------------------------------------------ offline answers
_DRV_AZ = {"liquid": "maye debiti", "water_cut": "su payı (WC)", "uptime": "işləmə vaxtı (uptime)", "other": "dayanma günləri",
           "liquid:choke": "ştuser açıqlığı", "liquid:choke_dp": "ştuserdə təzyiq fərqi (WHP - xətt təzyiqi)",
           "liquid:residual_model": "modelin digər hissəsi (qazlift / quyudibi təzyiq)"}
_DRV_EN = {"liquid": "liquid rate", "water_cut": "water cut", "uptime": "uptime", "other": "shut-in days",
           "liquid:choke": "choke opening", "liquid:choke_dp": "choke differential pressure",
           "liquid:residual_model": "other model terms (lift gas / bottom-hole pressure)"}
_TYPE_EN = {"sensor_fault": "sensor fault", "water_cut_jump": "water-cut jump", "pressure_anomaly": "pressure anomaly",
            "lift_gas_anomaly": "lift-gas anomaly"}


def _lang(q: str) -> str:
    ql = q.lower()
    az = re.search(r"[əğıöüşç]|niyə|nə |hansı|quyu|necə|düş|azal|artı|göstər|mədən", ql)
    en = re.search(r"\b(why|what|which|how|show|well|drop|field|best)\b", ql)
    return "en" if en and not az else "az"


def offline_answer(K: Knowledge, question: str) -> dict:
    """Deterministic, grounded answer without internet. Same tools as the LLM, templated text."""
    q = question.lower()
    lang = _lang(question)
    az = lang == "az"
    well = None
    m = re.search(r"gl[\s\-_]?0?(\d{1,2})", q) or re.search(r"(\d{1,2})\s*(?:-?(?:ci|cü|cu|cı|nömrəli)\s*)?quyu", q)
    if m:
        well = K._well(m.group(1))
    used = []

    def T(name, **a):
        used.append(name)
        return K.call(name, a)

    intent = "summary"
    if re.search(r"düş|azal|niyə|why|drop|declin|fall|decreas|aşağı|dəyiş|chang|art|increas|rise", q):
        intent = "change"
    if re.search(r"model|dəqiq|accura|mape|valid", q):
        intent = "model"
    if re.search(r"skin|pta|keçiricilik|permeab|horner|build", q):
        intent = "pta"
    if re.search(r"stim|tövsiyə|prioritet|candidate|intervention|workover|ən yaxşı|best|qazanc|gain|əlavə hasilat|müdaxilə", q):
        intent = "interventions"
    if re.search(r"su payı|sulaşma|water.?cut|\bwc\b", q) and well:
        intent = "wc"
    if re.search(r"anomal|nasaz|sensor|alert|xəbərdar|qəza|fault|xəta|problem", q):
        intent = "anomaly"
    if well is None and intent in ("summary", "change"):
        intent = "field" if intent == "summary" else "field_change"

    L = []
    if intent == "wc":
        r = T("well_summary", well=well)
        c = T("explain_change", well=well)
        a, b = r.get("wc_avg_prev90d_pct"), r.get("wc_avg_last30d_pct")
        if az:
            L.append(f"**{well}**: su payı son 30 gündə orta **{b} %**, əvvəlki 90 gündə {a} %, son gün {r['water_cut_pct']} %.")
            trend = "artır" if (a is not None and b is not None and b - a > 1) else ("azalır" if (a is not None and b is not None and a - b > 1) else "sabitdir")
            L.append(f"Trend: {trend} ({(b - a):+.1f} faiz bəndi)." if a is not None and b is not None else "")
            wcd = [x for x in c.get("drivers_ranked", []) if x["driver"] == "water_cut"]
            if wcd and wcd[0]["stbd"] is not None:
                L.append(f"Son dəyişiklikdə su payının neftə təsiri: {wcd[0]['stbd']:+.0f} STB/d.")
            wj = [e for e in r["anomaly_events"] if e["type"] == "water_cut_jump"]
            L.append("Sensorlarda su sıçrayışı əlaməti (lülədə sütunun ağırlaşması) " + ("var: " + ", ".join(e["start"] for e in wj) if wj else "yoxdur."))
            L.append("Qeyd: test arasındakı WC trendi VFM modelindən gəlir (son 3 test ortası + Theil-Sen meyli).")
        else:
            L.append(f"**{well}**: water cut averaged **{b} %** over the last 30 days vs {a} % in the previous 90 days.")
    elif intent == "change" and well:
        r = T("explain_change", well=well)
        if "error" in r:
            L.append(r["error"])
        else:
            D = _DRV_AZ if az else _DRV_EN
            sign = r["oil_change_stbd"] or 0
            if az:
                L.append(f"**{well}**: neft debiti {r['reference_window'][0]}–{r['reference_window'][1]} dövründə orta "
                         f"**{r['oil_ref_stbd']:.0f} STB/d**, {r['recent_window'][0]}–{r['recent_window'][1]} dövründə "
                         f"**{r['oil_recent_stbd']:.0f} STB/d** ({r['oil_change_stbd']:+.0f} STB/d, {r['oil_change_pct']:+.1f} %).")
                L.append("Səbəblər (oil = maye × (1 − WC) × uptime, STB/d payı):")
            else:
                L.append(f"**{well}**: oil averaged **{r['oil_ref_stbd']:.0f} STB/d** in {r['reference_window'][0]}–{r['reference_window'][1]} "
                         f"and **{r['oil_recent_stbd']:.0f} STB/d** in {r['recent_window'][0]}–{r['recent_window'][1]} "
                         f"({r['oil_change_stbd']:+.0f} STB/d, {r['oil_change_pct']:+.1f} %).")
                L.append("Drivers (oil = liquid × (1 − WC) × uptime, share in STB/d):")
            for x in r["drivers_ranked"]:
                if x["stbd"] is not None and abs(x["stbd"]) >= max(3, 0.05 * abs(sign)):
                    L.append(f"- {D[x['driver']]}: {x['stbd']:+.0f} STB/d")
            sub = [x for x in r["liquid_drivers_ranked"] if x["stbd"] is not None and abs(x["stbd"]) >= max(3, 0.05 * abs(sign))]
            if sub:
                L.append(("  Maye debitinin içində: " if az else "  Inside the liquid change: ") +
                         "; ".join(f"{D[x['driver']]} {x['stbd']:+.0f}" for x in sub))
            if r["choke_changes"]:
                L.append(("Ştuser dəyişiklikləri: " if az else "Choke changes: ") +
                         ", ".join(f"{c['time'][:10]} {c['from_pct']}→{c['to_pct']} %" for c in r["choke_changes"]))
            L.append((f"WC {r['wc_ref_pct']} → {r['wc_recent_pct']} %, ştuser {r['choke_ref_pct']} → {r['choke_recent_pct']} %, "
                      f"qazlift {r['gas_lift_ref']:.0f} → {r['gas_lift_recent']:.0f} Mscf/d, quyudibi {r['bhp_ref']:.0f} → {r['bhp_recent']:.0f} psi.")
                     if az else
                     (f"WC {r['wc_ref_pct']} → {r['wc_recent_pct']} %, choke {r['choke_ref_pct']} → {r['choke_recent_pct']} %, "
                      f"lift gas {r['gas_lift_ref']:.0f} → {r['gas_lift_recent']:.0f} Mscf/d, BHP {r['bhp_ref']:.0f} → {r['bhp_recent']:.0f} psi."))
            if r["anomaly_events"]:
                L.append(("Bu dövrdə anomaliya: " if az else "Anomalies in this period: ") +
                         "; ".join(f"{e['start']} {LABEL_AZ.get(e['type'], e['type']) if az else e['type']}" for e in r["anomaly_events"]))
            top = r["drivers_ranked"][0]["driver"] if r["drivers_ranked"] else ""
            sub0 = sub[0]["driver"] if sub else ""
            if az:
                if top == "liquid" and sub0 == "liquid:choke":
                    L.append("**Nəticə:** düşüş əsasən ştuserin bağlanmasından gəlir (əməliyyat qərarı, lay problemi deyil). "
                             "Ştuseri əvvəlki səviyyəyə qaytarmaq itən debiti bərpa edər, əgər bağlanma qum/su səbəbilə edilməyibsə.")
                elif top == "water_cut":
                    L.append("**Nəticə:** əsas səbəb su payının artmasıdır. Su konusu / su gəlməsi yoxlanılmalı; qazlift optimallaşdırma səhifəsində yeni WC ilə rejimi yoxlayın.")
                elif top in ("uptime", "other"):
                    L.append("**Nəticə:** itki dayanma / natamam işləmə günlərindən gəlir, quyunun potensialı dəyişməyib.")
                else:
                    L.append("**Nəticə:** maye debiti təzyiq rejimi ilə birlikdə dəyişib; PTA skin və quyudibi təzyiq trendini yoxlayın.")
            else:
                if top == "liquid" and sub0 == "liquid:choke":
                    L.append("**Conclusion:** the drop comes mainly from the choke being cut back (an operating decision, not a reservoir problem).")
                elif top == "water_cut":
                    L.append("**Conclusion:** the main driver is rising water cut; check for water breakthrough / coning.")
                elif top in ("uptime", "other"):
                    L.append("**Conclusion:** the loss comes from downtime; well potential is unchanged.")
                else:
                    L.append("**Conclusion:** liquid rate changed with the pressure regime; check PTA skin and BHP trend.")
    elif intent == "pta":
        r = T("pta_results", well=well) if well else T("pta_results")
        ev = r["events"]
        if not ev:
            L.append(f"{well or ''}: bu quyu üçün təzyiq bərpası (build-up) hadisəsi yoxdur, PTA nəticəsi yoxdur." if az
                     else f"{well or ''}: no build-up event, so no PTA result.")
        for e in ev:
            L.append((f"**{e['well']}** ({e['t_shut'][:10]}): k = {e['k_md']} md, skin = {e['skin']:+.2f}, ΔP skin = {e['dP_skin_psi']} psi, "
                      f"Horner m = {e['horner_slope_m_psi_cycle']} psi/dövr, axın səmərəliliyi {e['flow_efficiency']}.") if az else
                     (f"**{e['well']}** ({e['t_shut'][:10]}): k = {e['k_md']} md, skin = {e['skin']:+.2f}, dP skin = {e['dP_skin_psi']} psi, "
                      f"Horner m = {e['horner_slope_m_psi_cycle']} psi/cycle, flow efficiency {e['flow_efficiency']}."))
        if ev:
            hi = [e for e in ev if (e["skin"] or 0) > 3]
            if hi:
                L.append(("Skin > 3 olan quyular stimulyasiya namizədidir: " if az else "Skin > 3 = stimulation candidates: ") +
                         ", ".join(e["well"] for e in hi))
    elif intent == "anomaly":
        r = T("anomalies", well=well) if well else T("anomalies")
        ev = r["events"]
        v = r.get("validation") or {}
        if not ev:
            L.append((f"{well or 'Mədən'}: izah olunmamış anomaliya yoxdur." if az else f"{well or 'Field'}: no unexplained anomalies."))
        else:
            L.append((f"{well or 'Mədən'}: {len(ev)} izah olunmamış anomaliya hadisəsi (son əvvəldə):" if az
                      else f"{well or 'Field'}: {len(ev)} unexplained anomaly events (latest first):"))
            for e in ev[:6]:
                t = LABEL_AZ.get(e["type"], e["type"]) if az else _TYPE_EN.get(e["type"], e["type"])
                sv = {"high": "yüksək", "medium": "orta"}.get(e["severity"], e["severity"]) if az else e["severity"]
                L.append(f"- **{e['well']}** {e['start']} ({e['days']} {'gün' if az else 'd'}): {t}, {sv}. {e['evidence']}")
        if v:
            L.append((f"Detektorun yoxlanışı: süni əlavə edilmiş nasazlıqların {v.get('overall_recall', 0) * 100:.0f} %-i tutulur; "
                      f"real datada quyu-günlərin {v.get('alert_rate_pct_of_well_days')} %-i xəbərdarlıqdır "
                      f"({v.get('alert_events_real_data')} hadisə). Sınaq, ştuser dəyişikliyi və dayanmalar avtomatik izah olunur.") if az else
                     (f"Detector check: {v.get('overall_recall', 0) * 100:.0f} % of injected faults caught; "
                      f"{v.get('alert_rate_pct_of_well_days')} % of real well-days raise an alert ({v.get('alert_events_real_data')} events)."))
    elif intent == "interventions":
        r = T("interventions")
        rk = [x for x in r["ranking"] if x.get("dq_stbd")]
        if not rk:
            L.append("Stimulyasiya namizədi hesablanmayıb (PTA nəticəsi yoxdur)." if az else "No ranked candidates (no PTA results).")
        for x in rk[:5]:
            L.append((f"{int(x['priority'])}. **{x['well']}**: skin {x['skin']:+.1f}, nəzəri əlavə hasilat **+{x['dq_stbd']:.0f} STB/d** "
                      f"({x['dq_low']:.0f}–{x['dq_high']:.0f}), etibar: {x['confidence']}.") if az else
                     (f"{int(x['priority'])}. **{x['well']}**: skin {x['skin']:+.1f}, theoretical gain **+{x['dq_stbd']:.0f} STB/d** "
                      f"({x['dq_low']:.0f}–{x['dq_high']:.0f}), confidence {x['confidence']}."))
        if rk:
            tot = sum(x["dq_stbd"] for x in rk)
            L.append((f"Cəmi nəzəri potensial: +{tot:.0f} STB/d (skin = 0 fərziyyəsi ilə; real qazanc adətən daha azdır)." if az else
                      f"Total theoretical upside: +{tot:.0f} STB/d (assuming skin = 0; real gains are usually lower)."))
    elif intent == "model":
        r = T("model_card")
        v = r["vfm"].get("historical_forward_metrics", {})
        L.append((f"VFM: {r['vfm'].get('architecture', '')}. İrəli-zaman (forward) yoxlamada neft MAPE {v.get('oil_MAPE_%')} %, "
                  f"su {v.get('water_MAPE_%')} %, qaz {v.get('gas_MAPE_%')} %.") if az else
                 (f"VFM: {r['vfm'].get('architecture', '')}. Forward validation MAPE: oil {v.get('oil_MAPE_%')} %, "
                  f"water {v.get('water_MAPE_%')} %, gas {v.get('gas_MAPE_%')} %."))
    elif intent in ("field", "field_change"):
        r = T("field_overview")
        if az:
            L.append(f"**Mədən, {r['last_day']}**: neft **{r['field_oil_stbd']:.0f} STB/d**, su {r['field_water_stbd']:.0f} STB/d, "
                     f"qaz {r['field_gas_mscfd']:.0f} Mscf/d; {r['wells_flowing']}/{r['wells_total']} quyu işləyir.")
            if r.get("field_oil_30d_ago_stbd_weekavg"):
                L.append(f"30 gün əvvəl (həftəlik orta): {r['field_oil_30d_ago_stbd_weekavg']:.0f} STB/d.")
            L.append("Ən böyük hasilatçılar: " + ", ".join(f"{t['well']} {t['oil_stbd']:.0f}" for t in r["top_producers"]))
            L.append(f"Son 30 gündə açıq anomaliya hadisəsi: {r['open_anomaly_events_30d']}.")
            if r["best_intervention"]:
                b = r["best_intervention"][0]
                L.append(f"Ən yaxşı müdaxilə: {b['well']} (skin {b['skin']:+.1f}, +{b['dq_stbd']:.0f} STB/d nəzəri).")
        else:
            L.append(f"**Field, {r['last_day']}**: oil **{r['field_oil_stbd']:.0f} STB/d**, water {r['field_water_stbd']:.0f} STB/d, "
                     f"gas {r['field_gas_mscfd']:.0f} Mscf/d; {r['wells_flowing']}/{r['wells_total']} wells flowing.")
            L.append("Top producers: " + ", ".join(f"{t['well']} {t['oil_stbd']:.0f}" for t in r["top_producers"]))
            if r["best_intervention"]:
                b = r["best_intervention"][0]
                L.append(f"Best intervention: {b['well']} (skin {b['skin']:+.1f}, +{b['dq_stbd']:.0f} STB/d theoretical).")
        if intent == "field_change":
            L.append("Konkret quyu üçün səbəb soruşun, məsələn: “GL-04 niyə düşüb?”" if az else
                     "Ask about a specific well for drivers, e.g. “Why did GL-04 drop?”")
    else:  # well summary
        r = T("well_summary", well=well)
        if "error" in r:
            L.append(r["error"])
        else:
            if az:
                L.append(f"**{well}, {r['last_day']}**: neft **{r['oil_stbd']:.0f} STB/d** (P10–P90: {r['oil_p10']:.0f}–{r['oil_p90']:.0f}), "
                         f"su {r['water_stbd']:.0f} STB/d, qaz {r['gas_mscfd']:.0f} Mscf/d, WC {r['water_cut_pct']} %.")
                L.append(f"Son 30 gün orta neft {r['oil_avg_last30d']:.0f} STB/d, əvvəlki 90 gün {r['oil_avg_prev90d']:.0f} STB/d. "
                         f"Ştuser {r['choke_pct']} %, qazlift {r['gas_lift_mscfd']:.0f} Mscf/d, WHP {r['whp_psia']} psi, quyudibi {r['bhp_psia']:.0f} psi.")
            else:
                L.append(f"**{well}, {r['last_day']}**: oil **{r['oil_stbd']:.0f} STB/d** (P10–P90: {r['oil_p10']:.0f}–{r['oil_p90']:.0f}), "
                         f"water {r['water_stbd']:.0f} STB/d, gas {r['gas_mscfd']:.0f} Mscf/d, WC {r['water_cut_pct']} %.")
            for e in r["pta"]:
                L.append(f"PTA: k {e['k_md']} md, skin {e['skin']:+.2f}, ΔP skin {e['dP_skin_psi']} psi.")
            for x in r["intervention"]:
                if x.get("dq_stbd"):
                    L.append((f"Stimulyasiya potensialı: +{x['dq_stbd']:.0f} STB/d (prioritet {x['priority']})." if az else
                              f"Stimulation upside: +{x['dq_stbd']:.0f} STB/d (priority {x['priority']})."))
            if r["anomaly_events"]:
                e = r["anomaly_events"][0]
                L.append((f"Son anomaliya: {e['start']} {LABEL_AZ.get(e['type'], e['type'])} ({e['evidence']})." if az else
                          f"Latest anomaly: {e['start']} {_TYPE_EN.get(e['type'], e['type'])}."))
    L.append(("\n_Mənbə: " if az else "\n_Source: ") + ", ".join(used) + (" (oflayn rejim, CaspianX hesablamaları)_" if az else
                                                                          " (offline mode, CaspianX computations)_"))
    return {"answer": "\n\n".join(x for x in L if x), "tools_used": used,
            "mode": "offline", "lang": lang, "well": well, "intent": intent}


DEMO_QUESTIONS = [
    "GL-04 niyə düşüb?",
    "Hansı quyuya stimulyasiya etməliyik?",
    "Mədəndə hazırda vəziyyət necədir?",
    "Hansı quyularda anomaliya var?",
    "GL-01-in skin və keçiriciliyi nədir?",
    "GL-07 haqqında qısa məlumat ver",
    "VFM modeli nə qədər dəqiqdir?",
    "Why did GL-04 drop?",
]
