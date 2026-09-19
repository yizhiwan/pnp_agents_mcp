#!/usr/bin/env python3
# =============================================================================
# MCP tools server — transport-agnostic.
#
# One binary, two transports, selected by MCP_TRANSPORT:
#   stdio : the parent process spawns this as a subprocess and speaks MCP over
#           stdin/stdout. Logs go to STDERR only — anything on stdout would
#           corrupt the protocol framing.
#   http  : streamable HTTP on MCP_BIND_HOST:MCP_PORT under MCP_MOUNT_PATH,
#           plus an unauthenticated GET /health for container healthchecks.
#           Logs go to stdout.
#
# Tools: search_web, get_weather, run_shell_safe
#
# Contract every tool obeys:
#   * inputs validated by a Pydantic model with extra="forbid"
#   * a tool NEVER raises; it returns {"ok": false, "error": {...}} instead, so
#     one bad call cannot take down the server or the agent loop
#   * output shape is always {"ok": bool, "data": ..., "error": ...}
#
# Portability: every value comes from the environment. No IPs (other than the
# universal bind-all default), no absolute paths, no keys, no ports in code.
# =============================================================================
from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - dependency guard
    print(
        json.dumps(
            {
                "level": "critical",
                "event": "import_failed",
                "message": "the 'mcp' package is required: pip install 'mcp[cli]'",
                "detail": str(exc),
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(78)  # EX_CONFIG


# =============================================================================
# Configuration
# =============================================================================
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in _env(name, default).split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    server_name: str
    transport: str
    bind_host: str
    port: int
    mount_path: str
    log_level: str
    log_format: str
    http_timeout: float
    search_provider: str
    search_api_key: str
    search_base_url: str
    search_max_results: int
    weather_base_url: str
    geocode_base_url: str
    shell_enabled: bool
    shell_allowlist: tuple[str, ...]
    shell_timeout: float
    shell_max_output: int
    shell_workdir: str

    @classmethod
    def from_env(cls) -> "Settings":
        transport = _env("MCP_TRANSPORT", "stdio").lower()
        if transport in {"streamable-http", "streamable_http"}:
            transport = "http"
        mount = _env("MCP_MOUNT_PATH", "/mcp")
        if not mount.startswith("/"):
            mount = "/" + mount
        return cls(
            server_name=_env("MCP_SERVER_NAME", "tools-server"),
            transport=transport,
            bind_host=_env("MCP_BIND_HOST", "0.0.0.0"),
            port=_env_int("MCP_PORT", 8081),
            mount_path=mount,
            log_level=_env("LOG_LEVEL", "info").lower(),
            log_format=_env("LOG_FORMAT", "json").lower(),
            http_timeout=float(_env_int("HTTP_TIMEOUT_SECONDS", 20)),
            search_provider=_env("SEARCH_PROVIDER", "none").lower(),
            search_api_key=_env("SEARCH_API_KEY"),
            search_base_url=_env("SEARCH_BASE_URL"),
            search_max_results=_env_int("SEARCH_MAX_RESULTS", 5),
            weather_base_url=_env(
                "WEATHER_BASE_URL", "https://api.open-meteo.com/v1/forecast"
            ),
            geocode_base_url=_env(
                "GEOCODE_BASE_URL", "https://geocoding-api.open-meteo.com/v1/search"
            ),
            shell_enabled=_env_bool("SHELL_ENABLED", True),
            shell_allowlist=tuple(
                _env_list("SHELL_ALLOWLIST", "echo,ls,cat,pwd,whoami,date,uname")
            ),
            shell_timeout=float(_env_int("SHELL_TIMEOUT_SECONDS", 15)),
            shell_max_output=_env_int("SHELL_MAX_OUTPUT_BYTES", 65536),
            shell_workdir=_env("SHELL_WORKDIR", "."),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.transport not in {"stdio", "http"}:
            problems.append(
                f"MCP_TRANSPORT must be 'stdio' or 'http', got {self.transport!r}"
            )
        if not 1 <= self.port <= 65535:
            problems.append(f"MCP_PORT must be 1-65535, got {self.port}")
        if self.log_format not in {"json", "text"}:
            problems.append(
                f"LOG_FORMAT must be 'json' or 'text', got {self.log_format!r}"
            )
        if self.log_level not in {"debug", "info", "warning", "error", "critical"}:
            problems.append(f"LOG_LEVEL is not a known level: {self.log_level!r}")
        if self.search_provider not in {"tavily", "brave", "searxng", "none"}:
            problems.append(
                "SEARCH_PROVIDER must be tavily|brave|searxng|none, got "
                f"{self.search_provider!r}"
            )
        if self.search_provider in {"tavily", "brave"} and not self.search_api_key:
            problems.append(
                f"SEARCH_PROVIDER={self.search_provider} requires SEARCH_API_KEY"
            )
        if self.search_provider == "searxng" and not self.search_base_url:
            problems.append("SEARCH_PROVIDER=searxng requires SEARCH_BASE_URL")
        return problems


SETTINGS = Settings.from_env()


# =============================================================================
# Structured logging
# =============================================================================
_SECRET_HINTS = ("key", "token", "secret", "password", "authorization", "credential")


def _redact(value: Any, key: str = "") -> Any:
    if any(hint in key.lower() for hint in _SECRET_HINTS):
        return "***redacted***"
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(_redact(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _build_logger(settings: Settings) -> logging.Logger:
    # stdio transport owns stdout for protocol framing; logs must not touch it.
    stream = sys.stderr if settings.transport != "http" else sys.stdout
    handler = logging.StreamHandler(stream)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
        )
    logger = logging.getLogger(settings.server_name)
    logger.handlers.clear()
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


LOG = _build_logger(SETTINGS)


def log_event(level: str, event: str, message: str = "", **fields: Any) -> None:
    LOG.log(
        getattr(logging, level.upper(), logging.INFO),
        message or event,
        extra={"event": event, "fields": fields},
    )


# =============================================================================
# Uniform tool envelope
# =============================================================================
ToolResult = dict[str, Any]


def ok(data: Any, **meta: Any) -> ToolResult:
    return {"ok": True, "data": data, "error": None, "meta": meta}


def err(code: str, message: str, **details: Any) -> ToolResult:
    return {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message, "details": details or None},
        "meta": {},
    }


class ToolFailure(Exception):
    """Raised inside a tool implementation to produce a clean error envelope."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def run_tool(
    name: str,
    model: type[BaseModel],
    impl: Callable[[Any], Any],
    **raw: Any,
) -> ToolResult:
    """Validate, execute, and guarantee a structured result.

    Nothing escapes this function: a validation failure, a network error, a
    timeout, or an unexpected bug all become {"ok": false, ...}. The MCP session
    stays healthy and the calling agent gets something it can reason about.
    """
    call_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    try:
        args = model(**raw)
    except ValidationError as exc:
        log_event("warning", "tool_invalid_input", tool=name, call_id=call_id,
                  errors=exc.error_count())
        return err(
            "invalid_input",
            f"{name}: input failed validation",
            call_id=call_id,
            violations=[
                {
                    "field": ".".join(str(p) for p in e["loc"]) or "<root>",
                    "problem": e["msg"],
                }
                for e in exc.errors()
            ],
        )

    log_event("info", "tool_start", tool=name, call_id=call_id,
              args=_redact(args.model_dump()))
    try:
        data = impl(args)
    except ToolFailure as exc:
        log_event("warning", "tool_failed", tool=name, call_id=call_id,
                  code=exc.code, reason=exc.message)
        return err(exc.code, exc.message, call_id=call_id, **exc.details)
    except httpx.TimeoutException as exc:
        log_event("warning", "tool_timeout", tool=name, call_id=call_id)
        return err("upstream_timeout", f"{name}: upstream request timed out",
                   call_id=call_id, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        log_event("warning", "tool_http_error", tool=name, call_id=call_id,
                  status=exc.response.status_code)
        return err("upstream_error",
                   f"{name}: upstream returned HTTP {exc.response.status_code}",
                   call_id=call_id, status=exc.response.status_code)
    except httpx.HTTPError as exc:
        log_event("warning", "tool_network_error", tool=name, call_id=call_id)
        return err("network_error", f"{name}: could not reach upstream",
                   call_id=call_id, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all
        LOG.exception("tool_crashed", extra={"event": "tool_crashed",
                                             "fields": {"tool": name,
                                                        "call_id": call_id}})
        return err("internal_error", f"{name}: unexpected failure",
                   call_id=call_id, detail=f"{type(exc).__name__}: {exc}")
    finally:
        log_event("debug", "tool_end", tool=name, call_id=call_id,
                  duration_ms=round((time.monotonic() - started) * 1000, 2))

    return ok(data, call_id=call_id,
              duration_ms=round((time.monotonic() - started) * 1000, 2))


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


# =============================================================================
# Input models
# =============================================================================
class SearchWebInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=2, max_length=512,
                       description="Search query text.")
    max_results: int = Field(default=5, ge=1, le=25,
                             description="Maximum number of results to return.")
    recency_days: int | None = Field(default=None, ge=1, le=3650,
                                     description="Restrict to the last N days.")


class GetWeatherInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    location: str = Field(min_length=2, max_length=128,
                          description="City or place name, e.g. 'Kuala Lumpur'.")
    units: Literal["metric", "imperial"] = Field(default="metric")
    forecast_days: int = Field(default=3, ge=1, le=7,
                               description="Days of daily forecast to include.")


class RunShellSafeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    command: str = Field(min_length=1, max_length=4096,
                         description="Command line; the binary must be allowlisted.")
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)


# =============================================================================
# Tool implementations
# =============================================================================
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog", 51: "light drizzle",
    53: "moderate drizzle", 55: "dense drizzle", 56: "light freezing drizzle",
    57: "dense freezing drizzle", 61: "slight rain", 63: "moderate rain",
    65: "heavy rain", 66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow",
    77: "snow grains", 80: "slight rain showers", 81: "moderate rain showers",
    82: "violent rain showers", 85: "slight snow showers",
    86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=SETTINGS.http_timeout,
        follow_redirects=True,
        headers={"User-Agent": f"pnp-agents-mcp/{SETTINGS.server_name}"},
    )


def _search_web(args: SearchWebInput) -> dict[str, Any]:
    provider = SETTINGS.search_provider
    limit = min(args.max_results, SETTINGS.search_max_results or args.max_results)

    if provider == "none":
        raise ToolFailure(
            "not_configured",
            "web search is not configured on this station",
            remedy="set SEARCH_PROVIDER to tavily, brave or searxng and supply "
                   "SEARCH_API_KEY (or SEARCH_BASE_URL for searxng)",
        )

    results: list[dict[str, Any]] = []
    with _client() as client:
        if provider == "tavily":
            base = SETTINGS.search_base_url or "https://api.tavily.com"
            resp = client.post(
                f"{base.rstrip('/')}/search",
                json={
                    "api_key": SETTINGS.search_api_key,
                    "query": args.query,
                    "max_results": limit,
                    "search_depth": "basic",
                    **({"days": args.recency_days} if args.recency_days else {}),
                },
            )
            resp.raise_for_status()
            for item in (resp.json().get("results") or [])[:limit]:
                results.append({
                    "title": item.get("title") or "",
                    "url": item.get("url") or "",
                    "snippet": item.get("content") or "",
                    "score": item.get("score"),
                    "published": item.get("published_date"),
                })

        elif provider == "brave":
            base = SETTINGS.search_base_url or "https://api.search.brave.com"
            params: dict[str, Any] = {"q": args.query, "count": limit}
            if args.recency_days:
                params["freshness"] = f"pd{args.recency_days}"
            resp = client.get(
                f"{base.rstrip('/')}/res/v1/web/search",
                params=params,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": SETTINGS.search_api_key,
                },
            )
            resp.raise_for_status()
            for item in ((resp.json().get("web") or {}).get("results") or [])[:limit]:
                results.append({
                    "title": item.get("title") or "",
                    "url": item.get("url") or "",
                    "snippet": item.get("description") or "",
                    "score": None,
                    "published": item.get("age"),
                })

        elif provider == "searxng":
            resp = client.get(
                f"{SETTINGS.search_base_url.rstrip('/')}/search",
                params={"q": args.query, "format": "json"},
            )
            resp.raise_for_status()
            for item in (resp.json().get("results") or [])[:limit]:
                results.append({
                    "title": item.get("title") or "",
                    "url": item.get("url") or "",
                    "snippet": item.get("content") or "",
                    "score": item.get("score"),
                    "published": item.get("publishedDate"),
                })

    if not results:
        raise ToolFailure("no_results", f"no results for query: {args.query}",
                          provider=provider)

    return {"provider": provider, "query": args.query,
            "result_count": len(results), "results": results}


def _get_weather(args: GetWeatherInput) -> dict[str, Any]:
    with _client() as client:
        geo = client.get(
            SETTINGS.geocode_base_url,
            params={"name": args.location, "count": 1, "format": "json"},
        )
        geo.raise_for_status()
        matches = geo.json().get("results") or []
        if not matches:
            raise ToolFailure("location_not_found",
                              f"could not geocode location: {args.location}")
        place = matches[0]

        metric = args.units == "metric"
        forecast = client.get(
            SETTINGS.weather_base_url,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "forecast_days": args.forecast_days,
                "timezone": "auto",
                "temperature_unit": "celsius" if metric else "fahrenheit",
                "wind_speed_unit": "kmh" if metric else "mph",
            },
        )
        forecast.raise_for_status()
        body = forecast.json()

    current = body.get("current") or {}
    daily = body.get("daily") or {}
    days = []
    for i, date in enumerate(daily.get("time") or []):
        code = (daily.get("weather_code") or [None])[i] if daily.get("weather_code") else None
        days.append({
            "date": date,
            "temp_max": (daily.get("temperature_2m_max") or [None])[i],
            "temp_min": (daily.get("temperature_2m_min") or [None])[i],
            "precipitation_probability_max":
                (daily.get("precipitation_probability_max") or [None])[i],
            "conditions": _WMO.get(code, "unknown") if code is not None else None,
        })

    code_now = current.get("weather_code")
    return {
        "location": {
            "name": place.get("name"),
            "country": place.get("country"),
            "admin1": place.get("admin1"),
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "timezone": body.get("timezone"),
        },
        "units": {
            "temperature": "C" if args.units == "metric" else "F",
            "wind_speed": "km/h" if args.units == "metric" else "mph",
        },
        "current": {
            "observed_at": current.get("time"),
            "temperature": current.get("temperature_2m"),
            "relative_humidity": current.get("relative_humidity_2m"),
            "wind_speed": current.get("wind_speed_10m"),
            "conditions": _WMO.get(code_now, "unknown") if code_now is not None else None,
        },
        "forecast": days,
    }


_SHELL_METACHARS = set(";|&$`><\n\r*?!{}[]()~\\")


def _run_shell_safe(args: RunShellSafeInput) -> dict[str, Any]:
    if not SETTINGS.shell_enabled:
        raise ToolFailure("disabled", "run_shell_safe is disabled on this station",
                          remedy="set SHELL_ENABLED=true to enable it")
    if not SETTINGS.shell_allowlist:
        raise ToolFailure("disabled", "SHELL_ALLOWLIST is empty, so no command "
                                      "may run",
                          remedy="set SHELL_ALLOWLIST to a comma-separated list")

    # Reject shell metacharacters outright. Nothing is passed to a shell, so
    # these could not be interpreted anyway; refusing them makes the intent
    # explicit and stops callers from assuming pipelines work.
    offending = sorted(set(args.command) & _SHELL_METACHARS)
    if offending:
        raise ToolFailure(
            "unsupported_syntax",
            "shell metacharacters are not supported; commands run without a shell",
            characters=offending,
        )

    try:
        argv = shlex.split(args.command)
    except ValueError as exc:
        raise ToolFailure("unparseable_command", f"could not parse command: {exc}")
    if not argv:
        raise ToolFailure("empty_command", "command is empty after parsing")

    binary = os.path.basename(argv[0])
    if binary != argv[0]:
        raise ToolFailure("path_not_allowed",
                          "name the binary directly; paths are not accepted",
                          received=argv[0])
    if binary not in SETTINGS.shell_allowlist:
        raise ToolFailure("not_allowlisted",
                          f"'{binary}' is not in the allowlist",
                          allowed=list(SETTINGS.shell_allowlist))

    resolved = shutil.which(binary)
    if not resolved:
        raise ToolFailure("not_found",
                          f"'{binary}' is allowlisted but not installed here")

    timeout = args.timeout_seconds or SETTINGS.shell_timeout
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, shell=False, allowlisted
            [resolved, *argv[1:]],
            shell=False,
            # The child must NEVER inherit our stdin: under stdio transport that
            # handle is the MCP protocol channel, and a child holding or reading
            # it would corrupt the stream or hang the call.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=SETTINGS.shell_workdir,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C",
                 "HOME": os.environ.get("HOME", "/tmp")},
        )
    except subprocess.TimeoutExpired:
        raise ToolFailure("timeout", f"command exceeded {timeout}s",
                          timeout_seconds=timeout)

    stdout, out_trunc = _truncate(proc.stdout or "", SETTINGS.shell_max_output)
    stderr, err_trunc = _truncate(proc.stderr or "", SETTINGS.shell_max_output)
    return {
        "binary": binary,
        "argv": argv,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": {"stdout": out_trunc, "stderr": err_trunc},
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }


# =============================================================================
# Server wiring
# =============================================================================
def build_server(settings: Settings) -> FastMCP:
    instructions = (
        "Utility tools for agents. Every tool returns an envelope "
        "{ok, data, error, meta}: check `ok` before reading `data`, and read "
        "`error.code` plus `error.details` when it is false. Tools never raise."
    )
    kwargs: dict[str, Any] = {
        "name": settings.server_name,
        "instructions": instructions,
    }
    if settings.transport == "http":
        kwargs.update(
            host=settings.bind_host,
            port=settings.port,
            streamable_http_path=settings.mount_path,
        )
    try:
        mcp = FastMCP(**kwargs)
    except TypeError:
        # Older FastMCP signatures take only name/instructions; apply the HTTP
        # options to the settings object instead.
        mcp = FastMCP(name=settings.server_name, instructions=instructions)
        if settings.transport == "http":
            for attr, value in (
                ("host", settings.bind_host),
                ("port", settings.port),
                ("streamable_http_path", settings.mount_path),
            ):
                if hasattr(mcp.settings, attr):
                    setattr(mcp.settings, attr, value)

    @mcp.tool(
        name="search_web",
        description=(
            "Search the web through the station's configured backend "
            "(tavily, brave or searxng). Returns ranked results with title, "
            "url and snippet. Returns ok=false with code 'not_configured' when "
            "no backend is set up."
        ),
    )
    def search_web(  # noqa: D401 - schema comes from the signature
        query: str,
        max_results: int = 5,
        recency_days: int | None = None,
    ) -> ToolResult:
        return run_tool("search_web", SearchWebInput, _search_web,
                        query=query, max_results=max_results,
                        recency_days=recency_days)

    @mcp.tool(
        name="get_weather",
        description=(
            "Current conditions and a short daily forecast for a named place. "
            "Geocodes the name first; returns ok=false with code "
            "'location_not_found' if the place cannot be resolved."
        ),
    )
    def get_weather(
        location: str,
        units: Literal["metric", "imperial"] = "metric",
        forecast_days: int = 3,
    ) -> ToolResult:
        return run_tool("get_weather", GetWeatherInput, _get_weather,
                        location=location, units=units,
                        forecast_days=forecast_days)

    @mcp.tool(
        name="run_shell_safe",
        description=(
            "Run one allowlisted binary with arguments. No shell is involved: "
            "pipes, redirection, globs and command substitution are rejected. "
            "Output is truncated and the call is hard-timed."
        ),
    )
    def run_shell_safe(
        command: str,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        return run_tool("run_shell_safe", RunShellSafeInput, _run_shell_safe,
                        command=command, timeout_seconds=timeout_seconds)

    # --- /health (http transport only) ---------------------------------------
    if settings.transport == "http" and hasattr(mcp, "custom_route"):
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        @mcp.custom_route("/health", methods=["GET"])
        async def health(_request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "status": "ok",
                    "server": settings.server_name,
                    "transport": settings.transport,
                    "mount_path": settings.mount_path,
                    "tools": ["search_web", "get_weather", "run_shell_safe"],
                    "search_provider": settings.search_provider,
                    "shell_enabled": settings.shell_enabled,
                }
            )
    elif settings.transport == "http":  # pragma: no cover - old FastMCP
        log_event("warning", "health_route_unavailable",
                  "installed FastMCP has no custom_route(); /health is disabled")

    return mcp


def main() -> int:
    problems = SETTINGS.validate()
    if problems:
        log_event("critical", "invalid_configuration",
                  f"{len(problems)} configuration problem(s)", problems=problems)
        for i, problem in enumerate(problems, 1):
            print(f"  {i}) {problem}", file=sys.stderr)
        return 78  # EX_CONFIG

    server = build_server(SETTINGS)
    log_event(
        "info", "server_start", f"{SETTINGS.server_name} starting",
        transport=SETTINGS.transport,
        bind=f"{SETTINGS.bind_host}:{SETTINGS.port}" if SETTINGS.transport == "http" else None,
        mount_path=SETTINGS.mount_path if SETTINGS.transport == "http" else None,
        search_provider=SETTINGS.search_provider,
        shell_enabled=SETTINGS.shell_enabled,
        shell_allowlist=list(SETTINGS.shell_allowlist),
    )

    try:
        if SETTINGS.transport == "stdio":
            server.run(transport="stdio")
        else:
            server.run(transport="streamable-http")
    except KeyboardInterrupt:
        log_event("info", "server_stop", "interrupted")
        return 0
    except Exception:  # noqa: BLE001 - log then exit non-zero, never traceback-dump
        LOG.exception("server_crashed", extra={"event": "server_crashed",
                                               "fields": {}})
        return 70  # EX_SOFTWARE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
