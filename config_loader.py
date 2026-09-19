#!/usr/bin/env python3
# =============================================================================
# Layered configuration loader for the Agent Pack.
#
# Load order (later wins):
#     config/base.yaml  ->  env/<station>.yaml  ->  os.environ
#
# Pipeline:
#   1. read     base + station overlay
#   2. merge    deep merge (dicts merge, lists and scalars replace)
#   3. override PACK__<path>__<to>__<key> environment variables
#   4. subst    "${VAR}" (required) and "${VAR:default}" / "${VAR:-default}"
#   5. agents   load agents/<name>.yaml for each enabled agent, apply defaults,
#               inline the prompt file verbatim
#   6. derive   gateway.base_url and each HTTP MCP server's url
#   7. check    cross-references (agent -> mcp server -> tool, alias -> gateway)
#   8. validate config/schema.json
#
# Every problem found in steps 4-8 is collected and reported together, so one
# run tells you everything that is wrong instead of one thing at a time.
#
# Public API:
#     load_pack(pack_dir, station, environ=None) -> dict
#     load_pack_with_meta(pack_dir, station, environ=None) -> (dict, LoadMeta)
#     redact(obj) -> obj                      # secrets replaced, safe to log
#
# CLI:
#     python config_loader.py --pack . --station local --dry-run
# =============================================================================
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import yaml
except ImportError:  # pragma: no cover - dependency guard
    print("config_loader requires PyYAML: pip install -r requirements.txt",
          file=sys.stderr)
    raise SystemExit(78)

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - dependency guard
    print("config_loader requires jsonschema: pip install -r requirements.txt",
          file=sys.stderr)
    raise SystemExit(78)


# =============================================================================
# Constants
# =============================================================================
#   ${VAR}            -> required; absent or empty is an error
#   ${VAR:default}    -> default when absent or empty
#   ${VAR:-default}   -> same, shell-style spelling
PLACEHOLDER_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-?((?:[^{}]|\$\{[^{}]*\})*))?\}"
)
ENV_OVERRIDE_PREFIX = "PACK__"
SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|master[_-]?key|secret|token|password|passwd|credential|authorization)",
    re.IGNORECASE,
)
REDACTED = "***redacted***"
BOOL_TRUE = {"true"}
BOOL_FALSE = {"false"}
INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?$|^-?\d+[eE][+-]?\d+$")
MAX_SUBSTITUTION_PASSES = 8

DEFAULT_BASE_CONFIG = Path("config") / "base.yaml"
DEFAULT_SCHEMA = Path("config") / "schema.json"
DEFAULT_ENV_DIR = Path("env")


# =============================================================================
# Error reporting
# =============================================================================
@dataclass(frozen=True)
class ConfigProblem:
    """One thing that is wrong, with enough context to fix it."""

    category: str          # missing_env | schema | reference | io | syntax
    where: str             # dotted config path, or a file path
    message: str
    hint: str = ""

    def render(self) -> str:
        line = f"[{self.category}] {self.where}: {self.message}"
        if self.hint:
            line += f"\n      hint: {self.hint}"
        return line


class PackConfigError(Exception):
    """Raised once, carrying every problem found during the whole load."""

    def __init__(self, problems: Iterable[ConfigProblem], *, station: str,
                 pack_dir: str) -> None:
        self.problems = list(problems)
        self.station = station
        self.pack_dir = pack_dir
        super().__init__(self._render())

    def _render(self) -> str:
        count = len(self.problems)
        header = (
            f"configuration is invalid: {count} problem"
            f"{'s' if count != 1 else ''} found\n"
            f"  pack    : {self.pack_dir}\n"
            f"  station : {self.station}"
        )
        body = "\n".join(
            f"  {i:>2}. {p.render()}" for i, p in enumerate(self.problems, 1)
        )
        return f"{header}\n{body}"

    def by_category(self) -> dict[str, list[ConfigProblem]]:
        grouped: dict[str, list[ConfigProblem]] = {}
        for problem in self.problems:
            grouped.setdefault(problem.category, []).append(problem)
        return grouped


@dataclass
class LoadMeta:
    """Where the configuration actually came from."""

    pack_dir: Path
    station: str
    base_file: Path
    overlay_file: Path | None
    overlay_is_example: bool
    schema_file: Path
    agent_files: dict[str, Path] = field(default_factory=dict)
    env_overrides: dict[str, str] = field(default_factory=dict)
    resolved_env_vars: list[str] = field(default_factory=list)
    defaulted_env_vars: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# =============================================================================
# Redaction
# =============================================================================
def redact(obj: Any, key: str = "") -> Any:
    """Replace secret-looking values so the result is safe to log or print."""
    if SECRET_KEY_RE.search(key) and isinstance(obj, (str, int, float)):
        if obj in ("", None):
            return obj
        return REDACTED
    if isinstance(obj, Mapping):
        return {k: redact(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, key) for v in obj]
    return obj


# =============================================================================
# IO helpers
# =============================================================================
def _read_yaml(path: Path, problems: list[ConfigProblem]) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(ConfigProblem("io", str(path), f"cannot read file: {exc}"))
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        problems.append(ConfigProblem("syntax", str(path), f"invalid YAML: {exc}"))
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        problems.append(
            ConfigProblem("syntax", str(path),
                          f"top level must be a mapping, got {type(data).__name__}")
        )
        return {}
    return data


def _read_json(path: Path, problems: list[ConfigProblem]) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        problems.append(ConfigProblem("io", str(path), f"cannot read file: {exc}"))
    except json.JSONDecodeError as exc:
        problems.append(ConfigProblem("syntax", str(path), f"invalid JSON: {exc}"))
    return {}


# =============================================================================
# Step 2 — deep merge
# =============================================================================
def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge `overlay` onto `base`.

    Mappings merge recursively. Lists and scalars replace wholesale, so a
    station can shrink a list (for example `agents.enabled`) rather than only
    ever growing it.
    """
    result: dict[str, Any] = dict(copy.deepcopy(dict(base)))
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# =============================================================================
# Step 3 — environment overrides
# =============================================================================
def _split_override_path(raw: str) -> list[str]:
    return [seg for seg in raw.split("__") if seg]


def _match_key(node: Mapping[str, Any], segment: str) -> str | None:
    """Find the real config key a `PACK__` path segment refers to.

    Environment variable names cannot contain hyphens, and Windows upper-cases
    every name in os.environ, so a segment is matched leniently: exact, then
    underscores as hyphens, then either of those case-insensitively. That makes
    both PACK__mcp__servers__tools_server__port and
    PACK__MCP__SERVERS__TOOLS_SERVER__PORT reach `mcp.servers.tools-server.port`.
    """
    candidates = (segment, segment.replace("_", "-"))
    for candidate in candidates:
        if candidate in node:
            return candidate
    lowered = {str(k).lower(): str(k) for k in node}
    for candidate in candidates:
        hit = lowered.get(candidate.lower())
        if hit is not None:
            return hit
    return None


def apply_env_overrides(
    cfg: dict[str, Any],
    environ: Mapping[str, str],
    meta: LoadMeta,
    problems: list[ConfigProblem],
) -> dict[str, Any]:
    """Apply `PACK__a__b__c=value` variables onto the merged config.

    An override must target a path that already exists. Inventing new keys is
    refused on purpose: the schema forbids unknown properties, so a typo would
    otherwise surface as a confusing "additional properties" error far from its
    cause instead of naming the offending variable.
    """
    out = copy.deepcopy(cfg)
    for env_name in sorted(environ):
        if not env_name.startswith(ENV_OVERRIDE_PREFIX):
            continue
        raw_value = environ[env_name]
        path = _split_override_path(env_name[len(ENV_OVERRIDE_PREFIX):])
        if not path:
            problems.append(
                ConfigProblem("missing_env", env_name,
                              "override name has no config path after the prefix",
                              hint=f"use {ENV_OVERRIDE_PREFIX}SECTION__KEY=value")
            )
            continue

        node: Any = out
        dotted: list[str] = []
        failed = False
        for segment in path[:-1]:
            if not isinstance(node, dict):
                problems.append(
                    ConfigProblem("missing_env", env_name,
                                  f"cannot descend into '{'.'.join(dotted)}': "
                                  f"it is not a mapping")
                )
                failed = True
                break
            key = _match_key(node, segment)
            if key is None:
                problems.append(
                    ConfigProblem(
                        "missing_env", env_name,
                        f"override targets unknown config path "
                        f"'{'.'.join([*dotted, segment])}'",
                        hint=f"keys available at "
                             f"'{'.'.join(dotted) or '<root>'}': "
                             f"{', '.join(sorted(map(str, node))) or '<none>'}",
                    )
                )
                failed = True
                break
            node = node[key]
            dotted.append(key)
        if failed:
            continue
        if not isinstance(node, dict):
            problems.append(
                ConfigProblem("missing_env", env_name,
                              f"'{'.'.join(dotted)}' is not a mapping")
            )
            continue

        leaf = _match_key(node, path[-1])
        if leaf is None:
            problems.append(
                ConfigProblem(
                    "missing_env", env_name,
                    f"override targets unknown config key "
                    f"'{'.'.join([*dotted, path[-1]])}'",
                    hint=f"keys available at '{'.'.join(dotted) or '<root>'}': "
                         f"{', '.join(sorted(map(str, node))) or '<none>'}",
                )
            )
            continue

        try:
            parsed = yaml.safe_load(raw_value)
        except yaml.YAMLError:
            parsed = raw_value
        node[leaf] = raw_value if parsed is None and raw_value != "" else parsed
        dotted.append(leaf)
        meta.env_overrides[env_name] = ".".join(dotted)
    return out


# =============================================================================
# Step 4 — ${VAR} substitution
# =============================================================================
def _coerce_scalar(text: str) -> Any:
    """Turn a fully substituted scalar into the type the schema expects.

    Only booleans and numbers are converted. Strings such as "none" (a valid
    SEARCH_PROVIDER) and "" (a meaningful empty override) are left alone.
    """
    stripped = text.strip()
    if stripped.lower() in BOOL_TRUE:
        return True
    if stripped.lower() in BOOL_FALSE:
        return False
    if INT_RE.match(stripped):
        try:
            return int(stripped)
        except ValueError:
            return text
    if FLOAT_RE.match(stripped):
        try:
            return float(stripped)
        except ValueError:
            return text
    return text


@dataclass
class _SubstState:
    environ: Mapping[str, str]
    problems: list[ConfigProblem]
    meta: LoadMeta
    tainted: set[str] = field(default_factory=set)


def _resolve_one(var: str, default: str | None, path: str,
                 state: _SubstState) -> tuple[str, bool]:
    """Return (value, resolved_ok) for a single placeholder."""
    raw = state.environ.get(var)
    if raw is not None and raw != "":
        if var not in state.meta.resolved_env_vars:
            state.meta.resolved_env_vars.append(var)
        return raw, True
    if default is not None:
        if var not in state.meta.defaulted_env_vars:
            state.meta.defaulted_env_vars.append(var)
        return default, True
    state.problems.append(
        ConfigProblem(
            "missing_env", path,
            f"environment variable {var} is required but is unset or empty",
            hint=f"export {var}=... or give it a default with "
                 f"\"${{{var}:<default>}}\" in config/base.yaml",
        )
    )
    state.tainted.add(path)
    return "", False


def _substitute_string(text: str, path: str, state: _SubstState) -> Any:
    matches = list(PLACEHOLDER_RE.finditer(text))
    if not matches:
        return text

    only = matches[0]
    is_full_match = len(matches) == 1 and only.span() == (0, len(text))

    def replace(match: re.Match[str]) -> str:
        value, _ = _resolve_one(match.group(1), match.group(2), path, state)
        return value

    result = text
    for _ in range(MAX_SUBSTITUTION_PASSES):
        new_result = PLACEHOLDER_RE.sub(replace, result)
        if new_result == result:
            break
        result = new_result

    # A placeholder that survives every pass is either self-referential or
    # malformed. Either way the value is not usable, and letting a literal
    # "${VAR}" reach the running services would fail far from its cause.
    if PLACEHOLDER_RE.search(result):
        state.problems.append(
            ConfigProblem(
                "syntax", path,
                "placeholder could not be fully resolved; a default probably "
                "references its own variable",
                hint=f"value {text!r} still contains a placeholder after "
                     f"{MAX_SUBSTITUTION_PASSES} passes: {result!r}",
            )
        )
        state.tainted.add(path)
        return ""

    return _coerce_scalar(result) if is_full_match else result


def substitute(node: Any, state: _SubstState, path: str = "") -> Any:
    if isinstance(node, Mapping):
        return {
            key: substitute(value, state, f"{path}.{key}" if path else str(key))
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [
            substitute(item, state, f"{path}[{index}]")
            for index, item in enumerate(node)
        ]
    if isinstance(node, str):
        return _substitute_string(node, path, state)
    return node


# =============================================================================
# Step 5 — agents
# =============================================================================
def _load_agents(
    cfg: dict[str, Any],
    pack_dir: Path,
    state: _SubstState,
    meta: LoadMeta,
) -> None:
    agents_cfg = cfg.get("agents")
    if not isinstance(agents_cfg, dict):
        state.problems.append(
            ConfigProblem("schema", "agents", "section is missing or not a mapping")
        )
        return

    enabled = agents_cfg.get("enabled") or []
    if not isinstance(enabled, list):
        state.problems.append(
            ConfigProblem("schema", "agents.enabled", "must be a list of agent names")
        )
        return

    agents_dir = pack_dir / str(agents_cfg.get("dir") or "./agents")
    defaults = agents_cfg.get("defaults") or {}
    default_shell = {
        "model": {
            "provider": defaults.get("provider", "litellm"),
            "params": defaults.get("params") or {},
        },
        "limits": defaults.get("limits") or {},
    }

    loaded: dict[str, Any] = {}
    for name in enabled:
        if not isinstance(name, str):
            state.problems.append(
                ConfigProblem("schema", "agents.enabled",
                              f"agent names must be strings, got {name!r}")
            )
            continue

        agent_path = agents_dir / f"{name}.yaml"
        if not agent_path.is_file():
            state.problems.append(
                ConfigProblem(
                    "io", f"agents.loaded.{name}",
                    f"agent file not found: {agent_path}",
                    hint=f"create {agent_path.name} or remove '{name}' from "
                         f"agents.enabled",
                )
            )
            continue

        raw = _read_yaml(agent_path, state.problems)
        if not raw:
            continue
        meta.agent_files[name] = agent_path

        resolved = substitute(raw, state, f"agents.loaded.{name}")
        merged = deep_merge(default_shell, resolved)
        merged.setdefault("name", name)

        prompt_ref = merged.get("system_prompt_file")
        if not prompt_ref:
            state.problems.append(
                ConfigProblem("schema", f"agents.loaded.{name}.system_prompt_file",
                              "is required so the agent has a system prompt")
            )
        else:
            prompt_path = pack_dir / str(prompt_ref)
            try:
                # Verbatim on purpose: prompts commonly contain JSON examples
                # with braces, and ${...} inside a prompt is prompt text, not a
                # configuration placeholder.
                merged["system_prompt"] = prompt_path.read_text(encoding="utf-8")
            except OSError as exc:
                state.problems.append(
                    ConfigProblem(
                        "io", f"agents.loaded.{name}.system_prompt_file",
                        f"cannot read prompt file {prompt_path}: {exc}",
                        hint=f"path is resolved relative to the pack root "
                             f"({pack_dir})",
                    )
                )

        loaded[name] = merged

    agents_cfg["loaded"] = loaded


# =============================================================================
# Step 6 — derived values
# =============================================================================
def _derive(cfg: dict[str, Any], problems: list[ConfigProblem]) -> None:
    gateway = cfg.get("gateway")
    if isinstance(gateway, dict):
        explicit = str(gateway.get("base_url") or "").strip()
        if explicit:
            gateway["base_url"] = explicit.rstrip("/")
        else:
            scheme = gateway.get("scheme") or "http"
            host = gateway.get("host")
            port = gateway.get("port")
            if host and port:
                gateway["base_url"] = f"{scheme}://{host}:{port}"
            else:
                problems.append(
                    ConfigProblem(
                        "reference", "gateway.base_url",
                        "cannot be derived because gateway.host or gateway.port "
                        "is missing",
                        hint="set GATEWAY_BASE_URL, or set GATEWAY_HOST and "
                             "GATEWAY_PORT",
                    )
                )

    servers = ((cfg.get("mcp") or {}).get("servers") or {})
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        if server.get("transport") == "http":
            explicit_url = str(server.get("url") or "").strip()
            if explicit_url:
                server["url"] = explicit_url
                continue
            scheme = server.get("scheme") or "http"
            host = server.get("host")
            port = server.get("port")
            mount = str(server.get("mount_path") or "/mcp")
            if host and port:
                server["url"] = f"{scheme}://{host}:{port}{mount}"
            else:
                problems.append(
                    ConfigProblem(
                        "reference", f"mcp.servers.{name}.url",
                        "cannot be derived because host or port is missing",
                        hint="set MCP_HOST and the server's port variable",
                    )
                )
        else:
            server["url"] = ""


# =============================================================================
# Step 7 — cross-reference checks
# =============================================================================
def _gateway_aliases(pack_dir: Path, cfg: dict[str, Any]) -> set[str] | None:
    """Alias names declared in the gateway config, or None if unreadable."""
    ref = (cfg.get("gateway") or {}).get("config_file")
    if not ref:
        return None
    path = pack_dir / str(ref)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    entries = data.get("model_list") or []
    return {
        str(entry.get("model_name"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("model_name")
    }


def _semantic_checks(cfg: dict[str, Any], pack_dir: Path, meta: LoadMeta,
                     problems: list[ConfigProblem]) -> None:
    servers = ((cfg.get("mcp") or {}).get("servers") or {})
    known_servers = set(servers)
    aliases = _gateway_aliases(pack_dir, cfg)

    for name, agent in ((cfg.get("agents") or {}).get("loaded") or {}).items():
        if not isinstance(agent, dict):
            continue

        # agent -> mcp server
        refs = agent.get("mcp_servers") or []
        for ref in refs:
            if ref not in known_servers:
                problems.append(
                    ConfigProblem(
                        "reference", f"agents.loaded.{name}.mcp_servers",
                        f"references unknown MCP server '{ref}'",
                        hint=f"known servers: {', '.join(sorted(known_servers)) or '<none>'}",
                    )
                )

        # agent -> tool
        available: set[str] = set()
        for ref in refs:
            server = servers.get(ref)
            if isinstance(server, dict):
                available.update(server.get("tools") or [])
        for tool in agent.get("tools") or []:
            if tool not in available:
                problems.append(
                    ConfigProblem(
                        "reference", f"agents.loaded.{name}.tools",
                        f"tool '{tool}' is not exposed by any server this agent "
                        f"is bound to",
                        hint=f"available here: {', '.join(sorted(available)) or '<none>'}",
                    )
                )

        # agent -> gateway alias
        if aliases:
            model = agent.get("model") or {}
            candidates = [model.get("alias"), *(model.get("fallback_aliases") or [])]
            for alias in [c for c in candidates if c]:
                if alias not in aliases:
                    problems.append(
                        ConfigProblem(
                            "reference", f"agents.loaded.{name}.model",
                            f"alias '{alias}' is not declared in the gateway config",
                            hint=f"declared aliases: {', '.join(sorted(aliases))}",
                        )
                    )

    # Soft checks -> warnings, not failures.
    gateway = cfg.get("gateway") or {}
    if gateway.get("profile") == "external" and not os.environ.get("GATEWAY_BASE_URL"):
        meta.warnings.append(
            "gateway.profile is 'external' but GATEWAY_BASE_URL is not set; "
            f"falling back to the derived URL {gateway.get('base_url')!r}, which "
            "points at the bundled service name."
        )
    allow = (gateway.get("enabled_aliases") or "").strip()
    if allow and aliases:
        requested = {a.strip() for a in allow.split(",") if a.strip()}
        unknown = requested - aliases
        if unknown:
            problems.append(
                ConfigProblem(
                    "reference", "gateway.enabled_aliases",
                    f"names alias(es) the gateway does not declare: "
                    f"{', '.join(sorted(unknown))}",
                    hint=f"declared aliases: {', '.join(sorted(aliases))}",
                )
            )
        for name, agent in ((cfg.get("agents") or {}).get("loaded") or {}).items():
            alias = (agent.get("model") or {}).get("alias")
            if alias and alias not in requested:
                meta.warnings.append(
                    f"agent '{name}' uses alias '{alias}', which is not in "
                    f"gateway.enabled_aliases; the gateway will not serve it."
                )


# =============================================================================
# Step 8 — schema validation
# =============================================================================
def _error_path(error: Any) -> str:
    parts: list[str] = []
    for token in error.absolute_path:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        else:
            parts.append(f".{token}" if parts else str(token))
    return "".join(parts) or "<root>"


def _validate(cfg: dict[str, Any], schema: dict[str, Any], tainted: set[str],
              problems: list[ConfigProblem]) -> None:
    if not schema:
        return
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001 - surfaced as a problem, not a crash
        problems.append(
            ConfigProblem("schema", str(DEFAULT_SCHEMA),
                          f"the schema itself is invalid: {exc}")
        )
        return

    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(cfg), key=lambda e: list(e.absolute_path)):
        path = _error_path(error)
        # A value left empty by a missing variable already produced a clear
        # missing_env problem; do not report the knock-on type error too.
        if path in tainted or any(path.startswith(f"{t}.") or path.startswith(f"{t}[")
                                  for t in tainted):
            continue
        problems.append(
            ConfigProblem(
                "schema", path, error.message,
                hint=(f"schema rule: {error.validator} = "
                      f"{json.dumps(error.validator_value, default=str)[:160]}"),
            )
        )


# =============================================================================
# Orchestration
# =============================================================================
def _resolve_overlay(pack_dir: Path, station: str,
                     problems: list[ConfigProblem]) -> tuple[Path | None, bool]:
    """Find env/<station>.yaml, falling back to the committed example."""
    env_dir = pack_dir / DEFAULT_ENV_DIR
    for candidate, is_example in (
        (env_dir / f"{station}.yaml", False),
        (env_dir / f"{station}.yml", False),
        (env_dir / f"{station}.example.yaml", True),
        (env_dir / f"{station}.example.yml", True),
    ):
        if candidate.is_file():
            return candidate, is_example
    available = sorted(p.name for p in env_dir.glob("*.y*ml")) if env_dir.is_dir() else []
    problems.append(
        ConfigProblem(
            "io", f"env/{station}.yaml",
            f"no overlay found for station '{station}'",
            hint=(f"available in {env_dir}: {', '.join(available) or '<none>'}; "
                  f"create it with: cp env/{station}.example.yaml "
                  f"env/{station}.yaml"),
        )
    )
    return None, False


def load_pack_with_meta(
    pack_dir: str | os.PathLike[str],
    station: str,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], LoadMeta]:
    """Load, merge, substitute, derive, cross-check and validate the pack config.

    Raises PackConfigError listing every problem found. Never raises for a
    single problem when more could be reported.
    """
    root = Path(pack_dir).resolve()
    env = dict(os.environ if environ is None else environ)
    problems: list[ConfigProblem] = []

    base_file = root / DEFAULT_BASE_CONFIG
    schema_file = root / DEFAULT_SCHEMA
    overlay_file, overlay_is_example = _resolve_overlay(root, station, problems)

    meta = LoadMeta(
        pack_dir=root,
        station=station,
        base_file=base_file,
        overlay_file=overlay_file,
        overlay_is_example=overlay_is_example,
        schema_file=schema_file,
    )
    if overlay_is_example and overlay_file is not None:
        meta.warnings.append(
            f"using the committed example overlay {overlay_file.name}; copy it to "
            f"env/{station}.yaml before treating this station as configured."
        )

    if not base_file.is_file():
        problems.append(
            ConfigProblem("io", str(base_file), "base configuration is missing",
                          hint="the pack is incomplete; restore config/base.yaml")
        )
        raise PackConfigError(problems, station=station, pack_dir=str(root))

    # 1-2: read and merge
    merged = _read_yaml(base_file, problems)
    if overlay_file is not None:
        merged = deep_merge(merged, _read_yaml(overlay_file, problems))

    # 3: environment overrides
    merged = apply_env_overrides(merged, env, meta, problems)

    # 4: substitution
    state = _SubstState(environ=env, problems=problems, meta=meta)
    merged = substitute(merged, state)

    # 5: agents
    _load_agents(merged, root, state, meta)

    # 6: derived values
    _derive(merged, problems)

    # 7: cross-references
    _semantic_checks(merged, root, meta, problems)

    # 8: schema
    schema = _read_json(schema_file, problems) if schema_file.is_file() else {}
    if not schema_file.is_file():
        problems.append(
            ConfigProblem("io", str(schema_file), "schema is missing",
                          hint="the pack is incomplete; restore config/schema.json")
        )
    _validate(merged, schema, state.tainted, problems)

    if problems:
        raise PackConfigError(problems, station=station, pack_dir=str(root))
    return merged, meta


def load_pack(
    pack_dir: str | os.PathLike[str],
    station: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Convenience wrapper returning only the validated configuration."""
    cfg, _ = load_pack_with_meta(pack_dir, station, environ)
    return cfg


# =============================================================================
# CLI
# =============================================================================
class _Palette:
    def __init__(self, enabled: bool) -> None:
        self.red = "\033[31m" if enabled else ""
        self.yellow = "\033[33m" if enabled else ""
        self.green = "\033[32m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.off = "\033[0m" if enabled else ""


def _summarize(cfg: dict[str, Any], meta: LoadMeta, palette: _Palette) -> str:
    gateway = cfg.get("gateway") or {}
    lines = [
        f"{palette.bold}pack{palette.off}     "
        f"{cfg.get('pack', {}).get('name')} "
        f"v{cfg.get('pack', {}).get('version')}",
        f"{palette.bold}station{palette.off}  {cfg.get('pack', {}).get('station')}",
        f"{palette.bold}base{palette.off}     {meta.base_file}",
        f"{palette.bold}overlay{palette.off}  "
        f"{meta.overlay_file}{' (example)' if meta.overlay_is_example else ''}",
        f"{palette.bold}gateway{palette.off}  "
        f"{gateway.get('base_url')} profile={gateway.get('profile')} "
        f"key={REDACTED if gateway.get('api_key') else '<unset>'}",
    ]
    for name, server in ((cfg.get("mcp") or {}).get("servers") or {}).items():
        target = server.get("url") or " ".join(server.get("command") or [])
        lines.append(
            f"{palette.bold}mcp{palette.off}      {name} "
            f"transport={server.get('transport')} -> {target}"
        )
    for name, agent in ((cfg.get("agents") or {}).get("loaded") or {}).items():
        model = agent.get("model") or {}
        prompt_chars = len(agent.get("system_prompt") or "")
        lines.append(
            f"{palette.bold}agent{palette.off}    {name:<12} "
            f"alias={model.get('alias'):<22} "
            f"tools={','.join(agent.get('tools') or []) or '-':<28} "
            f"prompt={prompt_chars}c"
        )
    if meta.env_overrides:
        for env_name, path in meta.env_overrides.items():
            lines.append(f"{palette.dim}override {env_name} -> {path}{palette.off}")
    lines.append(
        f"{palette.dim}env vars: {len(meta.resolved_env_vars)} from environment, "
        f"{len(meta.defaulted_env_vars)} defaulted{palette.off}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="config_loader.py",
        description="Load, merge and validate the Agent Pack configuration.",
        epilog=(
            "exit codes: 0 valid, 2 invalid configuration, 3 unexpected error.\n"
            "example: python config_loader.py --pack . --station local --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pack", default=".",
                        help="path to the pack root (default: current directory)")
    parser.add_argument("--station", default=os.environ.get("STATION", "local"),
                        help="station overlay to load from env/ (default: $STATION or 'local')")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and print the merged config, change nothing")
    parser.add_argument("--format", choices=["summary", "yaml", "json"],
                        default="summary",
                        help="output shape for --dry-run (default: summary)")
    parser.add_argument("--strict-warnings", action="store_true",
                        help="treat warnings as failures (exit 2)")
    parser.add_argument("--no-color", action="store_true", help="disable colored output")
    args = parser.parse_args(argv)

    palette = _Palette(
        enabled=sys.stderr.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
    )

    try:
        cfg, meta = load_pack_with_meta(args.pack, args.station)
    except PackConfigError as exc:
        grouped = exc.by_category()
        print(f"{palette.red}FAIL{palette.off} {len(exc.problems)} problem(s) in "
              f"{exc.pack_dir} (station: {exc.station})", file=sys.stderr)
        for category in sorted(grouped):
            print(f"\n{palette.bold}{category}{palette.off} "
                  f"({len(grouped[category])})", file=sys.stderr)
            for i, problem in enumerate(grouped[category], 1):
                print(f"  {i:>2}. {problem.where}: {problem.message}", file=sys.stderr)
                if problem.hint:
                    print(f"      {palette.dim}hint: {problem.hint}{palette.off}",
                          file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI must not dump a traceback
        print(f"{palette.red}ERROR{palette.off} unexpected failure: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    for warning in meta.warnings:
        print(f"{palette.yellow}WARN{palette.off} {warning}", file=sys.stderr)

    print(f"{palette.green}OK{palette.off} configuration is valid "
          f"({len(meta.agent_files)} agent(s), "
          f"{len((cfg.get('mcp') or {}).get('servers') or {})} MCP server(s))",
          file=sys.stderr)

    if args.dry_run:
        safe = redact(cfg)
        if args.format == "json":
            print(json.dumps(safe, indent=2, default=str))
        elif args.format == "yaml":
            print(yaml.safe_dump(safe, sort_keys=False, default_flow_style=False,
                                 allow_unicode=True))
        else:
            print(_summarize(cfg, meta, palette))

    if args.strict_warnings and meta.warnings:
        print(f"{palette.red}FAIL{palette.off} --strict-warnings: "
              f"{len(meta.warnings)} warning(s)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
