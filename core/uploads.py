"""Upload handling for the app (testable without Streamlit). Never raises: every problem becomes a message."""
from __future__ import annotations

import io
import zipfile

import pandas as pd

from .ingest import ingest_table

TABLE_KINDS = ["daily_features", "well_tests", "choke_events", "reservoir_pvt", "well_completion"]


def is_reference(df: pd.DataFrame) -> bool:
    cols = {str(c).strip().lower() for c in df.columns}
    return {"well_name", "t_shut", "k_md", "skin"} <= cols


def read_any(name: str, data: bytes) -> pd.DataFrame:
    """CSV or XLSX from bytes. Raises ValueError with a clear message on empty / unreadable content."""
    if not data or not data.strip():
        raise ValueError("file is empty")
    if name.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(data))
    else:
        try:
            df = pd.read_csv(io.BytesIO(data), sep=None, engine="python")      # sniff , ; tab
        except pd.errors.EmptyDataError:
            raise ValueError("file has no columns")
    if df.shape[1] < 2 or len(df) == 0:
        raise ValueError(f"file has {len(df)} rows and {df.shape[1]} columns - nothing to use")
    return df


class _Member:
    """A file taken out of an uploaded ZIP, with the same interface as a Streamlit UploadedFile."""
    def __init__(self, name: str, data: bytes):
        self.name, self._d = name, data

    def getvalue(self) -> bytes:
        return self._d


def expand_zips(files) -> tuple[list, list]:
    """Replace every .zip upload by its CSV / XLSX members (folders flattened, macOS metadata skipped).
    A broken ZIP becomes a message, never an exception. Nested .zip files are opened one level deep."""
    out, msgs = [], []
    for u in files or []:
        name = getattr(u, "name", "upload")
        if not name.lower().endswith(".zip"):
            out.append(u)
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(u.getvalue())) as z:
                n = 0
                for info in z.infolist():
                    base = info.filename.replace("\\", "/").split("/")[-1]
                    if info.is_dir() or not base or base.startswith((".", "~$")) or "__MACOSX" in info.filename:
                        continue
                    low = base.lower()
                    if low.endswith(".zip"):
                        inner, im = expand_zips([_Member(base, z.read(info))])
                        out += inner; msgs += im; n += len(inner)
                    elif low.endswith((".csv", ".xlsx", ".xls", ".json")):
                        out.append(_Member(base, z.read(info))); n += 1
                msgs.append(("ok" if n else "warn", f"{name}: ZIP archive, {n} data file(s) extracted"))
        except Exception as ex:
            msgs.append(("bad", f"{name}: ZIP could not be opened ({ex})"))
    return out, msgs


def classify_uploads(files) -> dict:
    """files: objects with .name and .getvalue() (Streamlit UploadedFile or test doubles).
    Returns {"raw": {kind: DataFrame}, "hf_bytes": bytes|None, "messages": [(level, text)]}."""
    raw, hf, msgs, events = {}, None, [], {}
    files, zmsg = expand_zips(files)
    msgs += zmsg
    for u in files or []:
        name = getattr(u, "name", "upload")
        if name.lower().endswith(".json"):
            try:
                import json as _json
                e = _json.loads(u.getvalue())
                if isinstance(e, dict) and "post" in e and "pwf" in e:
                    events[name] = e
                    msgs.append(("ok", f"{name}: build-up pressure record"))
                else:
                    msgs.append(("warn", f"{name}: JSON without build-up data, ignored"))
            except Exception as ex:
                msgs.append(("bad", f"{name}: JSON not readable ({ex})"))
            continue
        try:
            data = u.getvalue()
            if name.lower().endswith(".csv") and len(data) > 20e6:
                head = pd.read_csv(io.BytesIO(data), nrows=5)
                if ingest_table(head)[1].kind == "high_freq":
                    hf = data
                    msgs.append(("ok", f"{name}: per-minute sensor file"))
                    continue
            df = read_any(name, data)
        except Exception as ex:
            msgs.append(("bad", f"{name}: not readable ({ex})"))
            continue
        if is_reference(df):
            raw["reference"] = df
            msgs.append(("ok", f"{name}: reference interpretation table (used for comparison only)"))
            continue
        kind = ingest_table(df.head(50))[1].kind
        if kind not in TABLE_KINDS and kind != "high_freq":
            import re as _re
            from .diameters import BEAN_EVENT_COLS
            low = [str(c).strip().lower() for c in df.columns]
            if "well_name" in low and "timestamp" in low and any(_re.match(BEAN_EVENT_COLS, c) for c in low):
                df.columns = low
                raw["choke_events"] = df
                msgs.append(("ok", f"{name}: choke events given as bean size (converted to % of the maximum bean)"))
                continue
        if kind == "high_freq":
            hf = data
            msgs.append(("ok", f"{name}: per-minute sensor file"))
        elif kind in TABLE_KINDS:
            if kind in raw:
                msgs.append(("warn", f"{name}: second {kind} file - replaces the first one"))
            raw[kind] = df
            msgs.append(("ok", f"{name}: recognised as {kind}"))
        else:
            from .ingest import normalise_columns
            from .preprocessing import closest_kind
            k2, miss = closest_kind(normalise_columns(df)[0].columns)
            numeric_hdr = all(str(c).replace(".", "", 1).lstrip("-").isdigit() for c in df.columns)
            if numeric_hdr:
                msgs.append(("warn", f"{name}: ignored - no header row (column names are numbers)"))
                continue
            msgs.append(("bad", f"{name}: not usable - it looks like {k2} but the required column(s) "
                                f"{miss} are missing (columns found: {', '.join(map(str, df.columns[:10]))})"))
    return {"raw": raw, "hf_bytes": hf, "messages": msgs, "events": events}
