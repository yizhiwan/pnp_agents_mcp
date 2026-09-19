#!/usr/bin/env bash
# =============================================================================
# pack.sh — package the pack for transport, refusing to ship station-specific
#           values or secrets.
#
# The whole premise of this pack is that it runs unchanged anywhere, so the
# packaging step is also the enforcement point. If a hardcoded IP, absolute
# path, or key-shaped string has crept in, packaging fails and names the line.
#
# Deliberately allowed:
#   * 0.0.0.0 and 127.0.0.1 — universal bind/loopback addresses, not stations
#   * anything inside a ${...} placeholder, including its default value
#   * env/*.example.yaml and .env.example — these exist to carry example values
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PACK_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
SELF_REL="scripts/$(basename -- "${BASH_SOURCE[0]}")"

# --- output ------------------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'
  C_DIM=$'\033[2m';  C_BLD=$'\033[1m';  C_OFF=$'\033[0m'
else
  C_RED=''; C_YEL=''; C_GRN=''; C_DIM=''; C_BLD=''; C_OFF=''
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s  ok  %s %s\n'   "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%s warn %s %s\n'   "$C_YEL" "$C_OFF" "$*"; }
bad()  { printf '%s fail %s %s\n'   "$C_RED" "$C_OFF" "$*"; }
note() { printf '       %s%s%s\n'   "$C_DIM" "$*" "$C_OFF"; }
hdr()  { printf '\n%s%s%s\n' "$C_BLD" "$*" "$C_OFF"; }

usage() {
  cat <<'EOF'
Usage: scripts/pack.sh [options]

Scans the pack for hardcoded secrets, IP addresses and absolute paths, then
writes a tarball to dist/ that is safe to copy to any station.

Options:
  --dry-run        Run the scan only; do not write a tarball.
  --out DIR        Output directory for the tarball.        [dist]
  --name NAME      Override the archive base name.          [from manifest.yaml]
  --allow-dirty    Package even if the scan finds problems (NOT recommended;
                   prints every finding first and exits 0).
  --no-color       Disable colored output.
  -h, --help       Show this help.

Exit codes: 0 success, 1 scan found problems, 2 usage or environment error.

Excluded from the archive: env/*.yaml (except *.example.yaml), .env, dist/,
.git/, __pycache__/, .venv/, .pytest_cache/, *.log
EOF
}

DRY_RUN=0
OUT_DIR="dist"
ARCHIVE_NAME=""
ALLOW_DIRTY=0
while (($#)); do
  case "$1" in
    --dry-run)     DRY_RUN=1 ;;
    --out)         OUT_DIR="${2:?--out needs a directory}"; shift ;;
    --name)        ARCHIVE_NAME="${2:?--name needs a value}"; shift ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --no-color)    C_RED=''; C_YEL=''; C_GRN=''; C_DIM=''; C_BLD=''; C_OFF='' ;;
    -h|--help)     usage; exit 0 ;;
    *)             bad "unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

cd "$PACK_ROOT"

command -v tar >/dev/null 2>&1 || { bad "tar is required"; exit 2; }

# Prefer python3, but fall back to python: some hosts ship only one of them,
# and on others they are different interpreters with different packages.
PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 \
     && "$candidate" -c 'import yaml' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"; break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  bad "no python interpreter with PyYAML found on PATH"
  note "install it with: pip install -r requirements.txt"
  exit 2
fi

# --- identity from the manifest ---------------------------------------------
read -r PACK_NAME PACK_VERSION <<<"$("$PYTHON_BIN" - <<'PY'
import sys
try:
    import yaml
    with open("manifest.yaml", encoding="utf-8") as fh:
        meta = (yaml.safe_load(fh) or {}).get("metadata") or {}
    print(meta.get("name", "pack"), meta.get("version", "unknown"))
except Exception as exc:
    print("pack unknown")
    print(f"manifest could not be read: {exc}", file=sys.stderr)
PY
)"
[[ -n "$ARCHIVE_NAME" ]] || ARCHIVE_NAME="${PACK_NAME}-${PACK_VERSION}"

hdr "pack ${PACK_NAME} v${PACK_VERSION}"
say "${C_DIM}root: ${PACK_ROOT}${C_OFF}"

# --- files to scan / ship ----------------------------------------------------
EXCLUDES=(
  "./.git/*" "./dist/*" "./build/*" "./__pycache__/*" "*/__pycache__/*"
  "./.venv/*" "./venv/*" "./.pytest_cache/*" "*/.pytest_cache/*"
  "./.mypy_cache/*" "./.ruff_cache/*" "*.pyc" "*.log" "./.env"
)
FIND_ARGS=()
for pattern in "${EXCLUDES[@]}"; do
  FIND_ARGS+=(-not -path "$pattern")
done
# Real station overlays never travel: only the examples do.
FIND_ARGS+=(-not \( -path "./env/*" -not -name "*.example.yaml" -not -name "*.example.yml" \))

mapfile -d '' SCAN_FILES < <(find . -type f "${FIND_ARGS[@]}" -print0)
if ((${#SCAN_FILES[@]} == 0)); then
  bad "no files found to package"
  exit 2
fi
say "${C_DIM}scanning ${#SCAN_FILES[@]} file(s)${C_OFF}"

# --- the scan ----------------------------------------------------------------
# Each rule: NAME|REGEX|EXPLANATION. Lines are pre-filtered to strip ${...}
# placeholders, so a default value inside one is never flagged.
FINDINGS_FILE="$(mktemp)"
trap 'rm -f "$FINDINGS_FILE"' EXIT

"$PYTHON_BIN" - "$FINDINGS_FILE" "$SELF_REL" <<'PY'
import os, re, sys

findings_path, self_rel = sys.argv[1], sys.argv[2]

# Strip ${...} (including nested defaults) before applying any rule.
PLACEHOLDER = re.compile(r"\$\{[^{}]*(?:\$\{[^{}]*\}[^{}]*)*\}")

# 0.0.0.0 (bind all) and 127.0.0.1 / ::1 (loopback) are universal, not stations.
ALLOWED_ADDRS = {"0.0.0.0", "127.0.0.1", "255.255.255.255", "::1"}

RULES = [
    ("ipv4-literal",
     re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
     "hardcoded IP address; use a hostname from ${VAR} instead"),
    # Only unambiguously HOST-user paths. /opt, /srv, /app and /pack are
    # normal container-internal locations and are not portability problems.
    ("absolute-path",
     re.compile(r"(?:^|[\s\"'=:(\[])(?:/home/|/Users/|/root/)"),
     "absolute host path; paths must be relative to the pack root"),
    ("windows-path",
     re.compile(r"\b[A-Za-z]:[\\/](?:Users|Program Files|Windows)\b"),
     "absolute Windows path"),
    ("openai-key",   re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "OpenAI-style API key"),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"), "Anthropic API key"),
    ("aws-key",      re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "AWS access key id"),
    ("google-key",   re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "Google API key"),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghs|ghr)_[A-Za-z0-9]{30,}\b"), "GitHub token"),
    ("slack-token",  re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    ("openrouter-key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"), "OpenRouter key"),
    ("bearer-literal",
     re.compile(r"(?i)\bauthorization\s*[:=]\s*[\"']?Bearer\s+[A-Za-z0-9._-]{16,}"),
     "literal bearer token"),
    ("private-key",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
     "private key material"),
]

# Files whose whole purpose is to carry example values.
EXAMPLE_FILES = {
    "./.env.example",
}
def is_example(path: str) -> bool:
    return path in EXAMPLE_FILES or ".example." in os.path.basename(path)

TEXT_EXT = {
    "", ".yaml", ".yml", ".json", ".py", ".sh", ".md", ".txt", ".toml", ".cfg",
    ".ini", ".env", ".example", ".dockerfile", ".conf",
}

findings = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in {
        ".git", "dist", "build", "__pycache__", ".venv", "venv",
        ".pytest_cache", ".mypy_cache", ".ruff_cache",
    }]
    for filename in files:
        path = os.path.join(root, filename).replace(os.sep, "/")
        rel = path[2:] if path.startswith("./") else path
        # Real station overlays are not shipped, so they are not scanned.
        if rel.startswith("env/") and ".example." not in filename:
            continue
        if rel == ".env":
            continue
        # This scanner necessarily contains the patterns it searches for.
        if rel == self_rel:
            continue
        ext = os.path.splitext(filename)[1].lower()
        if ext not in TEXT_EXT and filename.lower() not in {"dockerfile", "makefile"}:
            continue
        try:
            with open(path, encoding="utf-8", errors="strict") as fh:
                lines = fh.readlines()
        except (OSError, UnicodeDecodeError):
            continue

        example = is_example(path)
        for number, raw in enumerate(lines, 1):
            line = PLACEHOLDER.sub("", raw)
            for name, pattern, explanation in RULES:
                for match in pattern.finditer(line):
                    hit = match.group(0).strip(" \"'=:([")
                    if name == "ipv4-literal":
                        if hit in ALLOWED_ADDRS:
                            continue
                        # Version strings and the like are not addresses.
                        if not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", hit):
                            continue
                        if any(int(o) > 255 for o in hit.split(".")):
                            continue
                    # Example files may carry placeholder-ish example values,
                    # but never real credentials.
                    if example and name in {"ipv4-literal", "absolute-path",
                                            "windows-path"}:
                        continue
                    findings.append((rel, number, name, hit, explanation,
                                     raw.strip()[:120]))

with open(findings_path, "w", encoding="utf-8") as fh:
    for rel, number, name, hit, explanation, snippet in findings:
        fh.write(f"{rel}\t{number}\t{name}\t{hit}\t{explanation}\t{snippet}\n")
print(len(findings))
PY

FINDING_COUNT="$(wc -l <"$FINDINGS_FILE" | tr -d ' ')"

hdr "portability scan"
if [[ "$FINDING_COUNT" -eq 0 ]]; then
  ok "no hardcoded addresses, paths or credentials found"
else
  while IFS=$'\t' read -r file line rule hit explanation snippet; do
    bad "${file}:${line} ${C_BLD}${rule}${C_OFF} -> ${hit}"
    say "        ${C_DIM}${explanation}${C_OFF}"
    say "        ${C_DIM}${snippet}${C_OFF}"
  done <"$FINDINGS_FILE"
  say ""
  if (( ALLOW_DIRTY )); then
    warn "${FINDING_COUNT} finding(s); packaging anyway because --allow-dirty was given"
  else
    bad "${FINDING_COUNT} finding(s); refusing to package"
    say "${C_DIM}fix them, or re-run with --allow-dirty if you are certain.${C_OFF}"
    exit 1
  fi
fi

# --- config sanity -----------------------------------------------------------
hdr "config sanity"
for station_file in env/*.example.yaml; do
  [[ -e "$station_file" ]] || continue
  station="$(basename "$station_file" .example.yaml)"
  if GATEWAY_MASTER_KEY="packsh-validation-placeholder" \
     "$PYTHON_BIN" config_loader.py --pack . --station "$station" >/dev/null 2>&1; then
    ok "station '${station}' configuration is valid"
  else
    bad "station '${station}' configuration does not load"
    say "${C_DIM}run: ${PYTHON_BIN} config_loader.py --pack . --station ${station} --dry-run${C_OFF}"
    exit 1
  fi
done

if (( DRY_RUN )); then
  hdr "result"
  ok "--dry-run: scan and validation passed, no archive written"
  exit 0
fi

# --- build the archive -------------------------------------------------------
hdr "archive"
mkdir -p "$OUT_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${OUT_DIR}/${ARCHIVE_NAME}-${STAMP}.tar.gz"

TAR_EXCLUDES=(
  --exclude-vcs
  --exclude="./dist" --exclude="./build"
  --exclude="__pycache__" --exclude="*.pyc"
  --exclude="./.venv" --exclude="./venv"
  --exclude="./.pytest_cache" --exclude="./.mypy_cache" --exclude="./.ruff_cache"
  --exclude="*.log" --exclude="./.env"
)
# Ship only the example overlays.
while IFS= read -r overlay; do
  TAR_EXCLUDES+=(--exclude="./${overlay#./}")
done < <(find ./env -type f \( -name "*.yaml" -o -name "*.yml" \) \
           -not -name "*.example.yaml" -not -name "*.example.yml" 2>/dev/null || true)

tar -czf "$ARCHIVE" "${TAR_EXCLUDES[@]}" -C "$PACK_ROOT" .

SIZE="$(du -h "$ARCHIVE" | cut -f1 | tr -d ' ')"
ok "wrote ${ARCHIVE} (${SIZE})"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$ARCHIVE" >"${ARCHIVE}.sha256"
  ok "wrote ${ARCHIVE}.sha256"
fi

hdr "next"
say "  1. copy the archive to the target station"
say "  2. tar -xzf $(basename "$ARCHIVE")"
say "  3. cp env/<station>.example.yaml env/<station>.yaml   # then edit"
say "  4. ./scripts/deploy.sh <station>"
say ""
say "${C_DIM}see MIGRATION.md for the full procedure and rollback steps.${C_OFF}"
