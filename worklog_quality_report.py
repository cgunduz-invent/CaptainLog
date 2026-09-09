#!/usr/bin/env python3
"""
Worklog Takibi — Aylık Doluluk & Giriş Gecikmesi
================================================

İki metriği kişi bazında tek tabloda, her gün Slack'e basar:

1) AYLIK DOLULUK (month-to-date)
   Hedef = o aydaki iş günü sayısı × günlük hedef saat (8s).
   Bugüne kadar geçen iş günü baz alınır (ör. ayın 11'iyse 10'una kadar).
   Doluluk % = o ay girilen toplam saat / bugüne kadarki beklenen saat.
   Hafta sonu + Türkiye resmi tatilleri düşülür.

2) GİRİŞ GECİKMESİ (son 30 gün)
   Her worklog için created (girildiği gün) − started (işin yapıldığı gün).
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
LOOKBACK_DAYS = 30            # gecikme metriği penceresi
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
Cansel Memişoğlu <cansel.kaya@invent.ai>,
Deniz Nil Cengiz <deniz.cengiz@invent.ai>,
Didem Paloğlu <didem.paloglu@invent.ai>,
Doğukan Durukan <dogukan.durukan@invent.ai>,
Ece Yurtsever <ece.yurtsever@invent.ai>,
Ecem Sert <ecem.sert@invent.ai>,
Ekinsu Çiçek <ekinsu.cicek@invent.ai>,
Emre Gezer <emre.gezer@invent.ai>,
Furkan Oğuz Gümüş <furkan.gumus@invent.ai>,
Gözde Gözütok <gozde.gozutok@invent.ai>,
Hümeyra Arslan <humeyra.arslan@invent.ai>,
Kübra Gülcan <kubra.gulcan@invent.ai>,
Leyli Jafarova <leyli.jafarova@invent.ai>,
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
Yiğitcan Aksoy <yigitcan.aksoy@invent.ai>
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


def month_bounds(today: date) -> tuple[date, date, date]:
    """(ay başı, dünkü gün = MTD sınırı, ay sonu)."""
    month_start = today.replace(day=1)
    mtd_cutoff = today - timedelta(days=1)          # bugün hariç
    last = calendar.monthrange(today.year, today.month)[1]
    month_end = date(today.year, today.month, last)
    return month_start, mtd_cutoff, month_end


# --------------------------------------------------------------------------- #
# Kişi istatistikleri
# --------------------------------------------------------------------------- #
@dataclass
class PersonStats:
    name: str
    account_id: str = ""
    # gecikme (son 30g)
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
                lag_since: datetime, mtd_start: date, mtd_end: date,
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
        # gecikme metriği: son 30 gün
        if started >= lag_since:
            ps.add_lag(lag_days(started, created), seconds)
        # doluluk metriği: bu ay, düne kadar
        if mtd_start <= started.date() <= mtd_end:
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


def fetch_worklogs(lag_since: datetime, fetch_since: datetime):
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
                       wd_elapsed: int, wd_full: int,
                       unresolved: list[tuple[str, str]] | None = None) -> list[dict]:
    # En düşük dolulukta olan en üstte; eşitlikte gecikmesi yüksek olan
    stats = sorted(stats, key=lambda p: ((p.completeness if p.completeness
                   is not None else 1e9), -p.avg_lag))

    header = {"type": "header", "text": {"type": "plain_text",
              "text": "🕒 Worklog Takibi — Doluluk & Gecikme"}}
    target_to_date = wd_elapsed * DAILY_TARGET_HOURS
    ctx = {"type": "context", "elements": [{"type": "mrkdwn", "text": (
        f"*{today.strftime('%d.%m.%Y')}* • {TR_MONTHS[today.month]} ayı: "
        f"{wd_elapsed}/{wd_full} iş günü geçti • bugüne dek hedef "
        f"{target_to_date:.0f}s ({DAILY_TARGET_HOURS:.0f}s/gün) • "
        f"gecikme penceresi son {LOOKBACK_DAYS} gün")}]}

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


def post_to_slack(blocks: list[dict]):
    r = requests.post("https://slack.com/api/chat.postMessage",
                      headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}",
                               "Content-Type": "application/json; charset=utf-8"},
                      json={"channel": os.environ["SLACK_CHANNEL"], "blocks": blocks,
                            "text": "Worklog takip raporu"})
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack hatası: {data}")
    print("[bilgi] Slack'e gönderildi.", file=sys.stderr)


# --------------------------------------------------------------------------- #
def compute_windows(now: datetime):
    today = now.date()
    lag_since = now - timedelta(days=LOOKBACK_DAYS)
    month_start, mtd_cutoff, month_end = month_bounds(today)
    hol = holidays_lib.Turkey(years=[month_start.year, today.year])
    wd_elapsed = business_days(month_start, mtd_cutoff, hol)
    wd_full = business_days(month_start, month_end, hol)
    fetch_since_date = min(lag_since.date(), month_start)
    fetch_since = datetime.combine(fetch_since_date, datetime.min.time(), TZ)
    return dict(today=today, lag_since=lag_since, month_start=month_start,
                mtd_cutoff=mtd_cutoff, wd_elapsed=wd_elapsed, wd_full=wd_full,
                fetch_since=fetch_since)


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
    stats = build_stats(sample, {"a1", "e1", "z1"}, win["lag_since"],
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
    blocks = build_slack_blocks(stats, win["today"], win["wd_elapsed"], win["wd_full"])
    assert blocks[0]["type"] == "header"
    print(f"  Slack blokları üretildi ({len(blocks)} blok).")


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
        return
    if requests is None:
        sys.exit("requests kurulu değil: pip install -r requirements.txt")

    win = compute_windows(datetime.now(TZ))
    worklogs, allowed_ids, roster, unresolved = fetch_worklogs(
        win["lag_since"], win["fetch_since"])
    stats = build_stats(worklogs, allowed_ids, win["lag_since"],
                        win["month_start"], win["mtd_cutoff"], roster)
    assign_expected(stats, win["wd_elapsed"] * DAILY_TARGET_HOURS)
    if not stats:
        print("[uyarı] Worklog bulunamadı; mesaj gönderilmiyor.", file=sys.stderr)
        return
    blocks = build_slack_blocks(stats, win["today"], win["wd_elapsed"],
                                win["wd_full"], unresolved)
    if "--dry-run" in args:
        print(json.dumps(blocks, ensure_ascii=False, indent=2))
    else:
        post_to_slack(blocks)


if __name__ == "__main__":
    main()
