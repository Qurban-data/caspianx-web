"""
Ingestion & validation layer for unseen / hidden SOCAR-format files (adapter: the protected core is not modified).

Every table goes through ``ingest_table(df, kind)`` before it reaches ``core.preprocessing``:

1. column normalisation + configurable alias map (``data/column_map.json`` overrides ``DEFAULT_ALIASES``)
2. mandatory / optional column check (mandatory = ``core.preprocessing.SCHEMA``)
3. type coercion: numbers stored as text -> float; values that cannot be parsed are counted as *invalid*
4. -999.25 sentinels -> NaN, counted as *missing* (never silently replaced by zero)
5. timestamp parsing (ISO first, day-first fallback); unparseable rows are dropped and counted
6. duplicate records removed on the natural key; chronological order restored and reported
7. well-ID normalisation (strip, upper-case) and new-well detection against the trained model

The returned ``IngestReport`` is shown to the user BEFORE inference.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .preprocessing import SCHEMA, classify_columns

DEFAULT_ALIASES: dict[str, str] = {
    "well": "well_name", "wellname": "well_name", "well_id": "well_name", "well_name_": "well_name",
    "datetime": "timestamp", "date_time": "timestamp", "time": "timestamp",
    "thp_psia": "whp_psia", "whp": "whp_psia", "tubing_head_pressure_psia": "whp_psia",
    "flp_psia": "whp_downstream_psia", "downstream_pressure_psia": "whp_downstream_psia",
    "bhp_psia": "dh_pressure_psia", "pdh_psia": "dh_pressure_psia",
    "bht_degf": "dh_temperature_degf", "casing_pressure_psia": "annulus_pressure_psia",
    "glr_mscfd": "gas_lift_rate_mscfd", "gas_lift_mscfd": "gas_lift_rate_mscfd",
    "choke_pct": "choke_opening_pct", "choke": "choke_opening_pct",
}
OPTIONAL: dict[str, list[str]] = {
    "high_freq": [],
    "daily_features": ["n", "nf", "logger_gap_min", "dh_temperature_degf", "annulus_pressure_psia"],
    "well_tests": ["gor_scf_stb", "total_gas_rate_mscfd", "water_cut_pct", "dh_temperature_degf"],
    "choke_events": [],
    "reservoir_pvt": ["water_saturation_frac", "estimated_drainage_area_acres"],
    "well_completion": [],
}
TIME_COLS = {"high_freq": "timestamp", "daily_features": "date", "well_tests": "test_start",
             "choke_events": "timestamp"}
KEYS = {"high_freq": ["well_name", "timestamp"], "daily_features": ["well_name", "date"],
        "well_tests": ["well_name", "test_start"], "choke_events": ["well_name", "timestamp"],
        "reservoir_pvt": ["well_name"], "well_completion": ["well_name"]}
NON_NUMERIC = {"well_name", "timestamp", "date", "test_start", "test_end"}


def load_aliases(path: str | None = None) -> dict[str, str]:
    """Alias map: defaults, overridden by a JSON file {source_column: canonical_column} when present."""
    a = dict(DEFAULT_ALIASES)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            a.update({k.strip().lower(): v for k, v in json.load(fh).items()})
    return a


@dataclass
class IngestReport:
    """What happened to one input table; ``ok`` is False when a mandatory column is missing."""
    kind: str
    rows_in: int = 0
    rows_out: int = 0
    renamed: dict = field(default_factory=dict)
    missing_mandatory: list = field(default_factory=list)
    missing_optional: list = field(default_factory=list)
    coerced_numeric: dict = field(default_factory=dict)      # column -> values that were text but parsed
    invalid_numeric: dict = field(default_factory=dict)      # column -> values that could not be parsed
    sentinel_missing: dict = field(default_factory=dict)     # column -> -999.25 count
    invalid_time: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    wells: list = field(default_factory=list)
    new_wells: list = field(default_factory=list)
    period: tuple = (None, None)

    @property
    def ok(self) -> bool:
        return not self.missing_mandatory

    def messages(self) -> list[tuple[str, str]]:
        """(level, text) lines for the UI: level in {'ok', 'warn', 'bad'}."""
        out = []
        if self.missing_mandatory:
            out.append(("bad", f"{self.kind}: missing mandatory columns {self.missing_mandatory}; not usable"))
        if self.missing_optional:
            out.append(("warn", f"{self.kind}: optional columns absent {self.missing_optional}"))
        if self.renamed:
            out.append(("ok", f"{self.kind}: mapped columns {self.renamed}"))
        for c, n in self.invalid_numeric.items():
            out.append(("warn", f"{self.kind}: {n} unparseable values in {c} set to missing"))
        for c, n in self.coerced_numeric.items():
            out.append(("ok", f"{self.kind}: {n} numbers stored as text in {c} converted"))
        if self.sentinel_missing:
            out.append(("ok", f"{self.kind}: -999.25 sentinels -> missing {self.sentinel_missing}"))
        if self.invalid_time:
            out.append(("warn", f"{self.kind}: {self.invalid_time} rows with invalid timestamps dropped"))
        if self.duplicates:
            out.append(("warn", f"{self.kind}: {self.duplicates} duplicate records removed"))
        if self.out_of_order:
            out.append(("ok", f"{self.kind}: {self.out_of_order} rows out of chronological order re-sorted"))
        if self.new_wells:
            out.append(("warn", f"{self.kind}: wells not in the trained model {self.new_wells} (uncalibrated)"))
        if not out:
            out.append(("ok", f"{self.kind}: {self.rows_out} rows, no issues"))
        return out

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame([{"file": self.kind, "level": lv, "message": m} for lv, m in self.messages()])


def normalise_columns(df: pd.DataFrame, aliases: dict[str, str] | None = None) -> tuple[pd.DataFrame, dict]:
    """Lower-case, strip, spaces -> underscores, then apply the alias map (only if the target is absent)."""
    aliases = aliases or DEFAULT_ALIASES
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    ren = {c: aliases[c] for c in df.columns if c in aliases and aliases[c] not in df.columns}
    return df.rename(columns=ren), ren


def parse_time(s: pd.Series) -> pd.Series:
    """ISO-8601 first; if more than 20 % fail, retry day-first (dd/mm/yyyy). Unparseable -> NaT."""
    t = pd.to_datetime(s, errors="coerce", format="ISO8601")
    if t.isna().mean() > 0.2:
        t2 = pd.to_datetime(s, errors="coerce", dayfirst=True, format="mixed")
        if t2.notna().sum() > t.notna().sum():
            t = t2
    return t


def ingest_table(df: pd.DataFrame, kind: str | None = None, known_wells: set | None = None,
                 aliases: dict[str, str] | None = None) -> tuple[pd.DataFrame, IngestReport]:
    """Validate and clean one input table. Never raises on bad content: problems go into the report."""
    df, ren = normalise_columns(df, aliases)
    kind = kind or classify_columns(df.columns) or "unknown"
    rep = IngestReport(kind=kind, rows_in=len(df), renamed=ren)
    if kind not in SCHEMA:
        rep.missing_mandatory = ["<unrecognised file: no schema matches its columns>"]
        return df, rep
    rep.missing_mandatory = [c for c in SCHEMA[kind] if c not in df.columns]
    rep.missing_optional = [c for c in OPTIONAL.get(kind, []) if c not in df.columns]
    if rep.missing_mandatory:
        return df, rep
    if "well_name" in df:
        df["well_name"] = df["well_name"].astype(str).str.strip().str.upper()
    for c in df.columns:
        if c in NON_NUMERIC or df[c].dtype.kind in "fiub":
            continue
        raw = df[c]
        num = pd.to_numeric(raw.astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce")
        present = raw.notna() & (raw.astype(str).str.strip() != "")
        if num[present].notna().mean() >= 0.5:            # a numeric column stored as text
            bad = int((present & num.isna()).sum())
            if bad:
                rep.invalid_numeric[c] = bad
            rep.coerced_numeric[c] = int((present & num.notna()).sum())
            df[c] = num
    for c in df.select_dtypes("number").columns:
        # only the LAS/SCADA null codes are missing; other large negatives are sign flips that the core
        # QC (qc_tests) restores with |x| (e.g. GL-09 gas lift -1010.5), so they must not be blanked here
        sent = np.isclose(df[c], -999.25, atol=1e-3) | np.isclose(df[c], -9999.0, atol=1e-3)
        n = int(sent.sum())
        if n:
            rep.sentinel_missing[c] = n
            df.loc[sent, c] = np.nan
    tc = TIME_COLS.get(kind)
    if tc:
        cols = [tc] + (["test_end"] if kind == "well_tests" else [])
        for c in cols:
            df[c] = parse_time(df[c])
        bad = df[cols].isna().any(axis=1)
        rep.invalid_time = int(bad.sum())
        df = df[~bad]
        if kind == "daily_features":
            df[tc] = df[tc].dt.normalize()
        rep.out_of_order = int((df.groupby("well_name")[tc].diff().dt.total_seconds() < 0).sum())
        df = df.sort_values(["well_name", tc])
        rep.period = (df[tc].min(), df[tc].max())
    key = [k for k in KEYS.get(kind, []) if k in df]
    n0 = len(df)
    df = df.drop_duplicates(subset=key, keep="last") if key else df
    rep.duplicates = n0 - len(df)
    rep.rows_out = len(df)
    if "well_name" in df:
        rep.wells = sorted(df["well_name"].unique())
        if known_wells is not None:
            rep.new_wells = sorted(set(rep.wells) - set(known_wells))
    return df.reset_index(drop=True), rep


def ingest_bundle(tables: dict[str, pd.DataFrame], known_wells: set | None = None,
                  aliases: dict[str, str] | None = None) -> tuple[dict[str, pd.DataFrame], list[IngestReport]]:
    """Ingest several tables at once; keys may be file kinds or file names (classified by columns)."""
    out, reps = {}, []
    for name, df in tables.items():
        kind = name if name in SCHEMA else None
        clean, rep = ingest_table(df, kind, known_wells, aliases)
        reps.append(rep)
        if rep.ok:
            out[rep.kind] = clean
    return out, reps


def data_status(P: pd.DataFrame) -> pd.Series:
    """Per well-day semantics used in exports and UI (never inferred from one missing value):
    'shut-in' (observed, zero flowing minutes), 'partial shut-in', 'imputed' (sensor gap filled from the same
    well), 'invalid' (essential input unavailable -> no prediction), 'flowing'."""
    s = pd.Series("flowing", index=P.index)
    s[P.get("imputed", False) == True] = "imputed"  # noqa: E712
    s[(P["uptime"] > 0) & (P["uptime"] < 0.99)] = "partial shut-in"
    s[P["uptime"] <= 0] = "shut-in"
    ess = [c for c in ["whp_psia", "whp_downstream_psia", "choke_opening_pct"] if c in P]
    s[(P["uptime"] > 0) & P[ess].isna().any(axis=1)] = "invalid"
    return s
