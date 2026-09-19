# pnp-agents-mcp

A plug-and-play multi-agent pack. Copy the directory to any Linux host, set two
files, run one script. Everything it needs — the model gateway included — ships
inside the pack.

```
agents  ──▶  orchestrator  ──▶  gateway (LiteLLM)  ──▶  OpenAI / Anthropic /
                    │                                   Gemini / Ollama / OpenRouter
                    └──▶  MCP tools-server (stdio or HTTP)
```

## What it is

Four layers, all generated and all portable:

| Layer | What it does |
|---|---|
| **Gateway** (`gateway/`) | A LiteLLM proxy exposing eight model **aliases** (`gpt-4o`, `claude-sonnet`, `local-llama`, …). Agents name an alias; the gateway maps it to a provider. Swapping models never touches an agent. |
| **MCP server** (`mcp/tools-server/`) | `search_web`, `get_weather`, `run_shell_safe`. One binary serves **both** stdio and streamable HTTP, chosen by `MCP_TRANSPORT`. |
| **Agents** (`agents/`, `prompts/`) | `researcher`, `coder`, `reviewer` — each with its own model alias, tool allowlist, limits, and a JSON output contract. |
| **Orchestration** (`config_loader.py`, `orchestrator/`, `compose.yaml`) | Layered config with fail-fast validation, plus an HTTP front door that runs an agent turn with tool access. |

## The portability rule

**No IP address, absolute path, API key, provider model ID, or port is written
anywhere in the pack.** Every such value is `${VAR}` or `${VAR:default}`.

Only two files differ between stations:

- `env/<station>.yaml` — structure (which agents, which transport, which limits)
- `.env` — secrets and ports

`scripts/pack.sh` enforces this: it scans the whole pack and refuses to build an
archive if a hardcoded value has crept in.

## Quick start (local)

```bash
cp .env.example .env
```

Edit `.env` and set two things:

```bash
GATEWAY_MASTER_KEY=<openssl rand -hex 24>
OPENAI_API_KEY=<your key>          # or ANTHROPIC_API_KEY, GEMINI_API_KEY, …
```

Then:

```bash
python3 -m pip install -r requirements.txt
python3 config_loader.py --pack . --station local --dry-run
./scripts/doctor.sh --station local
./scripts/deploy.sh local
curl -s http://127.0.0.1:8080/agents | python3 -m json.tool
```

Moving to another machine? See **[MIGRATION.md](MIGRATION.md)** — it is four steps.

## Configuration

Three layers, later wins:

```
config/base.yaml  ──▶  env/<station>.yaml  ──▶  os.environ
```

`config_loader.py` merges them, substitutes `${VAR}` / `${VAR:default}`, loads
each agent and inlines its prompt, derives URLs, checks cross-references
(agent → MCP server → tool, agent → gateway alias), and validates the result
against `config/schema.json`.

**It reports every problem at once**, rather than one per run:

```
FAIL 4 problem(s) in /path/of/your/choosing (station: local)

reference (1)
   1. agents.loaded.researcher.model: alias 'gpt-5-turbo' is not declared in the gateway config
      hint: declared aliases: claude-haiku, claude-sonnet, gemini-flash, ...

schema (3)
   1. gateway.api_key: 'k' is too short
      hint: schema rule: minLength = 8
   2. runtime.log_level: 'verbose' is not one of ['debug', 'info', ...]
```

Secrets are redacted in every code path that prints configuration.

### Overriding any single value

Any config path can be overridden from the environment with a `PACK__` variable,
without editing a file:

```bash
PACK__ORCHESTRATOR__PORT=9090 \
PACK__MCP__SERVERS__TOOLS_SERVER__TRANSPORT=http \
  python3 config_loader.py --pack . --station local --dry-run
```

A typo names itself instead of failing obscurely later:

```
PACK__ORCHESTRATOR__PORTT: override targets unknown config key 'orchestrator.PORTT'
      hint: keys available at 'orchestrator': bind_host, cors_origins, gateway_wait_seconds, health_path, port
```

## Adding things

**A new agent** — copy `agents/researcher.yaml` (it is the annotated template),
write `prompts/<name>.md`, add the name to `agents.enabled`. No code changes.

**A new model alias** — add an entry to `gateway/litellm_config.yaml` with a
`model_info.requires_env` listing its credential. Aliases whose credentials are
absent are dropped at gateway boot with a warning rather than failing.

**A new tool** — add a Pydantic input model and a `@mcp.tool()` function in
`mcp/tools-server/mcp_server.py`, then list it in the server's `tools:` in
`config/base.yaml` and in the agents allowed to call it.

See **[prompts.md](prompts.md)** for ready-made prompts covering these and more.

## Transports

| Station type | `MCP_TRANSPORT` | Behaviour |
|---|---|---|
| Local / host-native | `stdio` | The orchestrator spawns the tools server as a subprocess. No port, no container, nothing to start first. |
| Containerized / remote | `http` | The tools server is its own service with a `/health` endpoint and a compose healthcheck. |

Inside compose the orchestrator always uses HTTP (set via `PACK__` overrides in
`compose.yaml`), whichever station overlay is loaded.

## Security posture

- Every container: non-root, `read_only` root filesystem, `cap_drop: ALL`,
  `no-new-privileges`, `/tmp` as tmpfs.
- The pack is bind-mounted **read-only** at `/pack`. Containers cannot modify it.
- `run_shell_safe` uses no shell: an argv list, an allowlist of binaries,
  rejected metacharacters, a hard timeout, and truncated output.
- Ports publish to `127.0.0.1` by default; exposing one is a deliberate
  `*_BIND_HOST` change.
- Secrets reach containers only as environment variables and are redacted in
  logs, `/config`, and `--dry-run` output.

## Development

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests/ -q        # 63 tests
docker compose config              # validate the topology
./scripts/pack.sh --dry-run        # portability scan, no archive
```

## Layout

| Path | Purpose |
|---|---|
| `manifest.yaml` | What the pack needs from a host; `doctor.sh` reads only this |
| `config/base.yaml` | Structural defaults, all values `${VAR:default}` |
| `config/schema.json` | JSON Schema for the merged config |
| `config_loader.py` | Merge, substitute, derive, cross-check, validate |
| `env/*.example.yaml` | Station overlay templates |
| `agents/*.yaml` | One file per agent |
| `prompts/*.md` | Provider-neutral system prompts with JSON contracts |
| `gateway/` | LiteLLM config, image, validating entrypoint |
| `mcp/tools-server/` | Transport-agnostic MCP server and image |
| `orchestrator/` | HTTP API and the agent/tool loop |
| `scripts/` | `pack.sh`, `doctor.sh`, `deploy.sh` |
| `compose.yaml` | Topology, healthchecks, read-only pack mount |
