# CaspianX · Çıxış mətni

Rəqəmlərin hamısı tətbiqdən və validasiya fayllarından götürülüb.

## 5 dəqiqəlik variant

**Qurban (slayd 1–2, ~50 s)**
Hörmətli münsiflər, biz CaspianX komandasıyıq. Verilən datada 10 quyu və 273 gün var, yəni 2730 quyu-gün. Amma cəmi 85 separator testi aparılıb. Deməli, günlərin yalnız 3 faizində debit real ölçülüb, qalan 97 faizində mühəndis debiti bilmir. Altı təzyiq bərpası isə əl ilə şərh olunmalıdır.

**Nurlan (slayd 3, ~35 s)**
Həllimiz bir platformadır. Mühəndis faylları yükləyir; sistem datanı yoxlayır, gündəlik debitləri hesablayır, bərpaları özü tapıb şərh edir və tədbir siyahısı verir. Real yataq qovluğu 6.5 saniyəyə emal olunur və arada əl ilə heç bir addım yoxdur.

**Qurban (slayd 4–6, ~100 s)**
Əvvəlcə dataya baxdıq. Doqquz ayda yatağın orta neft debiti gündə 15 027 bareldən 12 126 barelə düşüb, yəni 19 faiz. Su payı isə 30 faizdən 37 faizə qalxıb. Ən böyük aylıq düşüş avqustdadır.
Modeldə əvvəl fizika gəlir: ştuser tənliyi. Maşın öyrənməsi yalnız kiçik, məhdud düzəliş edir. İkinci model birbaşa reqressiyadır, nəticə bu ikisinin qarışığıdır. Cəmi 85 test olduğu üçün mürəkkəb modellərdən bilərəkdən imtina etdik.
Yoxlama dürüstdür: keçmişdə öyrədirik, növbəti 90 günü proqnoz edirik. Neftdə xəta 3.17 faiz, suda 4.73, qazda 6.55 faizdir. Təlim xətası ilə fərq təxminən 2 faiz bəndidir, yəni model əzbərləmir. P10–P90 zolağı testlərin 75–82 faizini əhatə edir.

**Zəhra (slayd 7–8, ~80 s)**
Təzyiq bərpası analizində radial axın pəncərəsini sistem özü seçir: Bourdet törəməsinin ən düz olduğu hissəni. Həmin pəncərədə Horner xətti çəkilir, meyldən keçiricilik və skin hesablanır. Uyğunluq xam manometr nöqtələrində yoxlanır: R kvadrat 0.9992-dən yuxarıdır, xəta 0.5 psi-dən azdır.
Nəticə mühəndis üçün birbaşa qərardır. GL-01-də skin 5.9-dur, bu ciddi zədələnmədir. Skin aradan qaldırılsa, nəzəri artım gündə 734 bareldir. Üç zədələnmiş quyu üzrə cəmi 1 422 barel. Bu yuxarı həddir, proqnoz deyil, amma stimulyasiyanı hansı quyudan başlamağı göstərir.

**Nurlan (slayd 9–11, ~50 s)**
Tətbiq Azərbaycan dilindədir. Aylıq təhlil səhifəsi hər ayı əvvəlki ayla müqayisə edir, üçölçülü səth bütün quyuları bir baxışda göstərir. Hər şey şirkətin öz şəbəkəsində işləyir, data kənara çıxmır. Növbəti addım real vaxt sensor axınına qoşulmaqdır.

**Qurban (slayd 12, ~10 s)**
Diqqətinizə görə təşəkkür edirik. Suallarınızı gözləyirik.

## 3 dəqiqəlik variant

**Qurban (~60 s):** Problem: 2730 quyu-günün cəmi 3 faizində debit ölçülüb. Biz mövcud sensorlardan hər gün üçün neft, su və qaz debitini hesablayırıq. Model əvvəl ştuser fizikasına, sonra kiçik statistik düzəlişə əsaslanır. 90 gün irəli proqnozda xəta: neft 3.17, su 4.73, qaz 6.55 faiz.

**Zəhra (~60 s):** Altı təzyiq bərpası avtomatik şərh olunub. Sistem radial pəncərəni özü seçir, Horner üsulu ilə keçiricilik və skin hesablayır; R kvadrat 0.9992-dən yuxarıdır. GL-01-də skin 5.9-dur: stimulyasiya ilə nəzəri artım gündə 734 barelə qədərdir.

**Nurlan (~50 s):** Bunların hamısı bir veb tətbiqdədir: fayl yüklə, nəticəni gör, ixrac et. Doqquz ayın hər biri əvvəlki ayla müqayisə olunur, 3D səth yatağı bir baxışda göstərir. Tətbiq şirkət şəbəkəsində işləyir. Təşəkkür edirik.

## Demo ssenarisi (münsif test datası verəndə)

1. Lokal tətbiqi açın (`localhost:8765`), faylları sol panelə atın.
2. **Sahə icmalı**: KPI kartları və tədbir növbəsi.
3. **Aylıq təhlil**: ay cədvəli, bir aya klik, 3D səthi fırladın.
4. **VFM**: bir quyu, P10–P90 zolağı, separator testləri ilə müqayisə.
5. **PTA**: bərpa seçin, dörd qrafik, düsturlar, şərh.
6. **İxrac**: VFM CSV və PTA CSV yükləyin.
