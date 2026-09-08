"""Alerting. Un scraper qui casse en silence coute une journee entiere.

Deux canaux, tous deux optionnels et sans dependance externe (urllib) :
Telegram et un webhook generique. Si aucun n'est configure, les messages
partent uniquement sur stdout - et le workflow, lui, echoue bruyamment, ce qui
declenche la notification GitHub par defaut.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


def _post(url: str, payload: dict, timeout: int = 10) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError):
        return False


def send(message: str) -> None:
    """Envoie une alerte sur tous les canaux configures. N'echoue jamais.

    Une panne du canal d'alerte ne doit pas faire tomber le bot : le message
    part sur stdout dans tous les cas, et stdout est conserve dans les logs du
    job GitHub Actions.
    """
    print(message, flush=True)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        _post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": message[:4000], "disable_web_page_preview": True},
        )

    webhook = os.environ.get("ALERT_WEBHOOK_URL")
    if webhook:
        _post(webhook, {"text": message[:4000]})
