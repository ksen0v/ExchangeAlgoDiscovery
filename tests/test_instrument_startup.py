"""Registration and UI work must not wait for cold exchange clients or market indexing."""
import asyncio
import threading
import time

import pytest

from app.collectors.ccxt_stream import CcxtPool
from app.detector import Detector, DetectorConfig
from app.hub import Hub
from app.manager import Manager


def test_cached_clients_share_prepared_markets_tls_and_the_correct_async_loop():
    import ccxt.pro as ccxtpro

    async def scenario():
        pool = CcxtPool()
        context = await pool._context()
        source = ccxtpro.binance({"asyncio_loop":asyncio.get_running_loop(), "ssl_context":context})
        source.timeout_on_exit = 0
        source.set_markets([{"id":"BTCUSDT","symbol":"BTC/USDT","base":"BTC","quote":"USDT",
                             "baseId":"BTC","quoteId":"USDT","type":"spot","spot":True,
                             "swap":False,"future":False,"option":False,"contract":False,
                             "linear":None,"inverse":None,"settle":None,"active":True,
                             "precision":{},"limits":{},"info":{}}])
        pool._markets["binance"] = (time.time(), pool._market_data(source))
        client = await pool.create("binance")
        client.timeout_on_exit = 0
        try:
            assert client.markets is source.markets and client.markets_by_id is source.markets_by_id
            assert client.symbols is source.symbols
            assert client.ssl_context is context and client.asyncio_loop is asyncio.get_running_loop()
            assert context.check_hostname and client.session is None
            assert await pool._context() is context
        finally:
            await pool.reset()
            await source.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_slow_client_initialization_keeps_the_loop_responsive_and_cleans_up_on_cancel(monkeypatch, cancel):
    import ccxt.pro as ccxtpro

    entered, release = threading.Event(), threading.Event()
    clients = []
    class SlowClient:
        def __init__(self, config):
            self.id, self.options, self.closed = "slow_fixture", {}, False
            for key in ("markets","markets_by_id","symbols","ids","currencies","currencies_by_id","baseCurrencies","quoteCurrencies","codes"):
                setattr(self, key, {})
            clients.append(self)
            entered.set()
            release.wait(3)
        async def load_markets(self):
            return self.markets
        async def close(self):
            self.closed = True
    monkeypatch.setattr(ccxtpro, "slow_fixture", SlowClient, raising=False)
    async def scenario():
        pool = CcxtPool()
        await pool._context()
        task = asyncio.create_task(pool.create("slow_fixture"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not task.done()  # the loop can handle other work while construction is held
            if cancel:
                task.cancel()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert clients[0].closed and not pool._live
            else:
                assert await task is clients[0]
        finally:
            release.set()
            await pool.reset()
    asyncio.run(scenario())


def test_registration_prepares_progress_without_running_collectors_and_delete_cancels_queued_start(monkeypatch):
    import app.manager as manager_module

    running = []
    class Source:
        def __init__(self, venue, kind, source, coin, *args):
            self.venue, self.kind, self.coin = venue, kind, coin
            self.key, self.status = f"{venue}:{kind}", "init"
        async def run(self):
            running.append(self.coin)
            await asyncio.Future()
        async def close(self):
            pass
    monkeypatch.setattr(manager_module, "make_stream", Source)
    async def scenario():
        manager = Manager(Detector(DetectorConfig()), Hub(), None,
                          venue_selection={"BTC":["Binance","Bybit"],"ETH":["Binance"]})
        await manager.set_coin("BTC", deferred=True)
        await manager.ensure_instruments(["ETH"], deferred=True)
        assert not manager.instruments["BTC"].tasks and not manager.instruments["ETH"].tasks
        summary = manager.summaries()[0]
        assert summary["state"] == "connecting" and summary["pending_venues"] == 2
        assert summary["starting"] and summary["connected_venues"] == 0
        await manager.remove_instrument("ETH")
        await manager.start_pending()
        await manager.start_pending()
        await asyncio.sleep(0)
        assert running == ["BTC"] * 4
        for key, stream in manager.streams.items():
            stream.status = "live" if key == "Binance:spot" else "na"
        summary = manager.summaries()[0]
        assert summary["state"] == "live" and summary["connected_venues"] == 1 and summary["total_venues"] == 2
        assert not summary["starting"] and summary["pending_venues"] == 0
        for stream in manager.streams.values():
            stream.status = "na"
        assert manager.summaries()[0]["state"] == "unavailable"
        await manager.shutdown()
    asyncio.run(scenario())
