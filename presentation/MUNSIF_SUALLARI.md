# CaspianX · Münsif sualları və cavablar

Cavablardakı rəqəmlər validasiya fayllarından və tətbiqdən götürülüb. "Bilmirik" yazılan yerlərdə rəqəm uydurmayın.

## VFM və validasiya

**1. Overfitting olmadığını necə sübut edirsiniz?**
Validasiya zamana görədir: 4 kəsim tarixi, hər birində keçmiş testlərlə öyrədib növbəti 90 günü proqnoz edirik (109 müqayisə). Təlim xətası 1.63 / 2.64 / 3.95 %, nümunədən kənar xəta 3.17 / 4.73 / 6.55 % (neft / su / qaz). Fərq təxminən 2 faiz bəndidir. Bütün hiperparametrlər yalnız təlim hissəsinin daxilində seçilir.

**2. Niyə neyron şəbəkə və ya gradient boosting istifadə etmədiniz?**
Cəmi 85 separator testi var, quyu başına təxminən 9. Bu qədər az datada mürəkkəb model əzbərləyir. Ona görə fizika tənliyi + güclü requlyarizasiyalı Ridge seçdik. Daha mürəkkəb variantları sınadıq, statistik üstünlük göstərmədilər.

**3. Model dəqiq nə hesablayır?**
Model A: `q_maye = C_w · (choke/100)² · √(WHP − P_ds) · exp(r)`. C_w hər quyu üçün testlərdən kalibrlənir, r Ridge düzəlişidir və ±0.3 ilə məhdudlaşdırılıb. Neft/su/qaz bölgüsü quyu üzrə su payı və GOR trendlərindəndir. Model B hər fazanı birbaşa Ridge ilə proqnoz edir. Yekun nəticə ikisinin həndəsi qarışığıdır (A çəkisi: neft 0.2, su 0.5, qaz 0.3).

**4. Qarışıq çəkilərini necə seçdiniz? Bu, test datasına baxmaq deyilmi?**
Çəkilər zamana görə sıralanmış validasiyada seçilib və sonra dəyişdirilməyib. Gizli (oktyabr–dekabr) dövrə heç vaxt baxılmayıb.

**5. Hansı girişlərdən istifadə edirsiniz?**
Quyuağzı təzyiqi, ştuserdən sonrakı təzyiq, quyudibi təzyiqi, ştuser açıqlığı, qazlift sərfi. Annulus təzyiqi və quyudibi temperaturu yalnız test günlərində var (istehsal günlərində əhatə 0 %), ona görə modelə daxil edilməyib.

**6. Separator testi az olan və ya heç olmayan quyuda model necə işləyir?**
Heç görmədiyi quyuda (leave-one-well-out) xəta 16.1 / 28.7 / 19.1 %-dir. Bunu gizlətmirik: yeni quyuda ən azı bir neçə test lazımdır ki, C_w kalibrlənsin. Tətbiq belə quyunu "kalibrlənməyib" kimi işarələyir.

**7. Model nə vaxt etibarsızdır?**
Üç halda: (1) quyunun testi yoxdur; (2) son testdən 180 gündən çox keçib, bu halda trendlər dondurulur; (3) ştuser və ya qazlift rejimi təlim diapazonundan kəskin kənara çıxıb. Hər üç halda yeni separator testi tövsiyə olunur.

**8. P10–P90 zolağı nə deməkdir və doğrudurmu?**
Zolaq nümunədən kənar xətaların empirik 10 % və 90 % kvantillərindən qurulur. Nominal əhatə 80 %-dir, faktiki: neft 81.8 %, su 75.1 %, qaz 75.2 %. Yəni su və qazda zolaq bir az dardır, bunu bilirik.

**9. Test şəraiti ilə istehsal şəraiti fərqlidirmi?**
Bəli. Test zamanı quyu test separatoruna axır və ştuserdən sonrakı təzyiq aşağıdır. P_ds/WHP nisbətinin medianı testlərdə 0.59, istehsal günlərində 0.66-dır. Bu sürüşməni ölçmüşük və model onu ştuser tənliyindəki təzyiq fərqi vasitəsilə nəzərə alır.

**10. Daha mürəkkəb fizika (kritik axın, Gilbert/Ros/Achong) sınadınızmı?**
Bəli, eyni dondurulmuş validasiyada. Kritik axın keçidi su və qaz xətasını artırdı (5.30 və 7.17 %). Sıxlıq əlaməti fərq vermədi (3.20 / 4.63 / 6.55 %). Statistik üstünlük olmadığı üçün sadə modeli saxladıq.

**11. Rəsmi metrikada nəticəniz neçədir, baseline-dan nə qədər yaxşıdır?**
Öz validasiyamızda MAPE 3.17 / 4.73 / 6.55 %-dir. Liderlər cədvəlindəki rəsmi balı portaldan özünüz deyin. Sadə baseline (son testin irəli daşınması) ilə müqayisə rəqəmi bizdə hesablanmayıb; soruşulsa, bunu açıq deyin.

**12. Bağlı quyuda model nə göstərir?**
Tam bağlanma günündə debit dəqiq 0-dır. Qismən işləyən gündə debit işləmə payına (uptime) vurulur. 9 ayda 5 tam bağlanma quyu-günü var.

**13. Datada hansı keyfiyyət problemləri var idi?**
Separator testlərində 5 mənfi dəyər və vahid uyğunsuzluğu (sm³/gün əvəzinə Mscf/gün) tapıldı. Hər biri təlimdən əvvəl düzəldilir və tətbiqdə "data keyfiyyəti" xəbərdarlığı kimi göstərilir. Gündəlik datada dublikat və pozulmuş tarix yoxdur.

## PTA

**14. Radial axın (MTR) pəncərəsini necə seçirsiniz?**
Avtomatik: Bourdet törəməsi hesablanır (L = 0.1), quyu lüləsi tutumu (wellbore storage) "donqarından" sonra törəmənin ən düz olduğu ən azı yarım loqarifmik dövr tapılır və ±10 % daxilində genişləndirilir. Pəncərə qrafikdə kölgəli göstərilir.

**15. Wellbore storage və sərhəd effektləri nəticəni pozmurmu?**
Erkən hissə (törəmə maksimumuna qədər) pəncərədən çıxarılır. Gec hissədə törəmə aşağı düşürsə (sərhəd əlaməti), ən düz hissə seçildiyi üçün o da kənarda qalır.

**16. k, skin və Δp_skin hansı düsturlarla hesablanır?**
`k = 162.6 q B μ / (m h)`; `s = 1.151[(p_1saat − p_wf)/m − log(k/(φ μ c_t r_w²)) + 3.23]`; `Δp_skin = 0.87 m s`. p_1saat Horner xəttindən götürülür, xam datadan yox.

**17. q və t_p haradan gəlir?**
q bağlanmadan əvvəlki son tam işləmə günündə VFM-in neft debitidir. t_p = 24 · N_p / q; N_p = tamamlama cədvəlindəki 01.01.2025-ə qədər kumulyativ neft + həmin tarixdən bağlanmaya qədər VFM cəmi. Yəni PTA bizim VFM-ə əsaslanır.

**18. PVT parametrləri (B, μ, c_t, h, φ, r_w) haradan gəldi? Səhv olsa nə olar?**
Hamısı verilən `reservoir_pvt.csv` faylındandır, heç biri fərz edilməyib. Düstura görə k birbaşa μ·B/h ilə mütənasibdir: μ 10 % səhvdirsə, k da 10 % dəyişir. Skin isə loqarifm altında olduğu üçün az dəyişir.

**19. R² = 0.999 çox yaxşı deyilmi? Hamarlanmış əyridə hesablamısınız?**
Xeyr, pəncərədəki xam manometr nöqtələrində hesablanıb: R² 0.9992–0.9999, RMSE 0.07–0.48 psi, 31–113 nöqtə. Radial axında təzyiq log-zamana görə həqiqətən düz xəttdir, ona görə yüksək R² gözləniləndir. R² 1.0000 yalnız sintetik demo datada çıxır.

**20. Nəticələriniz dayanıqlıdırmı?**
Bərpanın yalnız ilk yarısı ilə təkrar hesablayanda k orta hesabla 1.8 %, skin 0.11 dəyişir. Daha mürəkkəb reqressiya üsullarını da sınadıq: daha dayanıqlı idilər, amma skin üzrə istinad dəyərlərindən daha çox uzaqlaşdılar, ona görə Horner üsulunu saxladıq.

**21. Mənfi skin nə deməkdir?**
GL-02, GL-04 və GL-08-də skin mənfidir (−0.6, −1.3, −1.7): quyuətrafı zona zədələnməyib və ya yüngül stimulyasiya olunub. Müsbət skin (GL-01: +5.9) zədələnmə deməkdir.

**22. "1 422 barel artım" rəqəmi realdırmı?**
Bu, skin tam sıfırlansa və depressiya sabit qalsa, nəzəri yuxarı həddir (GL-01: 734, GL-10: 548, GL-06: 140 STB/gün). Proqnoz kimi yox, stimulyasiya namizədlərini sıralamaq üçün göstəririk.

## Məhsul və tətbiq

**23. Tətbiq münsifin verdiyi yeni data ilə işləyəcəkmi?**
Bəli. Quyu adları, tarixlər və saylar koda yazılmayıb, fayllardan oxunur. Lokal versiyada 236 MB-lıq dəqiqəlik test faylı təxminən 24 saniyəyə emal olundu. Faylda çatışmayan sütun varsa, tətbiq nəyin çatışmadığını yazır.

**24. Real vaxtda necə işləyəcək? SOCAR-da tətbiqi nə qədər çəkər?**
Hazırda fayl yükləmə ilə işləyir. Real vaxt üçün sensor arxivinə (historian) qoşulma lazımdır; bu hələ edilməyib, yol xəritəsindədir. Müddət barədə dəqiq rəqəm vermirik, çünki SOCAR sistemlərinin interfeysini görməmişik.

**25. Data təhlükəsizliyi?**
Tətbiq bir prosesdir, şirkət şəbəkəsində və ya noutbukda işləyir. Xarici xidmətə sorğu yoxdur, qrafik kitabxanası və şriftlər daxildədir. Yüklənən fayllar sessiya bitəndə silinir. Xam yataq datası repoda saxlanmır.

**26. Sizi mövcud kommersiya VFM-lərdən nə fərqləndirir?**
Kommersiya məhsulları ilə birbaşa müqayisə aparmamışıq, ona görə "daha yaxşıyıq" demirik. Fərqimiz: düsturlar və bütün aralıq dəyərlər tətbiqdə açıq göstərilir, VFM və PTA bir yerdədir (PTA debiti VFM-dən alır) və qeyri-müəyyənlik zolağının faktiki əhatəsi ölçülüb.

**27. Süni intellekt alətlərindən istifadə etmisinizmi?**
Bəli, kod yazarkən köməkçi kimi istifadə etmişik. Metod seçimi, validasiya dizaynı və nəticələrin mühəndis yoxlaması komandanındır; hər rəqəm koddan gəlir və təkrar istehsal olunur. (Hakatonun qaydaları AI alətlərinin bəyan edilməsini tələb edirsə, bunu mütləq qeyd edin.)

**28. Komandada kim nə edib?**
Qurban: data hazırlığı, VFM modeli və validasiya. Zəhra: PTA metodikası və mühəndis şərhi. Nurlan: tətbiq və arxitektura.
