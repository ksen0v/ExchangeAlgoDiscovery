from app.collectors.base import Stream
from app.models import Trade
from app.repeats import RepeatTracker

NOW = 1_800_000_000.0


def test_equal_sizes_are_grouped_after_min_count():
    rt = RepeatTracker(tolerance=0.03, window=60, min_count=3)
    tags = [rt.observe("A:spot", Trade(NOW + i * 2, 1.0, 1, 5000 * (1 + 0.01 * (i % 2)), "buy")) for i in range(5)]
    assert tags[:2] == [None, None]
    assert [t[1] for t in tags[2:]] == [3, 4, 5]
    assert len({t[0] for t in tags[2:]}) == 1  # one colour for the whole series


def test_groups_are_per_side_stream_and_size():
    rt = RepeatTracker(min_count=2)
    rt.observe("A:spot", Trade(NOW, 1, 1, 5000, "buy"))
    assert rt.observe("A:spot", Trade(NOW + 1, 1, 1, 5000, "sell")) is None
    assert rt.observe("B:spot", Trade(NOW + 1, 1, 1, 5000, "buy")) is None
    assert rt.observe("A:spot", Trade(NOW + 1, 1, 1, 7000, "buy")) is None
    assert rt.observe("A:spot", Trade(NOW + 2, 1, 1, 5050, "buy"))[1] == 2


def test_group_expires_after_window():
    rt = RepeatTracker(window=60, min_count=2)
    rt.observe("A:spot", Trade(NOW, 1, 1, 5000, "buy"))
    assert rt.observe("A:spot", Trade(NOW + 61, 1, 1, 5000, "buy")) is None
    assert rt.observe("A:spot", Trade(NOW + 62, 1, 1, 5000, "buy"))[1] == 2


def test_small_and_unknown_side_prints_are_ignored():
    rt = RepeatTracker(min_count=1, min_usd=100)
    assert rt.observe("A:spot", Trade(NOW, 1, 1, 50, "buy")) is None
    assert rt.observe("A:spot", Trade(NOW, 1, 1, 500, "?")) is None


def test_coarse_timestamp_streams_do_not_merge_orders():
    got = []
    s = Stream("V", "perp", "X", lambda _s, trades, _live: got.extend(trades))
    same_ts = [Trade(NOW, 1.0, 1000, 1000.0, "buy") for _ in range(5)]
    s.emit(same_ts)
    assert len(got) == 1  # ms-precision feed: one order swept 5 levels
    got.clear()
    s.merge_fills = False
    s.emit(same_ts)
    assert len(got) == 5  # coarse feed: 5 separate orders
