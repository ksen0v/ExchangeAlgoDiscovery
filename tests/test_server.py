import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.hub import Client


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "demo", True)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "radar.db"))
    monkeypatch.setattr(settings, "default_coin", "DEMOX")
    with TestClient(main.app) as c:
        yield c


def test_bad_config_is_rejected_and_not_saved(client):
    r = client.put("/api/config", json={"window_sec": 0})
    assert r.status_code == 400
    assert client.get("/api/config").json()["window_sec"] == 30
    r = client.put("/api/config", json={"window_sec": 20})
    assert r.status_code == 200 and r.json()["window_sec"] == 20


def test_workspaces_round_trip_to_database_and_preserve_independent_module_settings(client):
    import asyncio
    from app.storage import Storage

    assert client.get("/api/workspaces").json() is None
    layout = {"schema":1,"updatedAt":1234,"activeId":"oi-desk","spaces":[
        {"id":"oi-desk","name":"OI desktop","mode":"magnet","modules":[
            {"id":"chart-a","type":"oiChart","rect":{"x":0,"y":0,"w":0.5,"h":1},"settings":{"oiKey":"Binance:perp","unit":"usd"}},
            {"id":"chart-b","type":"oiChart","rect":{"x":0.5,"y":0,"w":0.5,"h":1},"settings":{"oiKey":"Bybit:perp","unit":"coins"}}]},
        {"id":"empty-desk","name":"Empty","mode":"free","modules":[]}]}
    assert client.put("/api/workspaces", json=layout).status_code == 200
    saved = client.get("/api/workspaces").json()
    assert saved["spaces"][0]["modules"][0]["settings"]["oiKey"] == "Binance:perp"
    assert saved["spaces"][0]["modules"][1]["settings"]["unit"] == "coins"
    assert saved["spaces"][1]["modules"] == []

    async def read_from_disk():
        db = Storage(settings.db_path)
        await db.open()
        try:
            return await db.get("workspaces.v1")
        finally:
            await db.close()
    assert asyncio.run(read_from_disk()) == saved
    bad = {**layout,"activeId":"missing"}
    assert client.put("/api/workspaces", json=bad).status_code == 422
    assert client.get("/api/workspaces").json() == saved
    assert client.put("/api/workspaces", json={"schema":1,"updatedAt":1235,"activeId":None,"spaces":[]}).status_code == 200


def test_workspaces_reject_overlapping_ids_and_outside_rectangles(client):
    module = {"id":"same","type":"tape","rect":{"x":0,"y":0,"w":0.5,"h":0.5},"settings":{}}
    layout = {"schema":1,"updatedAt":0,"activeId":"s","spaces":[{"id":"s","name":"Workspace","mode":"free","modules":[module,module]}]}
    assert client.put("/api/workspaces", json=layout).status_code == 422
    layout["spaces"][0]["modules"] = [{**module,"rect":{"x":0.8,"y":0,"w":0.5,"h":0.5}}]
    assert client.put("/api/workspaces", json=layout).status_code == 422
    assert client.get("/api/workspaces").json() is None


def test_ws_filter_by_venue_and_size(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "filter", "min_usd": 0, "keys": ["Binance:spot"], "lite": True})
        seen_snapshot = seen_trades = False
        for _ in range(200):
            msg = ws.receive_json()
            if msg["type"] == "snapshot":
                seen_snapshot = True
                assert "spark" not in msg["streams"][0]  # lite snapshot
            elif msg["type"] == "trades":
                seen_trades = True
                assert {r["key"] for r in msg["rows"]} == {"Binance:spot"}
            if seen_snapshot and seen_trades:
                break
        assert seen_snapshot and seen_trades


def test_auth_token(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_token", "s3cret")
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    r = client.get("/?token=s3cret")
    assert r.status_code == 200 and "radar_token" in r.cookies
    assert client.get("/api/state").status_code == 200  # cookie now set
    client.cookies.clear()
    with pytest.raises(Exception):
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
    with client.websocket_connect("/ws?token=s3cret") as ws:
        assert ws.receive_json()["type"] in ("snapshot", "trades")


def test_client_filter_message_parsing():
    c = Client(ws=None)
    c.set_filter({"keys": ["A:spot"]})
    assert c.min_usd == 3000 and c.keys == {"A:spot"}
    c.set_filter({"min_usd": 1000, "keys": []})
    assert c.min_usd == 1000 and c.keys is None
    assert c.wants({"usd": 1500, "key": "Z:perp"}) and not c.wants({"usd": 500, "key": "Z:perp"})


def test_per_market_trade_thresholds_are_used_before_allocating_or_sending_rows():
    from app.hub import Hub

    h = Hub()
    h.primary = "X"
    c = h.add(None)
    c.set_filter({"min_usd": 3000, "min_by_key":{"A:spot":500, "A:perp":10000}})
    assert h.trade_floor("X", "A:spot") == 500
    assert h.trade_floor("X", "A:perp") == 10000
    assert c.wants({"key":"A:spot", "usd":600}, "X")
    assert not c.wants({"key":"A:perp", "usd":6000}, "X")
    assert h.trade_floor("X", "B:spot") == 3000
    c.set_filter({"tape":False})
    assert h.trade_floor("X", "A:spot") == float("inf")
    assert not c.wants({"key":"A:spot", "usd":1e9}, "X")
    c.set_filter({"min_by_key":{"A:spot":"NaN", "A:perp":-5, "B:spot":0}, "tape":True})
    assert c.min_by_key == {"B:spot":0}


def test_switching_coin_works(client):
    r = client.post("/api/coin", json={"coin": "demoy"})
    assert r.status_code == 200 and r.json() == {"coin": "DEMOY"}
    assert client.get("/api/state").json()["coin"] == "DEMOY"


def test_instruments_have_no_three_coin_limit_and_selection_keeps_collectors(client):
    r = client.put("/api/watch", json={"coins": ["pepe", "WIF", "pepe"]})
    assert r.status_code == 200 and r.json()["coins"] == ["PEPE", "WIF"]
    original = main.S.manager.instruments["PEPE"]
    original_stream = original.streams["Binance:spot"]
    original.analytics.cvd_points.append([0, 123, 456, None])
    assert client.post("/api/instruments", json={"coins": ["A1", "B2", "C3"]}).status_code == 200
    assert client.put("/api/watch", json={"coins": ["BAD-1"]}).status_code == 400
    st = client.get("/api/state").json()
    assert [i["coin"] for i in st["instruments"]] == ["DEMOX", "PEPE", "WIF", "A1", "B2", "C3"]
    client.post("/api/coin", json={"coin": "PEPE"})
    assert main.S.manager.instruments["PEPE"] is original
    assert main.S.manager.streams["Binance:spot"] is original_stream
    assert main.S.analytics is original.analytics and [0, 123, 456, None] in main.S.analytics.cvd_points
    assert client.get("/api/watch").json()["coins"] == ["DEMOX", "WIF", "A1", "B2", "C3"]
    client.post("/api/coin", json={"coin": "DEMOX"})
    assert original.streams["Binance:spot"] is original_stream
    assert client.get("/api/analytics?coin=PEPE").json()["coin"] == "PEPE"


def test_ws_client_of_a_watched_coin_gets_only_that_coin(client):
    client.put("/api/watch", json={"coins": ["WIF"]})
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "filter", "min_usd": 0, "coin": "WIF", "lite": True})
        seen = set()
        snap = trades = False
        for _ in range(300):
            msg = ws.receive_json()
            if msg["type"] == "snapshot":
                seen.add(msg["coin"])
                snap = snap or msg["coin"] == "WIF"
            elif msg["type"] == "trades":
                seen.update(r["coin"] for r in msg["rows"])
                trades = True
            if msg["type"] == "walls":
                assert all(row["coin"] == "WIF" for row in msg["rows"])
            if snap and trades:
                break
        assert snap and trades and seen == {"WIF"}


def test_wall_override_per_venue_and_coin(client):
    r = client.put("/api/wall-overrides", json={"key": "WEEX:perp", "off": True})
    assert r.status_code == 200 and r.json()["overrides"]["WEEX:perp"]["off"] is True
    r = client.put("/api/wall-overrides", json={"key": "Gate:spot", "min_usd": 500000, "ratio": 20})
    assert r.json()["overrides"]["Gate:spot"] == {"off": False, "min_usd": 500000, "ratio": 20}
    assert client.put("/api/wall-overrides", json={"key": "Gate:spot", "ratio": 0.5}).status_code == 400
    # other coins are not affected
    assert client.get("/api/wall-overrides?coin=PEPE").json()["overrides"] == {}
    from app import main as m
    assert m.S.detector.ingest_book("WEEX:perp", 1.0, [(1.0, 1e9)], [(1.1, 1.0)]) == []
    # back to the common settings
    r = client.put("/api/wall-overrides", json={"key": "WEEX:perp"})
    assert "WEEX:perp" not in r.json()["overrides"]


def test_ws_analytics_only_on_request(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "filter", "analytics": True})
        for _ in range(400):
            msg = ws.receive_json()
            if msg["type"] == "analytics":
                assert msg["coin"] == "DEMOX" and "oi" in msg
                break
        else:
            raise AssertionError("no analytics snapshot")


def test_minimal_analytics_endpoints(client):
    snap = client.get("/api/analytics").json()
    assert snap["coin"] == "DEMOX" and set(snap) == {"type", "coin", "ts", "delta", "cvd", "oi"}
    assert [w["w"] for w in snap["delta"]["windows"]] == ["1m", "5m", "15m", "1h"]
    assert "streams" in client.get("/api/health").json()
    for path in ("/api/journal", "/api/lab", "/api/modules"):
        assert client.get(path).status_code == 404


def test_oi_history_endpoint_is_scoped_to_current_coin_and_perp(client):
    import time

    now = time.time()
    main.S.analytics.oi("Binance:perp", now, 100, 200)
    r = client.get("/api/oi/history", params={"coin": "DEMOX", "key": "Binance:perp", "seconds": 3600})
    assert r.status_code == 200
    assert r.json()["points"][-1] == [now, 100, 200]
    assert client.get("/api/oi/history?coin=OTHER&key=Binance:perp").status_code == 404
    assert client.get("/api/oi/history?coin=DEMOX&key=Binance:spot").status_code == 404
    assert client.get("/api/oi/history?coin=DEMOX&key=Binance:perp&seconds=999").status_code == 400
    client.post("/api/coin", json={"coin":"OTHER"})
    retained = client.get("/api/oi/history?coin=DEMOX&key=Binance:perp").json()
    assert [now, 100, 200] in retained["points"]


def test_removing_instrument_stops_only_its_tasks_and_preserves_settings(client):
    client.post("/api/instruments", json={"coins":["PEPE","WIF"]})
    client.put("/api/venues", json={"coin":"PEPE","venues":["Binance"]})
    removed = main.S.manager.instruments["PEPE"]
    kept = main.S.manager.instruments["WIF"]
    client.post("/api/coin", json={"coin":"PEPE"})
    response = client.delete("/api/instruments/PEPE")
    assert response.status_code == 200 and response.json()["selected"] == "DEMOX"
    assert all(t.done() for t in removed.tasks.values()) and not removed.analytics.feeds
    assert main.S.manager.instruments["WIF"] is kept
    assert not any(t.done() for t in kept.tasks.values())
    assert client.get("/api/analytics?coin=PEPE").status_code == 404
    assert client.get("/api/venues?coin=PEPE").json()["venues"] == ["Binance"]
    assert client.delete("/api/instruments/WIF").status_code == 200
    assert client.delete("/api/instruments/DEMOX").status_code == 400


def test_secondary_coin_has_full_analytics_ws_without_changing_selected_instrument(client):
    client.post("/api/instruments", json={"coins":["WIF"]})
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type":"filter", "coin":"WIF", "analytics":True,"tape":False,"walls":False})
        for _ in range(50):
            message = ws.receive_json()
            if message["type"] == "analytics":
                assert message["coin"] == "WIF"
                assert set(message) == {"type","coin","ts","delta","cvd","oi"}
                break
        else:
            raise AssertionError("Secondary instrument did not receive analytics")
    assert client.get("/api/state").json()["coin"] == "DEMOX"


def test_instrument_list_and_selection_survive_restart(client):
    import asyncio
    from app.storage import Storage

    client.post("/api/instruments", json={"coins":["WIF","PEPE"]})
    client.post("/api/coin", json={"coin":"PEPE"})
    async def saved():
        db = Storage(settings.db_path)
        await db.open()
        try:
            return await db.get("instruments"), await db.get("coin")
        finally:
            await db.close()
    assert asyncio.run(saved()) == (["DEMOX","WIF","PEPE"], "PEPE")


def test_walls_and_lite_snapshots_route_by_instrument():
    import orjson
    from app.hub import Hub
    hub = Hub()
    hub.primary = "A"
    first, second = hub.add(None), hub.add(None)
    first.set_filter({"coin":"A","lite":True})
    second.set_filter({"coin":"B","lite":True})
    hub.broadcast_snapshot({"coin":"A","streams":[{"key":"X:spot","spark":[1,2]}]},
                           {"B":{"coin":"B","streams":[{"key":"X:spot","spark":[3,4]}]}})
    assert "spark" not in orjson.loads(first.queue.get_nowait())["streams"][0]
    assert "spark" not in orjson.loads(second.queue.get_nowait())["streams"][0]
    hub.push_walls([{"coin":"A","key":"X:spot"},{"coin":"B","key":"X:spot"}])
    hub.flush_tape()
    assert [row["coin"] for row in orjson.loads(first.queue.get_nowait())["rows"]] == ["A"]
    assert [row["coin"] for row in orjson.loads(second.queue.get_nowait())["rows"]] == ["B"]


def test_venue_selection_is_per_coin_and_persisted(client):
    original = main.S.manager.streams["Binance:spot"]
    r = client.put("/api/venues", json={"coin": "DEMOX", "venues": ["Binance"]})
    assert r.status_code == 200 and r.json()["venues"] == ["Binance"]
    assert main.S.manager.streams["Binance:spot"] is original
    assert set(main.S.manager.streams) == {"Binance:spot", "Binance:perp"}
    assert client.get("/api/venues?coin=OTHER").json()["venues"] is None
    client.post("/api/coin", json={"coin": "OTHER"})
    client.post("/api/coin", json={"coin": "DEMOX"})
    assert set(main.S.manager.streams) == {"Binance:spot", "Binance:perp"}
    import asyncio
    from app.storage import Storage

    async def saved():
        db = Storage(settings.db_path)
        await db.open()
        try:
            return await db.get("venue_selection")
        finally:
            await db.close()
    assert asyncio.run(saved())["DEMOX"] == ["Binance"]


def test_empty_venue_selection_and_watch_coin(client):
    client.put("/api/watch", json={"coins": ["PEPE"]})
    assert client.put("/api/venues", json={"coin": "PEPE", "venues": []}).status_code == 200
    assert not main.S.manager.watch["PEPE"].streams
    assert main.S.manager.streams
    client.put("/api/venues", json={"coin": "DEMOX", "venues": []})
    assert not main.S.manager.streams
    assert not client.get("/api/analytics").json()["oi"]["venues"]
    client.put("/api/venues", json={"coin": "DEMOX", "venues": None})
    assert main.S.manager.streams
    assert client.get("/api/venues?coin=DEMOX").json()["venues"] is None


def test_invalid_venues_do_not_change_selection(client):
    assert client.put("/api/venues", json={"coin": "DEMOX", "venues": ["Unknown"]}).status_code == 400
    assert client.put("/api/venues", json={"coin": "BAD-COIN", "venues": []}).status_code == 400
    assert client.get("/api/venues?coin=DEMOX").json()["venues"] is None
