"""TradingView market data provider (websocket, anonymous).

Menggantikan provider MetaTrader 5 sepenuhnya: tidak butuh terminal, container,
atau akun broker. Data diambil dari chart session TradingView lewat
package `tradingview-api` (modul import: `TradingView`).

Aktifkan dengan MARKET_PROVIDER=tradingview di .env.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import pandas as pd

from .timeframes import normalize_tf

log = logging.getLogger(__name__)

OHLCV_COLS = ["open", "high", "low", "close", "volume"]

# timeframe internal -> timeframe TradingView
TV_TF: dict[str, str] = {
    "m1": "1",
    "m5": "5",
    "m15": "15",
    "m30": "30",
    "h1": "60",
    "h4": "240",
    "d1": "1D",
    "w1": "1W",
}

# Simbol yang di TradingView bukan pasangan FX spot biasa.
TV_SYMBOLS: dict[str, str] = {
    "XAUUSD": "TVC:GOLD", "GOLD": "TVC:GOLD", "XAU": "TVC:GOLD",
    "XAGUSD": "TVC:SILVER", "SILVER": "TVC:SILVER", "XAG": "TVC:SILVER",
    "BTCUSD": "BITSTAMP:BTCUSD", "BTCUSDT": "BINANCE:BTCUSDT", "BTC": "BITSTAMP:BTCUSD",
    "ETHUSD": "BITSTAMP:ETHUSD", "ETHUSDT": "BINANCE:ETHUSDT", "ETH": "BITSTAMP:ETHUSD",
    "US30": "TVC:DJI", "DJI": "TVC:DJI", "DOW": "TVC:DJI",
    "NAS100": "TVC:NDX", "NDX": "TVC:NDX", "USTEC": "TVC:NDX",
    "SPX500": "TVC:SPX", "SPX": "TVC:SPX", "US500": "TVC:SPX",
    "USOIL": "TVC:USOIL", "WTI": "TVC:USOIL", "CRUDE": "TVC:USOIL",
    "UKOIL": "TVC:UKOIL", "BRENT": "TVC:UKOIL",
    "DXY": "TVC:DXY", "USDX": "TVC:DXY",
}


class TVUnavailable(RuntimeError):
    """Dilempar saat package TradingView tidak ada atau chart session gagal."""


def to_tv_symbol(symbol: str, exchange: str = "") -> str:
    """EURUSD -> FX:EURUSD (atau OANDA:EURUSD bila TV_EXCHANGE diisi)."""
    raw = (symbol or "").strip().upper()
    if ":" in raw:
        return raw
    if raw in TV_SYMBOLS:
        return TV_SYMBOLS[raw]
    prefix = (exchange or "FX").strip().upper()
    return f"{prefix}:{raw}"


def _load_client() -> Any:
    try:
        from TradingView import Client, ChartSession  # type: ignore
    except ImportError as exc:  # pragma: no cover - tergantung environment
        raise TVUnavailable(
            "package tradingview-api tidak terpasang; jalankan "
            "`pip install tradingview-api` (butuh websockets + requests)"
        ) from exc
    return Client, ChartSession


class TradingViewProvider:
    """Provider sinkron di atas client websocket async TradingView."""

    def __init__(
        self,
        *,
        exchange: str = "",
        server: str = "data",
        timeout: float = 25.0,
        range_count: int = 300,
        cache_ttl: float = 0.0,
        debug: bool = False,
        retries: int = 3,
    ) -> None:
        self.exchange = exchange
        self.server = server
        self.timeout = timeout
        self.retries = max(int(retries), 1)
        self.range_count = max(int(range_count), 60)
        self.cache_ttl = cache_ttl
        self.debug = debug
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ fetch
    def fetch(self, symbol: str, timeframe: str, bars: int = 300) -> pd.DataFrame:
        tf = normalize_tf(timeframe)
        ticker = to_tv_symbol(symbol, self.exchange)
        key = (ticker, tf)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
        if hit and self.cache_ttl > 0 and now - hit[0] < self.cache_ttl:
            return hit[1].tail(bars).copy()

        want = max(int(bars), 1)
        df = self._run_with_retry(ticker, tf, max(want, self.range_count))
        if df.empty:
            raise TVUnavailable(f"no data for {ticker} {tf} from TradingView")
        df = df.tail(want)
        with self._lock:
            self._cache[key] = (now, df)
        return df.copy()

    def quote(self, symbol: str, ttl: float = 5.0) -> dict:
        """Harga terakhir + umur data, dari bar m1 terbaru."""
        tf = "m1"
        ticker = to_tv_symbol(symbol, self.exchange)
        df = self.fetch(symbol, tf, bars=60)
        stamp = df.index[-1]
        price = float(df["close"].iloc[-1])
        lag = max(int((pd.Timestamp.now(tz="UTC") - stamp).total_seconds()), 0)
        return {
            "price": price,
            "time": str(stamp),
            "lag_s": lag,
            "ticker": ticker,
            "source": "tradingview",
        }

    # ------------------------------------------------------------------ async
    async def afetch(self, symbol: str, timeframe: str, bars: int = 300) -> pd.DataFrame:
        """Versi async: dipakai dari dalam event loop bot tanpa thread tambahan.

        `fetch()` sinkron memindahkan pekerjaan ke thread dengan loop sendiri.
        Di proot/Termux pola itu membuat epoll loop utama mati dengan
        `OSError: [Errno 38] Function not implemented`, jadi jalur bot memakai
        coroutine ini.
        """
        tf = normalize_tf(timeframe)
        ticker = to_tv_symbol(symbol, self.exchange)
        key = (ticker, tf)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
        if hit and self.cache_ttl > 0 and now - hit[0] < self.cache_ttl:
            return hit[1].tail(bars).copy()

        want = max(int(bars), 1)
        df = await self._afetch_with_retry(ticker, tf, max(want, self.range_count))
        if df.empty:
            raise TVUnavailable(f"no data for {ticker} {tf} from TradingView")
        df = df.tail(want)
        with self._lock:
            self._cache[key] = (now, df)
        return df.copy()

    async def aquote(self, symbol: str, ttl: float = 5.0) -> dict:
        ticker = to_tv_symbol(symbol, self.exchange)
        df = await self.afetch(symbol, "m1", bars=60)
        stamp = df.index[-1]
        price = float(df["close"].iloc[-1])
        lag = max(int((pd.Timestamp.now(tz="UTC") - stamp).total_seconds()), 0)
        return {
            "price": price,
            "time": str(stamp),
            "lag_s": lag,
            "ticker": ticker,
            "source": "tradingview",
        }

    async def _afetch_with_retry(self, ticker: str, tf: str, count: int) -> pd.DataFrame:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return await self._fetch_async(ticker, tf, count)
            except TVUnavailable as exc:
                last = exc
                text = str(exc).lower()
                retryable = any(
                    k in text
                    for k in ("handshake", "timeout", "timed out", "connection", "closed")
                )
                if attempt >= self.retries or not retryable:
                    raise
                log.warning(
                    "TradingView %s %s gagal (attempt %d/%d): %s",
                    ticker, tf, attempt, self.retries, exc,
                )
                await asyncio.sleep(0.8 * attempt)
        raise last  # pragma: no cover

    def _run_with_retry(self, ticker: str, tf: str, count: int) -> pd.DataFrame:
        """Handshake TradingView kadang timeout; ulangi beberapa kali."""
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return self._run(lambda: self._fetch_async(ticker, tf, count))
            except TVUnavailable as exc:
                last = exc
                text = str(exc).lower()
                retryable = any(
                    k in text
                    for k in ("handshake", "timeout", "timed out", "connection", "closed")
                )
                if attempt >= self.retries or not retryable:
                    raise
                log.warning(
                    "TradingView %s %s gagal (attempt %d/%d): %s",
                    ticker, tf, attempt, self.retries, exc,
                )
                time.sleep(0.8 * attempt)
        raise last  # pragma: no cover - loop selalu raise/return

    def _run(self, factory: Any) -> Any:
        """Jalankan coroutine factory() di loop sendiri.

        Node graph berjalan di dalam event loop yang sudah aktif, dan
        `asyncio.run()` menolak dipanggil dari sana (RuntimeError: asyncio.run()
        cannot be called from a running event loop). Jadi bila loop aktif ada,
        coroutine dijalankan di thread terpisah dengan loop miliknya sendiri.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._guard(asyncio.run, factory)

        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                box["value"] = asyncio.run(factory())
            except BaseException as exc:  # noqa: BLE001 - diteruskan ke pemanggil
                box["error"] = exc

        thread = threading.Thread(target=worker, name="tv-fetch", daemon=True)
        thread.start()
        thread.join()
        if "error" in box:
            raise self._wrap(box["error"])
        return box["value"]

    @staticmethod
    def _wrap(exc: BaseException) -> BaseException:
        if isinstance(exc, TVUnavailable):
            return exc
        return TVUnavailable(f"TradingView session gagal: {type(exc).__name__}: {exc}")

    def _guard(self, runner: Any, factory: Any) -> Any:
        try:
            return runner(factory())
        except TVUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - websocket/library errors
            raise self._wrap(exc) from exc

    async def _fetch_async(self, ticker: str, tf: str, count: int) -> pd.DataFrame:
        Client, ChartSession = _load_client()
        client = Client(debug=self.debug, server=self.server)
        periods: dict[int, dict] = {}
        done = asyncio.Event()
        errors: list[str] = []

        def on_update(payload: dict) -> None:
            for row in payload.get("periods") or []:
                try:
                    periods[int(row["time"])] = row
                except (KeyError, TypeError, ValueError):
                    continue
            if periods:
                done.set()

        def on_symbol_loaded(*_: Any) -> None:
            return None

        def on_error(*messages: Any) -> None:
            errors.append(" ".join(str(m) for m in messages))
            done.set()

        # Client.send() menahan paket selama `logged` masih False dan hanya
        # mem-flush queue saat connect; tanpa hook ini subscribe tidak pernah
        # terkirim sehingga session diam sampai timeout. Daftarkan SEBELUM
        # connect karena event `logged` bisa datang sebelum connect() selesai.
        client.on_logged(lambda *_: client._send_queue())
        await client.connect()
        try:
            chart = ChartSession(client)
            chart.on_update(on_update)
            chart.on_symbol_loaded(on_symbol_loaded)
            chart.on_error(on_error)
            chart.subscribe(ticker, TV_TF[tf], count)
            try:
                await asyncio.wait_for(done.wait(), timeout=self.timeout)
            except asyncio.TimeoutError:
                if not periods:
                    raise TVUnavailable(
                        f"timeout {self.timeout:.0f}s menunggu {ticker} {tf} "
                        f"dari TradingView"
                        + (f" ({errors[-1]})" if errors else "")
                    )
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001 - best effort
                pass
            await asyncio.sleep(0)

        if not periods:
            raise TVUnavailable(
                f"TradingView tidak mengirim bar untuk {ticker} {tf}"
                + (f" ({errors[-1]})" if errors else "")
            )
        return _periods_to_frame(periods)


def _periods_to_frame(periods: dict[int, dict]) -> pd.DataFrame:
    rows = sorted(periods.values(), key=lambda r: int(r["time"]))
    df = pd.DataFrame(
        {
            "open": [float(r["open"]) for r in rows],
            "high": [float(r["max"]) for r in rows],
            "low": [float(r["min"]) for r in rows],
            "close": [float(r["close"]) for r in rows],
            "volume": [float(r.get("volume") or 0.0) for r in rows],
        },
        index=pd.to_datetime([int(r["time"]) for r in rows], unit="s", utc=True),
    )
    return df[OHLCV_COLS][~df.index.duplicated(keep="last")].sort_index()
