"""Telegram alerts (queued, ~1 message/second to stay under Bot API limits)."""
import asyncio
import html
import logging
from datetime import datetime

import aiohttp

from app.detector import fmt_usd

log = logging.getLogger(__name__)


def format_alert(a: dict) -> str:
    kind = "спот" if a["kind"] == "spot" else "фьючерс"
    t = datetime.fromtimestamp(a["ts"]).strftime("%H:%M:%S")
    lines = [
        f"🚨 <b>{html.escape(a['coin'])}</b> · <b>{html.escape(a['venue'])}</b> {kind} · score {a['score']:.0f}",
        *(f"• {html.escape(r)}" for r in a["reasons"]),
    ]
    if a.get("price"):
        lines.append(f"Цена {a['price']:.6g} · объём окна {fmt_usd(a['vol_w'])} · {t}")
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if self.configured and not self.queue.full():
            self.queue.put_nowait(text)

    async def _post(self, session: aiohttp.ClientSession, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 429:
                retry = (await r.json()).get("parameters", {}).get("retry_after", 5)
                await asyncio.sleep(retry)
            elif r.status != 200:
                log.warning("telegram send failed: %s %s", r.status, await r.text())

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                text = await self.queue.get()
                try:
                    await self._post(session, text)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("telegram error: %s", e)
                await asyncio.sleep(1.0)
