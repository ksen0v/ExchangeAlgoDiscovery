"""WebSocket client of the radar backend (local or remote) for the desktop windows.

Runs its own asyncio loop in a thread and hands messages to the GUI thread
through Qt signals (queued automatically across threads).
"""
import asyncio
import contextlib
import logging
import threading
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
import orjson
from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)


class Feed(QObject):
    trades = Signal(list)
    walls = Signal(list)  # order-book wall events
    snapshot = Signal(dict)
    alert = Signal(dict)
    regime = Signal(dict)  # market regime badge of the main coin (М3)
    coin_changed = Signal(str)
    watch_changed = Signal(list)  # extra coins on the server
    connection = Signal(bool, str)  # connected, error text
    request_failed = Signal(str)

    def __init__(self, base_url: str, token: str = "", coin: str | None = None):
        """coin=None follows the main coin; a ticker subscribes to that watched coin's tape."""
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.last_snapshot: dict = {}
        self._filter: dict = {"type": "filter", "min_usd": 1000.0, "keys": [], "lite": True, "coin": coin}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def ws_url(self) -> str:
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        query = urlencode({"token": self.token}) if self.token else ""
        return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/ws", query, ""))

    # ---- lifecycle (GUI thread) -----------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._main, name="radar-feed", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        if self._loop and self._task:
            self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread:
            self._thread.join(5)

    def set_filter(self, min_usd: float, keys: list[str], walls: bool = True, **extra) -> None:
        self._filter = {**self._filter, "min_usd": float(min_usd), "keys": list(keys), "walls": walls, **extra}
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._send_filter()))

    def follow(self, coin: str | None) -> None:
        """Switch this feed to another watched coin (None = the main coin)."""
        self.set_filter(self._filter["min_usd"], self._filter["keys"], self._filter.get("walls", True), coin=coin)

    def set_coin(self, coin: str) -> None:
        """Change the server's main coin."""
        self._request("POST", "/api/coin", {"coin": coin})

    def put_watch(self, coins: list[str]) -> None:
        """Extra coins the server should collect trades for."""
        self._request("PUT", "/api/watch", {"coins": coins})

    # ---- feed thread -----------------------------------------------------
    def _main(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._task = self._loop.create_task(self._run())
        self._ready.set()
        with contextlib.suppress(asyncio.CancelledError):
            self._loop.run_until_complete(self._task)
        self._loop.close()

    async def _run(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with aiohttp.ClientSession(headers=headers) as session:
            self._session = session
            while True:
                error = ""
                try:
                    async with session.ws_connect(self.ws_url(), heartbeat=20, max_msg_size=0) as ws:
                        self._ws = ws
                        await self._send_filter()
                        self.connection.emit(True, "")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self._dispatch(orjson.loads(msg.data))
                            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                                break
                        error = "соединение закрыто"
                except asyncio.CancelledError:
                    raise
                except aiohttp.WSServerHandshakeError as e:
                    error = "неверный токен доступа" if e.status in (401, 403) else f"сервер ответил {e.status}"
                except Exception as e:  # noqa: BLE001 - network: retry
                    error = f"{type(e).__name__}: {e}"[:200]
                self._ws = None
                self.connection.emit(False, error)
                await asyncio.sleep(2)

    async def _send_filter(self) -> None:
        ws = self._ws
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.send_str(orjson.dumps(self._filter).decode())

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "trades":
            self.trades.emit(msg.get("rows") or [])
        elif kind == "walls":
            self.walls.emit(msg.get("rows") or [])
        elif kind == "snapshot":
            self.last_snapshot = msg
            self.snapshot.emit(msg)
        elif kind == "alert":
            self.alert.emit(msg.get("alert") or {})
        elif kind == "regime":
            self.regime.emit(msg)
        elif kind == "watch":
            self.watch_changed.emit(msg.get("coins") or [])
        elif kind == "coin":
            self.coin_changed.emit(msg.get("coin") or "")

    def _request(self, method: str, path: str, body: dict | None = None) -> None:
        async def go() -> None:
            try:
                async with self._session.request(
                    method, self.base_url + path, json=body, timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status >= 400:
                        data = await r.json(content_type=None)
                        self.request_failed.emit(str((data or {}).get("detail") or r.status))
            except Exception as e:  # noqa: BLE001
                self.request_failed.emit(f"{type(e).__name__}: {e}")

        if self._loop and self._session:
            asyncio.run_coroutine_threadsafe(go(), self._loop)
        else:
            self.request_failed.emit("нет связи с сервером")
