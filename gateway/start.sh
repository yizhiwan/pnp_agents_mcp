#!/usr/bin/env bash
# =============================================================================
# Model gateway entrypoint — validate, then launch the LiteLLM proxy.
#
# Why this wrapper exists:
#   1. Fail fast with ALL configuration problems listed at once, before the
#      first request rather than at it.
#   2. Drop model aliases whose credentials are absent, so a pack with only one
#      provider key still boots and serves that provider.
#   3. Write the effective config to a writable tmpfs path — the pack itself is
#      mounted read-only, so LiteLLM cannot be pointed at it directly once we
#      need to filter aliases.
#
# Nothing station-specific is hardcoded here. The ALIAS_*_MODEL defaults below
# are the pack's out-of-the-box mapping; any station overrides them purely
# through the environment, with no file edit.
# =============================================================================
set -euo pipefail

# --- output helpers ----------------------------------------------------------
if [[ -t 2 && -z "${NO_COLOR:-}" ]]; then
  C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'
  C_DIM=$'\033[2m';  C_BLD=$'\033[1m';  C_OFF=$'\033[0m'
else
  C_RED=''; C_YEL=''; C_GRN=''; C_DIM=''; C_BLD=''; C_OFF=''
fi
log()  { printf '%s[gateway]%s %s\n'      "$C_DIM" "$C_OFF" "$*" >&2; }
ok()   { printf '%s[gateway]%s %s\n'      "$C_GRN" "$C_OFF" "$*" >&2; }
warn() { printf '%s[gateway WARN]%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
die()  { printf '%s[gateway FATAL]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: start.sh [--check] [--print-config] [--help]

Validates gateway configuration and launches the LiteLLM proxy.

Options:
  --check          Validate environment and alias credentials, then exit 0/1
                   without starting the proxy. Used by scripts/doctor.sh.
  --print-config   Write and print the effective (filtered) config, then exit.
  --help, -h       Show this help.

Environment (all optional unless marked REQUIRED):
  GATEWAY_MASTER_KEY        REQUIRED. Shared secret clients must present.
  GATEWAY_PORT              Listen port.                        [4000]
  GATEWAY_BIND_ADDR         Bind address inside the container.   [0.0.0.0]
  GATEWAY_ENABLED_ALIASES   Comma-separated allowlist. Empty = every alias
                            that has credentials present.
  GATEWAY_CONFIG            Path to litellm_config.yaml. Auto-detected.
  GATEWAY_RUNTIME_CONFIG    Where to write the filtered config. [/tmp/...]
  LITELLM_EXTRA_ARGS        Extra flags appended to the litellm command.

  Provider credentials: OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY,
  OPENROUTER_API_KEY, OLLAMA_BASE_URL. Supply only what you use.

  Alias -> provider model overrides (defaults shown in the script):
  ALIAS_GPT_4O_MODEL, ALIAS_GPT_4O_MINI_MODEL, ALIAS_CLAUDE_SONNET_MODEL,
  ALIAS_CLAUDE_HAIKU_MODEL, ALIAS_GEMINI_PRO_MODEL, ALIAS_GEMINI_FLASH_MODEL,
  ALIAS_LOCAL_LLAMA_MODEL, ALIAS_OPENROUTER_DEEPSEEK_MODEL
EOF
}

MODE="serve"
for arg in "$@"; do
  case "$arg" in
    --check)        MODE="check" ;;
    --print-config) MODE="print" ;;
    -h|--help)      usage; exit 0 ;;
    *)              die "unknown argument: $arg (try --help)" ;;
  esac
done

# --- defaults ----------------------------------------------------------------
: "${GATEWAY_PORT:=4000}"
: "${GATEWAY_BIND_ADDR:=0.0.0.0}"
: "${GATEWAY_ENABLED_ALIASES:=}"
: "${GATEWAY_RUNTIME_CONFIG:=/tmp/litellm.runtime.yaml}"
: "${LITELLM_EXTRA_ARGS:=}"

# Alias -> provider model defaults. These are the only provider model
# identifiers in the pack, they are defaults only, and every one is overridable
# by exporting the matching variable. No file edit is ever required.
: "${ALIAS_GPT_4O_MODEL:=openai/gpt-4o}"
: "${ALIAS_GPT_4O_MINI_MODEL:=openai/gpt-4o-mini}"
: "${ALIAS_CLAUDE_SONNET_MODEL:=anthropic/claude-sonnet-4-5}"
: "${ALIAS_CLAUDE_HAIKU_MODEL:=anthropic/claude-haiku-4-5}"
: "${ALIAS_GEMINI_PRO_MODEL:=gemini/gemini-2.5-pro}"
: "${ALIAS_GEMINI_FLASH_MODEL:=gemini/gemini-2.5-flash}"
: "${ALIAS_LOCAL_LLAMA_MODEL:=ollama_chat/llama3.1}"
: "${ALIAS_OPENROUTER_DEEPSEEK_MODEL:=openrouter/deepseek/deepseek-chat}"
: "${OPENROUTER_BASE_URL:=https://openrouter.ai/api/v1}"
export ALIAS_GPT_4O_MODEL ALIAS_GPT_4O_MINI_MODEL \
       ALIAS_CLAUDE_SONNET_MODEL ALIAS_CLAUDE_HAIKU_MODEL \
       ALIAS_GEMINI_PRO_MODEL ALIAS_GEMINI_FLASH_MODEL \
       ALIAS_LOCAL_LLAMA_MODEL ALIAS_OPENROUTER_DEEPSEEK_MODEL \
       OPENROUTER_BASE_URL

# --- locate the config -------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${GATEWAY_CONFIG:-}" ]]; then
  for candidate in \
    "/pack/gateway/litellm_config.yaml" \
    "${SCRIPT_DIR}/litellm_config.yaml" \
    "/app/litellm_config.yaml"
  do
    if [[ -r "$candidate" ]]; then GATEWAY_CONFIG="$candidate"; break; fi
  done
fi
[[ -n "${GATEWAY_CONFIG:-}" ]] || die "cannot locate litellm_config.yaml; set GATEWAY_CONFIG"
[[ -r "$GATEWAY_CONFIG" ]]     || die "config not readable: $GATEWAY_CONFIG"
export GATEWAY_CONFIG GATEWAY_RUNTIME_CONFIG GATEWAY_ENABLED_ALIASES

# --- validate environment (collect every problem, then report once) ----------
errors=()
if [[ -z "${GATEWAY_MASTER_KEY:-}" ]]; then
  errors+=("GATEWAY_MASTER_KEY is not set. Generate one with: openssl rand -hex 24")
elif (( ${#GATEWAY_MASTER_KEY} < 8 )); then
  errors+=("GATEWAY_MASTER_KEY is shorter than 8 characters (length ${#GATEWAY_MASTER_KEY}).")
fi
if ! [[ "$GATEWAY_PORT" =~ ^[0-9]+$ ]] || (( GATEWAY_PORT < 1 || GATEWAY_PORT > 65535 )); then
  errors+=("GATEWAY_PORT must be an integer 1-65535, got '${GATEWAY_PORT}'.")
fi
if ! command -v python3 >/dev/null 2>&1; then
  errors+=("python3 not found on PATH; it is required to build the effective config.")
fi
if [[ "$MODE" == "serve" ]] && ! command -v litellm >/dev/null 2>&1; then
  errors+=("litellm not found on PATH; rebuild the gateway image.")
fi
if (( ${#errors[@]} > 0 )); then
  printf '%s[gateway FATAL]%s %d configuration problem(s):\n' "$C_RED" "$C_OFF" "${#errors[@]}" >&2
  for i in "${!errors[@]}"; do
    printf '  %s%d)%s %s\n' "$C_BLD" "$((i+1))" "$C_OFF" "${errors[$i]}" >&2
  done
  exit 1
fi

# --- build the effective config ----------------------------------------------
# Keeps only aliases that are (a) allowed by GATEWAY_ENABLED_ALIASES and
# (b) backed by every credential named in model_info.requires_env.
set +e
SUMMARY="$(python3 - <<'PY'
import os, sys
try:
    import yaml
except ImportError:
    print("FATAL|PyYAML is not installed in the gateway image.", file=sys.stderr)
    sys.exit(3)

src = os.environ["GATEWAY_CONFIG"]
dst = os.environ["GATEWAY_RUNTIME_CONFIG"]
requested_raw = os.environ.get("GATEWAY_ENABLED_ALIASES", "").strip()
requested = [a.strip() for a in requested_raw.split(",") if a.strip()]

try:
    with open(src, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
except yaml.YAMLError as exc:
    print(f"FATAL|{src} is not valid YAML: {exc}", file=sys.stderr)
    sys.exit(3)

models = cfg.get("model_list") or []
if not isinstance(models, list) or not models:
    print(f"FATAL|{src} declares no model_list entries.", file=sys.stderr)
    sys.exit(3)

declared = [m.get("model_name") for m in models]
fatal, kept, dropped = [], [], []

unknown = [a for a in requested if a not in declared]
if unknown:
    fatal.append(
        "GATEWAY_ENABLED_ALIASES names alias(es) not declared in the gateway "
        f"config: {', '.join(sorted(unknown))}. Declared: {', '.join(declared)}"
    )

for entry in models:
    alias = entry.get("model_name")
    if requested and alias not in requested:
        continue
    needs = (entry.get("model_info") or {}).get("requires_env") or []
    missing = [v for v in needs if not (os.environ.get(v) or "").strip()]
    if missing:
        msg = f"{alias}: missing {', '.join(missing)}"
        if requested:
            # Explicitly requested but unusable -> a real misconfiguration.
            fatal.append(f"alias '{alias}' was requested but {', '.join(missing)} is not set.")
        else:
            dropped.append(msg)
        continue
    # Resolve the provider model for the summary only; LiteLLM reads the env
    # reference itself so no secret or model string is rewritten into the file.
    ref = str((entry.get("litellm_params") or {}).get("model", ""))
    shown = os.environ.get(ref.split("os.environ/", 1)[1], "<unset>") if ref.startswith("os.environ/") else ref
    kept.append(f"{alias}={shown}")

if fatal:
    for f in fatal:
        print(f"FATAL|{f}", file=sys.stderr)
    sys.exit(3)

if not kept:
    print(
        "FATAL|no model alias has usable credentials. Set at least one of "
        "OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, "
        "or OLLAMA_BASE_URL.",
        file=sys.stderr,
    )
    sys.exit(3)

keep_names = {k.split("=", 1)[0] for k in kept}
cfg["model_list"] = [m for m in models if m.get("model_name") in keep_names]

try:
    with open(dst, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)
except OSError as exc:
    print(f"FATAL|cannot write effective config to {dst}: {exc}", file=sys.stderr)
    sys.exit(3)

for d in dropped:
    print(f"DROP|{d}", file=sys.stderr)
print("|".join(kept))
PY
)"
rc=$?
set -e
if (( rc != 0 )); then
  die "gateway configuration is invalid; see the messages above. (exit $rc)"
fi

# --- report (never prints a credential) --------------------------------------
log "config source : ${GATEWAY_CONFIG}"
log "effective cfg : ${GATEWAY_RUNTIME_CONFIG}"
log "master key    : set (***redacted***, ${#GATEWAY_MASTER_KEY} chars)"
log "listen        : ${GATEWAY_BIND_ADDR}:${GATEWAY_PORT}"
log "aliases served:"
IFS='|' read -r -a served <<<"$SUMMARY"
for pair in "${served[@]}"; do
  [[ -n "$pair" ]] && printf '                %s%-22s%s -> %s\n' "$C_BLD" "${pair%%=*}" "$C_OFF" "${pair#*=}" >&2
done
ok "${#served[@]} alias(es) ready"

case "$MODE" in
  check) ok "--check passed"; exit 0 ;;
  print) cat "$GATEWAY_RUNTIME_CONFIG"; exit 0 ;;
esac

# --- launch ------------------------------------------------------------------
# shellcheck disable=SC2086
exec litellm \
  --config "$GATEWAY_RUNTIME_CONFIG" \
  --host "$GATEWAY_BIND_ADDR" \
  --port "$GATEWAY_PORT" \
  $LITELLM_EXTRA_ARGS
