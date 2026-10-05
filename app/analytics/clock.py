"""Clock check (ТЗ 3.3): offset of this computer's clock, at start and every hour.

SNTP (UDP 123) against public NTP servers; where UDP is blocked, the Binance
server time over HTTPS (half the round trip subtracted). Above 50 ms the
dashboard shows a warning: exchange delays and "who prints first" (М11) need
correct time.
"""
import asyncio
import logging
import socket
import struct
import time

import aiohttp

log = logging.getLogger(__name__)

NTP_SERVERS = ("pool.ntp.org", "time.google.com", "time.cloudflare.com")
NTP_EPOCH = 2208988800  # 1900 -> 1970
WARN_MS = 50.0
CHECK_EVERY = 3600.0


def _sntp(server: str, timeout: float = 2.0) -> float:
    """Offset in seconds (server - local), blocking."""
    packet = b"\x1b" + 47 * b"\0"
    addr = socket.getaddrinfo(server, 123, type=socket.SOCK_DGRAM)[0][4]
    with socket.socket(socket.AF_INET6 if ":" in addr[0] else socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t1 = time.time()
        s.sendto(packet, addr)
        data, _ = s.recvfrom(512)
        t4 = time.time()
    if len(data) < 48:
        raise ValueError("short NTP answer")
    recv = struct.unpack("!II", data[32:40])
    xmit = struct.unpack("!II", data[40:48])
    t2 = recv[0] - NTP_EPOCH + recv[1] / 2**32
    t3 = xmit[0] - NTP_EPOCH + xmit[1] / 2**32
    return ((t2 - t1) + (t3 - t4)) / 2


async def _http_offset(session: aiohttp.ClientSession) -> float:
    t1 = time.time()
    async with session.get("https://api.binance.com/api/v3/time", timeout=aiohttp.ClientTimeout(total=5)) as r:
        server = (await r.json(content_type=None))["serverTime"] / 1000
    t2 = time.time()
    return server - (t1 + t2) / 2


class Clock:
    def __init__(self) -> None:
        self.offset_ms: float | None = None
        self.source = ""
        self.checked_at = 0.0
        self.error = ""

    async def check(self, session: aiohttp.ClientSession | None = None) -> None:
        for server in NTP_SERVERS:
            try:
                off = await asyncio.to_thread(_sntp, server)
                self.offset_ms, self.source, self.error = off * 1000, f"NTP {server}", ""
                break
            except (OSError, ValueError) as e:
                self.error = f"NTP недоступен: {e}"
        else:
            if session is not None:
                try:
                    off = await _http_offset(session)
                    self.offset_ms, self.source, self.error = off * 1000, "время сервера Binance (HTTPS)", ""
                except Exception as e:  # noqa: BLE001
                    self.error = f"не удалось проверить часы: {e}"
        self.checked_at = time.time()
        if self.offset_ms is not None and abs(self.offset_ms) > WARN_MS:
            log.warning("clock offset %.0f ms (%s): sync the system clock", self.offset_ms, self.source)

    async def run(self, session: aiohttp.ClientSession | None = None) -> None:
        while True:
            await self.check(session)
            await asyncio.sleep(CHECK_EVERY)

    def status(self) -> dict:
        warn = self.offset_ms is not None and abs(self.offset_ms) > WARN_MS
        return {
            "offset_ms": None if self.offset_ms is None else round(self.offset_ms, 1),
            "source": self.source,
            "checked_at": self.checked_at or None,
            "warn": warn,
            "error": self.error,
        }
