# Migration — moving this pack to another station

A "station" is any machine that runs the pack: your laptop, a staging box, a
customer's server. **The pack itself is identical on every station.** The only
things that differ are `env/<station>.yaml` and the environment (`.env`).

Nothing inside the pack needs editing to move it. If you find yourself editing a
file under `config/`, `agents/`, `prompts/`, `mcp/`, `gateway/`, `orchestrator/`
or `compose.yaml` to make a station work, that is a bug in the pack — the value
you are hardcoding should become a `${VAR}` instead.

---

## The 4-step move

### Step 1 — Package on the source station

```bash
./scripts/pack.sh
```

This refuses to build the archive if it finds a hardcoded IP address, an
absolute host path, or anything key-shaped, and it validates every
`env/*.example.yaml` before writing anything. Fix whatever it reports; do not
reach for `--allow-dirty` unless you have personally confirmed each finding is a
false positive.

It writes `dist/pnp-agents-mcp-<version>-<timestamp>.tar.gz` plus a `.sha256`.
Real station overlays (`env/*.yaml`) and `.env` are deliberately **excluded** —
secrets and station addresses never travel inside the archive.

Copy it across by whatever means you already trust:

```bash
scp dist/pnp-agents-mcp-*.tar.gz  user@target:/path/of/your/choosing/
```

### Step 2 — Unpack and check the target station

```bash
tar -xzf pnp-agents-mcp-*.tar.gz -C ./pnp-agents-mcp
cd ./pnp-agents-mcp
./scripts/doctor.sh --station prod
```

`doctor.sh` reads `manifest.yaml` and verifies this specific host: Python,
Docker and Compose versions, CPU architecture, every variable the manifest
declares required, that each declared port is free, and that the station
configuration loads. It tells you exactly what is missing and how to fix it.

Expect it to fail on the first run — you have not supplied credentials yet.
That is step 3.

### Step 3 — Configure this station (the only editing step)

```bash
cp env/prod.example.yaml env/prod.yaml     # station overlay  (gitignored)
cp .env.example .env                       # secrets and ports (gitignored)
```

Then edit those two files and nothing else:

- `.env` — set `GATEWAY_MASTER_KEY` (generate: `openssl rand -hex 24`) and at
  least one provider credential. Set `STATION=prod`. Adjust published ports and
  `*_BIND_HOST` if this host already uses them.
- `env/prod.yaml` — adjust anything structural: which agents are enabled, which
  model alias each agent uses, the shell allowlist, transport, timeouts.

To use a model gateway that already exists on your network instead of the
bundled one, set `GATEWAY_BASE_URL` in `.env` and deploy with
`--external-gateway`. No file inside the pack changes.

Re-run until clean:

```bash
./scripts/doctor.sh --station prod
```

### Step 4 — Deploy and verify

```bash
./scripts/deploy.sh prod
```

This re-runs `doctor.sh`, brings the stack up, polls every health endpoint with
backoff, and prints an `agent -> model alias -> status` table. On failure it
dumps the last 25 log lines of whichever service did not come up.

Verify by hand:

```bash
curl -s http://127.0.0.1:8080/health  | python3 -m json.tool
curl -s http://127.0.0.1:8080/agents  | python3 -m json.tool
```

Then run one real agent turn:

```bash
curl -s -X POST http://127.0.0.1:8080/run \
  -H 'content-type: application/json' \
  -d '{"agent":"researcher","input":"What is an MCP server, in two sentences?"}' \
  | python3 -m json.tool
```

A `contract_satisfied: true` in the response means the whole chain works:
config → orchestrator → gateway → provider → back through the output contract.

---

## Rollback

Every step is reversible, and nothing in the pack mutates the host outside the
Docker state and the two files you created.

### Roll back a bad deploy (keep the data, previous version still on disk)

```bash
./scripts/deploy.sh prod --down          # stop and remove this stack
```

Then restore the previous pack directory and bring it back up:

```bash
cd ../pnp-agents-mcp-previous
./scripts/deploy.sh prod
```

Keeping the previous extraction next to the new one is the whole rollback plan.
Extract each release into its own directory rather than overwriting in place:

```
/path/of/your/choosing/
  pnp-agents-mcp-1.0.0/
  pnp-agents-mcp-1.1.0/     <- current
  current -> pnp-agents-mcp-1.1.0
```

### Roll back a bad configuration change

`env/<station>.yaml` and `.env` are the only files you edit, so keep a copy
before changing them:

```bash
cp env/prod.yaml env/prod.yaml.bak
cp .env .env.bak
```

To revert:

```bash
mv env/prod.yaml.bak env/prod.yaml
mv .env.bak .env
./scripts/deploy.sh prod --recreate
```

Always confirm a change loads before deploying it:

```bash
python3 config_loader.py --pack . --station prod --dry-run
```

### Roll back a model change

Model choice is per-agent and lives in the environment, so it needs no redeploy
of anything but the orchestrator:

```bash
# in .env
RESEARCHER_MODEL_ALIAS=claude-haiku     # was claude-sonnet
docker compose up -d --force-recreate orchestrator
```

If an alias itself is wrong, repoint it at a different provider model without
touching `litellm_config.yaml`:

```bash
ALIAS_CLAUDE_SONNET_MODEL=anthropic/claude-sonnet-4-5
docker compose up -d --force-recreate gateway
```

### Full teardown

```bash
./scripts/deploy.sh prod --down
docker image rm pnp-agents-mcp/gateway:1.0.0 \
                pnp-agents-mcp/tools-server:1.0.0 \
                pnp-agents-mcp/orchestrator:1.0.0
```

The pack writes nothing outside its own directory and the Docker state, so
removing the directory and those images returns the host to its prior state.
There are no volumes, no databases, and no files written to the host filesystem —
the pack directory is mounted **read-only** into every container.

---

## Verifying a move actually worked

A migration is complete when all five of these pass on the new station:

| Check | Command | Expected |
|---|---|---|
| Config loads | `python3 config_loader.py --pack . --station prod --dry-run` | `OK configuration is valid` |
| Host is ready | `./scripts/doctor.sh --station prod` | `ready` |
| Stack is healthy | `./scripts/deploy.sh prod` | all endpoints healthy |
| Gateway serves | `curl -s localhost:8080/health` | `gateway.status: "ok"` |
| An agent runs | the `/run` call above | `contract_satisfied: true` |

If the first four pass and only the last fails, the problem is a provider
credential or quota, not the migration.
