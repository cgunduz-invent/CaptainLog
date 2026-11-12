#!/usr/bin/env python3
"""
Worklog Takibi — Aylık Doluluk & Giriş Gecikmesi
================================================

İki metriği kişi bazında tek tabloda, her gün Slack'e basar:

RAPOR AYI (her iki metrik için ortak pencere)
   Ayın 3'üne kadar (dahil) BİR ÖNCEKİ ay tümüyle gösterilir.
   Ayın 3'ünden sonra İÇİNDE BULUNULAN aya odaklanılır (ay başından düne).

1) AYLIK DOLULUK
   Hedef = rapor ayındaki iş günü sayısı × günlük hedef saat (8s).
   İçinde bulunulan ayda bugüne kadar geçen iş günü baz alınır (MTD);
   önceki ayda ayın tamamı baz alınır.
   Doluluk % = rapor ayında girilen toplam saat / beklenen saat.
   Hafta sonu + Türkiye resmi tatilleri düşülür.

2) GİRİŞ GECİKMESİ (rapor ayı)
   Her worklog için created (girildiği gün) − started (işin yapıldığı gün).
   Sadece rapor ayı içinde YAPILAN (started) worklog'lar sayılır.
   Fark büyüdükçe disiplin düşer.

Ortam değişkenleri (GitHub Actions secrets):
  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, SLACK_BOT_TOKEN, SLACK_CHANNEL

Kullanım:
  python worklog_quality_report.py --selftest   # sadece hesaplama testi
  python worklog_quality_report.py --dry-run     # çek+hesapla, Slack'e basmaz
  python worklog_quality_report.py               # canlı
"""

from __future__ import annotations

import os
import sys
import json
import base64
import calendar
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:
    requests = None

import holidays as holidays_lib

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
TZ = ZoneInfo("Europe/Istanbul")
TR_MONTHS = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
             "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
PREV_MONTH_UNTIL_DAY = 3      # ayın bu gününe kadar (dahil) önceki ay gösterilir
DAILY_TARGET_HOURS = 8.0      # doluluk hedefi: iş günü × bu

# Gecikme skorlaması:
GRACE_DAYS = 1               # <= bu gecikme "zamanında"
HORIZON_DAYS = 7             # >= bu gecikmede kredi 0 (lineer sönüm)
BAD_DAYS = 3                 # > bu gecikme "geç" sayılır

# Doluluk eşikleri (renk):
COMP_GREEN = 90              # >= yeşil
COMP_YELLOW = 70            # >= sarı, altı kırmızı

# Kapsam (birini kullan):
JIRA_GROUP: str = ""    # doldurulursa Jira grubu üyeleri çekilir; boşsa PEOPLE_RAW
PROJECT_KEYS: list[str] = []

# "İsim <email>," bloğunu olduğu gibi yapıştırabilirsin (virgül/satır sonu serbest):
PEOPLE_RAW: str = """
Aret Cilingir <aret.cilingir@invent.ai>,
Artun Sarıoğlu <artun.sarioglu@invent.ai>,
Asena Ayyıldız <asena.ayyildiz@invent.ai>,
Ayşegül Arabacı <aysegul.arabaci@invent.ai>,
Bahar Şahin <bahar.sahin@invent.ai>,
Bekir Seçmen <bekir.secmen@invent.ai>,
Burcu Barın <burcu.barin@invent.ai>,
Büşra Tekin <busra.tekin@invent.ai>,
Can Köne <can.kone@invent.ai>,
Caner Gündüz <caner.gunduz@invent.ai>,
Cansel Memişoğlu <cansel.kaya@invent.ai>,
Deniz Nil Cengiz <deniz.cengiz@invent.ai>,
Didem Paloğlu <didem.paloglu@invent.ai>,
Doğukan Durukan <dogukan.durukan@invent.ai>,
Ece Yurtsever <ece.yurtsever@invent.ai>,
Ecem Sert <ecem.sert@invent.ai>,
Ekinsu Çiçek <ekinsu.cicek@invent.ai>,
Elif Çelik <elif.celik@invent.ai>,
Emre Gezer <emre.gezer@invent.ai>,
Emre Kondul <emre.kondul@invent.ai>,
Emre Şahna <emre.sahna@invent.ai>,
Esra Akgün <esra.akgun@invent.ai>,
Ferhat Cengiz <ferhat.cengiz@invent.ai>,
Furkan Oğuz Gümüş <furkan.gumus@invent.ai>,
Gökhan Evlek <gokhan.evlek@invent.ai>,
Gözde Gözütok <gozde.gozutok@invent.ai>,
Hamza Kılınç <hamza.kilinc@invent.ai>,
Hümeyra Arslan <humeyra.arslan@invent.ai>,
Kübra Gülcan <kubra.gulcan@invent.ai>,
Leyli Jafarova <leyli.jafarova@invent.ai>,
Mehmet Tilgen <mehmet.tilgen@invent.ai>,
Melike Şahin <melike.sahin@invent.ai>,
Memduh Yusuf Duranlı <memduh.duranli@invent.ai>,
Mert Altıntaş <mert.altintas@invent.ai>,
Müge Önder <muge.onder@invent.ai>,
Müzeyyen Yakan <muzeyyen.yakan@invent.ai>,
Neslihan Tanyeri <neslihan.tanyeri@invent.ai>,
Rana Kaya <rana.kaya@invent.ai>,
Selin Abatay <selin.abatay@invent.ai>,
Serap Seyrek <serap.seyrek@invent.ai>,
Serhat Evrenosoglu <serhat.evrenosoglu@invent.ai>,
Sümeyra Demir <sumeyra.demir@invent.ai>,
Tolga Aydın <tolga.aydin@invent.ai>,
Uğur Kılınç <ugur.kilinc@invent.ai>,
Yiğitcan Aksoy <yigitcan.aksoy@invent.ai>,
Yunus Akça <yunus.akca@invent.ai>
"""

# Müşteri bazında log grafiği: hedefler + Jira proje eşleşmesi repo içindeki
# customer_targets.csv'den okunur (kaynak: "CX Hub Planning 2026_v2.xlsx" →
# 2026_FTE_Target_v4 sayfası). Sütunlar:
#   Müşteri | Jira Projeleri (boşluk/virgül ile ayrılmış) | Q1 | Q2 | Q3 | Q4  (FTE)
# Aylık hedef saat = FTE × rapor ayındaki iş günü × DAILY_TARGET_HOURS
# (1.0 FTE = tam zamanlı bir kişinin o aydaki saati; 21 iş günü -> 168s).
# Müşteri grafiğinde log, issue'nun "Related Project" alanındaki projeye sayılır
# (SVCDEV/CX gibi iç projelerdeki iş, ilgili müşteriye gider); alan boşsa issue'nun
# kendi projesi kullanılır.
RELATED_PROJECT_FIELD = "customfield_11100"
CUSTOMER_TARGETS_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "customer_targets.csv")
CUSTOMER_CHART_MAX = 25       # hedefsiz müşterilerden en çok bu kadarı (en çok log girilenler);
                              # hedefi olan müşteriler her zaman grafikte

# Şirkete özel ekstra kapalı günler (resmi tatil dışı), ISO tarih:
EXTRA_OFF_DAYS: set[date] = set()
# Örn: EXTRA_OFF_DAYS = {date(2026, 12, 31)}

# İzin ayrı worklog olarak girildiği için hedeften izin düşmeye GEREK YOK
# (loglanan izin zaten doluluğa sayılır). Bu kanca normalde boş kalır; sadece
# istisnai bir durumda kişi bazında saat düşmek istersen kullan. accountId -> saat.
LEAVE_HOURS_BY_ACCOUNT: dict[str, float] = {}


def parse_people(raw: str) -> list[tuple[str, str]]:
    """'İsim <email>,' bloğunu [(isim, email), ...] listesine çevirir."""
    import re
    out = []
    for m in re.finditer(r"([^<,\n]+?)\s*<\s*([^>]+?)\s*>", raw):
        name = m.group(1).strip().strip(",").strip()
        email = m.group(2).strip().lower()
        if name and email:
            out.append((name, email))
    return out


PEOPLE: list[tuple[str, str]] = parse_people(PEOPLE_RAW)

# --------------------------------------------------------------------------- #
# Tarih / gecikme yardımcıları
# --------------------------------------------------------------------------- #
def parse_jira_dt(s: str) -> datetime:
    s = s.strip()
    if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


def lag_days(started: datetime, created: datetime) -> int:
    d = (created.astimezone(TZ).date() - started.astimezone(TZ).date()).days
    return max(0, d)


def decay(lag: int) -> float:
    if lag <= GRACE_DAYS:
        return 1.0
    if lag >= HORIZON_DAYS:
        return 0.0
    return max(0.0, 1.0 - (lag - GRACE_DAYS) / (HORIZON_DAYS - GRACE_DAYS))


def business_days(start: date, end: date, hol) -> int:
    """[start, end] aralığındaki iş günü (hafta sonu + tatil + ekstra hariç)."""
    if end < start:
        return 0
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5 and d not in hol and d not in EXTRA_OFF_DAYS:
            n += 1
        d += timedelta(days=1)
    return n


def reporting_month(today: date) -> tuple[int, int]:
    """Rapor ayı (yıl, ay): ayın PREV_MONTH_UNTIL_DAY'ine kadar (dahil) önceki
    ay, sonrasında içinde bulunulan ay."""
    if today.day <= PREV_MONTH_UNTIL_DAY:
        prev_last = today.replace(day=1) - timedelta(days=1)
        return prev_last.year, prev_last.month
    return today.year, today.month


def month_bounds(today: date) -> tuple[date, date, date]:
    """(rapor ayı başı, doluluk sınırı, rapor ayı sonu).

    İçinde bulunulan ayda sınır = dün (MTD); önceki ayda sınır = ay sonu (tam ay).
    """
    ry, rm = reporting_month(today)
    month_start = date(ry, rm, 1)
    last = calendar.monthrange(ry, rm)[1]
    month_end = date(ry, rm, last)
    if (ry, rm) == (today.year, today.month):
        cutoff = today - timedelta(days=1)          # içinde bulunulan ay: bugün hariç
    else:
        cutoff = month_end                          # önceki ay: tümü
    return month_start, cutoff, month_end


# --------------------------------------------------------------------------- #
# Kişi istatistikleri
# --------------------------------------------------------------------------- #
@dataclass
class PersonStats:
    name: str
    account_id: str = ""
    # gecikme (rapor ayı)
    n_logs: int = 0
    total_seconds: int = 0
    lags: list[int] = field(default_factory=list)
    weighted_credit: float = 0.0
    late_seconds: int = 0
    # aylık doluluk (MTD)
    mtd_seconds: int = 0
    expected_hours: float = 0.0        # bugüne kadarki hedef (izin düşülmüş)

    def add_lag(self, lag: int, seconds: int):
        self.n_logs += 1
        self.total_seconds += seconds
        self.lags.append(lag)
        self.weighted_credit += (seconds / 3600.0) * decay(lag)
        if lag > BAD_DAYS:
            self.late_seconds += seconds

    def add_mtd(self, seconds: int):
        self.mtd_seconds += seconds

    # --- gecikme ---
    @property
    def total_hours(self) -> float:
        return self.total_seconds / 3600.0

    @property
    def discipline_score(self) -> float:
        if self.total_hours == 0:
            return 0.0
        return 100.0 * self.weighted_credit / self.total_hours

    @property
    def avg_lag(self) -> float:
        return statistics.fmean(self.lags) if self.lags else 0.0

    @property
    def max_lag(self) -> int:
        return max(self.lags) if self.lags else 0

    # --- doluluk ---
    @property
    def mtd_hours(self) -> float:
        return self.mtd_seconds / 3600.0

    @property
    def completeness(self) -> float | None:
        if self.expected_hours <= 0:
            return None
        return 100.0 * self.mtd_hours / self.expected_hours


def build_stats(worklogs: list[dict], allowed_ids: set[str] | None,
                mtd_start: date, mtd_end: date,
                roster: dict[str, str] | None = None) -> list[PersonStats]:
    people: dict[str, PersonStats] = {}
    # Grup üyelerini önceden koy: log'u olmayan da 0 satırıyla görünsün
    for aid, name in (roster or {}).items():
        people[aid] = PersonStats(name=name, account_id=aid)
    for w in worklogs:
        author = w.get("author") or {}
        aid = author.get("accountId", "")
        if allowed_ids is not None and aid not in allowed_ids:
            continue
        started = parse_jira_dt(w["started"]).astimezone(TZ)
        created = parse_jira_dt(w["created"]).astimezone(TZ)
        seconds = int(w.get("timeSpentSeconds", 0))
        ps = people.setdefault(aid, PersonStats(
            name=author.get("displayName", aid or "?"), account_id=aid))
        # Her iki metrik de rapor ayı penceresinde (started içinde) sayılır
        if mtd_start <= started.date() <= mtd_end:
            ps.add_lag(lag_days(started, created), seconds)
            ps.add_mtd(seconds)
    return list(people.values())


def project_of(w: dict) -> str:
    """Worklog'un sayılacağı proje: issue'nun "Related Project" alanı doluysa o
    (örn. SVCDEV-8105 -> FLOCS), boşsa issue anahtarının projesi (LP2CS-251 -> LP2CS)."""
    rel = (w.get("_related_project") or "").strip()
    if rel:
        return rel
    return (w.get("_issue_key") or "").split("-")[0] or "?"


def build_customer_hours(worklogs: list[dict], allowed_ids: set[str] | None,
                         start: date, end: date,
                         proj_to_cust: dict[str, str]) -> dict[str, float]:
    """Rapor ayı penceresinde (started) müşteri -> toplam saat.

    Kişi tablosuyla aynı kapsam: sadece izlenen kişilerin worklog'ları.
    Eşleşmeyen proje kendi anahtarıyla ayrı satır olur (sessizce düşmez).
    """
    out: dict[str, float] = {}
    for w in worklogs:
        aid = (w.get("author") or {}).get("accountId", "")
        if allowed_ids is not None and aid not in allowed_ids:
            continue
        started = parse_jira_dt(w["started"]).astimezone(TZ).date()
        if not (start <= started <= end):
            continue
        proj = project_of(w)
        cust = proj_to_cust.get(proj.upper(), proj)
        out[cust] = out.get(cust, 0.0) + int(w.get("timeSpentSeconds", 0)) / 3600.0
    return out


def predict_remaining(logged_h: float, wd_elapsed: int, wd_remaining: int) -> float:
    """Kalan iş günlerinde girilmesi beklenen saat = log / geçen iş günü × kalan iş günü."""
    if wd_elapsed <= 0 or wd_remaining <= 0:
        return 0.0
    return logged_h / wd_elapsed * wd_remaining


def fte_to_month_hours(fte: float, wd_full: int) -> float:
    """FTE -> aylık hedef saat: FTE × ayın iş günü × 8."""
    return fte * wd_full * DAILY_TARGET_HOURS


def parse_customer_targets(csv_text: str, month: int, wd_full: int
                           ) -> tuple[dict[str, str], dict[str, float], list[str]]:
    """Hedef CSV'si -> (proje -> müşteri, müşteri -> aylık hedef saat,
    Jira projesi tanımsız müşteriler).

    Sadece Jira projesi tanımlı müşterilerin hedefi grafiğe girer; projesi
    olmayanın log'u hiç sayılamayacağı için 0 log + hedef göstermek yanıltır.
    """
    import csv
    import io
    import re
    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return {}, {}, []
    head = [h.strip().lower() for h in rows[0]]
    q = f"q{(month - 1) // 3 + 1}"

    def col(*names):
        for i, h in enumerate(head):
            if h in names:
                return i
        raise RuntimeError(f"Hedef CSV'sinde sütun bulunamadı: {names} (başlıklar: {rows[0]})")

    ci, pi, ti = col("müşteri", "customer"), col("jira projeleri", "projects"), col(q)
    proj_to_cust, targets, unmapped = {}, {}, []
    for r in rows[1:]:
        if len(r) <= max(ci, pi, ti) or not r[ci].strip():
            continue
        cust = r[ci].strip()
        projects = [p.strip().upper() for p in re.split(r"[,;/\s]+", r[pi]) if p.strip()]
        for p in projects:
            proj_to_cust[p] = cust
        t = r[ti].strip().replace(",", ".")
        try:
            fte = float(t)
        except ValueError:
            continue                  # bu çeyrek için hedef yok
        if not projects:
            if fte > 0:
                unmapped.append(cust)
            continue
        if fte > 0:                   # 0 FTE = o çeyrek hedef yok
            targets[cust] = fte_to_month_hours(fte, wd_full)
    return proj_to_cust, targets, unmapped


def load_customer_targets(month: int, wd_full: int, path: str = CUSTOMER_TARGETS_CSV
                          ) -> tuple[dict[str, str], dict[str, float], list[str]]:
    with open(path, encoding="utf-8") as f:
        return parse_customer_targets(f.read(), month, wd_full)


def assign_expected(stats: list[PersonStats], expected_to_date_h: float):
    for p in stats:
        leave = LEAVE_HOURS_BY_ACCOUNT.get(p.account_id, 0.0)
        p.expected_hours = max(0.0, expected_to_date_h - leave)


# --------------------------------------------------------------------------- #
# Jira (Cloud REST v3)
# --------------------------------------------------------------------------- #
class Jira:
    def __init__(self, base_url: str, email: str, token: str):
        self.base = base_url.rstrip("/")
        auth = base64.b64encode(f"{email}:{token}".encode()).decode()
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Basic {auth}",
                               "Accept": "application/json",
                               "Content-Type": "application/json"})

    def resolve_people(self, people: list[tuple[str, str]]
                       ) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """[(isim, email)] -> (roster {accountId: isim}, çözülemeyenler)."""
        roster, unresolved = {}, []
        for name, email in people:
            aid = None
            r = self.s.get(f"{self.base}/rest/api/3/user/search",
                           params={"query": email})
            if not r.ok:
                print(f"        [HTTP {r.status_code}] user/search '{email}' "
                      f"başarısız: {r.text[:120]}", file=sys.stderr)
            users = r.json() if r.ok else []
            # 1) e-posta birebir eşleşiyorsa (görünür ise)
            for u in users:
                if (u.get("emailAddress") or "").lower() == email:
                    aid = u["accountId"]
                    break
            # 2) e-posta gizliyse ama tek aktif atlassian kullanıcısı döndüyse
            if not aid:
                cand = [u for u in users if u.get("active")
                        and u.get("accountType") == "atlassian"]
                if len(cand) == 1:
                    aid = cand[0]["accountId"]
            # 3) son çare: isimle ara, birebir görünen ad eşleşmesi
            if not aid:
                r2 = self.s.get(f"{self.base}/rest/api/3/user/search",
                                params={"query": name})
                for u in (r2.json() if r2.ok else []):
                    if ((u.get("displayName") or "").strip().lower()
                            == name.lower() and u.get("active")):
                        aid = u["accountId"]
                        break
            if aid:
                roster[aid] = name
            else:
                unresolved.append((name, email))
        return roster, unresolved

    def find_group_id(self, name: str) -> str:
        """Grup adından groupId bulur (tam eşleşme, büyük/küçük harf duyarsız)."""
        r = self.s.get(f"{self.base}/rest/api/3/groups/picker",
                       params={"query": name, "maxResults": 50})
        r.raise_for_status()
        for g in r.json().get("groups", []):
            if g.get("name", "").lower() == name.lower():
                return g["groupId"]
        raise RuntimeError(
            f"'{name}' Jira grubu bulunamadı. Eğer bu bir Google Workspace "
            f"dağıtım listesiyse Jira onu tanımaz; PEOPLE_EMAILS'e üyeleri "
            f"yaz ya da grubu Jira'da oluştur.")

    def group_members(self, group_id: str) -> dict[str, str]:
        """Aktif üyeler: accountId -> displayName."""
        roster, start_at = {}, 0
        while True:
            r = self.s.get(f"{self.base}/rest/api/3/group/member",
                           params={"groupId": group_id, "startAt": start_at,
                                   "maxResults": 50,
                                   "includeInactiveUsers": "false"})
            r.raise_for_status()
            data = r.json()
            for u in data.get("values", []):
                if u.get("active") and u.get("accountType") == "atlassian":
                    roster[u["accountId"]] = u.get("displayName", u["accountId"])
            if data.get("isLast") or not data.get("values"):
                break
            start_at += data.get("maxResults", 50)
        return roster

    def search_issues(self, jql: str) -> list[tuple[str, str | None]]:
        """JQL -> [(issue anahtarı, Related Project anahtarı ya da None)]."""
        out, token = [], None
        while True:
            body = {"jql": jql, "maxResults": 100,
                    "fields": ["key", RELATED_PROJECT_FIELD]}
            if token:
                body["nextPageToken"] = token
            r = self.s.post(f"{self.base}/rest/api/3/search/jql", json=body)
            r.raise_for_status()
            data = r.json()
            for i in data.get("issues", []):
                rel = (i.get("fields") or {}).get(RELATED_PROJECT_FIELD)
                out.append((i["key"], rel.get("key") if isinstance(rel, dict) else None))
            token = data.get("nextPageToken")
            if not token or data.get("isLast"):
                break
        return out

    def issue_worklogs(self, key: str, started_after_ms: int) -> list[dict]:
        out, start_at = [], 0
        while True:
            r = self.s.get(f"{self.base}/rest/api/3/issue/{key}/worklog",
                           params={"startedAfter": started_after_ms,
                                   "startAt": start_at, "maxResults": 1000})
            r.raise_for_status()
            data = r.json()
            out += data.get("worklogs", [])
            start_at += data.get("maxResults", 0)
            if start_at >= data.get("total", 0):
                break
        return out


def fetch_worklogs(fetch_since: datetime):
    base = os.environ["JIRA_BASE_URL"]
    jira = Jira(base, os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])

    roster: dict[str, str] = {}         # accountId -> displayName (tam kapsam)
    allowed_ids: set[str] | None = None
    unresolved: list[tuple[str, str]] = []

    if JIRA_GROUP:
        gid = jira.find_group_id(JIRA_GROUP)
        roster = jira.group_members(gid)
        allowed_ids = set(roster)
        print(f"[bilgi] '{JIRA_GROUP}' grubunda {len(roster)} aktif üye.",
              file=sys.stderr)
    elif PEOPLE:
        roster, unresolved = jira.resolve_people(PEOPLE)
        allowed_ids = set(roster)
        print(f"[bilgi] {len(roster)}/{len(PEOPLE)} kişi eşleşti; "
              f"{len(unresolved)} çözülemedi.", file=sys.stderr)
        for name, email in unresolved:
            print(f"        [çözülemedi] {name} <{email}>", file=sys.stderr)

    if allowed_ids:
        ids = ",".join(f'"{a}"' for a in allowed_ids)
        author_clause = f"worklogAuthor in ({ids}) AND "
    else:
        author_clause = ""

    proj_clause = f"project in ({','.join(PROJECT_KEYS)}) AND " if PROJECT_KEYS else ""
    jql = f'{proj_clause}{author_clause}worklogDate >= "{fetch_since.strftime("%Y-%m-%d")}"'
    print(f"[bilgi] JQL: {jql}", file=sys.stderr)

    since_ms = int(fetch_since.timestamp() * 1000)
    issues = jira.search_issues(jql)
    n_rel = sum(1 for k, rel in issues if rel and rel != k.split("-")[0])
    print(f"[bilgi] {len(issues)} issue ({n_rel} tanesi Related Project ile başka "
          f"projeye sayılıyor), worklog'lar çekiliyor...", file=sys.stderr)

    all_wl = []
    for k, rel in issues:
        for w in jira.issue_worklogs(k, since_ms):
            if parse_jira_dt(w["started"]).astimezone(TZ) >= fetch_since:
                w["_issue_key"] = k            # müşteri grafiği için proje anahtarı
                w["_related_project"] = rel    # doluysa müşteri grafiğinde bu sayılır
                all_wl.append(w)
    print(f"[bilgi] {len(all_wl)} worklog toplandı.", file=sys.stderr)
    return all_wl, allowed_ids, roster, unresolved


# --------------------------------------------------------------------------- #
# Slack (Block Kit)
# --------------------------------------------------------------------------- #
def comp_flag(c: float | None) -> str:
    if c is None:
        return "⚪"
    if c >= COMP_GREEN:
        return "🟢"
    if c >= COMP_YELLOW:
        return "🟡"
    return "🔴"


def build_slack_blocks(stats: list[PersonStats], today: date,
                       wd_elapsed: int, wd_full: int, report_month: int,
                       is_prev_month: bool = False,
                       unresolved: list[tuple[str, str]] | None = None) -> list[dict]:
    # En düşük dolulukta olan en üstte; eşitlikte gecikmesi yüksek olan
    stats = sorted(stats, key=lambda p: ((p.completeness if p.completeness
                   is not None else 1e9), -p.avg_lag))

    header = {"type": "header", "text": {"type": "plain_text",
              "text": "🕒 Worklog Takibi — Doluluk & Gecikme"}}
    target_to_date = wd_elapsed * DAILY_TARGET_HOURS
    if is_prev_month:
        scope = (f"{TR_MONTHS[report_month]} ayı (tamamı): {wd_full} iş günü • "
                 f"hedef {target_to_date:.0f}s ({DAILY_TARGET_HOURS:.0f}s/gün)")
    else:
        scope = (f"{TR_MONTHS[report_month]} ayı (ay başından bugüne): "
                 f"{wd_elapsed}/{wd_full} iş günü geçti • bugüne dek hedef "
                 f"{target_to_date:.0f}s ({DAILY_TARGET_HOURS:.0f}s/gün)")
    ctx = {"type": "context", "elements": [{"type": "mrkdwn", "text": (
        f"*{today.strftime('%d.%m.%Y')}* • {scope} • "
        f"gecikme penceresi: rapor ayı")}]}

    h = f"{'Kişi':<18}{'Dol%':>5}{'Log/Hedef':>12}{'OrtGeç':>8}{'Maks':>6}"
    lines = [h, "-" * len(h)]
    for p in stats:
        name = p.name[:17]
        c = p.completeness
        cstr = f"{c:.0f}%" if c is not None else "—"
        loghed = f"{p.mtd_hours:.0f}/{p.expected_hours:.0f}s"
        if p.n_logs == 0:
            gec, maks = f"{'—':>7}", f"{'—':>5}"
        else:
            gec, maks = f"{p.avg_lag:>6.1f}g", f"{p.max_lag:>4}g"
        lines.append(f"{comp_flag(c)}{name:<17}{cstr:>5}{loghed:>12}{gec}{maks}")

    blocks = [header, ctx]
    chunk, count = [], 0
    for ln in lines:
        chunk.append(ln)
        count += len(ln) + 1
        if count > 2600:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": "```" + "\n".join(chunk) + "```"}})
            chunk, count = [], 0
    if chunk:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": "```" + "\n".join(chunk) + "```"}})

    low = [p for p in stats if p.completeness is not None
           and p.completeness < COMP_YELLOW]
    if low:
        names = ", ".join(f"{p.name} ({p.completeness:.0f}%)" for p in low[:8])
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*🔴 Doluluk düşük:* {names}"}})
    late = [p for p in stats if p.avg_lag > BAD_DAYS]
    if late:
        names = ", ".join(f"{p.name} ({p.avg_lag:.1f}g)"
                          for p in sorted(late, key=lambda x: -x.avg_lag)[:8])
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*⏰ Geç giriş:* {names}"}})
    if unresolved:
        names = ", ".join(n for n, _ in unresolved[:12])
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": f"⚠️ Jira'da eşleşmeyen ({len(unresolved)}): "
                               f"{names} — bu kişiler tabloda değil."}]})
    return blocks


def post_to_slack(blocks: list[dict], channel: str | None = None,
                  thread_ts: str | None = None) -> str | None:
    return post_message(blocks, channel, thread_ts).get("ts")


def post_message(blocks: list[dict], channel: str | None = None,
                 thread_ts: str | None = None) -> dict:
    """chat.postMessage; Slack yanıtını döner. Kanal yerine kullanıcı ID'si (U...)
    verilirse bot DM atar; yanıttaki "channel" o DM'in gerçek ID'sidir (D...)."""
    payload = {"channel": channel or os.environ["SLACK_CHANNEL"], "blocks": blocks,
               "text": "Worklog takip raporu"}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    r = requests.post("https://slack.com/api/chat.postMessage",
                      headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}",
                               "Content-Type": "application/json; charset=utf-8"},
                      json=payload)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack hatası: {data}")
    print("[bilgi] Tablo Slack'e gönderildi.", file=sys.stderr)
    return data


# --------------------------------------------------------------------------- #
# Scatter grafiği (doluluk vs gecikme)
# --------------------------------------------------------------------------- #
def render_scatter(stats: list[PersonStats], today: date,
                   wd_elapsed: int, wd_full: int, path: str) -> bool:
    """Log'u olan kişiler için doluluk-gecikme scatter'ı çizer. PNG kaydeder."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    pts = [p for p in stats if p.n_logs > 0 and p.completeness is not None]
    if not pts:
        return False

    def color(p):
        if p.completeness > 100:
            return "#8e5bd6"          # %100 üstü (hedefin üzerinde giriş)
        late, low = p.avg_lag > BAD_DAYS, p.completeness < COMP_YELLOW
        if late and low:
            return "#d64545"          # geç + eksik (öncelik)
        if not late and p.completeness >= COMP_YELLOW:
            return "#2e9e5b"          # zamanında + yeterli
        return "#e0a13b"              # karışık

    fig, ax = plt.subplots(figsize=(11.5, 8.2), dpi=140)
    ax.axvline(BAD_DAYS, color="#999", ls="--", lw=1, zorder=1)
    ax.axhline(COMP_YELLOW, color="#999", ls="--", lw=1, zorder=1)

    xmax = max(7.0, max(p.avg_lag for p in pts) + 0.5)
    ax.text(0.15, 98, "zamanında + yeterli", color="#2e9e5b", fontsize=9, style="italic")
    ax.text(xmax - 0.1, 2, "geç + eksik", color="#d64545", fontsize=9,
            style="italic", ha="right")

    for p in pts:
        ax.scatter(p.avg_lag, p.completeness, s=70, color=color(p),
                   edgecolor="white", linewidth=0.8, zorder=3)
        ax.annotate(p.name.split()[0], (p.avg_lag, p.completeness),
                    xytext=(4, 4), textcoords="offset points",
                    fontsize=7.5, color="#333")

    ax.set_xlim(0, xmax)
    ax.set_ylim(-4, 122)
    ax.axhline(100, color="#bbb", ls=":", lw=1, zorder=1)
    ax.set_xlabel("Ortalama giriş gecikmesi (gün)  →  kötüleşir", fontsize=11)
    ax.set_ylabel("Aylık doluluk (%)  →  iyileşir", fontsize=11)
    ax.set_title(f"Worklog Disiplini — Doluluk vs Giriş Gecikmesi "
                 f"({today.strftime('%d.%m.%Y')}, {wd_elapsed}/{wd_full} iş günü)",
                 fontsize=12.5, weight="bold", pad=12)
    ax.grid(True, alpha=0.25, zorder=0)
    legend = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#d64545",
               markersize=9, label="Geç + eksik (öncelik)"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#e0a13b",
               markersize=9, label="Karışık"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#2e9e5b",
               markersize=9, label="Zamanında + yeterli"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#8e5bd6",
               markersize=9, label="%100 üstü"),
    ]
    ax.legend(handles=legend, loc="lower center", ncol=4, fontsize=9,
              frameon=True, bbox_to_anchor=(0.5, -0.135))
    skipped = [p.name for p in stats if p.n_logs == 0]
    if skipped:
        fig.text(0.01, 0.005, "Grafik dışı (rapor ayında log yok): "
                 + ", ".join(skipped), fontsize=7.5, color="#777")
    fig.tight_layout(rect=[0, 0.02, 1, 1])
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return True


# --------------------------------------------------------------------------- #
# Müşteri bazında log grafiği (yatay bar)
# --------------------------------------------------------------------------- #
def render_customer_chart(cust_hours: dict[str, float], targets: dict[str, float],
                          today: date, wd_elapsed: int, wd_full: int,
                          report_month: int, path: str,
                          unmapped: list[str] | None = None) -> bool:
    """Her satır bir müşteri: log toplamı (mavi) + üstüne yığılmış kalan günler
    tahmini (turuncu) = ay sonu tahmini; aynı hizada aylık log hedefi (siyah
    çizgi). PNG kaydeder."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    # Hedefi olan müşteriler her zaman görünür (hiç log girilmese de); hedefsizlerden
    # en çok log girilen CUSTOMER_CHART_MAX tanesi eklenir.
    no_target = sorted((c for c in cust_hours if c not in targets),
                       key=lambda c: -cust_hours[c])[:CUSTOMER_CHART_MAX]
    names = set(targets) | set(no_target)
    if not names:
        return False
    wd_remaining = max(0, wd_full - wd_elapsed)
    rows = sorted(names, key=lambda c: -cust_hours.get(c, 0.0))
    rows.reverse()                                   # en çok log en üstte

    C_LOG, C_PRED, C_TGT = "#3b6fd6", "#e0a13b", "#222222"
    h = 0.6
    fig, ax = plt.subplots(figsize=(11.5, max(4.0, 0.42 * len(rows) + 1.8)), dpi=140)
    xmax = 0.0
    for i, c in enumerate(rows):
        logged = cust_hours.get(c, 0.0)
        pred = predict_remaining(logged, wd_elapsed, wd_remaining)
        total = logged + pred
        ax.barh(i, logged, height=h, color=C_LOG, zorder=2)
        ax.barh(i, pred, left=logged, height=h, color=C_PRED, zorder=2)
        tgt = targets.get(c)
        label = f" {logged:.0f}s" if pred <= 0 else f" {logged:.0f} + {pred:.0f} = {total:.0f}s"
        if tgt:
            label += f"  ({100.0 * total / tgt:.0f}% hedef)"
        ax.text(max(total, tgt or 0.0), i, label, va="center", fontsize=8, color="#333")
        if tgt is not None:
            ax.plot([tgt, tgt], [i - h / 2 - 0.08, i + h / 2 + 0.08], color=C_TGT, lw=2.4,
                    solid_capstyle="butt", zorder=3)
            ax.text(tgt, i + h / 2 + 0.1, f"{tgt:.0f}s", ha="center", va="bottom",
                    fontsize=7, color=C_TGT)
        xmax = max(xmax, total, tgt or 0.0)

    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(rows, fontsize=9.5)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_xlim(0, xmax * 1.28 + 1)
    ax.set_xlabel("Saat", fontsize=10.5)
    ax.grid(True, axis="x", alpha=0.25, zorder=0)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title(f"Müşteri Bazında Log — {TR_MONTHS[report_month]} "
                 f"({today.strftime('%d.%m.%Y')}, {wd_elapsed}/{wd_full} iş günü geçti, "
                 f"{wd_remaining} kaldı)", fontsize=12.5, weight="bold", pad=12)
    legend = [
        Patch(color=C_LOG, label="Log toplamı"),
        Patch(color=C_PRED, label=f"Tahmin: kalan {wd_remaining} iş günü "
                                  "(log / geçen iş günü × kalan iş günü)"),
        Line2D([0], [0], color=C_TGT, lw=2.4,
               label=f"Aylık hedef (FTE × {wd_full} iş günü × {DAILY_TARGET_HOURS:.0f}s)"),
    ]
    ax.legend(handles=legend, loc="upper center", ncol=3, fontsize=8.5,
              frameon=False, bbox_to_anchor=(0.5, -0.07 if len(rows) > 6 else -0.12))
    fig.tight_layout()
    if unmapped:
        import textwrap
        note = textwrap.fill("Jira projesi tanımsız (grafikte yok): " + ", ".join(unmapped),
                             width=170)
        fig.text(0.01, -0.01, note, fontsize=7.5, color="#777", va="top")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return True


def upload_image_to_slack(path: str, title: str, comment: str,
                          thread_ts: str | None = None, channel: str | None = None):
    """Slack'e resim yükler (files.getUploadURLExternal akışı; files:write gerekir)."""
    token = os.environ["SLACK_BOT_TOKEN"]
    channel = channel or os.environ["SLACK_CHANNEL"]
    size = os.path.getsize(path)
    fname = os.path.basename(path)

    # 1) yükleme URL'si al
    r = requests.get("https://slack.com/api/files.getUploadURLExternal",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"filename": fname, "length": size})
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(f"Slack upload URL hatası: {d}")
    upload_url, file_id = d["upload_url"], d["file_id"]

    # 2) dosyayı PUT et
    with open(path, "rb") as f:
        up = requests.post(upload_url, files={"file": (fname, f)})
    if up.status_code != 200:
        raise RuntimeError(f"Slack dosya yükleme hatası: {up.status_code}")

    # 3) yüklemeyi tamamla (kanala gönder)
    payload = {"files": [{"id": file_id, "title": title}],
               "channel_id": channel, "initial_comment": comment}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    r = requests.post("https://slack.com/api/files.completeUploadExternal",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json; charset=utf-8"},
                      json=payload)
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(f"Slack completeUpload hatası: {d}")
    print(f"[bilgi] '{title}' Slack'e gönderildi.", file=sys.stderr)


# --------------------------------------------------------------------------- #
def compute_windows(now: datetime):
    today = now.date()
    month_start, mtd_cutoff, month_end = month_bounds(today)
    is_prev_month = (month_start.year, month_start.month) != (today.year, today.month)
    hol = holidays_lib.Turkey(
        years=sorted({month_start.year, month_end.year, today.year}))
    wd_elapsed = business_days(month_start, mtd_cutoff, hol)
    wd_full = business_days(month_start, month_end, hol)
    # Worklog'ları rapor ayı başından çek (hem doluluk hem gecikme aynı pencere)
    fetch_since = datetime.combine(month_start, datetime.min.time(), TZ)
    return dict(today=today, month_start=month_start, mtd_cutoff=mtd_cutoff,
                month_end=month_end, is_prev_month=is_prev_month,
                wd_elapsed=wd_elapsed, wd_full=wd_full, fetch_since=fetch_since)


# --------------------------------------------------------------------------- #
def _selftest():
    def wl(name, aid, started, created, hours):
        return {"author": {"accountId": aid, "displayName": name},
                "started": started, "created": created,
                "timeSpentSeconds": int(hours * 3600)}

    # 11 Eylül'de çalıştığımızı varsayalım: MTD = 1–10 Eylül (8 iş günü, hedef 64s)
    now = datetime(2026, 9, 11, 9, 30, tzinfo=TZ)
    win = compute_windows(now)
    assert win["wd_elapsed"] == 8, win["wd_elapsed"]      # 1-10 Eylül
    assert win["wd_full"] == 22, win["wd_full"]           # tüm Eylül
    expected_to_date = win["wd_elapsed"] * DAILY_TARGET_HOURS
    assert expected_to_date == 64.0

    sample = [
        # Ada: 8 iş gününün 8'ini de 8'er saat, hep aynı gün girmiş -> 64s / doluluk 100
        *[wl("Ada", "a1", f"2026-09-{d:02d}T10:00:00.000+03:00",
             f"2026-09-{d:02d}T18:00:00.000+03:00", 8)
          for d in (1, 2, 3, 4, 7, 8, 9, 10)],
        # Ece: sadece 2 gün (16s) girmiş ve geç girmiş -> doluluk 25, gecikmeli
        wl("Ece", "e1", "2026-09-01T10:00:00.000+03:00",
           "2026-09-08T18:00:00.000+03:00", 8),   # 7 gün gecikme
        wl("Ece", "e1", "2026-09-02T10:00:00.000+03:00",
           "2026-09-09T18:00:00.000+03:00", 8),   # 7 gün gecikme
    ]
    stats = build_stats(sample, {"a1", "e1", "z1"},
                        win["month_start"], win["mtd_cutoff"],
                        roster={"a1": "Ada", "e1": "Ece", "z1": "Boş Kişi"})
    assign_expected(stats, expected_to_date)
    by = {p.name: p for p in stats}

    assert by["Ada"].mtd_hours == 64.0
    assert abs(by["Ada"].completeness - 100.0) < 0.01, by["Ada"].completeness
    assert by["Ada"].avg_lag == 0.0
    assert by["Ece"].mtd_hours == 16.0
    assert abs(by["Ece"].completeness - 25.0) < 0.01, by["Ece"].completeness
    assert by["Ece"].avg_lag == 7.0
    # Hiç log girmemiş üye: roster'dan geldi, doluluk %0, log yok
    assert by["Boş Kişi"].n_logs == 0
    assert by["Boş Kişi"].completeness == 0.0, by["Boş Kişi"].completeness

    print("SELFTEST OK")
    print(f"  İş günü: {win['wd_elapsed']}/{win['wd_full']} • bugüne dek hedef {expected_to_date:.0f}s")
    for n in ("Ada", "Ece", "Boş Kişi"):
        p = by[n]
        print(f"  {n:<5} doluluk {p.completeness:>3.0f}%  "
              f"({p.mtd_hours:.0f}/{p.expected_hours:.0f}s)  "
              f"ort gecikme {p.avg_lag:.1f}g")
    blocks = build_slack_blocks(stats, win["today"], win["wd_elapsed"],
                                win["wd_full"], win["month_start"].month,
                                win["is_prev_month"])
    assert blocks[0]["type"] == "header"
    assert win["is_prev_month"] is False
    print(f"  Slack blokları üretildi ({len(blocks)} blok).")

    # --- Ayın 3'üne kadar: önceki ay (tam) gösterilir ---
    # 2 Ekim'de çalışıldığında rapor ayı = Eylül'ün tamamı (22 iş günü).
    now2 = datetime(2026, 10, 2, 9, 30, tzinfo=TZ)
    win2 = compute_windows(now2)
    assert win2["is_prev_month"] is True
    assert win2["month_start"] == date(2026, 9, 1), win2["month_start"]
    assert win2["mtd_cutoff"] == date(2026, 9, 30), win2["mtd_cutoff"]
    assert win2["wd_elapsed"] == 22 == win2["wd_full"], win2["wd_elapsed"]
    stats2 = build_stats(sample, {"a1", "e1", "z1"},
                         win2["month_start"], win2["mtd_cutoff"],
                         roster={"a1": "Ada", "e1": "Ece", "z1": "Boş Kişi"})
    assign_expected(stats2, win2["wd_full"] * DAILY_TARGET_HOURS)
    by2 = {p.name: p for p in stats2}
    # Eylül tamamı: Ada 64s / 176s hedef, Ece hâlâ 7g gecikme (started Eylül'de)
    assert by2["Ada"].mtd_hours == 64.0
    assert abs(by2["Ada"].completeness - 100.0 * 64.0 / 176.0) < 0.01
    assert by2["Ece"].avg_lag == 7.0
    print(f"  Önceki ay senaryosu OK (rapor ayı {TR_MONTHS[win2['month_start'].month]}, "
          f"{win2['wd_full']} iş günü).")

    # --- Müşteri grafiği ---
    csv_text = ("Müşteri,Jira Projeleri,Q1,Q2,Q3,Q4\n"
                "Liwa,\"LP2CS, LCS LIWA\",1.0,1.0,\"1,5\",0.5\n"
                "Teknosa,TCS,1.2,1.2,2.0,1.0\n"
                "Beymen,BCS,,,,\n"                    # hedef boş -> çizgi yok
                "CCC,,1.4,1.2,1.2,1.1\n")             # proje yok -> grafikte yok, dipnotta
    p2c, tg, um = parse_customer_targets(csv_text, 9, 20)  # Eylül = Q3, 20 iş günü
    assert p2c == {"LP2CS": "Liwa", "LCS": "Liwa", "LIWA": "Liwa",
                   "TCS": "Teknosa", "BCS": "Beymen"}, p2c
    assert tg == {"Liwa": 240.0, "Teknosa": 320.0}, tg   # FTE × 20 × 8
    assert um == ["CCC"], um
    assert parse_customer_targets(csv_text, 11, 20)[1] == {"Liwa": 80.0, "Teknosa": 160.0}  # Q4
    # 0.3 FTE, 21 iş günü -> 0.3 × 21 × 8 = 50.4s; 1.0 FTE -> 168s
    assert abs(fte_to_month_hours(0.3, 21) - 50.4) < 1e-9
    assert fte_to_month_hours(1.0, 21) == 168.0
    # Repo'daki gerçek hedef dosyası okunabiliyor
    rp2c, rtg, _ = load_customer_targets(11, 20)
    assert rp2c.get("LP2CS") == "Liwa (MDN)" and rtg.get("Liwa (MDN)") == 80.0, rtg
    cw = [dict(w, _issue_key=k) for w, k in zip(sample, ["LP2CS-1"] * 4 + ["LCS-2"] * 4
                                                 + ["TCS-3", "XYZ-9"])]
    ch = build_customer_hours(cw, {"a1", "e1"}, win["month_start"], win["mtd_cutoff"], p2c)
    assert ch == {"Liwa": 64.0, "Teknosa": 8.0, "XYZ": 8.0}, ch   # eşleşmeyen proje kendi adıyla
    # Related Project doluysa o sayılır (XYZ-9 -> TCS = Teknosa); boşsa issue'nun projesi
    cw_rel = [dict(w, _related_project="TCS") if w["_issue_key"] == "XYZ-9" else w for w in cw]
    ch_rel = build_customer_hours(cw_rel, {"a1", "e1"}, win["month_start"],
                                  win["mtd_cutoff"], p2c)
    assert ch_rel == {"Liwa": 64.0, "Teknosa": 16.0}, ch_rel
    assert project_of({"_issue_key": "SVCDEV-1", "_related_project": None}) == "SVCDEV"
    assert project_of({"_issue_key": "SVCDEV-1", "_related_project": "FLOCS"}) == "FLOCS"
    # 8 iş günü geçti, 14 kaldı: 64 / 8 × 14 = 112
    assert predict_remaining(64.0, 8, 14) == 112.0
    assert predict_remaining(64.0, 0, 22) == 0.0 and predict_remaining(64.0, 22, 0) == 0.0
    import tempfile
    png = os.path.join(tempfile.gettempdir(), "selftest_customer_logs.png")
    assert render_customer_chart(ch, tg, win["today"], win["wd_elapsed"], win["wd_full"],
                                 win["month_start"].month, png, um)
    print(f"  Müşteri grafiği OK ({png}).")


def build_report(now: datetime | None = None):
    """Jira'dan o anki veriyi çekip tabloyu hesaplar.

    Döner: (slack blokları, [(png yolu, başlık, yorum), ...]) ya da veri yoksa None.
    Hem zamanlanmış rapor hem de "give me updated table" isteği bunu kullanır.
    """
    win = compute_windows(now or datetime.now(TZ))
    worklogs, allowed_ids, roster, unresolved = fetch_worklogs(win["fetch_since"])
    stats = build_stats(worklogs, allowed_ids,
                        win["month_start"], win["mtd_cutoff"], roster)
    assign_expected(stats, win["wd_elapsed"] * DAILY_TARGET_HOURS)
    if not stats:
        print("[uyarı] Worklog bulunamadı; mesaj gönderilmiyor.", file=sys.stderr)
        return None
    blocks = build_slack_blocks(stats, win["today"], win["wd_elapsed"],
                                win["wd_full"], win["month_start"].month,
                                win["is_prev_month"], unresolved)
    images = []
    scatter_path = os.path.join(os.getcwd(), "worklog_scatter.png")
    if render_scatter(stats, win["today"], win["wd_elapsed"],
                      win["wd_full"], scatter_path):
        images.append((scatter_path, "Doluluk vs Giriş Gecikmesi",
                       "Doluluk vs giriş gecikmesi dağılımı"))

    # Müşteri grafiği: hedef dosyası okunamazsa rapor yine gider, sadece uyarı basılır
    try:
        proj_to_cust, targets, unmapped = load_customer_targets(win["month_start"].month,
                                                                win["wd_full"])
    except Exception as e:                                   # noqa: BLE001
        print(f"[uyarı] Müşteri hedefleri okunamadı: {e}", file=sys.stderr)
        proj_to_cust, targets, unmapped = {}, {}, []
    cust_hours = build_customer_hours(worklogs, allowed_ids, win["month_start"],
                                      win["mtd_cutoff"], proj_to_cust)
    cust_path = os.path.join(os.getcwd(), "customer_logs.png")
    if render_customer_chart(cust_hours, targets, win["today"], win["wd_elapsed"],
                             win["wd_full"], win["month_start"].month, cust_path,
                             unmapped):
        images.append((cust_path, "Müşteri Bazında Log",
                       "Müşteri bazında log toplamı, kalan günler tahmini ve hedef"))
    return blocks, images


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
        return
    if requests is None:
        sys.exit("requests kurulu değil: pip install -r requirements.txt")

    report = build_report()
    if report is None:
        return
    blocks, images = report

    if "--dry-run" in args:
        print(json.dumps(blocks, ensure_ascii=False, indent=2))
        for path, title, _ in images:
            print(f"\n[bilgi] {title} kaydedildi: {path}", file=sys.stderr)
    else:
        msg = post_message(blocks)
        for path, title, comment in images:
            # tabloyu takip eden thread'e ekle; kanal yanıttan alınır ki
            # SLACK_CHANNEL kullanıcı ID'si (DM) olduğunda da çalışsın
            upload_image_to_slack(path, title=title, comment=comment,
                                  thread_ts=msg.get("ts"), channel=msg.get("channel"))


if __name__ == "__main__":
    main()
