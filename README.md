# CaspianX · Well Intelligence

SOCAR Hackathon 2026 · komanda **CaspianX** (Qurban Rəcəbov · Nurlan Abıyev · Zəhra Kazımbəyova)

Virtual debit ölçmə (VFM), avtomatik təzyiq bərpası analizi (PTA) və mühəndis üçün veb tətbiq.

## Təhvil faylları (Deliverables)

| # | Tələb | Yeri |
|---|---|---|
| 1 | Phase 1 (VFM) və Phase 2 (PTA) nəticə faylları, CSV və Excel | `results/vfm_submission.csv` · `.xlsx`, `results/pta_submission.csv` · `.xlsx` |
| 2 | Təlim olunmuş model, `.pkl` | `models/vfm_model_FINAL.pkl` |
| 3 | Kod və nəticələri olan, bütün xanaları işlədilmiş notebook | `notebook/CaspianX_Solution.ipynb` |
| 4 | PTA qrafikləri qovluğu | `pta_plots/` (hər bərpa üçün log-log + Horner) |

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
