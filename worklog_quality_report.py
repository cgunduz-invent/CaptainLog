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

    def search_issue_keys(self, jql: str) -> list[str]:
        keys, token = [], None
        while True:
            body = {"jql": jql, "maxResults": 100, "fields": ["key"]}
            if token:
                body["nextPageToken"] = token
            r = self.s.post(f"{self.base}/rest/api/3/search/jql", json=body)
            r.raise_for_status()
            data = r.json()
            keys += [i["key"] for i in data.get("issues", [])]
            token = data.get("nextPageToken")
            if not token or data.get("isLast"):
                break
        return keys

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
    keys = jira.search_issue_keys(jql)
    print(f"[bilgi] {len(keys)} issue, worklog'lar çekiliyor...", file=sys.stderr)

    all_wl = []
    for k in keys:
        for w in jira.issue_worklogs(k, since_ms):
            if parse_jira_dt(w["started"]).astimezone(TZ) >= fetch_since:
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


def post_to_slack(blocks: list[dict]) -> str | None:
    r = requests.post("https://slack.com/api/chat.postMessage",
                      headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}",
                               "Content-Type": "application/json; charset=utf-8"},
                      json={"channel": os.environ["SLACK_CHANNEL"], "blocks": blocks,
                            "text": "Worklog takip raporu"})
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack hatası: {data}")
    print("[bilgi] Tablo Slack'e gönderildi.", file=sys.stderr)
    return data.get("ts")


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
    ax.set_ylim(-4, 104)
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
    ]
    ax.legend(handles=legend, loc="lower center", ncol=3, fontsize=9,
              frameon=True, bbox_to_anchor=(0.5, -0.135))
    skipped = [p.name for p in stats if p.n_logs == 0]
    if skipped:
        fig.text(0.01, 0.005, "Grafik dışı (rapor ayında log yok): "
                 + ", ".join(skipped), fontsize=7.5, color="#777")
    fig.tight_layout(rect=[0, 0.02, 1, 1])
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return True


def upload_image_to_slack(path: str, title: str, comment: str,
                          thread_ts: str | None = None):
    """Slack'e resim yükler (files.getUploadURLExternal akışı; files:write gerekir)."""
    token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_CHANNEL"]
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
    print("[bilgi] Scatter Slack'e gönderildi.", file=sys.stderr)


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


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
        return
    if requests is None:
        sys.exit("requests kurulu değil: pip install -r requirements.txt")

    win = compute_windows(datetime.now(TZ))
    worklogs, allowed_ids, roster, unresolved = fetch_worklogs(win["fetch_since"])
    stats = build_stats(worklogs, allowed_ids,
                        win["month_start"], win["mtd_cutoff"], roster)
    assign_expected(stats, win["wd_elapsed"] * DAILY_TARGET_HOURS)
    if not stats:
        print("[uyarı] Worklog bulunamadı; mesaj gönderilmiyor.", file=sys.stderr)
        return
    blocks = build_slack_blocks(stats, win["today"], win["wd_elapsed"],
                                win["wd_full"], win["month_start"].month,
                                win["is_prev_month"], unresolved)
    scatter_path = os.path.join(os.getcwd(), "worklog_scatter.png")
    has_scatter = render_scatter(stats, win["today"], win["wd_elapsed"],
                                 win["wd_full"], scatter_path)

    if "--dry-run" in args:
        print(json.dumps(blocks, ensure_ascii=False, indent=2))
        if has_scatter:
            print(f"\n[bilgi] Scatter kaydedildi: {scatter_path}", file=sys.stderr)
    else:
        ts = post_to_slack(blocks)
        if has_scatter:
            upload_image_to_slack(
                scatter_path,
                title="Doluluk vs Giriş Gecikmesi",
                comment="Doluluk vs giriş gecikmesi dağılımı",
                thread_ts=ts)   # tabloyu takip eden thread'e ekle


if __name__ == "__main__":
    main()
