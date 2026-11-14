from __future__ import annotations

from bot.data import cache
from bot.models import Bar


def _bars(ts0=0, n=5, close=100.0):
    return [Bar("BTC/USD", "4h", ts0 + i * 14_400_000, close, close + 1,
                close - 1, close + 0.5, 10.0) for i in range(n)]


def test_cache_roundtrip(tmp_path):
    d = str(tmp_path)
    cache.save_bars(d, "crypto", _bars())
    loaded = cache.load_bars(d, "crypto", "BTC/USD", "4h")
    assert len(loaded) == 5
    assert loaded[0].close == 100.5


def test_cache_dedupes_overlap(tmp_path):
    d = str(tmp_path)
    cache.save_bars(d, "crypto", _bars(n=5))
    cache.save_bars(d, "crypto", _bars(ts0=2 * 14_400_000, n=5, close=200.0))
    loaded = cache.load_bars(d, "crypto", "BTC/USD", "4h")
    assert len(loaded) == 7                      # 0..6, overlap replaced
    assert loaded[2].close == 200.5              # keep="last" wins
    assert [b.ts for b in loaded] == sorted(b.ts for b in loaded)


def test_history_df_wide(tmp_path):
    d = str(tmp_path)
    cache.save_bars(d, "crypto", _bars())
    eth = [Bar("ETH/USD", "4h", i * 14_400_000, 10, 11, 9, 10.5, 5.0)
           for i in range(5)]
    cache.save_bars(d, "crypto", eth)
    df = cache.load_history_df(d, "crypto", ["BTC/USD", "ETH/USD"], "4h")
    assert list(df.columns) == ["BTC/USD", "ETH/USD"]
    assert len(df) == 5
