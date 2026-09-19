#!/usr/bin/env bash
# =============================================================================
# doctor.sh — verify a target station can run this pack, BEFORE deploying.
#
# Everything it checks comes from manifest.yaml, so the manifest stays the
# single source of truth about what a host must provide. Adding a required
# variable or a port to the manifest automatically extends this check.
#
# Checks:
#   1. runtime      python / docker / compose present and new enough
#   2. architecture uname -m is in compatibility.arch
#   3. env vars     every required_env set; conditional_env groups satisfied
#   4. ports        every declared port is free (or already ours)
#   5. config       the station overlay loads and validates
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
PASS=0; WARN=0; FAIL=0
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s  ok  %s %s\n' "$C_GRN" "$C_OFF" "$*"; PASS=$((PASS+1)); }
warn() { printf '%s warn %s %s\n' "$C_YEL" "$C_OFF" "$*"; WARN=$((WARN+1)); }
bad()  { printf '%s fail %s %s\n' "$C_RED" "$C_OFF" "$*"; FAIL=$((FAIL+1)); }
note() { printf '       %s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
hdr()  { printf '\n%s%s%s\n' "$C_BLD" "$*" "$C_OFF"; }

usage() {
  cat <<'EOF'
Usage: scripts/doctor.sh [options]

Verifies this host can run the pack, using manifest.yaml as the source of truth.
Run it before deploy.sh (deploy.sh runs it for you).

Options:
  --station NAME   Station overlay to validate.   [$STATION, or 'local']
  --strict         Treat warnings as failures.
  --skip-ports     Do not check port availability.
  --skip-config    Do not load the configuration.
  --no-color       Disable colored output.
  -h, --help       Show this help.

Exit codes: 0 all good (warnings allowed), 1 one or more checks failed,
            2 usage or environment error.
EOF
}

STATION="${STATION:-local}"
STRICT=0; SKIP_PORTS=0; SKIP_CONFIG=0
while (($#)); do
  case "$1" in
    --station)     STATION="${2:?--station needs a value}"; shift ;;
    --strict)      STRICT=1 ;;
    --skip-ports)  SKIP_PORTS=1 ;;
    --skip-config) SKIP_CONFIG=1 ;;
    --no-color)    C_RED=''; C_YEL=''; C_GRN=''; C_DIM=''; C_BLD=''; C_OFF='' ;;
    -h|--help)     usage; exit 0 ;;
    *)             bad "unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

cd "$PACK_ROOT"
[[ -f manifest.yaml ]] || { bad "manifest.yaml not found in ${PACK_ROOT}"; exit 2; }

# Interpreter selection. The manifest cannot be read without PyYAML, so prefer
# an interpreter that has it; `python3` and `python` are sometimes different
# installations with different packages.
PYTHON_BIN=""
PYTHON_FALLBACK=""
for candidate in python3 python; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  [[ -n "$PYTHON_FALLBACK" ]] || PYTHON_FALLBACK="$candidate"
  if "$candidate" -c 'import yaml, jsonschema' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"; break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -z "$PYTHON_FALLBACK" ]]; then
    bad "no python interpreter found on PATH"
    exit 2
  fi
  bad "no python interpreter on PATH has PyYAML and jsonschema installed"
  note "tried: python3, python (found: ${PYTHON_FALLBACK})"
  note "install them with: ${PYTHON_FALLBACK} -m pip install -r requirements.txt"
  note "the manifest cannot be read without them, so no further checks can run"
  exit 2
fi

say "${C_BLD}doctor${C_OFF} — station '${STATION}' on $(uname -s) $(uname -m)"
say "${C_DIM}pack: ${PACK_ROOT}${C_OFF}"

# --- read everything we need out of the manifest once ------------------------
MANIFEST_DUMP="$("$PYTHON_BIN" - <<'PY'
import sys
try:
    import yaml
except ImportError:
    print("ERR\tPyYAML is not installed for this interpreter")
    sys.exit(0)
try:
    with open("manifest.yaml", encoding="utf-8") as fh:
        m = yaml.safe_load(fh) or {}
except Exception as exc:
    print(f"ERR\tmanifest.yaml could not be parsed: {exc}")
    sys.exit(0)

meta = m.get("metadata") or {}
print(f"META\t{meta.get('name','pack')}\t{meta.get('version','unknown')}")

compat = m.get("compatibility") or {}
for key, value in (compat.get("min_runtime") or {}).items():
    print(f"RUNTIME\t{key}\t{value}")
for arch in compat.get("arch") or []:
    print(f"ARCH\t{arch}")
for os_name in compat.get("os") or []:
    print(f"OS\t{os_name}")

for item in m.get("required_env") or []:
    print(f"REQ\t{item.get('name')}\t{item.get('secret', False)}\t"
          f"{item.get('generate','')}\t{item.get('description','')}")

for group in m.get("conditional_env") or []:
    members = ",".join(str(x.get("name")) for x in (group.get("members") or []))
    print(f"COND\t{group.get('group')}\t{bool(group.get('at_least_one'))}\t{members}")

for port in m.get("ports") or []:
    print(f"PORT\t{port.get('name')}\t{port.get('env')}\t{port.get('default')}\t"
          f"{bool(port.get('required'))}\t{port.get('health_path','')}")
PY
)"

if grep -q $'^ERR\t' <<<"$MANIFEST_DUMP"; then
  bad "$(grep $'^ERR\t' <<<"$MANIFEST_DUMP" | cut -f2-)"
  exit 2
fi

PACK_NAME="$(grep $'^META\t' <<<"$MANIFEST_DUMP" | cut -f2)"
PACK_VERSION="$(grep $'^META\t' <<<"$MANIFEST_DUMP" | cut -f3)"
say "${C_DIM}manifest: ${PACK_NAME} v${PACK_VERSION}${C_OFF}"

# --- 1. runtime versions -----------------------------------------------------
hdr "1. runtime"

version_ge() {  # version_ge HAVE WANT  -> 0 when HAVE >= WANT
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

while IFS=$'\t' read -r _ tool want; do
  case "$tool" in
    python)
      have="$("$PYTHON_BIN" -c 'import sys; print("%d.%d"%sys.version_info[:2])')"
      if version_ge "$have" "$want"; then ok "python ${have} (needs >= ${want})"
      else bad "python ${have} is older than the required ${want}"; fi
      ;;
    docker)
      if command -v docker >/dev/null 2>&1; then
        # `docker version` exits non-zero when the daemon is down but still
        # prints the client version, so the fallback can also emit a line.
        # Take the first line only, and strip any CR.
        have="$({ docker version --format '{{.Client.Version}}' 2>/dev/null \
                  || docker --version | sed -E 's/.* ([0-9]+\.[0-9]+\.[0-9]+).*/\1/'; } \
                | tr -d '\r' | head -n1)"
        if [[ -n "$have" ]] && version_ge "$have" "$want"; then
          ok "docker ${have} (needs >= ${want})"
        else
          warn "docker version ${have:-unknown} could not be confirmed >= ${want}"
        fi
        if docker info >/dev/null 2>&1; then ok "docker daemon is reachable"
        else bad "docker is installed but the daemon is not reachable"
             note "start it, or add this user to the 'docker' group"; fi
      else
        bad "docker not found on PATH"
      fi
      ;;
    docker_compose)
      if docker compose version >/dev/null 2>&1; then
        have="$(docker compose version --short 2>/dev/null | sed 's/^v//')"
        if [[ -n "$have" ]] && version_ge "$have" "$want"; then
          ok "docker compose ${have} (needs >= ${want})"
        else
          warn "docker compose ${have:-unknown} could not be confirmed >= ${want}"
          note "depends_on.required needs >= ${want}"
        fi
      else
        bad "'docker compose' (v2 plugin) not available"
        note "the legacy docker-compose v1 binary is not supported"
      fi
      ;;
  esac
done < <(grep $'^RUNTIME\t' <<<"$MANIFEST_DUMP" || true)

# --- 2. architecture and OS --------------------------------------------------
hdr "2. platform"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  x86_64|amd64)          NORM_ARCH="amd64" ;;
  aarch64|arm64)         NORM_ARCH="arm64" ;;
  *)                     NORM_ARCH="$HOST_ARCH" ;;
esac
SUPPORTED_ARCHES="$(grep $'^ARCH\t' <<<"$MANIFEST_DUMP" | cut -f2 | paste -sd, - || true)"
if grep -qx "$NORM_ARCH" <(grep $'^ARCH\t' <<<"$MANIFEST_DUMP" | cut -f2); then
  ok "architecture ${HOST_ARCH} (${NORM_ARCH}) is supported"
else
  bad "architecture ${HOST_ARCH} (${NORM_ARCH}) is not in: ${SUPPORTED_ARCHES}"
fi

HOST_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
SUPPORTED_OS="$(grep $'^OS\t' <<<"$MANIFEST_DUMP" | cut -f2 | paste -sd, - || true)"
if [[ -z "$SUPPORTED_OS" ]] || grep -qx "$HOST_OS" <(grep $'^OS\t' <<<"$MANIFEST_DUMP" | cut -f2); then
  ok "operating system ${HOST_OS} is supported"
else
  warn "operating system ${HOST_OS} is not in: ${SUPPORTED_OS}"
  note "containers still run, but the scripts assume a POSIX shell"
fi

# --- 3. environment variables ------------------------------------------------
hdr "3. environment"
while IFS=$'\t' read -r _ name secret generate description; do
  [[ -n "$name" ]] || continue
  value="${!name:-}"
  if [[ -n "$value" ]]; then
    if [[ "$secret" == "True" ]]; then ok "${name} is set (***redacted***, ${#value} chars)"
    else ok "${name}=${value}"; fi
  else
    bad "${name} is not set — ${description}"
    [[ -n "$generate" ]] && note "generate one with: ${generate}"
  fi
done < <(grep $'^REQ\t' <<<"$MANIFEST_DUMP" || true)

while IFS=$'\t' read -r _ group at_least_one members; do
  [[ -n "$group" ]] || continue
  present=(); absent=()
  IFS=',' read -r -a member_list <<<"$members"
  for member in "${member_list[@]}"; do
    if [[ -n "${!member:-}" ]]; then present+=("$member"); else absent+=("$member"); fi
  done
  if ((${#present[@]} > 0)); then
    ok "${group}: ${#present[@]} of ${#member_list[@]} set (${present[*]})"
    ((${#absent[@]} > 0)) && note "not set: ${absent[*]}"
  elif [[ "$at_least_one" == "True" ]]; then
    bad "${group}: none of ${members} is set, but at least one is required"
  else
    warn "${group}: none of ${members} is set"
  fi
done < <(grep $'^COND\t' <<<"$MANIFEST_DUMP" || true)

# --- 4. ports ----------------------------------------------------------------
hdr "4. ports"
if (( SKIP_PORTS )); then
  warn "port checks skipped (--skip-ports)"
else
  while IFS=$'\t' read -r _ name env_var default required health; do
    [[ -n "$name" ]] || continue
    port="${!env_var:-$default}"
    if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
      bad "${name}: ${env_var}='${port}' is not a valid port"
      continue
    fi
    # A bind test is the portable answer: no ss/netstat/lsof dependency.
    if "$PYTHON_BIN" - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
    then
      ok "${name}: port ${port} is free (${env_var})"
    else
      # Already serving this pack is fine; anything else is a conflict.
      if command -v docker >/dev/null 2>&1 \
         && docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":${port}->"; then
        warn "${name}: port ${port} is in use by a running container"
        note "probably this pack; deploy.sh will recreate it"
      elif [[ "$required" == "True" ]]; then
        bad "${name}: port ${port} is already in use"
        note "free it, or set ${env_var} to another port"
      else
        warn "${name}: port ${port} is in use (this service is optional)"
      fi
    fi
  done < <(grep $'^PORT\t' <<<"$MANIFEST_DUMP" || true)
fi

# --- 5. configuration --------------------------------------------------------
hdr "5. configuration"
if (( SKIP_CONFIG )); then
  warn "configuration check skipped (--skip-config)"
elif [[ ! -f config_loader.py ]]; then
  bad "config_loader.py is missing; the pack is incomplete"
else
  if [[ -f "env/${STATION}.yaml" ]]; then
    ok "station overlay env/${STATION}.yaml is present"
  elif [[ -f "env/${STATION}.example.yaml" ]]; then
    warn "env/${STATION}.yaml is missing; the example will be used"
    note "cp env/${STATION}.example.yaml env/${STATION}.yaml"
  else
    bad "no overlay for station '${STATION}'"
    note "available: $(ls env/*.y*ml 2>/dev/null | xargs -n1 basename 2>/dev/null | paste -sd' ' - || echo none)"
  fi

  if CONFIG_OUT="$("$PYTHON_BIN" config_loader.py --pack . --station "$STATION" \
                    --no-color 2>&1)"; then
    ok "configuration loads and validates"
    while IFS= read -r line; do
      [[ -n "$line" ]] && note "$line"
    done <<<"$(grep -E '^(WARN|OK)' <<<"$CONFIG_OUT" || true)"
  else
    bad "configuration does not load"
    while IFS= read -r line; do
      [[ -n "$line" ]] && note "$line"
    done <<<"$CONFIG_OUT"
  fi
fi

# --- summary -----------------------------------------------------------------
hdr "summary"
say "  ${C_GRN}${PASS} ok${C_OFF}   ${C_YEL}${WARN} warning(s)${C_OFF}   ${C_RED}${FAIL} failure(s)${C_OFF}"

if (( FAIL > 0 )); then
  say ""
  say "${C_RED}not ready${C_OFF} — fix the failures above, then run doctor again."
  exit 1
fi
if (( STRICT && WARN > 0 )); then
  say ""
  say "${C_YEL}not ready${C_OFF} — --strict was given and there are warnings."
  exit 1
fi
say ""
say "${C_GRN}ready${C_OFF} — deploy with: ./scripts/deploy.sh ${STATION}"
exit 0
