#!/usr/bin/env python3
# =============================================================================
# Orchestrator — the pack's HTTP front door.
#
# Responsibilities:
#   * load and validate the pack configuration at boot (fail fast, loudly)
#   * expose /health, /agents, /run
#   * run one agent turn: gateway chat completion + MCP tool loop, bounded by
#     the agent's own limits, and enforce the agent's output contract
#
# It holds no model knowledge: an agent names an alias, the gateway resolves it.
# Swapping providers never touches this file.
#
# Configuration comes entirely from config_loader, so PACK_DIR and STATION are
# the only two things this process needs told.
# =============================================================================
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

PACK_DIR = Path(os.environ.get("PACK_DIR", ".")).resolve()
STATION = (os.environ.get("STATION") or "local").strip()

# config_loader.py lives at the pack root, which is mounted read-only at /pack.
if str(PACK_DIR) not in sys.path:
    sys.path.insert(0, str(PACK_DIR))

from config_loader import PackConfigError, load_pack_with_meta, redact  # noqa: E402

from mcp_bridge import McpBridgeError, ToolRouter  # noqa: E402


# =============================================================================
# Logging
# =============================================================================
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _setup_logging(level: str, fmt: str) -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(handler)
    return logging.getLogger("orchestrator")


LOG = logging.getLogger("orchestrator")


# =============================================================================
# State
# =============================================================================
class AppState:
    def __init__(self) -> None:
        self.config: dict[str, Any] = {}
        self.started_at: float = time.time()
        self.gateway_status: str = "unknown"
        self.gateway_checked_at: float = 0.0

    @property
    def gateway(self) -> dict[str, Any]:
        return self.config.get("gateway") or {}

    @property
    def agents(self) -> dict[str, Any]:
        return (self.config.get("agents") or {}).get("loaded") or {}

    @property
    def servers(self) -> dict[str, Any]:
        return (self.config.get("mcp") or {}).get("servers") or {}


STATE = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        config, meta = load_pack_with_meta(PACK_DIR, STATION)
    except PackConfigError as exc:
        # Print the whole report, then refuse to serve. A half-configured
        # orchestrator is worse than one that is plainly down.
        print(str(exc), file=sys.stderr, flush=True)
        raise SystemExit(78)  # EX_CONFIG

    STATE.config = config
    runtime = config.get("runtime") or {}
    _setup_logging(str(runtime.get("log_level", "info")),
                   str(runtime.get("log_format", "json")))
    for warning in meta.warnings:
        LOG.warning("config warning: %s", warning)
    LOG.info(
        "orchestrator ready station=%s agents=%s gateway=%s",
        STATION, ",".join(sorted(STATE.agents)), STATE.gateway.get("base_url"),
    )
    yield
    LOG.info("orchestrator shutting down")


app = FastAPI(
    title="pnp-agents-mcp orchestrator",
    version="1.0.0",
    summary="Runs configured agents against a model gateway with MCP tools.",
    lifespan=lifespan,
)


def _install_cors() -> None:
    raw = (os.environ.get("CORS_ORIGINS") or "").strip()
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["content-type"],
        )


_install_cors()


# =============================================================================
# Gateway client
# =============================================================================
def _gateway_headers() -> dict[str, str]:
    key = str(STATE.gateway.get("api_key") or "")
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers


async def _probe_gateway(force: bool = False) -> str:
    """Cached liveness probe so /health cannot be used to hammer the gateway."""
    now = time.time()
    if not force and now - STATE.gateway_checked_at < 10:
        return STATE.gateway_status
    base = str(STATE.gateway.get("base_url") or "").rstrip("/")
    path = str(STATE.gateway.get("health_path") or "/health/liveliness")
    STATE.gateway_checked_at = now
    if not base:
        STATE.gateway_status = "unconfigured"
        return STATE.gateway_status
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base}{path}")
        STATE.gateway_status = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except httpx.HTTPError as exc:
        STATE.gateway_status = f"unreachable ({type(exc).__name__})"
    return STATE.gateway_status


async def _chat(alias: str, messages: list[dict[str, Any]],
                tools: list[dict[str, Any]], params: dict[str, Any],
                timeout: float) -> dict[str, Any]:
    base = str(STATE.gateway.get("base_url") or "").rstrip("/")
    path = str(STATE.gateway.get("chat_completions_path") or "/v1/chat/completions")
    body: dict[str, Any] = {"model": alias, "messages": messages, **params}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base}{path}", json=body, headers=_gateway_headers())
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "gateway_error",
                "alias": alias,
                "status": resp.status_code,
                "body": resp.text[:2000],
            },
        )
    return resp.json()


# =============================================================================
# Output contract
# =============================================================================
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _enforce_contract(text: str, contract: str) -> tuple[Any, bool, str | None]:
    """Return (payload, satisfied, error). Text contracts always pass."""
    if contract != "json":
        return text, True, None
    candidate = text.strip()
    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate), True, None
    except json.JSONDecodeError as exc:
        return text, False, f"model output is not valid JSON: {exc}"


# =============================================================================
# API models
# =============================================================================
class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str = Field(min_length=1, description="Name of an enabled agent.")
    input: str = Field(min_length=1, max_length=100_000,
                       description="The task for the agent.")
    context: dict[str, Any] | None = Field(
        default=None, description="Optional extra context; sent as a second user turn."
    )
    max_iterations: int | None = Field(
        default=None, ge=1, le=50,
        description="Override the agent's configured iteration cap, downward only."
    )


class ToolCallRecord(BaseModel):
    iteration: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    result_preview: str


class RunResponse(BaseModel):
    agent: str
    alias_used: str
    station: str
    iterations: int
    contract: Literal["json", "text"]
    contract_satisfied: bool
    contract_error: str | None
    output: Any
    raw_output: str
    tool_calls: list[ToolCallRecord]
    duration_ms: float


# =============================================================================
# Routes
# =============================================================================
@app.get("/health", summary="Liveness plus wiring status")
async def health() -> dict[str, Any]:
    gateway_status = await _probe_gateway()
    return {
        "status": "ok",
        "station": STATION,
        "uptime_seconds": round(time.time() - STATE.started_at, 1),
        "gateway": {
            "base_url": STATE.gateway.get("base_url"),
            "profile": STATE.gateway.get("profile"),
            "status": gateway_status,
        },
        "mcp_servers": {
            name: {
                "transport": server.get("transport"),
                "target": server.get("url") or " ".join(server.get("command") or []),
                "enabled": bool(server.get("enabled", True)),
            }
            for name, server in STATE.servers.items()
        },
        "agents": sorted(STATE.agents),
    }


@app.get("/agents", summary="Enabled agents and their resolved wiring")
async def agents() -> dict[str, Any]:
    return {
        "station": STATION,
        "agents": [
            {
                "name": name,
                "role": agent.get("role"),
                "description": agent.get("description"),
                "alias": (agent.get("model") or {}).get("alias"),
                "fallback_aliases": (agent.get("model") or {}).get("fallback_aliases") or [],
                "params": (agent.get("model") or {}).get("params") or {},
                "tools": agent.get("tools") or [],
                "mcp_servers": agent.get("mcp_servers") or [],
                "limits": agent.get("limits") or {},
                "output_contract": agent.get("output_contract"),
                "handoff": agent.get("handoff") or {},
            }
            for name, agent in sorted(STATE.agents.items())
        ],
    }


@app.get("/config", summary="The effective configuration, secrets redacted")
async def config() -> dict[str, Any]:
    safe = redact(STATE.config)
    # Prompts are long and already reported by length in /agents.
    for agent in (safe.get("agents") or {}).get("loaded", {}).values():
        if isinstance(agent, dict) and "system_prompt" in agent:
            agent["system_prompt"] = f"<{len(agent['system_prompt'])} chars>"
    return safe


@app.post("/run", response_model=RunResponse, summary="Execute one agent turn")
async def run(request: RunRequest) -> RunResponse:
    agent = STATE.agents.get(request.agent)
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_agent", "agent": request.agent,
                    "available": sorted(STATE.agents)},
        )

    started = time.monotonic()
    model = agent.get("model") or {}
    limits = agent.get("limits") or {}
    params = dict(model.get("params") or {})
    contract = str(agent.get("output_contract") or "text")

    configured_max = int(limits.get("max_iterations") or 6)
    max_iterations = min(request.max_iterations or configured_max, configured_max)
    max_tool_calls = int(limits.get("max_tool_calls", 0) or 0)
    timeout = float(limits.get("timeout_seconds")
                    or STATE.gateway.get("timeout_seconds") or 120)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent.get("system_prompt") or ""},
        {"role": "user", "content": request.input},
    ]
    if request.context:
        messages.append({
            "role": "user",
            "content": "Additional context (JSON):\n"
                       + json.dumps(request.context, default=str),
        })

    aliases = [model.get("alias"), *(model.get("fallback_aliases") or [])]
    aliases = [a for a in aliases if a]
    if not aliases:
        raise HTTPException(status_code=500,
                            detail={"error": "no_alias_configured",
                                    "agent": request.agent})

    records: list[ToolCallRecord] = []
    final_text = ""
    iterations = 0
    alias_used = aliases[0]

    async with AsyncExitStack() as stack:
        router: ToolRouter | None = None
        wanted_tools = agent.get("tools") or []
        if wanted_tools:
            try:
                router = await ToolRouter.build(
                    stack,
                    list(agent.get("mcp_servers") or []),
                    STATE.servers,
                    PACK_DIR,
                    wanted_tools,
                )
            except McpBridgeError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"error": "mcp_unavailable", "agent": request.agent,
                            "detail": str(exc)},
                ) from exc

        tool_schemas = router.schemas if router else []

        for iteration in range(1, max_iterations + 1):
            iterations = iteration

            payload: dict[str, Any] | None = None
            last_error: HTTPException | None = None
            for alias in aliases:
                try:
                    payload = await _chat(alias, messages, tool_schemas, params, timeout)
                    alias_used = alias
                    break
                except HTTPException as exc:
                    LOG.warning("alias failed alias=%s agent=%s detail=%s",
                                alias, request.agent, exc.detail)
                    last_error = exc
            if payload is None:
                raise last_error or HTTPException(
                    status_code=502, detail={"error": "all_aliases_failed",
                                             "aliases": aliases})

            choices = payload.get("choices") or []
            if not choices:
                raise HTTPException(status_code=502,
                                    detail={"error": "empty_gateway_response",
                                            "alias": alias_used})
            message = choices[0].get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                final_text = message.get("content") or ""
                break

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })

            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                if max_tool_calls and len(records) >= max_tool_calls:
                    result = json.dumps({
                        "ok": False, "data": None,
                        "error": {"code": "budget_exhausted",
                                  "message": f"tool-call budget of {max_tool_calls} "
                                             f"is exhausted; answer with what you have",
                                  "details": None},
                    })
                else:
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("arguments must be a JSON object")
                    except (json.JSONDecodeError, ValueError) as exc:
                        result = json.dumps({
                            "ok": False, "data": None,
                            "error": {"code": "bad_arguments",
                                      "message": f"could not parse arguments: {exc}",
                                      "details": None},
                        })
                        arguments = {}
                    else:
                        result = (
                            await router.call(name, arguments) if router
                            else json.dumps({
                                "ok": False, "data": None,
                                "error": {"code": "no_tools",
                                          "message": "this agent has no tools",
                                          "details": None},
                            })
                        )

                parsed_ok = True
                try:
                    parsed_ok = bool(json.loads(result).get("ok", True))
                except (json.JSONDecodeError, AttributeError):
                    parsed_ok = True
                records.append(ToolCallRecord(
                    iteration=iteration,
                    tool=name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    ok=parsed_ok,
                    result_preview=result[:500],
                ))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": result,
                })
        else:
            # Loop exhausted without a tool-free answer.
            final_text = final_text or ""

    payload_out, satisfied, contract_error = _enforce_contract(final_text, contract)
    if not final_text:
        satisfied = False
        contract_error = (contract_error
                          or f"agent produced no final answer within "
                             f"{max_iterations} iteration(s)")

    duration = round((time.monotonic() - started) * 1000, 2)
    LOG.info("run complete agent=%s alias=%s iterations=%d tools=%d ok=%s ms=%.1f",
             request.agent, alias_used, iterations, len(records), satisfied, duration)

    return RunResponse(
        agent=request.agent,
        alias_used=alias_used,
        station=STATION,
        iterations=iterations,
        contract=contract if contract in ("json", "text") else "text",
        contract_satisfied=satisfied,
        contract_error=contract_error,
        output=payload_out,
        raw_output=final_text,
        tool_calls=records,
        duration_ms=duration,
    )


def main() -> int:
    import uvicorn

    orch = (STATE.config.get("orchestrator") or {}) if STATE.config else {}
    host = os.environ.get("ORCHESTRATOR_BIND_ADDR") or orch.get("bind_host") or "0.0.0.0"
    port = int(os.environ.get("ORCHESTRATOR_PORT") or orch.get("port") or 8080)
    uvicorn.run(app, host=str(host), port=port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
