# Worklog Takibi — Doluluk & Giriş Gecikmesi

İki metriği kişi bazında tek tabloda, her iş günü Slack'e basar.

## Rapor ayı
Her iki metrik de aynı pencereyi kullanır: ayın `PREV_MONTH_UNTIL_DAY`'ine
kadar (dahil, varsayılan 3) **bir önceki ay tümüyle**, sonrasında **içinde
bulunulan ay** (ay başından düne). Böylece ay başında önceki ay kapanmadan
sıfırlanmaz.

## Metrikler
**1) Aylık doluluk**
- Hedef = rapor ayındaki iş günü × `DAILY_TARGET_HOURS` (8s). Hafta sonu + TR
  resmi tatilleri (`holidays` paketi) düşülür.
- İçinde bulunulan ayda bugüne kadar geçen iş günü baz alınır (MTD); önceki
  ayda ayın tamamı baz alınır.
- **Doluluk %** = rapor ayında girilen saat / beklenen saat.
- Renk: 🟢 ≥%90, 🟡 ≥%70, 🔴 <%70.

**2) Giriş gecikmesi (rapor ayı)**
- Her worklog için `created` (girildiği gün) − `started` (yapıldığı gün).
- Sadece rapor ayı içinde yapılan (`started`) worklog'lar sayılır.
- OrtGeç = ortalama gecikme (gün), Maks = en kötü gecikme.

Tablo: `Kişi | Dol% | Log/Hedef | OrtGeç | Maks`, en düşük doluluk en üstte.

## Ayarlanabilir eşikler (`worklog_quality_report.py` → CONFIG)
`DAILY_TARGET_HOURS`, `PREV_MONTH_UNTIL_DAY`, `GRACE_DAYS`, `HORIZON_DAYS`,
`BAD_DAYS`, `COMP_GREEN`, `COMP_YELLOW`, `EXTRA_OFF_DAYS` (şirkete özel kapalı
günler), `LEAVE_HOURS_BY_ACCOUNT` (kişi bazında izin saati; hedeften düşülür).

## Kurulum
1. Kapsam: `PEOPLE_RAW` içine "İsim <email>," bloğunu yapıştır (46 kişi hazır
   girildi). Alternatif: `JIRA_GROUP="..."` verirsen grup üyeleri otomatik
   çekilir. Her iki yolda da hiç log girmemiş kişi 0% satırıyla görünür.
   E-postalar Jira accountId'sine çevrilir; eşleşmeyenler Slack'te ayrı bir
   "⚠️ Jira'da eşleşmeyen" satırında listelenir (sessizce düşmez).
2. Repo secrets: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
   `SLACK_BOT_TOKEN` (chat:write). Hedef kanallar workflow'da sabittir
   (aşağıya bak); botu **her iki** kanala da ekle. Kanalı elle
   çalıştırmada değiştirmek için `workflow_dispatch` → `channel` girdisini
   kullan.

## Çalıştırma
```
python worklog_quality_report.py --selftest   # hesaplama testi (Jira/Slack gerekmez)
python worklog_quality_report.py --dry-run     # çek+hesapla, Slack'e basmaz
python worklog_quality_report.py               # canlı
```
Cron: `.github/workflows/worklog-quality.yml`. İki zamanlama:
- **Perşembe** (09:30 TR) → `C08DPFWG5PB`
- **Cuma** (09:30 TR) → `C07L0PF1X6E`

Not: GitHub Actions cron'u kuyruğa alır; mesaj 09:30 TR yerine birkaç
dakika–15+ dk gecikmeli düşebilir (saat hesabı doğru, gecikme GitHub kaynaklı).

## Notlar
- `worklog.created` sonradan düzenlemede değişmez → gecikmeyi doğru yakalar.
- İzin günü düşülmezse izindeki kişi haksız yere düşük doluluk görünür;
  `LEAVE_HOURS_BY_ACCOUNT` veya Kolay İK entegrasyonuyla düzeltilebilir.
