"""MetaTrader 5 market data provider.

Dua mode koneksi:
1. `MetaTrader5` package asli (Windows / Wine) — import langsung.
2. `mt5linux` bridge (Linux/ARM) — RPyC ke terminal MT5 yang jalan di container
   docker/udocker. Modul ini hanya klien; terminalnya tetap harus hidup.

Aktifkan dengan MARKET_PROVIDER=mt5 di .env.
"""
from __future__ import annotations

import inspect
import logging
import time
import types
from typing import Any

import pandas as pd

from .timeframes import normalize_tf

log = logging.getLogger(__name__)

# Konstanta timeframe MT5 (nilai resmi, tidak diimpor agar modul tetap bisa
# di-import walau package MetaTrader5 tidak terpasang).
TF_CONST: dict[str, int] = {
    "m1": 1, "m5": 5, "m15": 15, "m30": 30,
    "h1": 16385, "h4": 16388, "d1": 16408, "w1": 32769,
}

OHLCV_COLS = ["open", "high", "low", "close", "volume"]


class MT5Unavailable(RuntimeError):
    """Dilempar saat terminal/package MT5 tidak bisa dipakai."""


def _import_mt5(host: str = "127.0.0.1", port: int = 18812, timeout: int = 300,
                engine: str = "auto") -> Any:
    """Package asli (modul) atau bridge mt5linux (instance class)."""
    try:
        import MetaTrader5 as mt5  # type: ignore

        return mt5
    except ImportError:
        pass
    try:
        from mt5linux import MetaTrader5 as Mt5Linux  # type: ignore
    except ImportError as exc:  # pragma: no cover - tergantung environment
        raise MT5Unavailable(
            "package MetaTrader5 / mt5linux tidak terpasang; "
            "jalankan `pip install MetaTrader5` (Windows) atau `pip install mt5linux`"
        ) from exc

    # Signature beda antar versi mt5linux:
    #   0.1.x/0.2.x -> (host, port)
    #   1.x         -> (host, port, timeout, engine, ...)
    # Kirim hanya parameter yang benar-benar diterima, kalau tidak TypeError.
    try:
        accepted = set(inspect.signature(Mt5Linux.__init__).parameters)
    except (TypeError, ValueError):  # pragma: no cover - signature aneh
        accepted = {"self", "host", "port", "timeout"}
    wanted = {"host": host, "port": port, "timeout": timeout, "engine": engine}
    kwargs = {k: v for k, v in wanted.items() if k in accepted}
    try:
        return Mt5Linux(**kwargs)
    except Exception as exc:  # noqa: BLE001 - mis. docker/udocker tidak ada
        raise MT5Unavailable(
            f"mt5linux gagal start ({type(exc).__name__}: {exc}). "
            "Versi 1.x butuh container runtime (docker/udocker) untuk menjalankan "
            "terminal MT5; pasang salah satunya atau pakai MT5_HOST ke terminal "
            "yang sudah jalan. Alternatif tanpa container: "
            "`pip install mt5linux==0.2.4` lalu jalankan terminal MT5 di Windows/Wine "
            "dengan server RPyC di port 18812."
        ) from exc


def _is_bridge(api: Any) -> bool:
    """True bila objek berasal dari mt5linux (bukan modul MetaTrader5)."""
    return not isinstance(api, types.ModuleType)


class MT5Provider:
    """Provider OHLCV + tick dari terminal MetaTrader 5."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 18812,
        login: int | None = None,
        password: str = "",
        server: str = "",
        path: str = "",
        timeout: int = 300,
        suffix: str = "",
        cache_ttl: float = 0.0,
        engine: str = "auto",
    ) -> None:
        self.host = host
        self.port = port
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self.timeout = timeout
        self.suffix = suffix
        self.cache_ttl = cache_ttl
        self.engine = engine
        self._mt5: Any | None = None
        self._connected = False
        self._cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = {}
        self._quote_cache: dict[str, tuple[float, dict]] = {}

    # ---------------------------------------------------------------- koneksi
    def _api(self) -> Any:
        if self._mt5 is None:
            self._mt5 = _import_mt5(self.host, self.port, self.timeout, self.engine)
        return self._mt5

    def connect(self) -> Any:
        """Initialize + login sekali; idempoten."""
        if self._connected and self._mt5 is not None:
            return self._mt5
        mt5 = self._api()
        kwargs: dict[str, Any] = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login:
            kwargs.update(
                login=int(self.login),
                password=self.password,
                server=self.server,
            )
        # host/port hanya untuk klien RPyC (mt5linux), bukan argumen initialize.
        if kwargs:
            ok = mt5.initialize(**kwargs)
        else:
            ok = mt5.initialize()
        if not ok:
            err = getattr(mt5, "last_error", lambda: ("?",))()
            raise MT5Unavailable(f"mt5.initialize gagal: {err}")
        self._connected = True
        return mt5

    def close(self) -> None:
        if self._mt5 is not None and self._connected:
            try:
                self._mt5.shutdown()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass
        self._connected = False

    # ---------------------------------------------------------------- simbol
    def mt5_symbol(self, symbol: str) -> str:
        s = symbol.strip().upper()
        if self.suffix and not s.endswith(self.suffix.upper()):
            return f"{s}{self.suffix.upper()}"
        return s

    def _ensure_symbol(self, mt5: Any, symbol: str) -> str:
        name = self.mt5_symbol(symbol)
        info = mt5.symbol_info(name)
        if info is None:
            raise MT5Unavailable(
                f"simbol {name!r} tidak ada di Market Watch; "
                f"cek MT5_SYMBOL_SUFFIX atau tambahkan manual"
            )
        if not getattr(info, "visible", True):
            mt5.symbol_select(name, True)
        return name

    # ---------------------------------------------------------------- OHLCV
    def fetch(self, symbol: str, timeframe: str, bars: int = 300) -> pd.DataFrame:
        tf = normalize_tf(timeframe)
        key = (self.mt5_symbol(symbol), tf)
        now = time.time()
        cached = self._cache.get(key)
        if cached and self.cache_ttl > 0 and now - cached[0] < self.cache_ttl:
            return cached[1].tail(bars).copy()

        mt5 = self.connect()
        name = self._ensure_symbol(mt5, symbol)
        count = max(int(bars), 60)
        rates = mt5.copy_rates_from_pos(name, TF_CONST[tf], 0, count)
        if rates is None or len(rates) == 0:
            err = getattr(mt5, "last_error", lambda: ("?",))()
            raise RuntimeError(f"tidak ada data {name} {tf} dari MT5: {err}")

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").rename(columns={"tick_volume": "volume"})
        for col in OHLCV_COLS:
            if col not in df.columns:
                df[col] = 0.0
        df = df[OHLCV_COLS].astype("float64").dropna()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.tail(count)
        self._cache[key] = (now, df)
        return df.copy()

    # ---------------------------------------------------------------- tick
    def quote(self, symbol: str, ttl: float = 2.0) -> dict:
        """Tick terakhir: bid/ask/mid + umur data dalam detik."""
        mt5 = self.connect()
        name = self._ensure_symbol(mt5, symbol)
        now = time.time()
        hit = self._quote_cache.get(name)
        if hit and now - hit[0] < ttl:
            return dict(hit[1])

        tick = mt5.symbol_info_tick(name)
        price: float | None = None
        stamp: str | None = None
        bid = ask = None
        if tick is not None:
            bid = float(getattr(tick, "bid", 0.0) or 0.0)
            ask = float(getattr(tick, "ask", 0.0) or 0.0)
            last = float(getattr(tick, "last", 0.0) or 0.0)
            if bid > 0 and ask > 0:
                price = (bid + ask) / 2
            elif last > 0:
                price = last
            ts = int(getattr(tick, "time", 0) or 0)
            if ts:
                stamp = pd.Timestamp(ts, unit="s", tz="UTC").isoformat()

        out = {
            "price": price,
            "time": stamp,
            "lag_s": _lag_seconds(stamp),
            "ticker": name,
            "bid": bid,
            "ask": ask,
            "spread_pips": round((ask - bid), 10) if bid and ask else None,
        }
        self._quote_cache[name] = (now, out)
        return dict(out)

    # ---------------------------------------------------------------- info
    def point(self, symbol: str) -> float | None:
        """Nilai 1 point broker, dipakai untuk pip yang akurat."""
        try:
            mt5 = self.connect()
            info = mt5.symbol_info(self.mt5_symbol(symbol))
            return float(info.point) if info is not None else None
        except Exception:  # noqa: BLE001 - opsional
            return None


def _lag_seconds(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        ts = pd.Timestamp(stamp)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return max(int((pd.Timestamp.now(tz="UTC") - ts).total_seconds()), 0)
