"""Well copilot: deterministic question answering over the computed tables of the loaded session.

No language model is involved. A question is parsed into (wells, months, metric, intent); the answer is assembled from
rows of the session's daily rate table, sensor table, PTA results, anomaly rules and the stored validation summary.
A number that is not in those tables is never produced: outside the data range, for an unknown well, or for a question
that cannot be mapped, the answer says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

PH = ["oil", "water", "gas"]
RATES = {"oil": "oil_rate_stbd", "water": "water_rate_stbd", "gas": "gas_rate_mscfd"}
MON_AZ = ["Yanvar", "Fevral", "Mart", "Aprel", "May", "İyun", "İyul", "Avqust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr"]
MON_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
MON_AZ_S = ["yan", "fev", "mar", "apr", "may", "iyn", "iyl", "avq", "sen", "okt", "noy", "dek"]
MON_EN_S = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
# month stems as typed (lower-cased, dotted İ folded to i): Azerbaijani, English, common short forms
MON_STEMS = [("yanvar", 1), ("january", 1), ("fevral", 2), ("february", 2), ("mart", 3), ("march", 3), ("aprel", 4), ("april", 4),
             ("iyun", 6), ("june", 6), ("iyul", 7), ("july", 7), ("avqust", 8), ("august", 8), ("sentyabr", 9), ("september", 9),
             ("oktyabr", 10), ("october", 10), ("noyabr", 11), ("november", 11), ("dekabr", 12), ("december", 12)]
MON_SHORT = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
             "yan": 1, "fev": 2, "iyn": 6, "iyl": 7, "avq": 8, "sen": 9, "okt": 10, "noy": 11, "dek": 12}
ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4,
       "birinci": 1, "ikinci": 2, "üçüncü": 3, "dördüncü": 4}
CLASS_AZ = {"severe damage": "ciddi zədələnmə", "moderate damage": "orta zədələnmə", "slight damage": "yüngül zədələnmə",
            "undamaged / slightly stimulated": "zədəsiz / yüngül stimulyasiya"}

KB_Q = {"hybrid": ("Why is the hybrid model better than pure physics and pure ML?", "Hibrid model niyə təmiz fizikadan və təmiz ML-dən yaxşıdır?"),
        "overfit": ("How do you know the model is not overfitted?", "Modelin overfit olmadığını haradan bilirsiniz?"),
        "missing": ("How is missing sensor data handled?", "Çatışmayan sensor datası necə emal olunur?"),
        "gas": ("Why is the gas error higher than the oil error?", "Qaz xətası niyə neft xətasından böyükdür?"),
        "limits": ("When should the model not be trusted?", "Modelə nə vaxt etibar etmək olmaz?")}
# topic keywords: specific on purpose (a bare "why" / "niyə" / "ml" must not select a canned answer)
KB_KW = {"hybrid": ("hybrid", "hibrid", "pure physics", "təmiz fizika", "pure ml", "təmiz ml", "fizikadan"),
         "overfit": ("overfit", "əzbər", "leakage", "sızma", "cross-valid", "validasiya", "validation"),
         "missing": ("missing", "çatışmayan", "çatışmır", "imput", "sensor data", "sensor boşlu", "boşluq necə", "gap handl"),
         "gas": ("gas error", "qaz xətası", "qazın xətası", "gas mape", "qaz mape"),
         "limits": ("etibar etmək olmaz", "etibar etmə", "not be trusted", "not trust", "limitation", "məhdudiyyət", "unseen well", "yeni quyu", "new well")}


@dataclass
class Ctx:
    s: dict | None
    lang: str
    well: str | None
    d0: str | None
    d1: str | None
    proof: dict
    card: dict
    daily_tables: Callable[..., Any]
    pta_rows: Callable[..., Any]
    anomaly_rows: Callable[..., Any]
    blend_with_band: Callable[..., Any]

    @property
    def az(self) -> bool:
        return self.lang == "az"

    def L(self, en: str, az: str) -> str:
        return az if self.az else en


# ------------------------------------------------------------------------------------------------ formatting
def nf(c: Ctx, x, d: int = 0) -> str:
    if x is None or not np.isfinite(float(x)):
        return "–"
    t = f"{float(x):,.{d}f}".replace("-", "−")
    return t.replace(",", " ").replace(".", ",") if c.az else t


def sg(c: Ctx, x, d: int = 1) -> str:
    if x is None or not np.isfinite(float(x)):
        return "–"
    t = nf(c, abs(float(x)), d)
    return ("+" if x > 0 else "−" if x < 0 else "") + t


def pc(c: Ctx, x, d: int = 1) -> str:
    return sg(c, x, d) + " %"


def mlabel(c: Ctx, ym: str, year: bool = True) -> str:
    m = int(ym[5:7]) - 1
    return (MON_AZ if c.az else MON_EN)[m] + (" " + ym[:4] if year else "")


def in_month(c: Ctx, ym: str) -> str:
    return (f"{mlabel(c, ym)} ayında" if c.az else f"in {mlabel(c, ym)}")


def span_label(c: Ctx, months: list[str]) -> str:
    if not months:
        return "–"
    S = MON_AZ_S if c.az else MON_EN_S
    a, b = months[0], months[-1]
    if a == b:
        return mlabel(c, a)
    return f"{S[int(a[5:7]) - 1]}–{S[int(b[5:7]) - 1]} {b[:4]}" if a[:4] == b[:4] else f"{mlabel(c, a)} – {mlabel(c, b)}"


UNIT = {"oil": "STB/d", "water": "STB/d", "gas": "Mscf/d", "wc": "%", "gor": "scf/STB"}


def unit(c: Ctx, k: str) -> str:
    u = UNIT.get(k, "")
    return u.replace("/d", "/gün") if c.az else u


def mname(c: Ctx, k: str) -> str:
    return {"oil": c.L("oil rate", "neft debiti"), "water": c.L("water rate", "su debiti"), "gas": c.L("gas rate", "qaz debiti"),
            "wc": c.L("water cut", "su payı"), "gor": "GOR", "uptime": c.L("uptime", "işləmə"), "skin": "skin",
            "k": c.L("permeability", "keçiricilik")}.get(k, k)


# ------------------------------------------------------------------------------------------------ tables
def tables(c: Ctx) -> dict:
    """Well x month and field x month tables over the WHOLE loaded data (the period only selects which rows are read,
    so a month-over-month change at the start of the selected period still has its previous month)."""
    V, P = c.daily_tables(c.s)
    cols = ["well_name", "date", "uptime", "whp_psia", "whp_downstream_psia", "dh_pressure_psia", "gas_lift_rate_mscfd",
            "choke_opening_pct"] + (["imputed"] if "imputed" in P else [])
    D = V.merge(P[cols], on=["well_name", "date"], how="left")
    D["m"] = D.date.dt.to_period("M").astype(str)
    D["dp"] = D.whp_psia - D.whp_downstream_psia
    if "imputed" not in D:
        D["imputed"] = False

    def agg(g: pd.DataFrame) -> pd.Series:
        n = g.date.nunique()
        fl = g[g.uptime >= 0.99]
        o, w, gs = (float(g[RATES[p]].sum()) for p in PH)
        return pd.Series({
            "days": n, "oil": o / n, "water": w / n, "gas": gs / n, "oil_sum": o, "water_sum": w, "gas_sum": gs,
            "wc": w / (o + w) * 100 if o + w > 0 else np.nan, "gor": gs * 1000 / o if o > 0 else np.nan,
            "uptime": float(g.uptime.mean() * 100), "shut": int((g.uptime <= 0).sum()),
            "partial": int(((g.uptime > 0) & (g.uptime < 0.99)).sum()), "imputed": int(g.imputed.fillna(False).astype(bool).sum()),
            "oil_flow": float(fl[RATES["oil"]].mean()) if len(fl) else np.nan,
            "whp": float(fl.whp_psia.mean()) if len(fl) else np.nan, "dp": float(fl.dp.mean()) if len(fl) else np.nan,
            "choke": float(fl.choke_opening_pct.mean()) if len(fl) else np.nan,
            "bhp": float(fl.dh_pressure_psia.mean()) if len(fl) else np.nan,
            "gl": float(fl.gas_lift_rate_mscfd.mean()) if len(fl) else np.nan})
    W = {w: g.groupby("m").apply(agg, include_groups=False) for w, g in D.groupby("well_name")}
    F = D.groupby("m").apply(agg, include_groups=False)
    months = sorted(D.m.unique())
    nd = D.groupby("m").date.nunique()
    full = [m for m in months if int(nd[m]) >= int(pd.Period(m).days_in_month)]
    return {"W": W, "F": F, "months": months, "full": full, "wells": sorted(W), "D": D}


def period_months(c: Ctx, T: dict) -> list[str]:
    """Months of the data that fall in the period selected in the UI (whole data when no period is set)."""
    a = pd.Timestamp(c.d0).strftime("%Y-%m") if c.d0 else T["months"][0]
    b = pd.Timestamp(c.d1).strftime("%Y-%m") if c.d1 else T["months"][-1]
    return [m for m in T["months"] if a <= m <= b] or T["months"]


# ------------------------------------------------------------------------------------------------ parsing
def _fold(t: str) -> str:
    return t.replace("İ", "i").lower().replace("i\u0307", "i")


def parse(c: Ctx, text: str, T: dict | None) -> dict:
    low = _fold(text)
    wells_data = T["wells"] if T else []
    by_num = {}
    for w in wells_data:
        m = re.search(r"(\d+)\s*$", w)
        if m:
            by_num[int(m.group(1))] = w
    wells, unknown = [], []
    for m in re.finditer(r"(?<![a-z])gl[\s\-_]*0*(\d{1,3})(?!\d)", low):
        n = int(m.group(1))
        (wells if n in by_num else unknown).append(by_num.get(n, f"GL-{n:02d}"))
    for w in wells_data:                                          # any other naming scheme: exact name in the text
        if w.lower() in low and w not in wells:
            wells.append(w)
    wells = list(dict.fromkeys(wells))
    unknown = list(dict.fromkeys(unknown))

    years = [int(y) for y in re.findall(r"(?<!\d)(20\d\d)(?!\d)", low)]
    nums: list[int] = []
    for stem, n in MON_STEMS:
        if re.search(r"(?<![a-zəöüğışç])" + stem, low):
            nums.append((low.index(stem), n))
    for m in re.finditer(r"(?<![a-zəöüğışç])([a-zəöüğışç]{3,4})(?![a-zəöüğışç])", low):
        if m.group(1) in MON_SHORT:
            nums.append((m.start(), MON_SHORT[m.group(1)]))
    if re.search(r"(?<![a-zəöüğışç])may(da|ın|dan|a)?(?![a-zəöüğışç])(\s+ay)?", low) and (c.az or re.search(r"\b(in|of|for|during)\s+may\b", low)):
        nums.append((low.index("may"), 5))
    for m in re.finditer(r"(?<!\d)(20\d\d)[-./](0?[1-9]|1[0-2])(?![\d])", low):
        nums.append((m.start(), int(m.group(2))))
    month_nums = [n for _, n in sorted(set(nums))]
    month_nums = list(dict.fromkeys(month_nums))

    quarters = [int(q) for q in re.findall(r"(?<![a-z0-9])[rq]\s?([1-4])(?![0-9])", low)]
    quarters += [int(q) for q in re.findall(r"([1-4])\s*[-–]?\s*(?:ci|cü|cu|cı)?\s*(?:rüb|quarter)", low)]
    for k, v in ORD.items():
        if re.search(k + r"\s+(rüb|quarter)", low):
            quarters.append(v)
    for q in dict.fromkeys(quarters):
        month_nums += [n for n in range(3 * q - 2, 3 * q + 1) if n not in month_nums]

    months, out = [], []
    if T:
        data_years = sorted({int(m[:4]) for m in T["months"]})
        for n in month_nums:
            hit = [m for m in T["months"] if int(m[5:7]) == n and (not years or int(m[:4]) in years)]
            if hit:
                months += hit
            else:
                out.append(f"{(years or data_years)[-1]}-{n:02d}")
        if years and not month_nums and not any(y in data_years for y in years):
            out += [f"{y}" for y in years]
        if re.search(r"son ay|last month|axırıncı ay", low):
            months.append(T["months"][-1])
        if re.search(r"ilk ay|first month", low) and not quarters:
            months.append(T["months"][0])
    months = sorted(dict.fromkeys(months))

    has = lambda *ws: any(w in low for w in ws)
    metric = None
    if has("su payı", "su payi", "sulaşma", "water cut", "water-cut", "watercut") or re.search(r"\bwc\b", low):
        metric = "wc"
    elif re.search(r"\bgor\b", low) or has("qaz amili", "gas-oil ratio", "gas oil ratio"):
        metric = "gor"
    elif has("skin", "zədə", "damage"):
        metric = "skin"
    elif has("keçirici", "permeab"):
        metric = "k"
    elif has("işləmə", "uptime", "dayan", "bağlan", "shut-in", "shut in", "downtime"):
        metric = "uptime"
    elif re.search(r"(?<![a-zəöüğışç])(qaz|gas)", low):
        metric = "gas"
    elif re.search(r"(?<![a-zəöüğışç])(su|water)(?![a-zəöüğışç])|suyun|su debit", low):
        metric = "water"
    elif has("neft", "oil", "hasilat", "debit", "production", "istehsal", "rate"):
        metric = "oil"

    down = has("düş", "azal", "aşağı", "drop", "declin", "fell", "fall", "decreas", "lower", "itki", "loss", "enmə", "enib")
    up = has("art", "yüksəl", "qalx", "increas", "rose", "rise", "higher", "growth")
    why = has("niyə", "nə üçün", "nəyə görə", "səbəb", "why", "reason", "cause", "explain", "izah", "nə baş ver", "what happened")
    kb = next((k for k, ws in KB_KW.items() if any(w in low for w in ws)), None)
    intent = None
    if has("stimul", "turşu", "acid", "müdaxilə", "intervention", "workover", "namizəd", "candidate") or \
            (has("hansı quyu", "which well") and has("birinci", "first", "əvvəl", "priorit")):
        intent = "stim"
    elif kb and not wells and not months and not out:
        intent = "kb:" + kb
    elif has("anomal", "xəbərdarl", "alert", "problem", "nasaz", "risk", "diqqət"):
        intent = "anomaly"
    elif why or ((down or up) and (wells or months) and not has("ən çox", "hansı quyu", "which well", "most", "top")):
        intent = "why"
    elif (len(wells) >= 2 or len(months) >= 2 and not quarters) and has("müqayisə", "compar", " vs ", "fərq", "differ", "versus", " və ", " and "):
        intent = "compare"
    elif has("hansı quyu", "which well", "ən yaxşı", "ən pis", "ən çox", "ən az", "ən yüksək", "ən aşağı", "top", "best", "worst", "highest",
             "lowest", "most", "least", "sırala", "rank"):
        intent = "rank"
    elif wells or months or metric or has("yataq", "field", "ümumi", "cəmi", "total", "necə", "how", "nə qədər", "neçə"):
        intent = "value" if (wells or months or metric) else None
    return {"wells": wells, "unknown": unknown, "months": months, "out": out, "metric": metric, "intent": intent, "down": down,
            "up": up, "low": low, "ctx_well": has("bu quyu", "this well", "seçilmiş quyu", "selected well", "quyuda", "quyunun", "quyu üzrə")}


# ------------------------------------------------------------------------------------------------ answers
def _drivers(c: Ctx, T: dict, w: str, ym: str, prev: str, metric: str) -> tuple[list[str], dict]:
    """Measured changes between two months of one well (flowing-day means), each line a computed fact."""
    a, b = T["W"][w].loc[prev], T["W"][w].loc[ym]
    L, out, f = c.L, [], {}
    ch = lambda k: (b[k] / a[k] - 1) * 100 if (np.isfinite(a[k]) and np.isfinite(b[k]) and a[k]) else np.nan
    if b.shut or b.partial:
        f["shut"] = True
        out.append(L(f"Shut-in: {int(b.shut)} full day(s) and {int(b.partial)} partial day(s) {in_month(c, ym)} (uptime {nf(c, b.uptime, 1)} %); "
                     "the calendar-month average includes these days.",
                     f"Bağlanma: {in_month(c, ym)} {int(b.shut)} tam gün və {int(b.partial)} qismən gün (işləmə {nf(c, b.uptime, 1)} %); "
                     "təqvim ayı üzrə orta debitə bu günlər də daxildir."))
        if np.isfinite(ch("oil_flow")):
            out.append(L(f"On full-uptime days the oil rate went {nf(c, a.oil_flow)} → {nf(c, b.oil_flow)} STB/d ({pc(c, ch('oil_flow'))}).",
                         f"Tam işlək günlərdə neft debiti: {nf(c, a.oil_flow)} → {nf(c, b.oil_flow)} STB/gün ({pc(c, ch('oil_flow'))})."))
    if np.isfinite(ch("choke")) and abs(ch("choke")) >= 1:
        f["choke"] = ch("choke")
        out.append(L(f"Choke opening {nf(c, a.choke, 1)} → {nf(c, b.choke, 1)} % ({pc(c, ch('choke'))}); the rate scales with the square of the opening.",
                     f"Ştuser açıqlığı {nf(c, a.choke, 1)} → {nf(c, b.choke, 1)} % ({pc(c, ch('choke'))}); debit açıqlığın kvadratı ilə mütənasibdir."))
    else:
        out.append(L(f"Choke opening unchanged ({nf(c, b.choke, 1)} %).", f"Ştuser açıqlığı dəyişməyib ({nf(c, b.choke, 1)} %)."))
    if np.isfinite(ch("dp")) and abs(ch("dp")) >= 3:
        f["dp"] = ch("dp")
        out.append(L(f"Pressure drop across the choke (WHP − P_ds): {nf(c, a.dp)} → {nf(c, b.dp)} psi ({pc(c, ch('dp'))}); WHP {nf(c, a.whp)} → {nf(c, b.whp)} psia. "
                     "The rate scales with √ΔP.",
                     f"Ştuser üzərində təzyiq düşküsü (WHP − P_ds): {nf(c, a.dp)} → {nf(c, b.dp)} psi ({pc(c, ch('dp'))}); WHP {nf(c, a.whp)} → {nf(c, b.whp)} psia. "
                     "Debit √ΔP ilə mütənasibdir."))
    if np.isfinite(ch("bhp")) and abs(ch("bhp")) >= 0.5:
        out.append(L(f"Flowing bottom-hole pressure {nf(c, a.bhp)} → {nf(c, b.bhp)} psia ({pc(c, ch('bhp'))}).",
                     f"Quyudibi axın təzyiqi {nf(c, a.bhp)} → {nf(c, b.bhp)} psia ({pc(c, ch('bhp'))})."))
    if np.isfinite(ch("gl")) and abs(ch("gl")) >= 3:
        out.append(L(f"Gas-lift rate {nf(c, a.gl)} → {nf(c, b.gl)} Mscf/d ({pc(c, ch('gl'))}).",
                     f"Qazlift sərfi {nf(c, a.gl)} → {nf(c, b.gl)} Mscf/gün ({pc(c, ch('gl'))})."))
    if metric in ("oil", "water", "wc") and np.isfinite(a.wc) and np.isfinite(b.wc) and abs(b.wc - a.wc) >= 1:
        out.append(L(f"Water cut {nf(c, a.wc, 1)} → {nf(c, b.wc, 1)} %.", f"Su payı {nf(c, a.wc, 1)} → {nf(c, b.wc, 1)} %."))
    if b.imputed:
        out.append(L(f"Sensor gap on {int(b.imputed)} day(s): the value was carried forward and the rows are flagged.",
                     f"Sensor boşluğu: {int(b.imputed)} gün (dəyər əvvəlki gündən götürülüb, sətirlər işarələnib)."))
    return out, f


def _pta_lines(c: Ctx, w: str) -> tuple[list[str], list[dict]]:
    rows = [x for x in c.pta_rows(c.s["R"]) if x["well"] == w and x.get("skin") is not None]
    out = []
    for x in rows:
        cl = x.get("class") or ""
        gain = (c.L(f"; theoretical gain if skin is removed {nf(c, x['dq_stb_d'])} STB/d", f"; skin aradan qaldırılsa nəzəri artım {nf(c, x['dq_stb_d'])} STB/gün")
                if (x.get("dq_stb_d") and np.isfinite(x["dq_stb_d"]) and x["skin"] > 0) else "")
        out.append(c.L(f"Build-up of {x['t_shut'][:10]}: skin {sg(c, x['skin'], 2)} ({cl}), Δp_skin {nf(c, x['dp_skin_psi'])} psi, k {nf(c, x['k_md'], 1)} mD{gain}.",
                       f"{x['t_shut'][:10]} tarixli təzyiq bərpası: skin {sg(c, x['skin'], 2)} ({CLASS_AZ.get(cl, cl)}), Δp_skin {nf(c, x['dp_skin_psi'])} psi, k {nf(c, x['k_md'], 1)} mD{gain}."))
    return out, rows


def a_why(c: Ctx, T: dict, P: dict, months: list[str]) -> dict:
    L = c.L
    metric = P["metric"] if P["metric"] in ("oil", "water", "gas", "wc", "gor") else "oil"
    w = P["wells"][0] if P["wells"] else (c.well if P["ctx_well"] and c.well in T["W"] else None)
    ym = (P["months"] or months)[-1]
    tab = T["W"][w] if w else T["F"]
    who = w or L("Field", "Yataq")
    idx = list(tab.index)
    if ym not in idx:
        return {"lines": [L(f"{who} has no data {in_month(c, ym)}.", f"{who} üçün {in_month(c, ym)} data yoxdur.")], "focus": {"well": w, "tab": "mon"}}
    lines, u = [], unit(c, metric)
    d = 1 if metric in ("wc",) else 0
    i = idx.index(ym)
    chg = lambda a_, b_: (tab.loc[b_, metric] / tab.loc[a_, metric] - 1) * 100
    focus = ym
    asked_down = P["down"] or not P["up"]
    if i == 0:
        lines.append(L(f"{who}: {mlabel(c, ym)} is the first month of the loaded data ({mname(c, metric)} {nf(c, tab.loc[ym, metric], d)} {u}); there is no previous month to compare with.",
                       f"{who}: {mlabel(c, ym)} yüklənmiş datanın ilk ayıdır ({mname(c, metric)} {nf(c, tab.loc[ym, metric], d)} {u}); müqayisə üçün əvvəlki ay yoxdur."))
        cur = None
    else:
        prev = idx[i - 1]
        cur = chg(prev, ym)
        lines.append(L(f"{who}, {mlabel(c, ym)}: average {mname(c, metric)} {nf(c, tab.loc[prev, metric], d)} → {nf(c, tab.loc[ym, metric], d)} {u}, {pc(c, cur)} vs {mlabel(c, prev, False)}.",
                       f"{who}, {mlabel(c, ym)}: orta {mname(c, metric)} {nf(c, tab.loc[prev, metric], d)} → {nf(c, tab.loc[ym, metric], d)} {u}, {mlabel(c, prev, False)} ayına nəzərən {pc(c, cur)}."))
    moved = cur is not None and ((asked_down and cur <= -3) or (not asked_down and cur >= 3))
    if not moved:
        # the asked month did not move that way: say so, then point at the month where the change really happened
        if cur is not None:
            lines.append(L(f"So there is no {'drop' if asked_down else 'rise'} {in_month(c, ym)}.",
                           f"Yəni {in_month(c, ym)} {'düşmə' if asked_down else 'artım'} yoxdur."))
        loc = "oil_flow" if (w and metric == "oil") else metric          # a well's drop is located on full-uptime days
        ch = [((tab.loc[idx[k], loc] / tab.loc[idx[k - 1], loc] - 1) * 100, idx[k]) for k in range(1, len(idx))
              if idx[k] in T["full"] and idx[k - 1] in T["full"]]
        ch = [x for x in ch if np.isfinite(x[0])]
        best = (min(ch) if asked_down else max(ch)) if ch else None
        if best and ((asked_down and best[0] <= -5) or (not asked_down and best[0] >= 5)):
            focus = best[1]
            pm = idx[idx.index(focus) - 1]
            lines.append(L(f"The largest monthly {'drop' if asked_down else 'rise'} of {who} is {in_month(c, focus)}: {nf(c, tab.loc[pm, metric], d)} → {nf(c, tab.loc[focus, metric], d)} {u} ({pc(c, chg(pm, focus))}).",
                           f"{who} üzrə ən böyük aylıq {'düşmə' if asked_down else 'artım'} {in_month(c, focus)} olub: {nf(c, tab.loc[pm, metric], d)} → {nf(c, tab.loc[focus, metric], d)} {u} ({pc(c, chg(pm, focus))})."))
            if idx.index(ym) > idx.index(focus):
                lv = chg(pm, ym)
                lines.append(L(f"{mlabel(c, ym, False)} is still {pc(c, lv)} against {mlabel(c, pm, False)}, the last month before that change.",
                               f"{mlabel(c, ym, False)} səviyyəsi həmin dəyişmədən əvvəlki {mlabel(c, pm, False)} ayına nəzərən hələ də {pc(c, lv)}."))
        else:
            lines.append(L(f"No month-over-month change of 5 % or more for {who} in the loaded data.", f"Yüklənmiş datada {who} üzrə 5 % və daha böyük aylıq dəyişmə yoxdur."))
            return {"lines": lines, "focus": {"well": w, "month": ym, "tab": "mon"}, "source": L("monthly table of the loaded data", "yüklənmiş datanın aylıq cədvəli"), "source_tab": "mon"}
    pm = idx[idx.index(focus) - 1]
    if w:
        dl, f = _drivers(c, T, w, focus, pm, metric)
        lines += dl
        pl, rows = _pta_lines(c, w)
        lines += pl
        hi = [x for x in rows if x["skin"] >= 2]
        if f.get("dp", 0) <= -5 and "choke" not in f and hi:
            lines.append(L("Reading: the choke was not touched, the drawdown across it fell and the build-up shows high skin, so the drop is consistent with near-wellbore damage.",
                           "Nəticə: ştuserə toxunulmayıb, onun üzərindəki təzyiq düşküsü azalıb və bərpa yüksək skin göstərir; düşmə quyudibi zonanın zədələnməsi (skin) ilə uyğundur."))
        elif "choke" in f:
            sm = f["choke"] < 0
            lines.append(L(f"Reading: the main measured cause is the {'smaller' if sm else 'larger'} choke opening (an operating action, not a reservoir change).",
                           f"Nəticə: ölçülmüş əsas səbəb ştuserin {'qısılmasıdır' if sm else 'açılmasıdır'} (operator əməliyyatı, lay dəyişikliyi deyil)."))
        elif f.get("shut") and not f.get("dp"):
            lines.append(L("Reading: the monthly average is lower mainly because of the shut-in days.", "Nəticə: aylıq orta əsasən bağlanma günlərinə görə aşağıdır."))
        elif len(dl) <= 1 and not pl:
            lines.append(L("The computed indicators do not show a clear cause (choke, pressures and uptime did not change); the change follows the model's water-cut / GOR trend.",
                           "Hesablanmış göstəricilərdə aydın səbəb görünmür (ştuser, təzyiqlər və işləmə dəyişməyib); dəyişmə modelin su payı / GOR trendindən gəlir."))
    else:
        dq = sorted(((T["W"][x].loc[focus, "oil"] - T["W"][x].loc[pm, "oil"], x) for x in T["wells"] if focus in T["W"][x].index and pm in T["W"][x].index))
        top = dq[:3] if asked_down else dq[::-1][:3]
        lines.append(L("Largest contributions by well (oil): ", "Quyular üzrə ən böyük töhfə (neft): ")
                     + "; ".join(f"{x} {sg(c, v, 0)} {unit(c, 'oil')}" for v, x in top) + ".")
        sh = int(sum(T["W"][x].loc[focus, "shut"] for x in T["wells"] if focus in T["W"][x].index))
        if sh:
            lines.append(L(f"Shut-in well-days {in_month(c, focus)}: {sh}.", f"{in_month(c, focus)} bağlı quyu-gün sayı: {sh}."))
    return {"lines": lines, "focus": {"well": w, "month": focus, "tab": "vfm" if w else "mon"},
            "source": L("monthly rates, sensor table and PTA results of the loaded data", "yüklənmiş datanın aylıq debitləri, sensor cədvəli və PTA nəticələri"),
            "source_tab": "vfm" if w else "mon"}


def _period_row(T: dict, w: str | None, months: list[str]) -> pd.Series | None:
    tab = (T["W"][w] if w else T["F"]).reindex(months).dropna(subset=["days"])
    if tab.empty:
        return None
    n = tab.days.sum()
    o, wt, g = tab.oil_sum.sum(), tab.water_sum.sum(), tab.gas_sum.sum()
    return pd.Series({"oil": o / n, "water": wt / n, "gas": g / n, "wc": wt / (o + wt) * 100 if o + wt > 0 else np.nan,
                      "gor": g * 1000 / o if o > 0 else np.nan, "uptime": float((tab.uptime * tab.days).sum() / n),
                      "shut": int(tab.shut.sum()), "oil_sum": o, "days": int(n)})


def a_value(c: Ctx, T: dict, P: dict, months: list[str]) -> dict:
    L = c.L
    w = P["wells"][0] if P["wells"] else (c.well if P["ctx_well"] and c.well in T["W"] else None)
    ms = P["months"] or months
    who, lab = w or L("Field", "Yataq"), span_label(c, ms)
    metric = P["metric"] or "oil"
    if metric in ("skin", "k"):
        ws = [w] if w else T["wells"]
        lines = [l for x in ws for l in _pta_lines(c, x)[0]]
        none = [x for x in ws if not _pta_lines(c, x)[0]]
        if none:
            lines.append(L(f"No build-up record, so skin and permeability are unknown for: {', '.join(none)}.",
                           f"Bərpa qeydi yoxdur, ona görə skin və keçiricilik məlum deyil: {', '.join(none)}."))
        return {"lines": lines, "focus": {"well": w, "tab": "pta"}, "source": L("PTA results of the loaded data", "yüklənmiş datanın PTA nəticələri"), "source_tab": "pta"}
    r = _period_row(T, w, ms)
    if r is None:
        return {"lines": [L(f"{who} has no data for {lab}.", f"{who} üçün {lab} dövründə data yoxdur.")], "focus": {"well": w, "tab": "mon"}}
    lines = [L(f"{who}, {lab} ({int(r.days)} days): oil {nf(c, r.oil)} STB/d, water {nf(c, r.water)} STB/d, gas {nf(c, r.gas)} Mscf/d on average.",
               f"{who}, {lab} ({int(r.days)} gün): orta hesabla neft {nf(c, r.oil)} STB/gün, su {nf(c, r.water)} STB/gün, qaz {nf(c, r.gas)} Mscf/gün."),
             L(f"Water cut {nf(c, r.wc, 1)} %, GOR {nf(c, r.gor)} scf/STB, uptime {nf(c, r.uptime, 1)} %, cumulative oil {nf(c, r.oil_sum)} STB.",
               f"Su payı {nf(c, r.wc, 1)} %, GOR {nf(c, r.gor)} scf/STB, işləmə {nf(c, r.uptime, 1)} %, kumulyativ neft {nf(c, r.oil_sum)} STB.")]
    r["shut"] = int(r.shut)
    tab = (T["W"][w] if w else T["F"]).reindex(ms).dropna(subset=["days"])
    k = metric if metric in ("oil", "water", "gas", "wc", "gor") else "oil"
    full = [m for m in tab.index if m in T["full"]]
    if len(full) >= 2:
        a, b = full[0], full[-1]
        ch = tab.loc[b, k] - tab.loc[a, k] if k == "wc" else (tab.loc[b, k] / tab.loc[a, k] - 1) * 100
        lines.append(L(f"{mname(c, k).capitalize()} {mlabel(c, a, False)} → {mlabel(c, b, False)}: {nf(c, tab.loc[a, k], 1 if k == 'wc' else 0)} → {nf(c, tab.loc[b, k], 1 if k == 'wc' else 0)} {unit(c, k)} ({sg(c, ch)} {'pp' if k == 'wc' else '%'}).",
                       f"{mname(c, k).capitalize()} {mlabel(c, a, False)} → {mlabel(c, b, False)}: {nf(c, tab.loc[a, k], 1 if k == 'wc' else 0)} → {nf(c, tab.loc[b, k], 1 if k == 'wc' else 0)} {unit(c, k)} ({sg(c, ch)} {'f.b.' if k == 'wc' else '%'})."))
    if metric == "uptime" and r.shut:
        lines.append(L(f"Full shut-in days in the period: {r.shut}.", f"Dövrdə tam bağlanma günləri: {r.shut}."))
    return {"lines": lines, "focus": {"well": w, "month": ms[0] if len(ms) == 1 else None, "tab": "vfm" if w else "mon"},
            "source": L("daily rate table of the loaded data", "yüklənmiş datanın gündəlik debit cədvəli"), "source_tab": "vfm" if w else "mon"}


def a_rank(c: Ctx, T: dict, P: dict, months: list[str]) -> dict:
    L = c.L
    ms = P["months"] or months
    k = P["metric"] if P["metric"] in ("oil", "water", "gas", "wc", "gor") else "oil"
    lab, u = span_label(c, ms), unit(c, k)
    d = 1 if k == "wc" else 0
    lines = []
    full = [m for m in ms if m in T["full"]]
    if (P["down"] or P["up"]) and len(full) >= 2:
        a, b = full[0], full[-1]
        ch = sorted(((T["W"][w].loc[b, k] / T["W"][w].loc[a, k] - 1) * 100, w) for w in T["wells"] if a in T["W"][w].index and b in T["W"][w].index)
        ch = ch if P["down"] else ch[::-1]
        lines.append(L(f"Change of the {mname(c, k)} from {mlabel(c, a, False)} to {mlabel(c, b, False)}, by well:",
                       f"{mlabel(c, a, False)} → {mlabel(c, b, False)}: quyular üzrə {mname(c, k)} dəyişməsi:"))
        for i, (v, w) in enumerate(ch[:3], 1):
            lines.append(f"{i}. {w}: {nf(c, T['W'][w].loc[a, k], d)} → {nf(c, T['W'][w].loc[b, k], d)} {u} ({pc(c, v)})")
        v, w = ch[-1]
        lines.append(L(f"Opposite end: {w} ({pc(c, v)}).", f"Əks tərəf: {w} ({pc(c, v)})."))
    else:
        vals = [(r[k], w) for w in T["wells"] if (r := _period_row(T, w, ms)) is not None and np.isfinite(r[k])]
        lo = any(x in P["low"] for x in ("ən pis", "ən az", "ən aşağı", "worst", "lowest", "least"))
        vals = sorted(vals, reverse=not lo)
        lines.append(L(f"{'Lowest' if lo else 'Highest'} average {mname(c, k)}, {lab}:", f"{lab}: orta {mname(c, k)} üzrə ən {'aşağı' if lo else 'yüksək'} quyular:"))
        for i, (v, w) in enumerate(vals[:3], 1):
            lines.append(f"{i}. {w}: {nf(c, v, d)} {u}")
        if len(vals) > 3:
            v, w = vals[-1]
            lines.append(L(f"Opposite end: {w} ({nf(c, v, d)} {u}).", f"Əks tərəf: {w} ({nf(c, v, d)} {u})."))
    return {"lines": lines, "focus": {"tab": "mon"}, "source": L("well × month table of the loaded data", "yüklənmiş datanın quyu × ay cədvəli"), "source_tab": "mon"}


def a_compare(c: Ctx, T: dict, P: dict, months: list[str]) -> dict:
    L = c.L
    k = P["metric"] if P["metric"] in ("oil", "water", "gas", "wc", "gor") else "oil"
    u, d, lines = unit(c, k), (1 if k == "wc" else 0), []
    if len(P["wells"]) >= 2:
        ms = P["months"] or months
        rows = [(w, _period_row(T, w, ms)) for w in P["wells"][:4]]
        rows = [(w, r) for w, r in rows if r is not None]
        for w, r in rows:
            lines.append(L(f"{w}, {span_label(c, ms)}: oil {nf(c, r.oil)} STB/d, water cut {nf(c, r.wc, 1)} %, GOR {nf(c, r.gor)} scf/STB, uptime {nf(c, r.uptime, 1)} %.",
                           f"{w}, {span_label(c, ms)}: neft {nf(c, r.oil)} STB/gün, su payı {nf(c, r.wc, 1)} %, GOR {nf(c, r.gor)} scf/STB, işləmə {nf(c, r.uptime, 1)} %."))
        if len(rows) == 2:
            (w1, a), (w2, b) = rows
            lines.append(L(f"Difference in {mname(c, k)}: {w1} is {pc(c, (a[k] / b[k] - 1) * 100)} against {w2}.",
                           f"{mname(c, k).capitalize()} fərqi: {w1} {w2} ilə müqayisədə {pc(c, (a[k] / b[k] - 1) * 100)}."))
        return {"lines": lines, "focus": {"well": P["wells"][0], "tab": "mon"}, "source": L("daily rate table of the loaded data", "yüklənmiş datanın gündəlik debit cədvəli"), "source_tab": "mon"}
    w = P["wells"][0] if P["wells"] else (c.well if P["ctx_well"] and c.well in T["W"] else None)
    tab, who = (T["W"][w] if w else T["F"]), (w or L("Field", "Yataq"))
    ms = [m for m in P["months"] if m in tab.index]
    for m in ms:
        note = "" if m in T["full"] else L(f" (incomplete month: {int(tab.loc[m, 'days'])} days)", f" (natamam ay: {int(tab.loc[m, 'days'])} gün)")
        lines.append(f"{who}, {mlabel(c, m)}: {mname(c, k)} {nf(c, tab.loc[m, k], d)} {u}{note}")
    if len(ms) >= 2:
        a, b = ms[0], ms[-1]
        lines.append(L(f"{mlabel(c, b, False)} vs {mlabel(c, a, False)}: {pc(c, (tab.loc[b, k] / tab.loc[a, k] - 1) * 100)}.",
                       f"{mlabel(c, b, False)} ayı {mlabel(c, a, False)} ayına nəzərən: {pc(c, (tab.loc[b, k] / tab.loc[a, k] - 1) * 100)}."))
    return {"lines": lines, "focus": {"well": w, "tab": "mon"}, "source": L("monthly table of the loaded data", "yüklənmiş datanın aylıq cədvəli"), "source_tab": "mon"}


def anomaly_text(c: Ctx, r: dict) -> str:
    L, w, t = c.L, r["well"], r["type"]
    ml = lambda m: mlabel(c, m, False)
    if t == "rate_drop":
        return L(f"{w}: sudden drop of the flowing-day oil rate {nf(c, r['from'])} → {nf(c, r['to'])} STB/d ({pc(c, r['change_pct'])}) from {ml(r['first'])} to {ml(r['last'])} with the choke unchanged.",
                 f"{w}: ştuser dəyişmədən işlək günlərdə neft debiti kəskin düşüb: {nf(c, r['from'])} → {nf(c, r['to'])} STB/gün ({pc(c, r['change_pct'])}), {ml(r['first'])} → {ml(r['last'])}.")
    if t == "oil_decline":
        return L(f"{w}: oil decline {pc(c, r['change_pct'])} from {ml(r['first'])} to {ml(r['last'])}.", f"{w}: neft debiti {ml(r['first'])} → {ml(r['last'])} arasında {pc(c, r['change_pct'])}.")
    if t == "water_cut_drift":
        return L(f"{w}: water cut {nf(c, r['from'], 1)} → {nf(c, r['to'], 1)} % ({ml(r['first'])} → {ml(r['last'])}).", f"{w}: su payı {nf(c, r['from'], 1)} → {nf(c, r['to'], 1)} % ({ml(r['first'])} → {ml(r['last'])}).")
    if t == "gor_drift":
        return L(f"{w}: GOR {nf(c, r['from'])} → {nf(c, r['to'])} scf/STB ({ml(r['first'])} → {ml(r['last'])}).", f"{w}: GOR {nf(c, r['from'])} → {nf(c, r['to'])} scf/STB ({ml(r['first'])} → {ml(r['last'])}).")
    span = r["first"] + ("" if r.get("last") == r["first"] else " → " + r["last"])
    if t == "shut_in":
        return L(f"{w}: shut-in, {r['days']} day(s), {span}.", f"{w}: bağlanma, {r['days']} gün, {span}.")
    if t == "partial_uptime":
        return L(f"{w}: partial uptime on {r['days']} day(s), {span}.", f"{w}: qismən işləmə, {r['days']} gün, {span}.")
    if t == "sensor_gap":
        return L(f"{w}: sensor gap on {r['days']} day(s), {span} (values carried forward, rows flagged).", f"{w}: sensor boşluğu, {r['days']} gün, {span} (dəyər əvvəlki gündən götürülüb, sətirlər işarələnib).")
    if t == "critical_choke":
        return L(f"{w}: critical choke flow on {r['days']} day(s) ({nf(c, r['share_pct'])} %), median P_ds/WHP {nf(c, r['value'], 3)}.", f"{w}: kritik ştuser axını, {r['days']} gün ({nf(c, r['share_pct'])} %), median P_ds/WHP {nf(c, r['value'], 3)}.")
    return f"{w}: {t}"


def a_anomaly(c: Ctx, T: dict, P: dict, months: list[str]) -> dict:
    L = c.L
    ms = P["months"] or months
    d0 = ms[0] + "-01"
    d1 = str((pd.Period(ms[-1]).end_time).date())
    if not P["months"]:
        d0, d1 = c.d0 or d0, c.d1 or d1
    rows, n, _ = c.anomaly_rows(c.s, d0, d1)
    w = P["wells"][0] if P["wells"] else (c.well if P["ctx_well"] and c.well in T["W"] else None)
    if w:
        rows = [r for r in rows if r["well"] == w]
    main = [r for r in rows if r["severity"] in ("alert", "warn")]
    lab = span_label(c, ms) + (f" · {w}" if w else "")
    if not rows:
        return {"lines": [L(f"No anomaly found by the rules for {lab}.", f"{lab}: qaydalar üzrə anomaliya aşkarlanmayıb.")], "focus": {"well": w, "tab": "anom"},
                "source": L("anomaly rules over the loaded data", "yüklənmiş data üzrə anomaliya qaydaları"), "source_tab": "anom"}
    lines = [L(f"{lab}: {len(main)} alert / warning row(s), {len(rows) - len(main)} informational, over {nf(c, n)} well-days.",
               f"{lab}: {len(main)} həyəcan / xəbərdarlıq, {len(rows) - len(main)} məlumat sətri ({nf(c, n)} quyu-gün üzrə).")]
    show = (main or rows)[:8]
    lines += [anomaly_text(c, r) for r in show]
    if len(main or rows) > len(show):
        lines.append(L(f"… and {len(main or rows) - len(show)} more in the anomaly tab.", f"… daha {len(main or rows) - len(show)} sətir anomaliya tabındadır."))
    return {"lines": lines, "focus": {"well": w or show[0]["well"], "tab": "anom"}, "source": L("anomaly rules over the loaded data", "yüklənmiş data üzrə anomaliya qaydaları"), "source_tab": "anom"}


def a_stim(c: Ctx, T: dict, P: dict, months: list[str]) -> dict:
    """Every tested well ranked by the theoretical gain; the interval is the VFM P10-P90 band of the rate used in the PTA."""
    L = c.L
    allr = [x for x in c.pta_rows(c.s["R"]) if x.get("skin") is not None]
    a, b = (c.d0 or "0000"), (c.d1 or "9999")
    rows = [x for x in allr if a <= x["t_shut"][:10] <= b]
    lines = []
    if not rows and allr:
        lines.append(L("No build-up inside the selected period; ranking over the whole loaded data:", "Seçilmiş dövrdə bərpa yoxdur; bütün yüklənmiş data üzrə sıralama:"))
        rows = allr
    if not rows:
        return {"lines": [L("No build-up record in the loaded data, so skin is unknown for every well and no candidate can be ranked.",
                            "Yüklənmiş datada bərpa qeydi yoxdur; bütün quyular üçün skin məlum deyil və namizəd sıralamaq olmur.")], "focus": {"tab": "pta"}}
    B = c.blend_with_band(c.s["R"])

    def band(x):
        try:
            r = B[(B.well_name == x["well"]) & (B.date == pd.Timestamp(x["q_source_date"]))].iloc[0]
            q = float(r["oil_rate_stbd"])
            return x["dq_stb_d"] * float(r["oil_p10"]) / q, x["dq_stb_d"] * float(r["oil_p90"]) / q
        except Exception:
            return None
    cand = sorted([x for x in rows if x["skin"] > 0 and x.get("dq_stb_d") and np.isfinite(x["dq_stb_d"])], key=lambda x: -x["dq_stb_d"])
    rest = [x for x in rows if x not in cand]
    if cand:
        lines.append(L(f"Stimulation candidates ranked by theoretical oil gain ({len(cand)} of {len(rows)} tested wells have positive skin):",
                       f"Nəzəri neft artımına görə stimulyasiya namizədləri (sınaqdan keçmiş {len(rows)} quyudan {len(cand)}-də skin müsbətdir):"))
    for i, x in enumerate(cand[:3], 1):
        bd = band(x)
        ci = (L(f" (P10–P90 {nf(c, bd[0])}–{nf(c, bd[1])}, from the rate uncertainty)", f" (P10–P90 {nf(c, bd[0])}–{nf(c, bd[1])}, debit qeyri-müəyyənliyindən)") if bd else "")
        cl = x.get("class") or ""
        lines.append(L(f"{i}. {x['well']}: gain {nf(c, x['dq_stb_d'])} STB/d{ci}; skin {sg(c, x['skin'], 2)} ({cl}), Δp_skin {nf(c, x['dp_skin_psi'])} psi, build-up {x['t_shut'][:10]}.",
                       f"{i}. {x['well']}: artım {nf(c, x['dq_stb_d'])} STB/gün{ci}; skin {sg(c, x['skin'], 2)} ({CLASS_AZ.get(cl, cl)}), Δp_skin {nf(c, x['dp_skin_psi'])} psi, bərpa {x['t_shut'][:10]}."))
    for x in rest:
        lines.append(L(f"{x['well']}: skin {sg(c, x['skin'], 2)}, not a candidate (no damage to remove).", f"{x['well']}: skin {sg(c, x['skin'], 2)}, namizəd deyil (aradan qaldırılacaq zədə yoxdur)."))
    untested = [w for w in T["wells"] if w not in {x["well"] for x in allr}]
    if untested:
        lines.append(L(f"{len(untested)} well(s) have no build-up, so their skin is unknown and they are not ranked: {', '.join(untested)}.",
                       f"{len(untested)} quyuda bərpa qeydi yoxdur; skin məlum deyil və onlar sıralanmır: {', '.join(untested)}."))
    lines.append(L("The gain is an upper bound at constant drawdown, for ranking; the intervention cost is not included.",
                   "Artım sabit depressiyada yuxarı həddir və sıralama üçündür; müdaxilə xərci daxil deyil."))
    return {"lines": lines, "focus": {"well": cand[0]["well"] if cand else rows[0]["well"], "tab": "pta"},
            "source": L("PTA results of the loaded data", "yüklənmiş datanın PTA nəticələri"), "source_tab": "pta"}


def a_kb(c: Ctx, q: str) -> dict:
    """Model-validation answers from the stored validation summary (pre-computed aggregates)."""
    d, T_ = c.proof or {}, c.L
    M_ = {m["key"]: m for m in d.get("models", [])}
    f3 = lambda x, n=2: " / ".join(nf(c, x[p], n) for p in PH)
    owg = T_("oil / water / gas", "neft / su / qaz")
    PHN = {"oil": T_("oil", "neft"), "water": T_("water", "su"), "gas": T_("gas", "qaz")}
    lines, src = [], T_("validation summary (frozen forward CV)", "validasiya xülasəsi (dondurulmuş irəli CV)")
    if q in ("hybrid", "overfit", "gas") and not M_:
        return {"lines": [T_("Validation summary file is not available in this build.", "Validasiya xülasəsi faylı bu versiyada yoxdur.")], "focus": {"tab": "proof"}}
    if q == "hybrid":
        h, ph_ = M_["hybrid"], M_["physics"]
        lines = [T_(f"Forward-CV MAPE {owg}: hybrid {f3(h['oos_mape'])} %, pure physics {f3(ph_['oos_mape'])} %.",
                    f"İrəli CV MAPE {owg}: hibrid {f3(h['oos_mape'])} %, təmiz fizika {f3(ph_['oos_mape'])} %."),
                 T_(f"R² {owg}: hybrid {f3(h['oos_r2'], 3)}, pure physics {f3(ph_['oos_r2'], 3)}.",
                    f"R² {owg}: hibrid {f3(h['oos_r2'], 3)}, təmiz fizika {f3(ph_['oos_r2'], 3)}.")]
        for p in PH:
            v = ph_["vs_hybrid"][p]
            sig = v["ci_lo"] > 0 or v["ci_hi"] < 0
            lines.append(T_(f"{PHN[p]}: pure physics is {sg(c, v['delta_pp'], 2)} pp vs the hybrid (95 % CI {sg(c, v['ci_lo'], 2)} to {sg(c, v['ci_hi'], 2)}); "
                            + ("significant." if sig else "not statistically significant."),
                            f"{PHN[p]}: təmiz fizika hibriddən {sg(c, v['delta_pp'], 2)} f.b. fərqlənir (95 % etibarlılıq intervalı {sg(c, v['ci_lo'], 2)} … {sg(c, v['ci_hi'], 2)}); "
                            + ("fərq əhəmiyyətlidir." if sig else "fərq statistik əhəmiyyətli deyil.")))
        for k, nm in (("ml_ridge", T_("Pure ML (Ridge on raw sensors)", "Təmiz ML (xam sensorlarda Ridge)")), ("ml_gbdt", T_("Pure ML (boosted trees)", "Təmiz ML (boosting ağacları)"))):
            if k in M_:
                e = M_[k]["sanity"]["choke_elasticity_min"]
                lines.append(T_(f"{nm}: MAPE {f3(M_[k]['oos_mape'])} %, train-to-OOS gap {sg(c, M_[k]['gap_pp'], 2)} pp (hybrid {sg(c, h['gap_pp'], 2)} pp); "
                                f"minimum choke elasticity {sg(c, e, 2)}" + (" (unphysical: opening the choke lowers the rate)." if e <= 0 else "."),
                                f"{nm}: MAPE {f3(M_[k]['oos_mape'])} %, təlim–OOS fərqi {sg(c, M_[k]['gap_pp'], 2)} f.b. (hibrid {sg(c, h['gap_pp'], 2)} f.b.); "
                                f"minimum ştuser elastikliyi {sg(c, e, 2)}" + (" (qeyri-fiziki: ştuser açılanda debit azalır)." if e <= 0 else ".")))
    elif q == "overfit":
        h = M_["hybrid"]
        cov = " / ".join(nf(c, d['coverage_p10_p90_pct'].get(p, float('nan')), 1) for p in PH)
        lines = [T_(f"Train MAPE {f3(h['train_mape'])} % vs forward out-of-sample {f3(h['oos_mape'])} %: gap {sg(c, h['gap_pp'], 2)} pp.",
                    f"Təlim MAPE {f3(h['train_mape'])} %, irəli nümunədən kənar {f3(h['oos_mape'])} %: fərq {sg(c, h['gap_pp'], 2)} f.b."),
                 T_(f"Month-blocked CV mean MAPE {nf(c, h['month_blocked_mean'], 2)} % (every calendar month held out in turn).",
                    f"Ay-bloklu CV orta MAPE {nf(c, h['month_blocked_mean'], 2)} % (hər təqvim ayı növbə ilə kənarda saxlanır)."),
                 T_(f"Protocol: {d['protocol']['forward_cv']}; hyper-parameters are chosen inside each training fold only.",
                    "Protokol: 4 kəsim, 90 günlük üfüq, n = 109; hiperparametrlər yalnız hər təlim qatının daxilində seçilir."),
                 T_(f"P10–P90 coverage {owg}: {cov} % (nominal 80 %).", f"P10–P90 əhatəsi {owg}: {cov} % (nominal 80 %).")]
    elif q == "gas":
        h = M_["hybrid"]
        worst = ", ".join(f"{r['well']} {nf(c, r['gas'], 1)} %" for r in sorted(h["per_well"], key=lambda r: -r["gas"])[:2])
        lines = [T_(f"Forward-CV MAPE: oil {nf(c, h['oos_mape']['oil'], 2)} %, gas {nf(c, h['oos_mape']['gas'], 2)} %.",
                    f"İrəli CV MAPE: neft {nf(c, h['oos_mape']['oil'], 2)} %, qaz {nf(c, h['oos_mape']['gas'], 2)} %."),
                 T_("Gas = oil rate × GOR, so the gas error contains the oil error plus the GOR-trend error between tests.",
                    "Qaz = neft debiti × GOR; ona görə qaz xətasına neft xətası və testlər arasındakı GOR trendinin xətası daxildir."),
                 T_(f"Largest gas errors: {worst}.", f"Ən böyük qaz xətaları: {worst}.")]
    elif q == "missing":
        lines = [T_(f"{m['sensor']}: missing on {nf(c, m['prod_missing_pct'], 2)} % of production days, {nf(c, m['test_missing_pct'])} % of test days.",
                    f"{m['sensor']}: istehsal günlərinin {nf(c, m['prod_missing_pct'], 2)} %-də, test günlərinin {nf(c, m['test_missing_pct'])} %-də yoxdur.") for m in d.get("missing", [])]
        lines += [T_(f"{d.get('imputed_production_rows', 0)} of {d.get('production_rows', 0)} production well-days had a sensor gap: the value is carried "
                     "forward (then backward) within the same well and the row is flagged 'imputed'; a full shut-in day is exactly 0.",
                     f"{d.get('production_rows', 0)} istehsal quyu-günündən {d.get('imputed_production_rows', 0)}-də sensor boşluğu var: dəyər eyni quyunun "
                     "əvvəlki (sonra növbəti) günündən götürülür və sətir 'imputed' kimi işarələnir; tam bağlanma günündə debit dəqiq 0-dır."),
                  T_("Sensors that exist only on test days (annulus pressure, downhole temperature) are not model inputs.",
                     "Yalnız test günlərində olan sensorlar (annulus təzyiqi, quyudibi temperaturu) modelin girişi deyil.")]
        src = T_("missing-data audit (validation set)", "çatışmayan data auditi (validasiya dəsti)")
    elif q == "limits":
        lines = [T_("No separator test for the well: rates use field-median calibration and are flagged uncalibrated.",
                    "Quyunun separator testi yoxdursa: debit yataq medianı ilə kalibrlənir və 'kalibrlənməyib' kimi işarələnir."),
                 T_("More than 180 days after the last test: water-cut and GOR trends are frozen, not extrapolated.",
                    "Son testdən 180 gündən çox keçibsə: su payı və GOR trendləri dondurulur, ekstrapolyasiya edilmir."),
                 T_("Inputs far outside the tested choke / pressure range are extrapolation (the what-if panel flags this).",
                    "Sınaqdan keçmiş ştuser / təzyiq diapazonundan çox kənar girişlər ekstrapolyasiyadır (what-if paneli bunu göstərir).")]
        lo = (c.card or {}).get("vfm", {}).get("leave_one_well_out", {})
        if lo:
            v = " / ".join(nf(c, lo[p]['MAPE_%'], 1) for p in PH)
            lines.append(T_(f"Unseen-well (leave-one-well-out) MAPE {owg}: {v} %.", f"Görülməmiş quyu (leave-one-well-out) MAPE {owg}: {v} %."))
        src = T_("model card", "model kartı")
    return {"lines": lines, "focus": {"tab": "proof"}, "source": src, "source_tab": "proof"}


# ------------------------------------------------------------------------------------------------ context, chips, entry point
def context(c: Ctx, T: dict | None = None) -> dict:
    if c.s is None:
        return {"loaded": False}
    T = T or tables(c)
    ms = period_months(c, T)
    return {"loaded": True, "well": c.well if c.well in T["W"] else None, "period": span_label(c, ms), "months": ms,
            "data_period": span_label(c, T["months"]), "wells": T["wells"],
            "synthetic": "synthetic" in str(c.s.get("source", ""))}


def chips(c: Ctx, T: dict | None = None) -> list[str]:
    """Suggested questions built from the selected well and period (each one is answered by the same parser)."""
    L = c.L
    out = []
    if c.s is not None:
        T = T or tables(c)
        ms = period_months(c, T)
        w = c.well if c.well in T["W"] else T["wells"][0]
        tab = T["W"][w]
        idx = [m for m in tab.index if m in T["full"]]
        ch = [(tab.loc[idx[k], "oil_flow"] / tab.loc[idx[k - 1], "oil_flow"] - 1, idx[k]) for k in range(1, len(idx)) if idx[k] in ms]
        if ch and min(ch)[0] <= -0.05:
            m = min(ch)[1]
            out.append(L(f"Why did {w} drop in {mlabel(c, m, False)}?", f"{w} {mlabel(c, m, False)} ayında niyə düşdü?"))
        else:
            out.append(L(f"How is {w} producing in {mlabel(c, ms[-1], False)}?", f"{w} {mlabel(c, ms[-1], False)} ayında necə işləyir?"))
        out += [L("Which well should be stimulated first?", "Hansı quyu birinci stimulyasiya olunmalıdır?"),
                L(f"Which anomalies are there in {span_label(c, ms)}?", f"{span_label(c, ms)} dövründə hansı anomaliyalar var?"),
                L("Which wells declined the most?", "Hansı quyularda neft ən çox düşüb?"),
                L(f"What is the water cut of {w}?", f"{w} quyusunda su payı necədir?")]
    out += [KB_Q[k][1 if c.az else 0] for k in ("hybrid", "overfit", "limits")]
    return out


def answer(c: Ctx, text: str) -> dict:
    L = c.L
    base = {"question": text, "matched": True, "in_range": True, "lines": [], "source": "–", "source_tab": None, "focus": {}}
    T = tables(c) if c.s is not None else None
    P = parse(c, text, T)
    base["context"] = context(c, T)
    base["chips"] = chips(c, T)
    base["wells"] = T["wells"] if T else []
    intent = P["intent"]
    if intent and intent.startswith("kb:"):
        return {**base, "intent": intent, **a_kb(c, intent[3:])}
    if intent is None and not P["wells"] and not P["unknown"] and not P["out"]:
        return {**base, "matched": False, "intent": "unknown",
                "lines": [L("I could not connect this question to the loaded well data, so I give no number.",
                            "Bu sualı yüklənmiş quyu datası ilə əlaqələndirə bilmədim, ona görə rəqəm vermirəm."),
                          L("I answer only from computed results: well and field rates by month, water cut, GOR, uptime, skin / PTA, anomalies, stimulation ranking and model validation.",
                            "Yalnız hesablanmış nəticələrdən cavab verirəm: quyu və yataq debitləri (aylar üzrə), su payı, GOR, işləmə, skin / PTA, anomaliyalar, stimulyasiya sıralaması və model validasiyası.")]}
    if T is None:
        return {**base, "intent": intent or "value", "matched": False, "lines": [L("Load a dataset first: there is nothing computed to answer from.", "Əvvəlcə data yükləyin: cavab üçün hesablanmış nəticə yoxdur.")]}
    rng = span_label(c, T["months"])
    if P["unknown"] and not P["wells"]:
        return {**base, "intent": intent or "value", "in_range": False,
                "lines": [L(f"{', '.join(P['unknown'])} is not in the loaded data, so there is no result for it.", f"{', '.join(P['unknown'])} yüklənmiş datada yoxdur, ona görə onun üçün nəticə yoxdur."),
                          L(f"Wells in the data: {', '.join(T['wells'])}.", f"Datadakı quyular: {', '.join(T['wells'])}.")],
                "source": L("loaded dataset", "yüklənmiş data")}
    if P["out"] and not P["months"]:
        asked = ", ".join(mlabel(c, m) if len(m) == 7 else m for m in P["out"])
        return {**base, "intent": intent or "value", "in_range": False,
                "lines": [L(f"The loaded data covers {rng}. There is no computed result for {asked}, so I give no number for it.",
                            f"Yüklənmiş data {rng} aralığını əhatə edir. {asked} üçün hesablanmış nəticə yoxdur, ona görə bu dövr üçün rəqəm vermirəm."),
                          L("Ask about a month inside that range, or load data that covers the period.", "Bu aralıqdakı ay barədə soruşun və ya həmin dövrü əhatə edən data yükləyin.")],
                "source": L("loaded dataset", "yüklənmiş data")}
    months = period_months(c, T)
    fn = {"stim": a_stim, "anomaly": a_anomaly, "why": a_why, "compare": a_compare, "rank": a_rank, "value": a_value}[intent or "value"]
    out = fn(c, T, P, months)
    pre = []
    if P["out"]:
        asked = ", ".join(mlabel(c, m) if len(m) == 7 else m for m in P["out"])
        pre.append(L(f"Note: {asked} is outside the loaded data ({rng}); the answer covers only the months that exist.", f"Qeyd: {asked} yüklənmiş datadan ({rng}) kənardır; cavab yalnız mövcud ayları əhatə edir."))
    if P["unknown"]:
        pre.append(L(f"Note: {', '.join(P['unknown'])} is not in the loaded data.", f"Qeyd: {', '.join(P['unknown'])} yüklənmiş datada yoxdur."))
    out["lines"] = pre + out["lines"]
    return {**base, "intent": intent or "value", **out}
