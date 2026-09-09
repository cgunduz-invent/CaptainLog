# Worklog Takibi — Doluluk & Giriş Gecikmesi

İki metriği kişi bazında tek tabloda, her iş günü Slack'e basar.

## Metrikler
**1) Aylık doluluk (month-to-date)**
- Hedef = aydaki iş günü × `DAILY_TARGET_HOURS` (8s). Hafta sonu + TR resmi
  tatilleri (`holidays` paketi) düşülür.
- Bugüne kadar geçen iş günü baz alınır (ayın 11'iyse 10'una kadar).
- **Doluluk %** = o ay girilen saat / bugüne kadarki beklenen saat.
- Renk: 🟢 ≥%90, 🟡 ≥%70, 🔴 <%70.

**2) Giriş gecikmesi (son 30 gün)**
- Her worklog için `created` (girildiği gün) − `started` (yapıldığı gün).
- OrtGeç = ortalama gecikme (gün), Maks = en kötü gecikme.

Tablo: `Kişi | Dol% | Log/Hedef | OrtGeç | Maks`, en düşük doluluk en üstte.

## Ayarlanabilir eşikler (`worklog_quality_report.py` → CONFIG)
`DAILY_TARGET_HOURS`, `LOOKBACK_DAYS`, `GRACE_DAYS`, `HORIZON_DAYS`,
`BAD_DAYS`, `COMP_GREEN`, `COMP_YELLOW`, `EXTRA_OFF_DAYS` (şirkete özel kapalı
günler), `LEAVE_HOURS_BY_ACCOUNT` (kişi bazında izin saati; hedeften düşülür).

## Kurulum
1. Kapsam: `PEOPLE_RAW` içine "İsim <email>," bloğunu yapıştır (32 kişi hazır
   girildi). Alternatif: `JIRA_GROUP="..."` verirsen grup üyeleri otomatik
   çekilir. Her iki yolda da hiç log girmemiş kişi 0% satırıyla görünür.
   E-postalar Jira accountId'sine çevrilir; eşleşmeyenler Slack'te ayrı bir
   "⚠️ Jira'da eşleşmeyen" satırında listelenir (sessizce düşmez).
2. Repo secrets: `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
   `SLACK_BOT_TOKEN` (chat:write), `SLACK_CHANNEL`. Botu kanala ekle.

## Çalıştırma
```
python worklog_quality_report.py --selftest   # hesaplama testi (Jira/Slack gerekmez)
python worklog_quality_report.py --dry-run     # çek+hesapla, Slack'e basmaz
python worklog_quality_report.py               # canlı
```
Cron: `.github/workflows/worklog-quality.yml` (Pzt–Cum 09:30 TR).

## Notlar
- `worklog.created` sonradan düzenlemede değişmez → gecikmeyi doğru yakalar.
- İzin günü düşülmezse izindeki kişi haksız yere düşük doluluk görünür;
  `LEAVE_HOURS_BY_ACCOUNT` veya Kolay İK entegrasyonuyla düzeltilebilir.
