"""FastAPI app: REST API, WebSocket for the dashboard, background loops."""
import asyncio
import contextlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from app.collectors.base import http_session
from app.config import VENUES, settings
from app.detector import Detector, DetectorConfig
from app.fx import run_fx_updater
from app.hub import Client, Hub
from app.manager import Manager
from app.storage import Storage
from app.telegram import Telegram, format_alert

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("ccxt").setLevel(logging.WARNING)
log = logging.getLogger("radar")

STATIC = Path(__file__).resolve().parent.parent / "static"
AUTH_COOKIE = "radar_token"


class State:
    storage: Storage
    detector: Detector
    hub: Hub
    manager: Manager
    telegram: Telegram


S = State()


async def tick_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            S.manager.sync_connected()
            metrics, alerts = S.detector.tick()
            empty = {"score": 0, "reasons": []}
            rows = [{**info, **metrics.get(info["key"], empty)} for info in S.manager.infos()]
            S.hub.broadcast_snapshot(
                {
                    "type": "snapshot",
                    "coin": S.manager.coin,
                    "ts": time.time(),
                    "consensus": S.detector.consensus,
                    "streams": rows,
                }
            )
            for a in alerts:
                a["id"] = await S.storage.add_alert(a)
                S.hub.broadcast({"type": "alert", "alert": a})
                if S.detector.cfg.telegram:
                    S.telegram.send(format_alert(a, settings.timezone))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("tick failed")
        await asyncio.sleep(max(0.05, 1.0 - (time.monotonic() - started)))


async def maintenance_loop() -> None:
    while True:
        if settings.alerts_keep_days > 0:
            try:
                n = await S.storage.prune_alerts(time.time() - settings.alerts_keep_days * 86400)
                if n:
                    log.info("deleted %d old alerts", n)
            except Exception:  # noqa: BLE001
                log.exception("alert cleanup failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    S.storage = Storage(settings.db_path)
    await S.storage.open()
    cfg = DetectorConfig()
    try:
        cfg.update(await S.storage.get("detector", {}))
    except (TypeError, ValueError) as e:
        log.warning("saved detector settings ignored: %s", e)
    S.detector = Detector(cfg, settings.history_sec)
    S.hub = Hub()
    S.telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
    session = http_session()
    S.manager = Manager(S.detector, S.hub, session)

    tasks = [
        asyncio.create_task(S.hub.run_tape()),
        asyncio.create_task(S.telegram.run()),
        asyncio.create_task(tick_loop()),
        asyncio.create_task(maintenance_loop()),
    ]
    if not settings.demo:
        tasks.append(asyncio.create_task(run_fx_updater()))
    await S.manager.set_coin(await S.storage.get("coin", settings.default_coin))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await S.manager.shutdown()
        with contextlib.suppress(Exception):
            await session.close()
        await S.storage.close()


app = FastAPI(title="Manipulation Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _token_ok(value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value.encode(), settings.auth_token.encode())


def authorized(conn: HTTPConnection) -> bool:
    """AUTH_TOKEN via ?token=, the cookie set from it, or an Authorization: Bearer header."""
    if not settings.auth_token:
        return True
    auth = conn.headers.get("authorization", "")
    bearer = auth[7:] if auth.lower().startswith("bearer ") else None
    return any(_token_ok(v) for v in (conn.query_params.get("token"), conn.cookies.get(AUTH_COOKIE), bearer))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not authorized(request):
        return PlainTextResponse(
            "Нужен токен доступа: откройте http://<сервер>:<порт>/?token=<AUTH_TOKEN>", status_code=401
        )
    response = await call_next(request)
    token = request.query_params.get("token")
    if settings.auth_token and _token_ok(token):
        response.set_cookie(AUTH_COOKIE, token, max_age=365 * 86400, httponly=True, samesite="lax")
    return response


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def get_state() -> dict:
    return {
        "coin": S.manager.coin,
        "config": S.detector.cfg.dict(),
        "telegram": S.telegram.configured,
        "demo": settings.demo,
        "venues": [
            {"name": v.name, "spot": bool(v.spot), "perp": bool(v.perp), "custom": any(
                s is not None and s.kind == "custom" for s in (v.spot, v.perp)
            )}
            for v in VENUES
        ],
    }


class CoinIn(BaseModel):
    coin: str


@app.post("/api/coin")
async def set_coin(body: CoinIn) -> dict:
    coin = body.coin.strip().upper()
    if not coin.isalnum() or len(coin) > 20:
        raise HTTPException(400, "Тикер должен состоять из букв и цифр, например PEPE")
    await S.manager.set_coin(coin)
    await S.storage.set("coin", coin)
    await S.hub.broadcast({"type": "coin", "coin": coin})
    return {"coin": coin}


@app.get("/api/config")
async def get_config() -> dict:
    return S.detector.cfg.dict()


@app.put("/api/config")
async def put_config(body: dict) -> dict:
    try:
        S.detector.cfg.update(body)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Неверное значение: {e}") from e
    await S.storage.set("detector", S.detector.cfg.dict())
    return S.detector.cfg.dict()


@app.get("/api/alerts")
async def get_alerts(coin: str | None = None, limit: int = 200) -> list[dict]:
    return await S.storage.alerts(coin.upper() if coin else None, min(limit, 1000))


@app.get("/api/walls")
async def get_walls(limit: int = 100) -> list[dict]:
    """Latest wall events of the current coin, newest first."""
    return list(S.detector.walls.recent)[::-1][: max(0, min(limit, 300))]


@app.post("/api/telegram/test")
async def telegram_test() -> dict:
    if not S.telegram.configured:
        raise HTTPException(400, "Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
    S.telegram.send(f"✅ Manipulation Radar на связи. Монета: <b>{S.manager.coin}</b>")
    return {"ok": True}


async def _ws_reader(ws: WebSocket, client: Client) -> None:
    with contextlib.suppress(WebSocketDisconnect, RuntimeError, ValueError, TypeError):
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "filter":
                client.set_filter(msg)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """Client messages: {"type": "filter", "min_usd": 3000, "keys": ["KuCoin:spot"], "lite": false}."""
    if not authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    client = S.hub.add(ws)
    tasks = [
        asyncio.create_task(_ws_reader(ws, client)),
        asyncio.create_task(client.writer()),
        asyncio.create_task(client.overflow.wait()),  # too slow: drop, the client reconnects
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        S.hub.remove(client)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), 2)
