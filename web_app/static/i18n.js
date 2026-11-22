"use strict";
/* Interface language (AZ default, EN toggle). The app renders English source strings; this layer translates text nodes
   by exact match or pattern, so numbers, well names and dates always come from the data untouched. */
const I18N = { lang: (() => { try { return localStorage.getItem("cx_lang") || "az"; } catch (e) { return "az"; } })() };
const MONTHS = { az: ["Yan", "Fev", "Mar", "Apr", "May", "İyn", "İyl", "Avq", "Sen", "Okt", "Noy", "Dek"],
  en: ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] };
const MONTHS_LONG_AZ = ["Yanvar", "Fevral", "Mart", "Aprel", "May", "İyun", "İyul", "Avqust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr"];
const AZ = {
  "Overview": "Sahə icmalı", "Monthly analysis": "Aylıq təhlil", "Measurements": "Ölçmələr", "VFM & Predictions": "VFM və proqnozlar",
  "PTA & Reservoir": "PTA və lay", "Period": "Dövr", "Whole period": "Bütün dövr", "Notes": "Qeydlər", "Data": "Data", "Well": "Quyu",
  "Export": "İxrac", "Excel": "Excel", "Report": "Hesabat", "Load demo data": "Demo datanı yüklə", "Field data": "Yataq datası",
  "Open field data": "Yataq datasını aç", "Drop CSV · Excel · ZIP": "CSV · Excel · ZIP faylını bura atın",
  "per-minute files of any size are streamed to disk": "istənilən ölçülü dəqiqəlik fayllar hissə-hissə diskə yazılır",
  "No dataset loaded": "Data yüklənməyib", "NO DATA": "DATA YOXDUR", "FIELD DATA": "YATAQ DATASI", "DEMO": "DEMO",
  "Upload well data or load the demo to start.": "Başlamaq üçün quyu datasını yükləyin və ya demonu açın.",
  "Field surveillance": "Yatağın monitorinqi", "Field": "Yataq", "Daily production": "Gündəlik hasilat",
  "Submitted VFM blend": "Təqdim edilən VFM qarışığı", "Findings & action queue": "Tapıntılar və tədbir növbəsi",
  "No dataset loaded.": "Data yüklənməyib.", "Oil": "Neft", "Water": "Su", "Gas": "Qaz", "Water cut": "Su payı", "GOR": "GOR",
  "Build-ups · alerts": "Bərpalar · xəbərdarlıqlar", "detected · open": "aşkarlanıb · açıq",
  "% · derived from rates": "% · debitlərdən hesablanıb", "scf/STB · derived from rates": "scf/STB · debitlərdən hesablanıb",
  "Build-ups · ranked by skin": "Bərpalar · skin üzrə sıralanıb", "Alerts": "Xəbərdarlıqlar", "No alerts.": "Xəbərdarlıq yoxdur.",
  "Switch to Field view": "Yataq görünüşünə keç", "No build-up record in the loaded data.": "Yüklənmiş datada bərpa (buildup) qeydi yoxdur.",
  "Sensor telemetry": "Sensor telemetriyası", "Shut-in days": "Bağlanma günləri", "Build-up records": "Bərpa qeydləri",
  "Daily oil · water · gas": "Gündəlik neft · su · qaz", "Separator tests": "Separator testləri", "Shut-in": "Bağlanma",
  "Solid line = submitted blend. Zoom or pan any phase: all three phases and the range presets stay in sync.":
    "Bütöv xətt = təqdim edilən qarışıq. İstənilən fazanı böyüdün və ya sürüşdürün: üç faza və dövr seçimi sinxron qalır.",
  "Predicted vs separator test": "Proqnoz və separator testi", "Model & formula transparency": "Model və düsturların şəffaflığı",
  "Training set": "Təlim dəsti", "tests": "test", "wells": "quyu", "Test date": "Test tarixi", "MAPE": "MAPE",
  "oil pred": "neft proqnoz", "oil test": "neft test", "water pred": "su proqnoz", "water test": "su test", "gas pred": "qaz proqnoz",
  "gas test": "qaz test", "err %": "xəta %", "in-sample · these tests were used for training": "təlimdə istifadə olunmuş testlər",
  "includes tests after the training period": "təlim dövründən sonrakı testlər daxildir",
  "P10–P90 coverage · nominal 80 %": "P10–P90 əhatəsi · nominal 80 %", "Build-up": "Bərpa (buildup)",
  "permeability": "keçiricilik", "skin": "skin", "Horner slope": "Horner meyli", "skin pressure drop": "skin üzrə təzyiq itkisi",
  "extrapolated pressure": "ekstrapolyasiya təzyiqi", "VFM oil rate": "VFM neft debiti",
  "raw downhole pressure points": "quyudibi təzyiqin xam nöqtələri", "field gauge": "yataq manometri", "synthetic demo": "sintetik demo",
  "analytic curve: R² near 1 is expected": "analitik əyri: R² ≈ 1 gözləniləndir",
  "Shaded band = automatic radial-flow (MTR) window where the Bourdet derivative is flattest. The Horner line, R² and RMSE use only the raw pressure points inside that window.":
    "Kölgəli zolaq = Bourdet törəməsinin ən düz olduğu avtomatik radial axın (MTR) pəncərəsi. Horner xətti, R² və RMSE yalnız bu pəncərədəki xam təzyiq nöqtələrindən hesablanır.",
  "Equations for this build-up": "Bu bərpa üçün düsturlar", "live engine values": "canlı hesablama dəyərləri",
  "Engineering interpretation": "Mühəndis şərhi", "Multi-well comparison & intervention priority": "Quyuların müqayisəsi və müdaxilə prioriteti",
  "sort by header · open by row": "başlıqla sırala · sətirlə aç", "Engineer alerts": "Mühəndis xəbərdarlıqları",
  "Last build-up": "Son bərpa", "k, mD": "k, mD", "Skin S": "Skin S", "Δp skin, psi": "Δp skin, psi", "Condition": "Vəziyyət",
  "Gain if S → 0, STB/d": "S → 0 olsa artım, STB/gün", "Severity": "Səviyyə", "Type": "Növ", "Detail": "Təfərrüat",
  "severe damage": "ciddi zədələnmə", "moderate damage": "orta zədələnmə", "slight damage": "yüngül zədələnmə",
  "undamaged / slightly stimulated": "zədəsiz / yüngül stimulyasiya", "high skin": "yüksək skin", "sensor": "sensor",
  "data quality": "data keyfiyyəti", "erosional": "eroziya", "alert": "həyəcan", "warn": "xəbərdarlıq", "info": "məlumat",
  "Fit quality on the radial window": "Radial pəncərədə uyğunluq keyfiyyəti", "Engineering notes": "Mühəndis qeydləri", "Close": "Bağla",
  "Input check": "Giriş yoxlaması", "Source": "Mənbə", "Access code": "Giriş kodu",
  "Every calendar month of the loaded period compared with the previous month and with the period average ·":
    "Yüklənmiş dövrün hər təqvim ayı əvvəlki ayla və dövr ortalaması ilə müqayisə olunur ·",
  "Average daily rate by month": "Aylar üzrə orta gündəlik debit", "label = change vs previous month": "etiket = əvvəlki aya görə dəyişmə",
  "Month-by-month comparison": "Ay-ay müqayisə", "click a month to zoom every chart": "aya klikləyin: bütün qrafiklər həmin aya keçir",
  "Well × month": "Quyu × ay", "Change %": "Dəyişmə %", "3D production surface": "3D hasilat səthi",
  "well × day × oil rate · drag to rotate": "quyu × gün × neft debiti · fırlatmaq üçün sürüşdürün",
  "Cumulative oil": "Kumulyativ neft", "Cumulative water": "Kumulyativ su", "Cumulative gas": "Kumulyativ qaz",
  "Best month · oil": "Ən yaxşı ay · neft", "Largest monthly drop · oil": "Ən böyük aylıq düşüş · neft", "First → last month · oil": "İlk → son ay · neft",
  "Month": "Ay", "Days": "Gün", "Oil, STB/d": "Neft, STB/gün", "Water, STB/d": "Su, STB/gün", "Gas, Mscf/d": "Qaz, Mscf/gün",
  "vs prev": "əvvəlki aya", "vs avg": "ortalamaya", "Water cut, %": "Su payı, %", "GOR, scf/STB": "GOR, scf/STB", "Uptime, %": "İşləmə, %",
  "Shut-in well-days": "Bağlı quyu-gün", "Oil total, STB": "Neft cəmi, STB", "average daily rate, STB/d": "orta gündəlik debit, STB/gün",
  "average daily rate": "orta gündəlik debit", "vs previous month": "əvvəlki aya görə", "model": "model", "period average": "dövr ortalaması",
  "Log-log diagnostic": "Log-log diaqnostika", "Horner semilog": "Horner yarımloqarifmik", "Cartesian build-up": "Dekart bərpa əyrisi",
  "Wrong access code.": "Giriş kodu səhvdir.", "Field data is protected: enter the access code.": "Yataq datası qorunur: giriş kodunu daxil edin.",
  "wells ·": "quyu ·", "build-ups": "bərpa", "Virtual flow metering × pressure-transient analysis ·": "Virtual debit ölçmə (VFM) × təzyiq bərpası analizi (PTA) ·",
  "Oil · water, STB/d": "Neft · su, STB/gün", "detected": "aşkarlanıb", "STB/d": "STB/gün", "Mscf/d": "Mscf/gün",
  "Generating synthetic demo field": "Sintetik demo yataq hazırlanır", "Loading field data": "Yataq datası yüklənir",
};
const AZ_RE = [
  [/^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (severe damage|moderate damage|slight damage|undamaged \/ slightly stimulated)$/, (m, a, b) => `${a} · ${AZ[b]}`],
  [/^([\d,.]+) STB\/d$/, "$1 STB/gün"], [/^([\d,.]+) → ([\d,.]+) STB\/d$/, "$1 → $2 STB/gün"],
  [/^· (\d+) wells$/, "· $1 quyu"], [/^latest day (.+)$/, "son gün $1"], [/^(\d+) build-ups · (\d+) alerts$/, "$1 bərpa · $2 xəbərdarlıq"],
  [/^(\d+) open · (\d+) total$/, "$1 açıq · $2 cəmi"], [/^· field total$/, "· yataq cəmi"],
  [/^Forward-CV MAPE · 90-day horizon · n = (.+)$/, "İrəli CV MAPE · 90 günlük üfüq · n = $1"],
  [/^Unseen well \(LOWO\) MAPE · n = (.+)$/, "Görülməmiş quyu (LOWO) MAPE · n = $1"],
  [/^radial window · n = (.+)$/, "radial pəncərə · n = $1"], [/^No alerts for (.+)\.$/, "$1 üçün xəbərdarlıq yoxdur."],
  [/^Open (.+) build-up$/, "$1 bərpasını aç"], [/^Show (.+) everywhere$/, "Hər yerdə $1 göstər"],
  [/^(Demo|Field data) loaded · (\d+) wells · (\d+) build-ups$/, (m, a, b, c) => `${a === "Demo" ? "Demo" : "Yataq datası"} yükləndi · ${b} quyu · ${c} bərpa`],
  [/^(Synthetic demo|Field data) · (\d+) well-days$/, (m, a, b) => `${a === "Field data" ? "Yataq datası" : "Sintetik demo"} · ${b} quyu-gün`],
  [/^Processing · (.+) · (.+) s$/, "Emal olunur · $1 · $2 s"], [/^Uploading (.+)$/, "Yüklənir $1"],
];
function tr(s) {
  if (I18N.lang !== "az" || !s) return s;
  const k = s.trim(); if (!k) return s;
  if (AZ[k] !== undefined) return s.replace(k, AZ[k]);
  for (const [re, rep] of AZ_RE) if (re.test(k)) return s.replace(k, k.replace(re, rep));
  return s;
}
function trNode(n) {
  if (n.nodeType === 3) {
    if (n.__src === undefined || (n.nodeValue !== n.__out)) n.__src = n.nodeValue;
    const out = tr(n.__src); n.__out = out; if (n.nodeValue !== out) n.nodeValue = out;
  } else if (n.nodeType === 1 && !/^(SCRIPT|STYLE|svg)$/i.test(n.tagName) && !n.classList.contains("js-plotly-plot")) {
    if (n.placeholder !== undefined && n.placeholder) { if (!n.__ph) n.__ph = n.placeholder; n.placeholder = tr(n.__ph); }
    n.childNodes.forEach(trNode);
  }
}
function trAll() { document.documentElement.lang = I18N.lang; trNode(document.body); }
new MutationObserver((ms) => ms.forEach((m) => { if (m.type === "characterData") trNode(m.target); else m.addedNodes.forEach(trNode); }))
  .observe(document.documentElement, { childList: true, subtree: true, characterData: true });
if (window.Plotly) Plotly.register({ moduleType: "locale", name: "az", dictionary: { "Zoom": "Böyüt", "Pan": "Sürüşdür", "Reset axes": "Oxları sıfırla", "Download plot as a png": "PNG kimi yüklə" },
  format: { months: MONTHS_LONG_AZ, shortMonths: MONTHS.az, days: ["Bazar", "Bazar ertəsi", "Çərşənbə axşamı", "Çərşənbə", "Cümə axşamı", "Cümə", "Şənbə"],
    shortDays: ["B.", "B.e.", "Ç.a.", "Ç.", "C.a.", "C.", "Ş."], date: "%d.%m.%Y", decimal: ".", thousands: "," } });
function monthLabel(ym, long) { const m = +ym.slice(5, 7) - 1; return (long && I18N.lang === "az" ? MONTHS_LONG_AZ[m] : MONTHS[I18N.lang][m]) + (long ? " " + ym.slice(0, 4) : ""); }
