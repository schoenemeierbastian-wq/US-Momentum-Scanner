from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

import pandas as pd

from spike_scanner.config import Settings
from spike_scanner.models import UniverseItem

logger = logging.getLogger(__name__)

_SYMBOL_KEYS = ("symbol", "ticker", "code")
_PRICE_KEYS = ("price", "last_price", "latest_price", "close")
_CHANGE_KEYS = ("change_ratio", "changeRatio", "change_rate")
_REL_VOLUME_KEYS = ("relative_volume_10d", "relativeVolume10d", "relative_volume")
_VOLUME_KEYS = ("volume", "total_volume")
_NAME_KEYS = ("name", "instrument_name", "display_name")

_TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
_WEBULL_CONNECT_TIMEOUT_SECONDS = 10
_WEBULL_READ_TIMEOUT_SECONDS = 20
_WEBULL_INIT_RETRY_ATTEMPTS = 3

_TRANSIENT_ERROR_MARKERS = (
    "winerror 10053",
    "winerror 10054",
    "winerror 10060",
    "[errno 10053]",
    "[errno 10054]",
    "[errno 10060]",
    "winerror 11001",
    "[errno 11001]",
    "nameresolutionerror",
    "getaddrinfo failed",
    "failed to resolve",
    "name resolution",
    "remotehost geschlossen",
    "connection reset",
    "connection aborted",
    "connection closed",
    "remote host",
    "remotedisconnected",
    "read timed out",
    "connect timeout",
    "temporarily unavailable",
    "max retries exceeded",
    "eof occurred",
)


class TransientNetworkError(RuntimeError):
    """Expliziter vor?bergehender Webull-/HTTP-Netzwerkfehler."""


def is_transient_network_error(error: BaseException | str) -> bool:
    """Erkennt vorübergehende Transportfehler, die gefahrlos wiederholt werden können."""
    if isinstance(error, str):
        text = error.lower()
    else:
        parts: list[str] = []
        current: BaseException | None = error
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, TransientNetworkError):
                return True
            parts.append(f"{type(current).__name__}: {current}")
            current = current.__cause__ or current.__context__
        text = " | ".join(parts).lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


class WebullMarketDataProvider:
    """Adapter für das offizielle Webull OpenAPI Python SDK.

    Der Parser ist absichtlich defensiv, weil Antworten je nach Endpoint und
    SDK-Version unterschiedlich verschachtelt sein können.
    """

    name = "webull"

    def __init__(self, settings: Settings) -> None:
        settings.validate_live()
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
        except ImportError as exc:
            raise RuntimeError(
                "Das Webull SDK fehlt. Installieren mit: "
                "pip install webull-openapi-python-sdk"
            ) from exc

        self.settings = settings
        self._api_client_class = ApiClient
        self._data_client_class = DataClient
        self.client = self._build_client_with_retry()

    def _build_client_once(self):
        # Das SDK verwendet sonst nur 5 s Connect- und 10 s Read-Timeout.
        # Wir setzen die Werte explizit und lassen Retries ausschließlich hier
        # im Scanner steuern, damit keine verschachtelten Retry-Schleifen entstehen.
        api_client = self._api_client_class(
            self.settings.webull_app_key,
            self.settings.webull_app_secret,
            self.settings.webull_region,
            connect_timeout=_WEBULL_CONNECT_TIMEOUT_SECONDS,
            timeout=_WEBULL_READ_TIMEOUT_SECONDS,
            auto_retry=False,
        )
        api_client.add_endpoint(
            self.settings.webull_region, self.settings.webull_api_endpoint
        )
        if self.settings.webull_token_dir:
            api_client.set_token_dir(self.settings.webull_token_dir)

        # DataClient initialisiert sich sofort über /openapi/config. Das Webull-SDK
        # protokolliert bei Transportfehlern standardmäßig den kompletten Request
        # inklusive Auth-Headern. Unterdrücken, die Exception selbst bleibt erhalten.
        api_client.set_stream_logger(
            log_level=logging.CRITICAL, logger_name="webull.core"
        )
        return self._data_client_class(api_client)

    def _build_client_with_retry(self):
        # Der /openapi/config-Aufruf passiert bereits im DataClient-Konstruktor,
        # also bevor _request_with_retry() greifen kann. Deshalb braucht gerade
        # die Initialisierung einen eigenen, begrenzten Retry.
        configured = max(1, int(self.settings.network_retry_attempts))
        attempts = min(_WEBULL_INIT_RETRY_ATTEMPTS, configured)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._build_client_once()
            except Exception as exc:
                last_error = exc
                if not is_transient_network_error(exc) or attempt >= attempts:
                    raise
                wait = min(5.0, self._retry_wait(attempt))
                logger.warning(
                    "Webull-Initialisierung vorübergehend nicht erreichbar; "
                    "neuer Versuch %s/%s in %.1f Sekunden",
                    attempt + 1, attempts, wait,
                )
                time.sleep(wait)
        if last_error is not None:
            raise last_error
        raise RuntimeError("Webull-Initialisierung ohne Ergebnis")

    def _rebuild_client(self) -> None:
        """Erstellt den Client einmal neu; der Aufrufer steuert weitere Retries."""
        try:
            self.client = self._build_client_once()
        except Exception as exc:
            logger.warning("Webull-Client konnte nicht neu aufgebaut werden: %s", exc)

    def _retry_wait(self, attempt: int) -> float:
        base = max(0.5, float(self.settings.network_retry_base_seconds))
        return min(30.0, base * (2 ** max(0, attempt - 1)))

    def _request_with_retry(self, label: str, call, *, attempts_override: int | None = None):
        configured = max(1, int(self.settings.network_retry_attempts))
        attempts = (
            configured
            if attempts_override is None
            else max(1, min(configured, int(attempts_override)))
        )
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = call()
                status_code = getattr(response, "status_code", None)
                if status_code in _TRANSIENT_HTTP_CODES:
                    if attempt < attempts:
                        wait = self._retry_wait(attempt)
                        logger.warning(
                            "%s: HTTP %s; neuer Versuch %s/%s in %.1f Sekunden",
                            label, status_code, attempt + 1, attempts, wait,
                        )
                        time.sleep(wait)
                        continue
                    raise TransientNetworkError(
                        f"{label}: HTTP {status_code}: {getattr(response, 'text', '')}"
                    )
                return response
            except Exception as exc:
                last_error = exc
                if not is_transient_network_error(exc) or attempt >= attempts:
                    raise
                wait = self._retry_wait(attempt)
                logger.warning(
                    "%s: vorübergehender Netzwerkfehler; neuer Versuch %s/%s in %.1f Sekunden (%s)",
                    label, attempt + 1, attempts, wait, exc,
                )
                time.sleep(wait)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{label}: Anfrage ohne Ergebnis")

    def discover_universe(self, limit: int) -> list[UniverseItem]:
        responses: list[Any] = []
        calls = [
            lambda: self.client.screener.get_gainers_losers(
                rank_type="DAY_1",
                category="US_STOCK",
                sort_by="CHANGE_RATIO",
                direction="DESC",
                page_size=limit,
            ),
            lambda: self.client.screener.get_gainers_losers(
                rank_type="PRE_MARKET",
                category="US_STOCK",
                sort_by="CHANGE_RATIO",
                direction="DESC",
                page_size=limit,
            ),
            lambda: self.client.screener.get_most_active(
                category="US_STOCK",
                rank_type="RELATIVE_VOLUME_10D",
                sort_by="RELATIVE_VOLUME_10D",
                direction="DESC",
                page_size=limit,
            ),
        ]
        for call in calls:
            try:
                response = self._request_with_retry(
                    "Webull Screener", call, attempts_override=2
                )
                self._sleep()
                if response.status_code == 200:
                    responses.append(response.json())
                else:
                    logger.warning("Webull Screener HTTP %s: %s", response.status_code, response.text)
            except Exception:
                logger.exception("Webull Screener-Aufruf fehlgeschlagen")

        merged: dict[str, UniverseItem] = {}
        rank = 0
        for payload in responses:
            for row in _find_market_rows(payload):
                symbol = _first_text(row, _SYMBOL_KEYS)
                if not symbol:
                    continue
                symbol = symbol.upper().strip()
                if symbol not in merged:
                    rank += 1
                    merged[symbol] = UniverseItem(
                        symbol=symbol,
                        name=_first_text(row, _NAME_KEYS) or "",
                        source_rank=rank,
                        change_ratio=_first_number(row, _CHANGE_KEYS),
                        relative_volume=_first_number(row, _REL_VOLUME_KEYS),
                        volume=_first_number(row, _VOLUME_KEYS),
                        last_price=_first_number(row, _PRICE_KEYS),
                        raw=row,
                    )
                else:
                    item = merged[symbol]
                    item.change_ratio = _coalesce(item.change_ratio, _first_number(row, _CHANGE_KEYS))
                    item.relative_volume = _coalesce(
                        item.relative_volume, _first_number(row, _REL_VOLUME_KEYS)
                    )
                    item.volume = _coalesce(item.volume, _first_number(row, _VOLUME_KEYS))
                    item.last_price = _coalesce(item.last_price, _first_number(row, _PRICE_KEYS))

        items = list(merged.values())
        items.sort(
            key=lambda item: (
                item.change_ratio if item.change_ratio is not None else -999,
                item.relative_volume if item.relative_volume is not None else -999,
            ),
            reverse=True,
        )
        return items[:limit]

    def get_bars(self, symbol: str, count: int) -> pd.DataFrame:
        from webull.data.common.category import Category
        from webull.data.common.timespan import Timespan

        response = self._request_with_retry(
            f"Minutenbalken {symbol}",
            lambda: self.client.market_data.get_history_bar(
                symbol, Category.US_STOCK.name, Timespan.M1.name
            ),
            attempts_override=1,
        )
        self._sleep()
        if response.status_code != 200:
            raise RuntimeError(f"Bars {symbol}: HTTP {response.status_code}: {response.text}")
        bars = _parse_bars(response.json())
        if bars.empty:
            raise ValueError(f"Keine parsebaren Minutenbalken für {symbol}")
        return bars.tail(count).reset_index(drop=True)

    def get_order_book(self, symbol: str, depth: int = 1) -> dict:
        from webull.data.common.category import Category

        # Webull EU akzeptiert für diesen Endpoint maximal depth=1.
        # Auch alte Aufrufer mit depth=5/10 bleiben dadurch kompatibel.
        safe_depth = 1
        response = self._request_with_retry(
            f"Orderbuch {symbol}",
            lambda: self.client.market_data.get_quotes(
                symbol,
                Category.US_STOCK.name,
                depth=safe_depth,
                overnight_required=False,
            ),
            attempts_override=1,
        )
        self._sleep()
        if response.status_code != 200:
            raise RuntimeError(
                f"Orderbuch {symbol}: HTTP {response.status_code}: {response.text}"
            )
        return _parse_order_book(response.json())

    def _sleep(self) -> None:
        if self.settings.request_delay_seconds > 0:
            time.sleep(self.settings.request_delay_seconds)


def _coalesce(current: float | None, new: float | None) -> float | None:
    return current if current is not None else new


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("%", "").strip()
            if not value:
                return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _walk_dicts(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_dicts(value)


def _find_market_rows(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _walk_dicts(payload):
        symbol = _first_text(row, _SYMBOL_KEYS)
        if symbol and symbol.upper() not in seen:
            seen.add(symbol.upper())
            rows.append(row)
    return rows


def _first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in row.values():
        if isinstance(value, dict):
            found = _first_text(value, keys)
            if found:
                return found
    return None


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in row:
            value = _to_float(row.get(key))
            if value is not None:
                if "ratio" in key.lower() and abs(value) > 2:
                    value /= 100.0
                return value
    for value in row.values():
        if isinstance(value, dict):
            found = _first_number(value, keys)
            if found is not None:
                return found
    return None


def _parse_bars(payload: Any) -> pd.DataFrame:
    aliases = {
        "timestamp": ("timestamp", "time", "trade_time", "start_time", "t"),
        "open": ("open", "o"),
        "high": ("high", "h"),
        "low": ("low", "l"),
        "close": ("close", "price", "c"),
        "volume": ("volume", "v"),
    }
    candidate_rows: list[dict[str, Any]] = []
    for obj in _walk_dicts(payload):
        present = sum(any(key in obj for key in keys) for keys in aliases.values())
        if present >= 4:
            candidate_rows.append(obj)

    parsed: list[dict[str, Any]] = []
    for row in candidate_rows:
        out: dict[str, Any] = {}
        for target, keys in aliases.items():
            value = next((row.get(k) for k in keys if row.get(k) is not None), None)
            out[target] = value
        if all(_to_float(out[k]) is not None for k in ("open", "high", "low", "close", "volume")):
            parsed.append(out)

    if not parsed:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    frame = pd.DataFrame(parsed).drop_duplicates()
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
    if frame["timestamp"].isna().all():
        frame["timestamp"] = pd.date_range(
            end=pd.Timestamp.now(tz="UTC"), periods=len(frame), freq="min"
        )
    frame = frame.dropna(subset=["open", "high", "low", "close", "volume"])
    return frame.sort_values("timestamp").reset_index(drop=True)


def _parse_order_book(payload: Any) -> dict:
    result: dict[str, list[dict[str, float]]] = {"bids": [], "asks": []}

    def parse_levels(levels: Any) -> list[dict[str, float]]:
        parsed_levels: list[dict[str, float]] = []
        if not isinstance(levels, list):
            return parsed_levels
        for level in levels:
            if not isinstance(level, dict):
                continue
            price = _first_number(level, ("price", "p"))
            size = _first_number(level, ("size", "quantity", "qty", "volume"))
            if price is not None and size is not None:
                parsed_levels.append({"price": price, "size": size})
        return parsed_levels

    for obj in _walk_dicts(payload):
        for key in ("bids", "bid"):
            if key in obj and not result["bids"]:
                result["bids"] = parse_levels(obj[key])
        for key in ("asks", "ask"):
            if key in obj and not result["asks"]:
                result["asks"] = parse_levels(obj[key])
    return result
