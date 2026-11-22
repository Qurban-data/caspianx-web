# CaspianX · Well Intelligence

SOCAR Hackathon 2026 · komanda **CaspianX** (Qurban Rəcəbov · Nurlan Abıyev · Zəhra Kazımbəyova)

Virtual debit ölçmə (VFM), avtomatik təzyiq bərpası analizi (PTA) və mühəndis üçün veb tətbiq.

## Təhvil faylları (Deliverables)

| # | Tələb | Fayl |
|---|---|---|
| 1 | Phase 1 (VFM) nəticəsi, CSV və Excel | [`results/vfm_submission.csv`](results/vfm_submission.csv) · [`.xlsx`](results/vfm_submission.xlsx) (2 730 quyu-gün, 10 quyu, 2025-01-01 → 2025-09-30) |
| 1 | Phase 2 (PTA) nəticəsi, CSV və Excel | [`results/pta_submission.csv`](results/pta_submission.csv) · [`.xlsx`](results/pta_submission.xlsx) (6 bərpa: k, skin, Δp_skin, Horner meyli) |
| 2 | Təlim olunmuş model, `.pkl` | [`models/vfm_model_FINAL.pkl`](models/vfm_model_FINAL.pkl) (tətbiqdəki nüsxə: `web_app/models/`) |
| 3 | Bütün xanaları işlədilmiş notebook | [`notebook/CaspianX_Solution.ipynb`](notebook/CaspianX_Solution.ipynb) |
| 4 | PTA qrafikləri | [`pta_plots/`](pta_plots/) (hər bərpa üçün log-log + Bourdet və Horner) |
| – | Veb tətbiq (canlı) | https://caspianx-web-q725.vercel.app/ · kod: [`web_app/`](web_app/) |

PTA-nın əvvəlki versiyası arxivdədir: `results/archive/`.

## Tətbiq

| Tələb | Tətbiqdə |
|---|---|
| Xam datanın yüklənməsi | sol panel: CSV · Excel · JSON · ZIP (istənilən ölçü, lokal işlədikdə) |
| Yataq ölçmələrinin vizuallaşdırılması | **Ölçmələr** |
| VFM ilə debit proqnozu (model tətbiqin içindədir) | **VFM və proqnozlar**, **Sahə icmalı**, **Aylıq təhlil** |
| Quyu testi qrafikləri | **PTA və lay**: log-log, Horner, MDH, Dekart |
| Lay parametrləri | k, skin, m, Δp_skin, p* kartları və düsturlar |
| Lay davranışının şərhi | mühəndis şərhi, quyuların müqayisəsi, xəbərdarlıqlar |
| VFM və PTA nəticələrinin ixracı | VFM CSV · PTA CSV · Excel · Hesabat |

### Sintetik demo data

"Demo datanı yüklə" düyməsi **sintetik** (süni yaradılmış) yataq açır: 2025-01-01 → 2025-09-30, 10 quyu (GL-01 … GL-10).
Bu, real yataq datası deyil; tətbiqdə "Sintetik demo data" nişanı, Excel ixracında `Info` vərəqi və hesabatda xəbərdarlıq bunu göstərir.
Generator deterministikdir (`web_app/demo_data.py`, seed 2026); nəticə `web_app/demo_static/` qovluğunda statik fayl kimi saxlanır
(`python tools/build_demo_static.py` ilə yenilənir). Datada qəsdən qoyulmuş hadisələr: GL-03-də iyulda skin zədəsi və 12 avqust bərpası
(skin ≈ +8), GL-01 (mart) və GL-04 (may) bərpaları, GL-06-da fevralda 2 günlük bağlanma, GL-08-də mayda 4 günlük WHP sensor boşluğu.

Demo sessiyasının sabit identifikatoru var (`demo`): serverless hostda sorğu başqa instansa düşsə, həmin instans eyni demonu yenidən qurur,
ona görə demo heç vaxt "Session not found or expired" xətası vermir.

### Copilot

Copilot dil modeli istifadə etmir: sualdan quyu, ay/rüb, metrik və niyyət çıxarılır (`web_app/copilot.py`), cavab yalnız yüklənmiş datanın
hesablanmış cədvəllərindən (aylıq debitlər, sensorlar, PTA, anomaliya qaydaları, validasiya xülasəsi) yığılır. Data aralığından kənar dövr,
olmayan quyu və ya aidiyyatsız sual üçün rəqəm vermir və bunu açıq deyir.

### İşə salma

```
pip install -r web_app/requirements.txt
uvicorn web_app.main:app --port 8000
```

Windows: `tools/START_WEB_APP.bat`. Data fayllarını `data_autoload/` qovluğuna qoysanız, tətbiq açılışda onları özü yükləyir.

## Metod (qısa)

- **VFM:** Model A = ştuser fizikası `q = C_w (choke/100)² √(WHP − P_ds)` + məhdud Ridge düzəlişi, su payı və GOR trendləri;
  Model B = birbaşa Ridge; təqdim edilən nəticə = hər faza üçün həndəsi qarışıq. P10–P90 = nümunədən kənar xətaların empirik kvantilləri.
- **Validasiya:** 4 kəsimli 90 günlük irəli CV (n = 109): MAPE neft 3.17 %, su 4.73 %, qaz 6.55 %; P10–P90 əhatəsi 81.8 / 75.1 / 75.2 %.
- **PTA:** Bourdet törəməsinin ən düz hissəsində avtomatik radial pəncərə, Horner xətti,
  `k = 162.6 q B μ / (m h)`, `s = 1.151[(p1h − pwf)/m − log(k/φμc_t r_w²) + 3.23]`, `Δp_skin = 0.87 m s`.

Xam yataq datası bu repoda saxlanmır.
