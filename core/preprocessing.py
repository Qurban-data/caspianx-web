"""
Data preparation for the SOCAR gas-lift dataset.

Units (oilfield): pressure psia, rates STB/d (liquid) and Mscf/d (gas), choke % open, time in hours/days.

Steps
-----
1. ``qc_tests``             well-test QC: sign-flip repair, unit repair (sm3/d -> Mscf/d), identity checks
                            q_liq = q_o + q_w,  WC = q_w / q_liq,  GOR = 1000 q_g / q_o [scf/STB].
2. ``daily_features_from_hf`` per-minute sensors -> daily means over *flowing* minutes, uptime.
                            uptime = flowing minutes / observed minutes (logger gaps are not shut-ins).
3. ``prepare_daily``        attach time-weighted daily choke, fill sensor gaps per well, flag imputed rows.
4. ``detect_events_hf`` / ``event_series_hf``  shut-in (build-up) events for PTA.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

NULL_SENTINEL = -900.0          # SOCAR files use -999.25 for missing values
SM3_TO_MSCF = 1 / 28.3168       # 1 sm3 = 35.3147 scf  ->  sm3/d / 28.3168 = Mscf/d
FEAT = ["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "dh_temperature_degf",
        "annulus_pressure_psia", "gas_lift_rate_mscfd"]
HF_COLS = ["timestamp", "well_name"] + FEAT
TEST_COLS = ["well_name", "test_start", "test_end", "whp_psia", "whp_downstream_psia", "dh_pressure_psia",
             "gas_lift_rate_mscfd", "oil_rate_stbd", "water_rate_stbd", "gas_rate_mscfd"]

# column sets that identify each input file (used by the UI and by the end-to-end test)
SCHEMA: dict[str, list[str]] = {
    # per-minute file: temperature / annulus pressure are optional sensors (missing -> NaN, never invented)
    "high_freq": ["timestamp", "well_name", "whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd"],
    "daily_features": ["well_name", "date", "whp_psia", "whp_downstream_psia", "dh_pressure_psia",
                       "gas_lift_rate_mscfd", "uptime"],
    "well_tests": TEST_COLS,
    "choke_events": ["timestamp", "well_name", "choke_opening_pct"],
    "reservoir_pvt": ["well_name", "net_pay_thickness_ft", "porosity_frac", "total_compressibility_1_psi",
                      "wellbore_radius_ft", "oil_fvf_rb_stb", "oil_viscosity_cp"],
    # completion: recognised by well_name + any cumulative-oil column (its reference date may differ) or
    # typical completion columns; the cumulative column is optional for PTA (see core.pta.np_coverage)
    "well_completion": ["well_name"],
}
COMPLETION_HINTS = {"gauge_tvd_ft", "tubing_id_in", "solution_gor_at_pb_scf_stb", "first_production_date",
                    "choke_max_size_64ths", "gl_valve_tvd_ft"}


def _is_completion(c: set) -> bool:
    return "well_name" in c and (any(x.startswith("cum_oil") for x in c) or len(c & COMPLETION_HINTS) >= 2)


def closest_kind(cols: Iterable[str]) -> tuple[str | None, list[str]]:
    """Input kind whose required columns overlap most with ``cols`` and the required columns that are missing."""
    c = {str(x).strip().lower() for x in cols}
    kinds = [k for k in SCHEMA if len(SCHEMA[k]) >= 3]          # completion (only well_name) is matched by hints
    best = max(kinds, key=lambda k: (len(set(SCHEMA[k]) & c) / len(SCHEMA[k]), len(SCHEMA[k])))
    return best, [x for x in SCHEMA[best] if x not in c]


def classify_columns(cols: Iterable[str]) -> str | None:
    """Return the input-file kind whose required columns are all present (most specific first)."""
    c = {str(x).strip().lower() for x in cols}
    for kind in ["high_freq", "well_tests", "daily_features", "reservoir_pvt", "well_completion", "choke_events"]:
        if kind == "well_completion":
            if _is_completion(c):
                return kind
        elif set(SCHEMA[kind]) <= c:
            return kind
    return None


def schema_report(df: pd.DataFrame, kind: str) -> dict:
    """Missing required columns, null share and row count for one input table."""
    req = SCHEMA[kind]
    miss = [c for c in req if c not in df.columns]
    num = df[[c for c in req if c in df.columns]].select_dtypes("number")
    nulls = float(((num <= NULL_SENTINEL) | num.isna()).mean().mean()) if num.size else 0.0
    return {"kind": kind, "rows": int(len(df)), "missing": miss, "null_share": nulls, "ok": not miss}


# ------------------------------------------------------------------ well tests
def qc_tests(t: pd.DataFrame, choke: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Clean the well-test table. Returns (clean tests, QC log).

    * Negative physical values are sign flips -> |x| (confirmed by the identities below).
    * Formation gas must satisfy q_g [Mscf/d] = q_o * GOR / 1000; a well whose median ratio is ~28.3
      reports gas in sm3/d and is divided by 28.3168.
    * Adds q_liq, WC (fraction), GOR (scf/STB), total GLR (scf/STB) and test mid-time; attaches the
      choke opening in force at test mid-time.
    """
    t = t.copy()
    t.columns = [c.strip().lower() for c in t.columns]
    t["test_start"] = pd.to_datetime(t["test_start"])
    t["test_end"] = pd.to_datetime(t["test_end"])
    log = []
    for c in t.select_dtypes("number").columns:
        neg = t[c] < 0
        for i in t.index[neg]:
            log.append({"well": t.at[i, "well_name"], "test_start": t.at[i, "test_start"], "column": c,
                        "issue": "negative value", "old": t.at[i, c], "new": abs(t.at[i, c])})
        t.loc[neg, c] = t.loc[neg, c].abs()
    if "gor_scf_stb" in t:
        ratio = t["gas_rate_mscfd"] / (t["oil_rate_stbd"] * t["gor_scf_stb"] / 1000)
        for w, r in ratio.groupby(t["well_name"]):
            f = float(np.median(r))
            if abs(f - 28.3168) < 2:
                idx = t.index[t["well_name"] == w]
                for c in [x for x in ["gas_rate_mscfd", "total_gas_rate_mscfd"] if x in t]:
                    log.append({"well": w, "test_start": "all", "column": c,
                                "issue": f"unit sm3/d -> Mscf/d (x{f:.2f})",
                                "old": t.loc[idx, c].median(), "new": t.loc[idx, c].median() * SM3_TO_MSCF})
                    t.loc[idx, c] = t.loc[idx, c] * SM3_TO_MSCF
    t["liquid_rate_stbd"] = t["oil_rate_stbd"] + t["water_rate_stbd"]
    if "total_gas_rate_mscfd" in t:
        t["chk_total_gas"] = (t["gas_rate_mscfd"] + t["gas_lift_rate_mscfd"] - t["total_gas_rate_mscfd"]).abs()
    t["wc"] = t["water_rate_stbd"] / t["liquid_rate_stbd"]
    t["gor"] = t["gas_rate_mscfd"] * 1000 / t["oil_rate_stbd"]
    t["glr_total"] = (t["gas_rate_mscfd"] + t["gas_lift_rate_mscfd"]) * 1000 / t["liquid_rate_stbd"]
    t["test_mid"] = t["test_start"] + (t["test_end"] - t["test_start"]) / 2
    if choke is not None:
        ch = choke.copy()
        ch["timestamp"] = pd.to_datetime(ch["timestamp"])
        t = pd.merge_asof(t.sort_values("test_mid"),
                          ch.sort_values("timestamp").rename(columns={"timestamp": "test_mid"}),
                          on="test_mid", by="well_name", direction="backward")
    return t.sort_values(["well_name", "test_mid"]).reset_index(drop=True), pd.DataFrame(log)


# ------------------------------------------------------------------ high-frequency -> daily
def flowing_mask(df: pd.DataFrame) -> pd.Series:
    """A minute is *flowing* when gas lift is on (>50 Mscf/d) and the choke has a real dP (>=3 psi)."""
    gl = df["gas_lift_rate_mscfd"]
    whp, ds = df["whp_psia"], df["whp_downstream_psia"]
    lift_on = (gl > 50) | gl.isna()
    no_dp = whp.notna() & ds.notna() & ((whp - ds) < 3)
    return lift_on & ~no_dp


def daily_features_from_hf(path_or_df: str | pd.DataFrame, chunksize: int = 2_000_000) -> pd.DataFrame:
    """Aggregate the per-minute file (257 MB) to daily features in constant memory.

    Sensor means are taken over flowing minutes only (shut-in minutes would bias WHP/BHP);
    uptime = flowing / observed minutes; logger_gap_min = 1440 - observed minutes.
    """
    it = [path_or_df] if isinstance(path_or_df, pd.DataFrame) else pd.read_csv(path_or_df, chunksize=chunksize)
    parts = []
    tail_keys: set = set()                      # (well, timestamp) keys of the previous chunk's tail
    for ch in it:
        ch = ch.copy()
        ch.columns = [c.strip().lower() for c in ch.columns]
        # duplicated logger minutes would inflate the observed-minute count and bias uptime:
        # drop them inside the chunk and across the chunk boundary
        ch["timestamp"] = ch["timestamp"].astype(str).str.strip()
        ch["well_name"] = ch["well_name"].astype(str).str.strip().str.upper()   # same convention as core.ingest
        ch = ch.drop_duplicates(["well_name", "timestamp"], keep="first")
        if tail_keys:
            ch = ch[[k not in tail_keys for k in zip(ch["well_name"], ch["timestamp"])]]
        tail_keys = set(zip(ch["well_name"].tail(20000), ch["timestamp"].tail(20000)))
        for c in FEAT:
            if c not in ch:                     # optional sensor absent in this file -> missing, never invented
                ch[c] = np.nan
            ch[c] = pd.to_numeric(ch[c], errors="coerce").astype("float64")
            ch[c] = _clean_sensor(ch[c])
        ch["date"] = pd.to_datetime(ch["timestamp"].astype(str).str.slice(0, 10))
        f = flowing_mask(ch)
        g = ch.assign(nf=f.astype(int), n=1)
        flow = ch[f].groupby(["well_name", "date"])[FEAT].agg(["sum", "count"])
        flow.columns = [f"{a}_{b}" for a, b in flow.columns]
        cnt = g.groupby(["well_name", "date"])[["n", "nf"]].sum()
        parts.append(cnt.join(flow, how="left"))
    P = pd.concat(parts).fillna(0).groupby(level=[0, 1]).sum()
    out = pd.DataFrame(index=P.index)
    out["n"], out["nf"] = P["n"], P["nf"]
    for c in FEAT:
        out[c] = P[f"{c}_sum"] / P[f"{c}_count"].replace(0, np.nan)
    out["uptime"] = (out["nf"] / out["n"].clip(lower=1)).clip(0, 1)
    out["logger_gap_min"] = (1440 - out["n"]).clip(lower=0)
    return out.reset_index()


def daily_choke(choke: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Time-weighted daily mean of the choke step function (hourly grid), averaged while OPEN (>0.5 %).
    Choke = 0 periods are shut-ins and are already represented by ``uptime``."""
    ch = choke.copy()
    ch["timestamp"] = pd.to_datetime(ch["timestamp"])
    rows = []
    for w, g in daily.groupby("well_name"):
        c = ch[ch["well_name"] == w].sort_values("timestamp")
        if c.empty:
            continue
        grid = pd.DataFrame({"timestamp": pd.date_range(g["date"].min(), g["date"].max() + pd.Timedelta(hours=23),
                                                        freq="h")})
        grid = pd.merge_asof(grid, c[["timestamp", "choke_opening_pct"]], on="timestamp", direction="backward")
        grid["choke_opening_pct"] = grid["choke_opening_pct"].fillna(c["choke_opening_pct"].iloc[0])
        grid["date"] = grid["timestamp"].dt.normalize()
        d = grid.groupby("date")["choke_opening_pct"].agg(
            lambda x: x[x > 0.5].mean() if (x > 0.5).any() else 0.0).reset_index()
        d["well_name"] = w
        rows.append(d)
    return daily.merge(pd.concat(rows), on=["well_name", "date"], how="left")


def prepare_daily(daily: pd.DataFrame, choke: pd.DataFrame) -> pd.DataFrame:
    """Attach choke, forward/back-fill flowing features per well on sensor-gap days and flag them ``imputed``."""
    d = daily.copy()
    d.columns = [c.strip().lower() for c in d.columns]
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["well_name", "date"]).reset_index(drop=True)
    d.loc[d["uptime"] < 0.01, "uptime"] = 0.0
    if "choke_opening_pct" not in d:
        d = daily_choke(choke, d)
    key = ["whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd", "choke_opening_pct"]
    d["imputed"] = d[key].isna().any(axis=1)
    d[key] = d.groupby("well_name")[key].transform(lambda s: s.ffill().bfill())
    return d


# ------------------------------------------------------------------ shut-in events (per-minute data)
_HF_CACHE: dict = {}


def _clean_sensor(x: pd.Series) -> pd.Series:
    """Null codes (-999.25, -9999) -> NaN; any other negative physical reading is a sign flip -> |x|
    (same rule as core.ingest and qc_tests, e.g. GL-09 gas lift -1010.5 -> 1010.5)."""
    x = x.astype("float64")
    sent = np.isclose(x, -999.25, atol=1e-3) | np.isclose(x, -9999.0, atol=1e-3)
    return x.mask(sent).abs()


def _hf_min(path_or_df: str | pd.DataFrame) -> pd.DataFrame:
    """Minimal per-minute frame for shut-in detection (cached): null codes -> NaN, sign flips -> |x|, upper-case wells, de-duplicated."""
    key = id(path_or_df) if isinstance(path_or_df, pd.DataFrame) else path_or_df
    if key not in _HF_CACHE:
        cols = ["timestamp", "well_name", "whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd"]
        h = path_or_df[cols].copy() if isinstance(path_or_df, pd.DataFrame) else pd.read_csv(path_or_df, usecols=cols)
        h["timestamp"] = pd.to_datetime(h["timestamp"])
        h["well_name"] = h["well_name"].astype(str).str.strip().str.upper().astype("category")  # as core.ingest
        for c in cols[2:]:
            h[c] = _clean_sensor(pd.to_numeric(h[c], errors="coerce"))
        _HF_CACHE[key] = h.drop_duplicates(["well_name", "timestamp"]).sort_values(["well_name", "timestamp"])
    return _HF_CACHE[key]


def detect_events_hf(path_or_df: str | pd.DataFrame, min_hours: float = 2.0, rise_psi: float = 15.0) -> pd.DataFrame:
    """Shut-in = contiguous non-flowing block >= ``min_hours`` with a downhole pressure rise >= ``rise_psi``."""
    h = _hf_min(path_or_df)
    ev = []
    for w, g in h.groupby("well_name"):
        off = ~flowing_mask(g).values
        ts = g["timestamp"].values
        blk = np.cumsum(np.r_[True, off[1:] != off[:-1]])
        for b in np.unique(blk[off]):
            idx = np.where(blk == b)[0]
            t0, t1 = pd.Timestamp(ts[idx[0]]), pd.Timestamp(ts[idx[-1]])
            dur = (t1 - t0).total_seconds() / 3600
            if dur < min_hours:
                continue
            pre = g["dh_pressure_psia"].iloc[max(idx[0] - 10, 0):idx[0]].median()
            rise = g["dh_pressure_psia"].iloc[idx].max() - pre
            if rise >= rise_psi:
                ev.append({"well_name": w, "t_shut": t0 - pd.Timedelta(minutes=1), "t_open": t1,
                           "duration_hr": dur, "bhp_rise_psi": rise})
    return pd.DataFrame(ev)


def event_series_hf(path_or_df: str | pd.DataFrame, e: pd.Series) -> tuple:
    """Return (well, t_shut, dt [hr], p_ws [psia], p_wf [psia]) for one detected shut-in."""
    h = _hf_min(path_or_df)
    g = h[h.well_name == e["well_name"]]
    pwf = float(g[g.timestamp <= e["t_shut"]]["dh_pressure_psia"].dropna().iloc[-1])
    seg = g[(g.timestamp > e["t_shut"]) & (g.timestamp <= e["t_open"])].dropna(subset=["dh_pressure_psia"])
    dt = (seg.timestamp - e["t_shut"]).dt.total_seconds().values / 3600
    return e["well_name"], pd.Timestamp(e["t_shut"]), dt, seg["dh_pressure_psia"].values, pwf
