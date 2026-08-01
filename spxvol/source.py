"""cvforge MCP data access: bars, caching, and capability discovery.

Implemented directly on ``urllib`` so the package has no third-party runtime
dependencies. The wire protocol mirrors what ``convexvalue-client`` does --
JSON-RPC over HTTP with an MCP handshake, and responses that may arrive either
as plain JSON or as an SSE ``data:`` stream.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_BASE_URL = "https://tap.convexvalue.com/api/data/mcp"

# Server-side cap on aggregate rows per call.
MAX_ROWS_PER_CALL = 5000

INTERVALS: dict[str, tuple[int, str]] = {
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1h": (1, "hour"),
    "1d": (1, "day"),
}

# Chunk sizes chosen so a full session's worth of bars stays under the row cap.
CHUNK_DAYS: dict[str, int] = {"1m": 5, "5m": 25, "15m": 60, "30m": 120, "1h": 240, "1d": 3650}

# Keywords suggesting a tool serves underlying (not option) price data.
_UNDERLYING_HINTS = (
    "stock", "equity", "index", "indices", "underlying", "spot", "share",
    "ticker_bars", "aggregate", "aggs", "snapshot", "quote", "chart", "price",
)
_OPTION_MARKERS = ("option", "contract", "strike", "greek")


class SourceError(RuntimeError):
    """Any failure talking to cvforge."""


def api_key_from_env() -> str | None:
    """Resolve the cvforge key the same way ``convexvalue-client`` does."""
    for name in ("CVFORGE_API_KEY", "CV_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    for name in ("CVFORGE_API_KEY_FILE", "CV_API_KEY_FILE"):
        path = os.environ.get(name)
        if path:
            try:
                value = Path(path).expanduser().read_text().strip()
            except OSError as exc:
                raise SourceError(f"cannot read API key from {path}: {exc}") from exc
            if value:
                return value
    return None


class MCPClient:
    """Minimal JSON-RPC-over-HTTP MCP client (stdlib only)."""

    def __init__(self, api_key: str | None = None, *, base_url: str = DEFAULT_BASE_URL,
                 timeout: int = 60, max_retries: int = 4):
        self.api_key = api_key or api_key_from_env()
        if not self.api_key:
            raise SourceError(
                "no cvforge API key: set CVFORGE_API_KEY, CV_API_KEY, "
                "CVFORGE_API_KEY_FILE or CV_API_KEY_FILE"
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._initialized = False
        self._next_id = 1
        self._session_id: str | None = None

    # -- transport -------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "spxvol/0.1.0",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: dict[str, Any]) -> tuple[int, str, dict[str, str]]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url, data=body, headers=self._headers(),
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8"), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers or {})
        except urllib.error.URLError as exc:
            raise SourceError(f"network error contacting {self.base_url}: {exc.reason}") from exc

    def _post_with_retry(self, payload: dict[str, Any]) -> tuple[int, str, dict[str, str]]:
        delay = 2.0
        last: tuple[int, str, dict[str, str]] | None = None
        for attempt in range(self.max_retries + 1):
            status, text, headers = self._post(payload)
            if status not in (429, 500, 502, 503, 504):
                return status, text, headers
            last = (status, text, headers)
            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2
        assert last is not None
        return last

    @staticmethod
    def _parse_body(text: str, headers: dict[str, str]) -> Any:
        stripped = text.strip()
        content_type = headers.get("Content-Type", headers.get("content-type", ""))
        is_sse = ("text/event-stream" in content_type or stripped.startswith("event:")
                  or stripped.startswith("data:") or "\ndata:" in stripped)
        if is_sse:
            lines = [ln[5:].strip() for ln in stripped.splitlines() if ln.startswith("data:")]
            stripped = "\n".join(ln for ln in lines if ln).strip()
            if not stripped:
                raise SourceError("empty SSE response from cvforge")
        if not stripped:
            raise SourceError("empty response from cvforge")
        return json.loads(stripped)

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        self._next_id += 1
        if params is not None:
            payload["params"] = params
        status, text, headers = self._post_with_retry(payload)
        if status != 200:
            raise SourceError(f"cvforge HTTP {status} from {self.base_url}: {text[:400]}")
        self._capture_session(headers)
        data = self._parse_body(text, headers)
        if not isinstance(data, dict):
            raise SourceError(f"expected JSON-RPC object, got {type(data).__name__}")
        if data.get("error"):
            raise SourceError(f"cvforge JSON-RPC error: {data['error']}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise SourceError("cvforge JSON-RPC response had no object result")
        return result

    def _notify(self, method: str) -> None:
        status, _, headers = self._post_with_retry({"jsonrpc": "2.0", "method": method})
        if status not in (200, 202):
            raise SourceError(f"cvforge notification {method} failed with HTTP {status}")
        self._capture_session(headers)

    def _capture_session(self, headers: dict[str, str]) -> None:
        for key in ("Mcp-Session-Id", "mcp-session-id", "MCP-Session-Id"):
            if headers.get(key):
                self._session_id = headers[key]
                return

    # -- protocol --------------------------------------------------------
    def initialize(self) -> None:
        if self._initialized:
            return
        self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "spxvol", "version": "0.1.0"},
        })
        self._notify("notifications/initialized")
        self._initialized = True

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._rpc("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise SourceError("tools/list returned no tools array")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        text = _content_text(result)
        if result.get("isError"):
            raise SourceError(text or f"cvforge tool {name!r} returned isError")
        if not text:
            return result
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def _content_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [item.get("text", "") for item in content if isinstance(item, dict)]
    return "\n".join(part for part in parts if part)


@dataclass
class UnderlyingProbe:
    """What the account can actually serve for underlying (non-option) prices."""

    mcp_tools: list[dict[str, str]] = field(default_factory=list)
    fmp_endpoints: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_candidate(self) -> bool:
        return bool(self.mcp_tools or self.fmp_endpoints)

    def summary(self) -> str:
        lines = []
        if self.mcp_tools:
            lines.append("MCP tools that may serve underlying prices:")
            lines += [f"  - {t['name']}: {t.get('description', '')[:120]}" for t in self.mcp_tools]
        else:
            lines.append("No MCP tool names/descriptions matched underlying-price keywords.")
        if self.fmp_endpoints:
            lines.append("FMP endpoints matching price/chart searches:")
            lines += [f"  - {e}" for e in self.fmp_endpoints[:40]]
        for err in self.errors:
            lines.append(f"  ! {err}")
        return "\n".join(lines)


class CvForgeSource:
    """Bar fetching with on-disk caching, date chunking and capability probing."""

    def __init__(self, client: MCPClient | None = None, *, cache_dir: str | Path | None = None):
        self.client = client or MCPClient()
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "spxvol"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- caching ---------------------------------------------------------
    def _cache_path(self, key: dict[str, Any]) -> Path:
        digest = hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()
        return self.cache_dir / f"{digest}.json.gz"

    def _cached(self, key: dict[str, Any]) -> Any | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

    def _store(self, key: dict[str, Any], value: Any) -> None:
        try:
            with gzip.open(self._cache_path(key), "wt", encoding="utf-8") as handle:
                json.dump(value, handle)
        except OSError:
            pass  # cache is an optimisation, never a correctness requirement

    # -- bars ------------------------------------------------------------
    def option_bars(self, ticker: str, start: date, end: date, interval: str = "1m", *,
                    use_cache: bool = True) -> list[dict[str, Any]]:
        """Fetch aggregates for one contract, chunking so no call hits the row cap."""
        if interval not in INTERVALS:
            raise ValueError(f"unknown interval {interval!r}; expected one of {sorted(INTERVALS)}")
        multiplier, timespan = INTERVALS[interval]
        rows: list[dict[str, Any]] = []
        for chunk_start, chunk_end in chunk_ranges(start, end, CHUNK_DAYS[interval]):
            key = {"t": ticker, "m": multiplier, "s": timespan,
                   "f": chunk_start.isoformat(), "e": chunk_end.isoformat()}
            payload = self._cached(key) if use_cache else None
            if payload is None:
                payload = self.client.call_tool("get_option_bars", {
                    "ticker": ticker,
                    "multiplier": multiplier,
                    "timespan": timespan,
                    "from": chunk_start.isoformat(),
                    "to": chunk_end.isoformat(),
                    "adjusted": True,
                    "sort": "asc",
                    "limit": MAX_ROWS_PER_CALL,
                })
                if use_cache:
                    self._store(key, payload)
            rows.extend(normalize_bars(payload))
        return rows

    def option_daily(self, ticker: str, day: date) -> dict[str, Any]:
        return self.client.call_tool("get_option_daily",
                                     {"ticker": ticker, "date": day.isoformat(), "adjusted": True})

    # -- discovery -------------------------------------------------------
    def probe_underlying_price(self) -> UnderlyingProbe:
        """Ask the account what it can serve for underlying/index prices.

        The cvforge docs are not always reachable, and entitlements vary by plan,
        so this answers the question empirically against the caller's own key
        rather than from documentation.
        """
        probe = UnderlyingProbe()
        try:
            tools = self.client.list_tools()
        except SourceError as exc:
            probe.errors.append(f"tools/list failed: {exc}")
            tools = []

        for tool in tools:
            name = str(tool.get("name", ""))
            desc = str(tool.get("description", ""))
            haystack = f"{name} {desc}".lower()
            if any(marker in haystack for marker in _OPTION_MARKERS):
                continue
            if any(hint in haystack for hint in _UNDERLYING_HINTS):
                probe.mcp_tools.append({"name": name, "description": desc})

        for term in ("historical-chart", "chart", "quote", "index", "historical-price"):
            try:
                found = self.client.call_tool("list_fmp_endpoints", {"search": term, "limit": 25})
            except SourceError as exc:
                probe.errors.append(f"list_fmp_endpoints({term!r}) failed: {exc}")
                continue
            for item in _iter_endpoint_names(found):
                if item not in probe.fmp_endpoints:
                    probe.fmp_endpoints.append(item)
        return probe

    def fmp_request(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        return self.client.call_tool("fmp_request", {"endpoint": endpoint, "params": params or {}})


def _iter_endpoint_names(payload: Any) -> Iterable[str]:
    """Pull endpoint identifiers out of a loosely-typed list_fmp_endpoints reply."""
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_endpoint_names(item)
    elif isinstance(payload, dict):
        for field_name in ("endpoint", "name", "path", "url"):
            value = payload.get(field_name)
            if isinstance(value, str):
                yield value
                return
        for value in payload.values():
            if isinstance(value, (list, dict)):
                yield from _iter_endpoint_names(value)


def normalize_bars(payload: Any) -> list[dict[str, Any]]:
    """Normalize a Massive aggregates payload to stable rows (Polygon shape)."""
    if not isinstance(payload, dict):
        return []
    ticker = payload.get("ticker")
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    out = []
    for row in results:
        if not isinstance(row, dict):
            continue
        out.append({
            "ticker": ticker,
            "timestamp_ms": row.get("t"),
            "open": row.get("o"),
            "high": row.get("h"),
            "low": row.get("l"),
            "close": row.get("c"),
            "volume": row.get("v"),
            "vwap": row.get("vw"),
            "transactions": row.get("n"),
        })
    return out


def chunk_ranges(start: date, end: date, days: int) -> list[tuple[date, date]]:
    """Split an inclusive date range into chunks of at most ``days`` days."""
    if end < start:
        raise ValueError("end must not precede start")
    if days < 1:
        raise ValueError("days must be >= 1")
    out = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=days - 1), end)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out
