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
from pydantic import BaseModel, ValidationError
from starlette.requests import HTTPConnection

from app.analytics.engine import OI_CHART_WINDOWS, OI_TOTAL, Analytics
from app.collectors.base import http_session
from app.config import VENUES, settings
from app.detector import LIMITS, Detector, DetectorConfig
from app.fx import run_fx_updater
from app.hub import Client, Hub
from app.manager import MAX_WATCH, Manager
from app.storage import Storage
from app.telegram import Telegram, format_alert
from app.workspaces import Layout

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
    analytics: Analytics


S = State()


async def _publish_alerts(alerts: list[dict], telegram_on: bool) -> None:
    for a in alerts:
        a["id"] = await S.storage.add_alert(a)
        S.hub.broadcast({"type": "alert", "alert": a})
        if telegram_on:
            S.telegram.send(format_alert(a, settings.timezone))


async def tick_loop() -> None:
    analytics_at = 0.0
    while True:
        started = time.monotonic()
        try:
            S.manager.sync_connected()
            metrics, alerts = S.detector.tick()
            empty = {"score": 0, "reasons": []}
            rows = [{**info, **metrics.get(info["key"], empty)}
                    for info in S.manager.infos()]
            now = time.time()
            S.hub.broadcast_snapshot(
                {
                    "type": "snapshot",
                    "coin": S.manager.coin,
                    "ts": now,
                    "consensus": S.detector.consensus,
                    "streams": rows,
                },
                S.manager.watch_snapshots(now),
            )
            await _publish_alerts(alerts, S.detector.cfg.telegram)

            S.analytics.tick(now, S.detector.consensus, S.manager.streams)
            if now - analytics_at >= 2 and any(c.analytics and c.follows(S.manager.coin, S.hub.primary)
                                              for c in S.hub.clients):
                analytics_at = now
                S.hub.broadcast_main(S.analytics.snapshot(now, S.manager.streams), analytics_only=True)
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
    overrides = await S.storage.get("wall_overrides", {})
    S.detector.wall_overrides = overrides if isinstance(overrides, dict) else {}
    S.hub = Hub()
    S.telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
    session = http_session()
    S.analytics = Analytics(demo=settings.demo)
    selection = await S.storage.get("venue_selection", {})
    S.manager = Manager(S.detector, S.hub, session, analytics=S.analytics,
                        venue_selection=selection if isinstance(selection, dict) else {})

    tasks = [
        asyncio.create_task(S.hub.run_tape()),
        asyncio.create_task(S.telegram.run()),
        asyncio.create_task(tick_loop()),
        asyncio.create_task(maintenance_loop()),
    ]
    if not settings.demo:
        tasks.append(asyncio.create_task(run_fx_updater()))
    await S.manager.set_coin(await S.storage.get("coin", settings.default_coin))
    watch = await S.storage.get("watch", [])
    await S.manager.set_watch(watch if isinstance(watch, list) else [])
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await S.manager.shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)
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
async def no_stale_dashboard(request: Request, call_next):
    """The dashboard must never run from a browser cache after an update (revalidated via ETag)."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


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
        "watch": list(S.manager.watch),
        "max_watch": MAX_WATCH,
        "venue_selection": S.manager.venue_selection,
        "venues": [
            {"name": v.name, "spot": bool(v.spot), "perp": bool(v.perp), "custom": any(
                s is not None and s.kind == "custom" for s in (v.spot, v.perp)
            )}
            for v in VENUES
        ],
    }


@app.get("/api/version")
async def get_version() -> dict:
    from desktop.version import VERSION
    return {"version": VERSION}


@app.get("/api/workspaces")
async def get_workspaces():
    saved = await S.storage.get("workspaces.v1", None)
    if saved is None:
        return None
    try:
        return Layout.model_validate(saved).model_dump(mode="json")
    except ValidationError:
        log.warning("Invalid saved workspace layout; retaining the stored copy for recovery")
        return None


@app.put("/api/workspaces")
async def put_workspaces(request: Request):
    raw = await request.body()
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, "Слишком большой набор рабочих пространств")
    try:
        layout = Layout.model_validate_json(raw)
    except ValidationError as error:
        raise HTTPException(422, "Некорректная компоновка рабочего пространства") from error
    await S.storage.set("workspaces.v1", layout.model_dump(mode="json"))
    return {"updatedAt": layout.updatedAt}


class CoinIn(BaseModel):
    coin: str


@app.post("/api/coin")
async def set_coin(body: CoinIn) -> dict:
    coin = body.coin.strip().upper()
    if not coin.isalnum() or len(coin) > 20:
        raise HTTPException(400, "Тикер должен состоять из букв и цифр, например PEPE")
    await S.manager.set_coin(coin)
    await S.storage.set("coin", coin)
    await S.storage.set("watch", list(S.manager.watch))  # the new main coin leaves the watch list
    S.hub.broadcast({"type": "coin", "coin": coin})
    S.hub.broadcast({"type": "watch", "coins": list(S.manager.watch)})
    return {"coin": coin}


class WatchIn(BaseModel):
    coins: list[str]


@app.get("/api/watch")
async def get_watch() -> dict:
    return {"coins": list(S.manager.watch), "max": MAX_WATCH}


@app.put("/api/watch")
async def put_watch(body: WatchIn) -> dict:
    """Extra coins with a tape of their own (no detector): at most MAX_WATCH."""
    coins = [c.strip().upper() for c in body.coins if c.strip()]
    for c in coins:
        if not c.isalnum() or len(c) > 20:
            raise HTTPException(400, f"Тикер «{c}» должен состоять из букв и цифр")
    if len(set(coins) - {S.manager.coin}) > MAX_WATCH:
        raise HTTPException(400, f"Можно добавить не больше {MAX_WATCH} монет к основной")
    result = await S.manager.set_watch(coins)
    await S.storage.set("watch", result)
    S.hub.broadcast({"type": "watch", "coins": result})
    return {"coins": result, "max": MAX_WATCH}


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


class WallOverrideIn(BaseModel):
    key: str  # stream, e.g. "WEEX:perp"
    coin: str | None = None  # default: the main coin
    off: bool = False  # do not look for walls on this venue
    min_usd: float | None = None  # own "wall from $"
    ratio: float | None = None  # own "x the typical level"


@app.get("/api/wall-overrides")
async def get_wall_overrides(coin: str | None = None) -> dict:
    coin = (coin or S.manager.coin).upper()
    return {"coin": coin, "overrides": S.detector.wall_overrides.get(coin, {})}


@app.put("/api/wall-overrides")
async def put_wall_override(body: WallOverrideIn) -> dict:
    """Per venue and coin: no wall search, or own thresholds (market makers' quotes differ by venue)."""
    coin = (body.coin or S.manager.coin).strip().upper()
    if not coin.isalnum() or ":" not in body.key or len(body.key) > 60:
        raise HTTPException(400, "Неверная биржа или монета")
    for name, value in (("wall_min_usd", body.min_usd), ("wall_ratio", body.ratio)):
        lo, hi = LIMITS[name]
        if value is not None and not lo <= value <= hi:
            raise HTTPException(400, f"{'Плита от, $' if name == 'wall_min_usd' else '× к стакану'}: от {lo:g} до {hi:g}")
    per_coin = S.detector.wall_overrides.setdefault(coin, {})
    if body.off or body.min_usd is not None or body.ratio is not None:
        per_coin[body.key] = {"off": body.off, "min_usd": body.min_usd, "ratio": body.ratio}
    else:
        per_coin.pop(body.key, None)  # back to the common settings
    if not per_coin:
        S.detector.wall_overrides.pop(coin, None)
    if coin == S.manager.coin:
        S.detector.walls.forget(body.key)  # re-detect with the new thresholds
    await S.storage.set("wall_overrides", S.detector.wall_overrides)
    return {"coin": coin, "overrides": S.detector.wall_overrides.get(coin, {})}


@app.get("/api/walls")
async def get_walls(limit: int = 100) -> list[dict]:
    """Latest wall events of the current coin, newest first."""
    return list(S.detector.walls.recent)[::-1][: max(0, min(limit, 300))]


@app.get("/api/analytics")
async def get_analytics() -> dict:
    """The latest module snapshot of the main coin (the same as the "analytics" WebSocket message)."""
    return S.analytics.snapshot(time.time(), S.manager.streams)


@app.get("/api/health")
async def get_health() -> dict:
    now = time.time()
    return {"ts": now, "coin": S.manager.coin, "demo": settings.demo,
            "streams": S.manager.health(now), "feeds": S.analytics.feed_status(S.manager.streams, now)}


@app.get("/api/oi/history")
async def get_oi_history(key: str, coin: str, seconds: int = 3600) -> dict:
    if coin.strip().upper() != S.manager.coin:
        raise HTTPException(409, "Основная монета изменилась")
    stream = S.manager.streams.get(key)
    if key != OI_TOTAL and (stream is None or stream.kind != "perp"):
        raise HTTPException(404, "Фьючерсная биржа не включена для этой монеты")
    if seconds not in OI_CHART_WINDOWS:
        raise HTTPException(400, "Период графика: 15 минут, 1 час, 4 часа или 24 часа")
    series = S.analytics.oi_series(key, time.time(), seconds)
    series["stale"] = series["stale"] or (key != OI_TOTAL and stream.status not in ("live", "polling"))
    return series


class VenuesIn(BaseModel):
    coin: str
    venues: list[str] | None = None  # null = every venue, [] = no connections


@app.get("/api/venues")
async def get_venues(coin: str | None = None) -> dict:
    coin = (coin or S.manager.coin).strip().upper()
    return {"coin": coin, "venues": S.manager.venue_selection.get(coin)}


@app.put("/api/venues")
async def put_venues(body: VenuesIn) -> dict:
    coin = body.coin.strip().upper()
    if not coin.isascii() or not coin.isalnum() or len(coin) > 20:
        raise HTTPException(400, "Неверный тикер монеты")
    known = {v.name for v in VENUES}
    if body.venues is not None and set(body.venues) - known:
        raise HTTPException(400, "Неизвестная биржа")
    await S.manager.set_venues(coin, body.venues)
    await S.storage.set("venue_selection", S.manager.venue_selection)
    msg = {"coin": coin, "venues": S.manager.venue_selection.get(coin)}
    S.hub.broadcast({"type": "venues", **msg})
    return msg


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
    reader = asyncio.create_task(_ws_reader(ws, client))
    tasks = [
        reader,
        asyncio.create_task(client.writer()),
        asyncio.create_task(client.overflow.wait()),  # too slow: drop, the client reconnects
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # No awaiting here when the client simply left: the handler must end promptly.
        S.hub.remove(client)
        client_left = reader.done()
        for t in tasks:
            t.cancel()
        if not client_left:  # we end it (slow client / send failed): close, but never hang on it
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), 2)
