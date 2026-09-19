#!/usr/bin/env bash
# =============================================================================
# deploy.sh <station> — bring the pack up on this host and prove it is working.
#
#   1. doctor.sh          refuse to deploy onto a host that cannot run this
#   2. docker compose up  build and start, gateway included unless external
#   3. health poll        wait for every declared endpoint, with backoff
#   4. report             agent -> model alias -> status table
#
# Idempotent: re-running reconciles the existing stack rather than duplicating
# it, and a re-run after a partial failure is safe.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PACK_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# --- output ------------------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'
  C_DIM=$'\033[2m';  C_BLD=$'\033[1m';  C_OFF=$'\033[0m'
else
  C_RED=''; C_YEL=''; C_GRN=''; C_DIM=''; C_BLD=''; C_OFF=''
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s  ok  %s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%s warn %s %s\n' "$C_YEL" "$C_OFF" "$*"; }
bad()  { printf '%s fail %s %s\n' "$C_RED" "$C_OFF" "$*"; }
note() { printf '       %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
hdr()  { printf '\n%s%s%s\n' "$C_BLD" "$*" "$C_OFF"; }

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh <station> [options]

Brings the pack up on this host and waits until every service is healthy.

Arguments:
  <station>          Station overlay to deploy (env/<station>.yaml).

Options:
  --no-build         Start without rebuilding images.
  --recreate         Force containers to be recreated.
  --external-gateway Do not start the bundled gateway; requires GATEWAY_BASE_URL.
  --timeout SECONDS  How long to wait for health.              [180]
  --skip-doctor      Do not run doctor.sh first (not recommended).
  --down             Stop and remove this station's stack, then exit.
  --logs             Tail service logs after a successful deploy.
  --no-color         Disable colored output.
  -h, --help         Show this help.

Environment: anything compose reads, notably GATEWAY_MASTER_KEY and the
provider credentials. Put them in .env (see .env.example).

Exit codes: 0 deployed and healthy, 1 deploy or health check failed,
            2 usage or environment error.
EOF
}

STATION=""
DO_BUILD=1; RECREATE=0; EXTERNAL_GATEWAY=0; TIMEOUT=180
SKIP_DOCTOR=0; DO_DOWN=0; TAIL_LOGS=0
while (($#)); do
  case "$1" in
    --no-build)         DO_BUILD=0 ;;
    --recreate)         RECREATE=1 ;;
    --external-gateway) EXTERNAL_GATEWAY=1 ;;
    --timeout)          TIMEOUT="${2:?--timeout needs seconds}"; shift ;;
    --skip-doctor)      SKIP_DOCTOR=1 ;;
    --down)             DO_DOWN=1 ;;
    --logs)             TAIL_LOGS=1 ;;
    --no-color)         C_RED=''; C_YEL=''; C_GRN=''; C_DIM=''; C_BLD=''; C_OFF='' ;;
    -h|--help)          usage; exit 0 ;;
    -*)                 bad "unknown option: $1"; usage; exit 2 ;;
    *)                  if [[ -z "$STATION" ]]; then STATION="$1"
                        else bad "unexpected argument: $1"; exit 2; fi ;;
  esac
  shift
done

if [[ -z "$STATION" ]]; then
  STATION="${STATION_DEFAULT:-${STATION:-}}"
fi
if [[ -z "$STATION" ]]; then
  bad "a station argument is required"
  usage
  exit 2
fi

cd "$PACK_ROOT"

command -v docker >/dev/null 2>&1 || { bad "docker not found on PATH"; exit 2; }
docker compose version >/dev/null 2>&1 || { bad "'docker compose' v2 required"; exit 2; }

PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PYTHON_BIN="$candidate"; break; fi
done
[[ -n "$PYTHON_BIN" ]] || { bad "no python interpreter on PATH"; exit 2; }

export STATION
# The bundled gateway lives behind a compose profile so an external gateway
# needs no file edit.
if (( EXTERNAL_GATEWAY )); then
  export COMPOSE_PROFILES=""
  if [[ -z "${GATEWAY_BASE_URL:-}" ]]; then
    bad "--external-gateway needs GATEWAY_BASE_URL to be set"
    exit 2
  fi
else
  export COMPOSE_PROFILES="${COMPOSE_PROFILES:-bundled}"
fi

say "${C_BLD}deploy${C_OFF} station '${STATION}' on $(uname -s) $(uname -m)"
say "${C_DIM}pack: ${PACK_ROOT}${C_OFF}"
say "${C_DIM}profiles: ${COMPOSE_PROFILES:-<none>}${C_OFF}"

# --- --down shortcut ---------------------------------------------------------
if (( DO_DOWN )); then
  hdr "stopping"
  docker compose down --remove-orphans
  ok "stack stopped and removed"
  exit 0
fi

# --- 1. doctor ---------------------------------------------------------------
if (( SKIP_DOCTOR )); then
  warn "doctor.sh skipped (--skip-doctor)"
else
  hdr "1. preflight"
  if bash "${SCRIPT_DIR}/doctor.sh" --station "$STATION"; then
    ok "preflight passed"
  else
    bad "preflight failed; not deploying"
    note "re-run ./scripts/doctor.sh --station ${STATION} for detail"
    exit 1
  fi
fi

# --- 2. bring the stack up ---------------------------------------------------
hdr "2. starting services"
UP_ARGS=(up --detach --remove-orphans)
(( DO_BUILD )) && UP_ARGS+=(--build)
(( RECREATE )) && UP_ARGS+=(--force-recreate)

if ! docker compose "${UP_ARGS[@]}"; then
  bad "docker compose up failed"
  note "inspect with: docker compose logs --tail 100"
  exit 1
fi
ok "containers started"

# --- 3. wait for health ------------------------------------------------------
hdr "3. waiting for health"

# Resolve the published host ports the same way compose did.
GATEWAY_PORT_EFF="${GATEWAY_PORT:-4000}"
TOOLS_PORT_EFF="${TOOLS_SERVER_PORT:-8081}"
ORCH_PORT_EFF="${ORCHESTRATOR_PORT:-8080}"
PROBE_HOST="127.0.0.1"

probe() {  # probe URL -> 0 when HTTP 200
  "$PYTHON_BIN" - "$1" <<'PY'
import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=4) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

ENDPOINTS=()
if [[ "${COMPOSE_PROFILES:-}" == *bundled* ]]; then
  ENDPOINTS+=("gateway|http://${PROBE_HOST}:${GATEWAY_PORT_EFF}/health/liveliness")
fi
ENDPOINTS+=("tools-server|http://${PROBE_HOST}:${TOOLS_PORT_EFF}/health")
ENDPOINTS+=("orchestrator|http://${PROBE_HOST}:${ORCH_PORT_EFF}/health")

DEADLINE=$(( $(date +%s) + TIMEOUT ))
FAILED_ENDPOINTS=()
for entry in "${ENDPOINTS[@]}"; do
  name="${entry%%|*}"; url="${entry#*|}"
  printf '       waiting for %-14s %s' "$name" "$url"
  delay=1
  healthy=0
  while (( $(date +%s) < DEADLINE )); do
    if probe "$url"; then healthy=1; break; fi
    printf '.'
    sleep "$delay"
    (( delay < 5 )) && delay=$(( delay + 1 ))
  done
  printf '\n'
  if (( healthy )); then
    ok "${name} is healthy"
  else
    bad "${name} did not become healthy within ${TIMEOUT}s"
    FAILED_ENDPOINTS+=("$name")
  fi
done

if ((${#FAILED_ENDPOINTS[@]} > 0)); then
  hdr "diagnostics"
  for name in "${FAILED_ENDPOINTS[@]}"; do
    say "${C_DIM}--- last 25 log lines: ${name}${C_OFF}"
    docker compose logs --tail 25 "$name" 2>&1 | sed 's/^/       /' || true
  done
  say ""
  bad "deploy incomplete: ${FAILED_ENDPOINTS[*]}"
  note "full logs: docker compose logs -f"
  note "stop:      ./scripts/deploy.sh ${STATION} --down"
  exit 1
fi

# --- 4. report ---------------------------------------------------------------
hdr "4. agent wiring"
if ! "$PYTHON_BIN" - "http://${PROBE_HOST}:${ORCH_PORT_EFF}" <<'PY'
import json, sys, urllib.request

base = sys.argv[1]

def fetch(path):
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as response:
        return json.load(response)

try:
    health = fetch("/health")
    agents = fetch("/agents")["agents"]
except Exception as exc:
    print(f"       could not read orchestrator state: {exc}")
    sys.exit(1)

gateway = health.get("gateway") or {}
gateway_ok = gateway.get("status") == "ok"

rows = []
for agent in agents:
    tools = ",".join(agent.get("tools") or []) or "-"
    # An agent is only usable when the gateway that serves its alias answers.
    status = "ready" if gateway_ok else "gateway-down"
    rows.append((agent["name"], agent.get("alias") or "-",
                 agent.get("output_contract") or "-", tools, status))

widths = [max(len(str(row[i])) for row in [("agent", "model alias", "contract",
                                           "tools", "status"), *rows])
          for i in range(5)]
def line(cells, pad=" "):
    return "       " + pad.join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

print(line(("agent", "model alias", "contract", "tools", "status"), "  "))
print("       " + "  ".join("-" * w for w in widths))
for row in rows:
    print(line(row, "  "))

print()
print(f"       gateway      {gateway.get('base_url')}  [{gateway.get('status')}]")
for name, server in (health.get("mcp_servers") or {}).items():
    print(f"       mcp {name:<12} {server.get('transport'):<6} {server.get('target')}")

sys.exit(0 if gateway_ok else 2)
PY
then
  rc=$?
  if (( rc == 2 )); then
    warn "services are up but the gateway is not answering"
    note "check provider credentials: docker compose logs gateway --tail 40"
  else
    warn "could not read the orchestrator's agent table"
  fi
fi

hdr "deployed"
ok "station '${STATION}' is up"
say ""
say "  orchestrator  http://${PROBE_HOST}:${ORCH_PORT_EFF}"
say "  agents        http://${PROBE_HOST}:${ORCH_PORT_EFF}/agents"
say "  health        http://${PROBE_HOST}:${ORCH_PORT_EFF}/health"
say ""
say "  try it:"
say "    curl -s http://${PROBE_HOST}:${ORCH_PORT_EFF}/agents | python3 -m json.tool"
say ""
say "  logs:  docker compose logs -f"
say "  stop:  ./scripts/deploy.sh ${STATION} --down"

if (( TAIL_LOGS )); then
  hdr "logs (ctrl-c to stop)"
  docker compose logs -f
fi
