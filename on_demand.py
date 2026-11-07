#!/usr/bin/env python3
"""
"give me updated table" — anlık tablo isteği
============================================

İzlenen Slack kanallarında son LOOKBACK_MINUTES içinde yazılmış ve içinde
TRIGGER geçen mesajları bulur; henüz cevaplanmamışsa o anki rakamlarla
tabloyu (Jira'dan taze çekilir) + scatter'ı o mesajın thread'ine yazar.

GitHub Actions'ta birkaç dakikada bir çalışır (.github/workflows/on-demand-table.yml).
Aynı mesaja iki kez cevap vermez: botun o thread'de zaten cevabı varsa atlar.

Ortam değişkenleri:
  SLACK_BOT_TOKEN   (scope: chat:write, files:write, channels:history, groups:history)
  WATCH_CHANNELS    virgüllü kanal ID listesi
  LOOKBACK_MINUTES  geriye bakış (varsayılan 120)
  JIRA_*            sadece istek bulunduğunda gerekir

Kullanım:
  python on_demand.py --check   # sadece istek var mı bak; GITHUB_OUTPUT'a pending=true/false yaz
  python on_demand.py           # istek varsa tabloyu hesapla ve thread'e cevapla
  python on_demand.py --selftest
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime

import requests

TRIGGER = "give me updated table"
DEFAULT_CHANNELS = "C08DPFWG5PB,C07L0PF1X6E"
SLACK_API = "https://slack.com/api"


def _slack(method: str, **params) -> dict:
    r = requests.get(f"{SLACK_API}/{method}",
                     headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
                     params=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack {method} hatası: {data.get('error')} {data}")
    return data


def is_trigger(text: str) -> bool:
    """Mention'ları, noktalama ve büyük/küçük harfi yok sayarak tetik ifadesini arar."""
    t = re.sub(r"<[^>]+>", " ", text or "")          # <@U123>, <#C123|x>, linkler
    t = re.sub(r"[^\w\s]", " ", t.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return TRIGGER in t


def find_pending(channels: list[str], lookback_min: int) -> tuple[str, list[tuple[str, dict]]]:
    """Cevaplanmamış istekler: (bot_user_id, [(kanal, mesaj), ...])."""
    me = _slack("auth.test")
    bot_user, bot_id = me["user_id"], me.get("bot_id")
    oldest = f"{time.time() - lookback_min * 60:.6f}"
    pending = []
    for ch in channels:
        msgs = _slack("conversations.history", channel=ch, oldest=oldest,
                      limit=200).get("messages", [])
        for m in msgs:
            if m.get("user") == bot_user or (bot_id and m.get("bot_id") == bot_id):
                continue
            if not is_trigger(m.get("text", "")):
                continue
            if bot_user in (m.get("reply_users") or []):
                continue                                   # zaten cevaplanmış
            pending.append((ch, m))
    return bot_user, pending


def reply_all(pending: list[tuple[str, dict]]):
    import worklog_quality_report as rep

    report = rep.build_report()                            # tek Jira çekimi, tüm isteklere
    stamp = datetime.now(rep.TZ).strftime("%d.%m.%Y %H:%M")
    for ch, m in pending:
        who = f"<@{m['user']}>" if m.get("user") else "İstek"
        if report is None:
            rep.post_to_slack([{"type": "section", "text": {"type": "mrkdwn",
                               "text": f"{who} rapor ayında worklog bulunamadı."}}],
                              channel=ch, thread_ts=m["ts"])
            continue
        blocks, images = report
        note = {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"🔄 {who} isteği üzerine *{stamp}* itibarıyla Jira'dan güncel çekildi "
            f"(doluluk düne kadar olan iş günlerine göre).")}]}
        rep.post_to_slack([note, *blocks], channel=ch, thread_ts=m["ts"])
        for path, title, comment in images:
            rep.upload_image_to_slack(path, title=title, comment=comment,
                                      thread_ts=m["ts"], channel=ch)
        print(f"[bilgi] {ch} / {m['ts']} cevaplandı.", file=sys.stderr)


def _selftest():
    for ok in ["give me updated table", "Give me updated table!",
               "<@U0BOT> give me   updated TABLE pls", "hey, give me updated table?"]:
        assert is_trigger(ok), ok
    for no in ["give me table", "updated table", "", "give me the updated table"]:
        assert not is_trigger(no), no
    print("SELFTEST OK")


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
        return
    channels = [c.strip() for c in os.environ.get("WATCH_CHANNELS", DEFAULT_CHANNELS).split(",")
                if c.strip()]
    lookback = int(os.environ.get("LOOKBACK_MINUTES", "120"))
    _, pending = find_pending(channels, lookback)
    print(f"[bilgi] {len(pending)} cevaplanmamış istek.", file=sys.stderr)

    if "--check" in args:
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"pending={'true' if pending else 'false'}\n")
        return
    if pending:
        reply_all(pending)


if __name__ == "__main__":
    main()
