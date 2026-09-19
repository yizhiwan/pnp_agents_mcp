# Follow-up prompts

Copy-paste prompts for extending this pack. Each one is self-contained: it names
the files involved and the constraints that must hold, so it can be handed to a
fresh session with no other context.

**The rule every prompt inherits:** no hardcoded IPs, absolute paths, API keys,
provider model IDs, or ports. Everything is `${VAR}` or `${VAR:default}`, and
`./scripts/pack.sh --dry-run` must still pass afterwards.

---

## Agents

**Add an agent**
> Add a new agent called `<name>` to the pack. Copy the structure of
> `agents/researcher.yaml` (the annotated template). It should use model alias
> `${<NAME>_MODEL_ALIAS:<alias>}`, have tools `[...]`, and a JSON output
> contract. Write `prompts/<name>.md` under 400 tokens, provider-neutral, ending
> with an explicit JSON schema. Add it to `agents.enabled` in `config/base.yaml`
> and to `provides.capabilities` in `manifest.yaml`. Add a case to
> `tests/test_config_loader.py::TestRealPack` and prove it with
> `python3 config_loader.py --pack . --station local --dry-run`.

**Add agent-to-agent handoff**
> The `handoff.on_success` / `on_failure` fields in `agents/*.yaml` are declared
> and validated but the orchestrator ignores them. Implement them in
> `orchestrator/main.py`: after a successful run, if the agent declares a
> handoff target, feed its output as context to that agent and return the chain.
> Add a `max_chain_depth` limit to `config/base.yaml` under `runtime` to stop
> cycles, and validate the handoff graph is acyclic in
> `config_loader._semantic_checks`.

**Add a per-agent retry and fallback policy**
> `agents/*.yaml` declares `model.fallback_aliases`, and `orchestrator/main.py`
> tries them in order when a call fails. Extend this: add a `retry` block per
> agent (attempts, backoff, which HTTP statuses are retryable), honour
> `runtime.retry` as the default, and record each attempt in the `/run`
> response so a caller can see which alias actually served the turn.

**Make prompts station-overridable**
> Prompt files are currently inlined verbatim by `config_loader._load_agents`.
> Add an optional `prompts_overlay_dir` to `config/base.yaml` so a station can
> shadow a prompt (`prompts.local/researcher.md`) without editing the shipped
> one. Keep the verbatim rule: no `${VAR}` substitution inside prompt bodies,
> because prompts contain JSON examples with braces.

---

## MCP tools

**Add a tool**
> Add a `<name>` tool to `mcp/tools-server/mcp_server.py`. Follow the existing
> pattern exactly: a Pydantic input model with `extra="forbid"`, an
> implementation that raises `ToolFailure` for expected failures, and a
> `@mcp.tool()` wrapper that calls `run_tool(...)` so it can never raise. Read
> any configuration from environment variables in `Settings.from_env` and
> validate it in `Settings.validate`. Then add the tool name to the server's
> `tools:` list in `config/base.yaml`, to the agents allowed to call it, and to
> `provides.capabilities` in `manifest.yaml`.

**Add a second MCP server**
> Create `mcp/<name>-server/` alongside `tools-server`, with its own
> `mcp_server.py`, `requirements.txt` and multi-stage `Dockerfile`. Register it
> under `mcp.servers` in `config/base.yaml` with its own
> `${<NAME>_SERVER_PORT:...}`, add a compose service with a healthcheck, and add
> its port to `manifest.yaml`. Confirm `ToolRouter` in
> `orchestrator/mcp_bridge.py` routes correctly when one agent is bound to two
> servers, including the duplicate-tool-name case.

**Add authentication to the HTTP transport**
> The MCP server's streamable-HTTP endpoint is currently unauthenticated, which
> is fine inside a compose network but not across hosts. Add a bearer token
> check (`${MCP_AUTH_TOKEN:}`, empty meaning disabled) as Starlette middleware,
> leave `/health` unauthenticated so healthchecks still work, and teach
> `orchestrator/mcp_bridge.open_session` to send the header. Add the variable to
> `manifest.yaml` under `conditional_env`.

**Wire up a real search backend**
> `search_web` supports tavily, brave and searxng but ships with
> `SEARCH_PROVIDER=none`. Add `<provider>`: a branch in `_search_web`, the
> required env vars in `Settings`, validation in `Settings.validate`, and the
> credential in `manifest.yaml` under the `search_backend` conditional group.
> Normalize its results to the existing `{title, url, snippet, score, published}`
> shape.

---

## Gateway and models

**Add a model alias**
> Add `<alias>` to `gateway/litellm_config.yaml` pointing at
> `os.environ/ALIAS_<ALIAS>_MODEL`, with `model_info.requires_env` naming its
> credential. Add the default mapping to the `: "${ALIAS_...:=...}"` block in
> `gateway/start.sh`, pass the variable through in `compose.yaml`, and list it
> in `manifest.yaml` under `provides.model_aliases`. Verify an alias without its
> credential is dropped with a warning rather than failing the boot:
> `bash gateway/start.sh --check`.

**Add per-alias rate limits and budgets**
> Use LiteLLM's `router_settings` to add per-alias rpm/tpm limits and a monthly
> budget, all driven by `${VAR:default}`. Surface the effective limits in
> `gateway/start.sh`'s startup summary, and add them to `manifest.yaml` so
> `doctor.sh` can report them.

**Add response caching**
> Enable LiteLLM's cache in `gateway/litellm_config.yaml` behind
> `${CACHE_ENABLED:false}`, with an in-memory default and optional Redis via
> `${REDIS_URL:}`. Add Redis as an optional compose service in its own profile
> so the default topology stays three services. Make sure `gateway/start.sh`
> fails fast if caching is set to redis but no URL is given.

---

## Operations

**Add metrics**
> Add a Prometheus `/metrics` endpoint to `orchestrator/main.py`: counters for
> runs per agent, per alias, contract failures and tool calls; histograms for
> run and tool-call duration. Gate it behind `${METRICS_ENABLED:true}`, add the
> path to `manifest.yaml` under `provides.endpoints`, and keep the orchestrator
> read-only-filesystem compatible.

**Add request tracing**
> Thread a request id from `/run` through the agent loop, the gateway call and
> every MCP tool call, so one turn can be followed across all three services'
> JSON logs. Accept an inbound `X-Request-ID` when present and generate one
> otherwise. The MCP server already emits a per-call `call_id` — connect them.

**Add a smoke-test script**
> Write `scripts/smoke.sh <station>` that runs after `deploy.sh`: call `/health`,
> call `/agents`, then run one real turn per enabled agent with a trivial prompt
> and assert `contract_satisfied: true`. Follow the conventions of the existing
> scripts — `set -euo pipefail`, `--help`, colored output disabled when not a
> TTY, idempotent, exit 0/1/2. Report a per-agent pass/fail table.

**Add CI**
> Add a GitHub Actions workflow that runs `pytest`, `docker compose config`,
> `./scripts/pack.sh --dry-run`, `bash -n` over every script, and builds all
> three images for amd64 and arm64. The portability scan failing must fail the
> build — that is the guarantee the pack is built on.

**Add structured log shipping**
> All three services already emit one JSON object per line. Add an optional
> log-shipping sidecar (Vector or Fluent Bit) in its own compose profile,
> configured entirely by `${VAR:default}`, so the default three-service topology
> is unchanged when it is off.

---

## Configuration and validation

**Add a new station type**
> Create `env/<name>.example.yaml` following the annotated structure of
> `env/prod.example.yaml`. Describe at the top what makes this station type
> different. Keep every value `${VAR:default}` so one overlay can serve several
> hosts of the same type. Add it to the `pack.sh` config-sanity loop
> automatically (it globs `env/*.example.yaml`) and prove it with
> `python3 config_loader.py --pack . --station <name> --dry-run`.

**Add secret-manager support**
> Today secrets come from the environment. Add an optional resolver in
> `config_loader.py` for `${secret:<name>}` values that reads from a pluggable
> backend (file, Vault, AWS Secrets Manager) selected by `${SECRET_BACKEND:env}`.
> Resolution must happen before schema validation, failures must join the same
> aggregated `PackConfigError` report, and resolved values must still be
> redacted by `redact()`.

**Add config diffing between stations**
> Add `--diff <other-station>` to `config_loader.py` that loads two stations and
> prints only the differences, with secrets redacted. This is the fastest way to
> answer "why does prod behave differently from local".

**Generate documentation from the manifest**
> Write a script that renders `manifest.yaml` into a markdown table of every
> required and optional variable, its default and its description, and injects
> it into `README.md` between marker comments. Add a test asserting the README
> table matches the manifest, so they cannot drift.
