"""Send one alert to Telegram, Discord or a plain webhook. Whatever is configured is used."""

import json
import urllib.parse
import urllib.request


def _post(url, payload, form=False, timeout=15):
    if form:
        data = urllib.parse.urlencode(payload).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
    headers["User-Agent"] = "gno-node-tools"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def send(cfg, text, log=print):
    """Return the number of channels that accepted the message."""
    text = "%s %s" % (cfg.get("notify", "prefix"), text)
    sent = 0
    token, chat = cfg.get("notify", "telegram_bot_token"), cfg.get("notify", "telegram_chat_id")
    channels = []
    if token and chat:
        channels.append(("telegram", "https://api.telegram.org/bot%s/sendMessage" % token,
                         {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, True))
    if cfg.get("notify", "discord_webhook"):
        channels.append(("discord", cfg.get("notify", "discord_webhook"), {"content": text[:1900]}, False))
    if cfg.get("notify", "webhook_url"):
        channels.append(("webhook", cfg.get("notify", "webhook_url"), {"text": text}, False))
    for name, url, payload, form in channels:
        try:
            _post(url, payload, form)
            sent += 1
        except Exception as e:  # never let a failed alert crash the watch
            log("alert via %s failed: %s" % (name, str(e)[:120]))
    if not channels:
        log("no alert channel configured; alert was: " + text)
    return sent
