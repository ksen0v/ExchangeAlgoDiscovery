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
