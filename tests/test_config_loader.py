#!/usr/bin/env python3
# =============================================================================
# Tests for config_loader.py
#
#   TestMechanics  — a tiny synthetic pack with a tiny schema, so each behaviour
#                    (merge order, substitution forms, overrides, coercion,
#                    redaction, error aggregation) is asserted in isolation.
#   TestRealPack   — the actual pack in this repository, so the shipped
#                    base.yaml / schema.json / agents / prompts are proven to
#                    load and to fail correctly.
#
# Run:  python -m pytest tests/ -q
# =============================================================================
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

PACK_ROOT = Path(__file__).resolve().parents[1]
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from config_loader import (  # noqa: E402
    PackConfigError,
    apply_env_overrides,
    deep_merge,
    load_pack,
    load_pack_with_meta,
    redact,
)

MASTER_KEY = "unit-test-master-key"


# =============================================================================
# Synthetic pack fixture
# =============================================================================
MINI_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["pack", "gateway", "mcp", "agents", "orchestrator"],
    "properties": {
        "pack": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "version", "station"],
            "properties": {
                "name": {"type": "string"},
                "version": {"type": "string"},
                "station": {"type": "string"},
            },
        },
        "gateway": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scheme", "host", "port", "base_url", "api_key", "config_file"],
            "properties": {
                "scheme": {"type": "string", "enum": ["http", "https"]},
                "host": {"type": "string"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "base_url": {"type": "string", "pattern": "^https?://"},
                "api_key": {"type": "string", "minLength": 8},
                "config_file": {"type": "string"},
                "profile": {"type": "string", "enum": ["bundled", "external"]},
                "enabled_aliases": {"type": "string"},
                "debug": {"type": "boolean"},
                "ratio": {"type": "number"},
                # A free-form string field with no cross-reference checks, so
                # coercion behaviour can be asserted without tripping the
                # alias validator.
                "label": {"type": "string"},
            },
        },
        "mcp": {
            "type": "object",
            "additionalProperties": False,
            "required": ["servers"],
            "properties": {
                "servers": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["transport", "tools"],
                        "properties": {
                            "transport": {"type": "string", "enum": ["stdio", "http"]},
                            "scheme": {"type": "string"},
                            "host": {"type": "string"},
                            "port": {"type": "integer"},
                            "mount_path": {"type": "string"},
                            "url": {"type": "string"},
                            "command": {"type": "array", "items": {"type": "string"}},
                            "cwd": {"type": "string"},
                            "enabled": {"type": "boolean"},
                            "tools": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                }
            },
        },
        "agents": {
            "type": "object",
            "additionalProperties": False,
            "required": ["dir", "prompts_dir", "enabled", "defaults", "loaded"],
            "properties": {
                "dir": {"type": "string"},
                "prompts_dir": {"type": "string"},
                "enabled": {"type": "array", "items": {"type": "string"}},
                "defaults": {"type": "object"},
                "loaded": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "required": ["name", "model", "system_prompt", "tools"],
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "model": {"type": "object"},
                            "system_prompt_file": {"type": "string"},
                            "system_prompt": {"type": "string"},
                            "mcp_servers": {"type": "array"},
                            "tools": {"type": "array"},
                            "limits": {"type": "object"},
                            "output_contract": {"type": "string"},
                        },
                    },
                },
            },
        },
        "orchestrator": {
            "type": "object",
            "additionalProperties": False,
            "required": ["port"],
            "properties": {
                "port": {"type": "integer"},
                "bind_host": {"type": "string"},
            },
        },
    },
}

MINI_BASE = {
    "pack": {"name": "mini", "version": "0.1.0", "station": "${STATION:local}"},
    "gateway": {
        "profile": "${GATEWAY_PROFILE:bundled}",
        "scheme": "${GATEWAY_SCHEME:http}",
        "host": "${GATEWAY_HOST:gateway}",
        "port": "${GATEWAY_PORT:4000}",
        "base_url": "${GATEWAY_BASE_URL:}",
        "api_key": "${GATEWAY_MASTER_KEY}",
        "config_file": "./gateway/litellm_config.yaml",
        "enabled_aliases": "${GATEWAY_ENABLED_ALIASES:}",
        "debug": "${GATEWAY_DEBUG:false}",
        "ratio": "${GATEWAY_RATIO:1.5}",
    },
    "mcp": {
        "servers": {
            "tools-server": {
                "enabled": "${TOOLS_ENABLED:true}",
                "transport": "${MCP_TRANSPORT:stdio}",
                "scheme": "http",
                "host": "${MCP_HOST:localhost}",
                "port": "${MCP_PORT:8081}",
                "mount_path": "/mcp",
                "url": "",
                "command": ["python", "server.py"],
                "cwd": ".",
                "tools": ["alpha", "beta"],
            }
        }
    },
    "agents": {
        "dir": "./agents",
        "prompts_dir": "./prompts",
        "enabled": ["solo"],
        "defaults": {
            "provider": "litellm",
            "params": {"temperature": "${DEFAULT_TEMP:0.2}"},
            "limits": {"timeout_seconds": 60},
        },
        "loaded": {},
    },
    "orchestrator": {"bind_host": "0.0.0.0", "port": "${ORCHESTRATOR_PORT:8080}"},
}

MINI_AGENT = {
    "name": "solo",
    "role": "test",
    "model": {"alias": "${SOLO_ALIAS:alias-one}"},
    "system_prompt_file": "./prompts/solo.md",
    "mcp_servers": ["tools-server"],
    "tools": ["alpha"],
    "output_contract": "json",
}

MINI_GATEWAY_CFG = {
    "model_list": [
        {"model_name": "alias-one", "litellm_params": {"model": "os.environ/X"}},
        {"model_name": "alias-two", "litellm_params": {"model": "os.environ/Y"}},
    ]
}

# A prompt containing braces and a ${...} sequence: it must be inlined verbatim,
# never treated as a configuration placeholder.
MINI_PROMPT = 'Return {"ok": true}. Literal ${NOT_A_VAR} stays as written.\n'


@pytest.fixture()
def mini_pack(tmp_path: Path) -> Path:
    """A minimal but complete pack on disk."""
    (tmp_path / "config").mkdir()
    (tmp_path / "env").mkdir()
    (tmp_path / "agents").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "gateway").mkdir()

    (tmp_path / "config" / "base.yaml").write_text(
        yaml.safe_dump(MINI_BASE), encoding="utf-8")
    (tmp_path / "config" / "schema.json").write_text(
        json.dumps(MINI_SCHEMA), encoding="utf-8")
    (tmp_path / "env" / "unit.yaml").write_text(
        yaml.safe_dump({"pack": {"station": "unit"}}), encoding="utf-8")
    (tmp_path / "agents" / "solo.yaml").write_text(
        yaml.safe_dump(MINI_AGENT), encoding="utf-8")
    (tmp_path / "prompts" / "solo.md").write_text(MINI_PROMPT, encoding="utf-8")
    (tmp_path / "gateway" / "litellm_config.yaml").write_text(
        yaml.safe_dump(MINI_GATEWAY_CFG), encoding="utf-8")
    return tmp_path


def base_env(**extra: str) -> dict[str, str]:
    env = {"GATEWAY_MASTER_KEY": MASTER_KEY}
    env.update(extra)
    return env


def write_overlay(pack: Path, station: str, data: dict) -> None:
    (pack / "env" / f"{station}.yaml").write_text(
        yaml.safe_dump(data), encoding="utf-8")


def categories(exc: PackConfigError) -> set[str]:
    return {p.category for p in exc.problems}


def messages(exc: PackConfigError) -> str:
    """Full report text, hints included — hints carry the actionable detail."""
    return "\n".join(f"{p.where}: {p.message} {p.hint}" for p in exc.problems)


# =============================================================================
class TestDeepMerge:
    def test_nested_mappings_merge(self) -> None:
        merged = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})
        assert merged == {"a": {"b": 1, "c": 3}}

    def test_lists_replace_so_a_station_can_shrink_them(self) -> None:
        merged = deep_merge({"enabled": ["a", "b", "c"]}, {"enabled": ["a"]})
        assert merged["enabled"] == ["a"]

    def test_inputs_are_not_mutated(self) -> None:
        base = {"a": {"b": [1, 2]}}
        deep_merge(base, {"a": {"b": [9]}})
        assert base == {"a": {"b": [1, 2]}}


# =============================================================================
class TestSubstitution:
    def test_required_var_is_taken_from_the_environment(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["gateway"]["api_key"] == MASTER_KEY

    def test_default_is_used_when_var_is_unset(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["gateway"]["host"] == "gateway"

    def test_environment_beats_default(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env(GATEWAY_HOST="elsewhere"))
        assert cfg["gateway"]["host"] == "elsewhere"

    def test_empty_env_value_falls_back_to_default(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env(GATEWAY_HOST=""))
        assert cfg["gateway"]["host"] == "gateway"

    def test_shell_style_dash_default_is_accepted(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"gateway": {"host": "${SOME_HOST:-dashed}"}})
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["gateway"]["host"] == "dashed"

    def test_placeholder_inside_a_longer_string_is_interpolated(
            self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"gateway": {"host": "prefix-${GATEWAY_HOST:mid}-suffix"}})
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["gateway"]["host"] == "prefix-mid-suffix"

    def test_missing_required_var_is_reported_with_its_path(
            self, mini_pack: Path) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", {})
        assert "missing_env" in categories(caught.value)
        assert "gateway.api_key" in messages(caught.value)
        assert "GATEWAY_MASTER_KEY" in messages(caught.value)

    def test_missing_var_does_not_also_raise_a_schema_error(
            self, mini_pack: Path) -> None:
        # api_key has minLength 8; the empty substitution would violate it, but
        # that knock-on error must be suppressed in favour of the real cause.
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", {})
        assert len(caught.value.problems) == 1
        assert caught.value.problems[0].category == "missing_env"

    def test_self_referential_value_is_caught_not_leaked(
            self, mini_pack: Path) -> None:
        # LOOP expands to a placeholder that expands to itself: substitution
        # reaches a fixed point that is still a placeholder. That must be an
        # error, not a literal "${LOOP:again}" leaking into the config.
        write_overlay(mini_pack, "unit", {"gateway": {"label": "${LOOP:x}"}})
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env(LOOP="${LOOP:again}"))
        assert "syntax" in categories(caught.value)
        assert "could not be fully resolved" in messages(caught.value)

    def test_unresolved_placeholder_never_reaches_the_config(
            self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env())
        assert "${" not in json.dumps(
            {k: v for k, v in cfg.items() if k != "agents"}, default=str
        )


# =============================================================================
class TestCoercion:
    def test_integers_become_integers(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env(GATEWAY_PORT="4321"))
        assert cfg["gateway"]["port"] == 4321
        assert isinstance(cfg["gateway"]["port"], int)

    def test_booleans_become_booleans(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env(GATEWAY_DEBUG="true"))
        assert cfg["gateway"]["debug"] is True

    def test_floats_become_floats(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env(GATEWAY_RATIO="2.75"))
        assert cfg["gateway"]["ratio"] == pytest.approx(2.75)

    def test_word_like_values_stay_strings(self, mini_pack: Path) -> None:
        # "none" must not become None: it is a valid SEARCH_PROVIDER value.
        write_overlay(mini_pack, "unit", {"gateway": {"label": "${LBL:none}"}})
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["gateway"]["label"] == "none"

    def test_null_like_values_stay_strings(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit", {"gateway": {"label": "${LBL:null}"}})
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["gateway"]["label"] == "null"

    def test_interpolated_string_is_not_coerced(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"gateway": {"label": "n=${GATEWAY_PORT:7}"}})
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["gateway"]["label"] == "n=7"


# =============================================================================
class TestEnvOverrides:
    def test_override_reaches_a_nested_path(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit",
                        base_env(PACK__ORCHESTRATOR__PORT="9099"))
        assert cfg["orchestrator"]["port"] == 9099

    def test_underscores_match_a_hyphenated_key(self, mini_pack: Path) -> None:
        cfg = load_pack(
            mini_pack, "unit",
            base_env(PACK__MCP__SERVERS__TOOLS_SERVER__TRANSPORT="http",
                     PACK__MCP__SERVERS__TOOLS_SERVER__HOST="remote-host"),
        )
        server = cfg["mcp"]["servers"]["tools-server"]
        assert server["transport"] == "http"
        assert server["host"] == "remote-host"

    def test_lowercase_spelling_also_matches(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit",
                        base_env(PACK__orchestrator__port="7007"))
        assert cfg["orchestrator"]["port"] == 7007

    def test_override_wins_over_overlay_and_base(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"pack": {"station": "unit"}, "orchestrator": {"port": 1111}})
        cfg = load_pack(mini_pack, "unit",
                        base_env(PACK__ORCHESTRATOR__PORT="2222"))
        assert cfg["orchestrator"]["port"] == 2222

    def test_unknown_path_names_the_variable_and_lists_valid_keys(
            self, mini_pack: Path) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env(PACK__ORCHESTRATOR__PORTT="1"))
        report = messages(caught.value)
        assert "PACK__ORCHESTRATOR__PORTT" in report
        assert "bind_host" in report  # the hint enumerates real keys

    def test_apply_env_overrides_is_pure(self) -> None:
        from config_loader import LoadMeta

        cfg = {"a": {"b": 1}}
        meta = LoadMeta(pack_dir=Path("."), station="s", base_file=Path("b"),
                        overlay_file=None, overlay_is_example=False,
                        schema_file=Path("s"))
        out = apply_env_overrides(cfg, {"PACK__A__B": "2"}, meta, [])
        assert cfg == {"a": {"b": 1}}
        assert out == {"a": {"b": 2}}
        assert meta.env_overrides == {"PACK__A__B": "a.b"}


# =============================================================================
class TestPrecedence:
    def test_overlay_overrides_base(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"pack": {"station": "unit"}, "gateway": {"host": "from-overlay"}})
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["gateway"]["host"] == "from-overlay"

    def test_full_chain_base_then_overlay_then_environ(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"pack": {"station": "unit"}, "orchestrator": {"port": 1111}})
        # base 8080 -> overlay 1111 -> override 3333
        assert load_pack(mini_pack, "unit", base_env())["orchestrator"]["port"] == 1111
        cfg = load_pack(mini_pack, "unit", base_env(PACK__ORCHESTRATOR__PORT="3333"))
        assert cfg["orchestrator"]["port"] == 3333

    def test_missing_overlay_falls_back_to_example_with_a_warning(
            self, mini_pack: Path) -> None:
        (mini_pack / "env" / "staging.example.yaml").write_text(
            yaml.safe_dump({"pack": {"station": "staging"}}), encoding="utf-8")
        cfg, meta = load_pack_with_meta(mini_pack, "staging", base_env())
        assert cfg["pack"]["station"] == "staging"
        assert meta.overlay_is_example is True
        assert any("example" in w for w in meta.warnings)

    def test_unknown_station_lists_what_is_available(self, mini_pack: Path) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "nosuch", base_env())
        report = messages(caught.value)
        assert "nosuch" in report
        assert "unit.yaml" in report


# =============================================================================
class TestDerivedValues:
    def test_gateway_base_url_is_derived_from_parts(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit",
                        base_env(GATEWAY_HOST="gw", GATEWAY_PORT="4100"))
        assert cfg["gateway"]["base_url"] == "http://gw:4100"

    def test_explicit_base_url_wins_and_is_stripped(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit",
                        base_env(GATEWAY_BASE_URL="https://gw.example/"))
        assert cfg["gateway"]["base_url"] == "https://gw.example"

    def test_http_server_url_is_derived(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit",
                        base_env(MCP_TRANSPORT="http", MCP_HOST="ts",
                                 MCP_PORT="8181"))
        assert cfg["mcp"]["servers"]["tools-server"]["url"] == "http://ts:8181/mcp"

    def test_stdio_server_url_is_blank(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env())
        assert cfg["mcp"]["servers"]["tools-server"]["url"] == ""


# =============================================================================
class TestAgentLoading:
    def test_agent_file_is_loaded_and_defaults_applied(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env())
        agent = cfg["agents"]["loaded"]["solo"]
        assert agent["name"] == "solo"
        assert agent["model"]["alias"] == "alias-one"
        assert agent["model"]["provider"] == "litellm"          # from defaults
        assert agent["model"]["params"]["temperature"] == 0.2   # from defaults
        assert agent["limits"]["timeout_seconds"] == 60         # from defaults

    def test_agent_alias_is_overridable_per_station(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env(SOLO_ALIAS="alias-two"))
        assert cfg["agents"]["loaded"]["solo"]["model"]["alias"] == "alias-two"

    def test_prompt_file_is_inlined_verbatim(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env())
        prompt = cfg["agents"]["loaded"]["solo"]["system_prompt"]
        assert prompt == MINI_PROMPT
        # Braces and a ${...} sequence in prompt text survive untouched.
        assert '{"ok": true}' in prompt
        assert "${NOT_A_VAR}" in prompt

    def test_missing_agent_file_is_reported(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"pack": {"station": "unit"},
                       "agents": {"enabled": ["solo", "ghost"]}})
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env())
        assert "ghost.yaml" in messages(caught.value)

    def test_missing_prompt_file_is_reported(self, mini_pack: Path) -> None:
        (mini_pack / "prompts" / "solo.md").unlink()
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env())
        assert "system_prompt_file" in messages(caught.value)


# =============================================================================
class TestCrossReferences:
    def test_tool_not_exposed_by_the_bound_server_is_rejected(
            self, mini_pack: Path) -> None:
        agent = dict(MINI_AGENT, tools=["alpha", "gamma"])
        (mini_pack / "agents" / "solo.yaml").write_text(
            yaml.safe_dump(agent), encoding="utf-8")
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env())
        report = messages(caught.value)
        assert "gamma" in report
        assert "reference" in categories(caught.value)

    def test_unknown_mcp_server_is_rejected(self, mini_pack: Path) -> None:
        agent = dict(MINI_AGENT, mcp_servers=["nope"])
        (mini_pack / "agents" / "solo.yaml").write_text(
            yaml.safe_dump(agent), encoding="utf-8")
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env())
        assert "nope" in messages(caught.value)

    def test_alias_absent_from_the_gateway_is_rejected(self, mini_pack: Path) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env(SOLO_ALIAS="alias-nine"))
        report = messages(caught.value)
        assert "alias-nine" in report
        assert "alias-one" in report  # hint lists the declared aliases

    def test_enabled_aliases_typo_is_rejected(self, mini_pack: Path) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit",
                      base_env(GATEWAY_ENABLED_ALIASES="alias-one,alias-typo"))
        assert "alias-typo" in messages(caught.value)


# =============================================================================
class TestSchemaValidation:
    def test_bad_enum_is_rejected(self, mini_pack: Path) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env(GATEWAY_SCHEME="ftp"))
        assert "schema" in categories(caught.value)
        assert "gateway.scheme" in messages(caught.value)

    def test_out_of_range_port_is_rejected(self, mini_pack: Path) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env(GATEWAY_PORT="99999"))
        assert "gateway.port" in messages(caught.value)

    def test_unknown_key_is_rejected(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"pack": {"station": "unit"}, "surprise": {"x": 1}})
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", base_env())
        assert "schema" in categories(caught.value)


# =============================================================================
class TestErrorAggregation:
    def test_every_problem_is_reported_in_one_raise(self, mini_pack: Path) -> None:
        write_overlay(mini_pack, "unit",
                      {"pack": {"station": "unit"}, "junk_key": True})
        with pytest.raises(PackConfigError) as caught:
            load_pack(
                mini_pack, "unit",
                {  # no GATEWAY_MASTER_KEY, bad scheme, bad port, bad alias
                    "GATEWAY_SCHEME": "ftp",
                    "GATEWAY_PORT": "70000",
                    "SOLO_ALIAS": "alias-missing",
                },
            )
        exc = caught.value
        assert len(exc.problems) >= 4
        assert {"missing_env", "schema", "reference"} <= categories(exc)
        # The report is one readable block naming the pack and the station.
        rendered = str(exc)
        assert "problems found" in rendered
        assert "station : unit" in rendered

    def test_by_category_groups_problems(self, mini_pack: Path) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(mini_pack, "unit", {"GATEWAY_SCHEME": "ftp"})
        grouped = caught.value.by_category()
        assert set(grouped) == {"missing_env", "schema"}


# =============================================================================
class TestRedaction:
    def test_secret_keys_are_replaced(self) -> None:
        safe = redact({"api_key": "sk-abc", "master_key": "m", "token": "t",
                       "password": "p", "authorization": "Bearer x"})
        assert all(v == "***redacted***" for v in safe.values())

    def test_non_secret_values_are_untouched(self) -> None:
        safe = redact({"host": "gateway", "port": 4000, "keyboard": "n/a"})
        assert safe == {"host": "gateway", "port": 4000, "keyboard": "n/a"}

    def test_redaction_reaches_nested_structures(self) -> None:
        safe = redact({"a": {"b": [{"api_key": "x"}]}, "list": ["plain"]})
        assert safe["a"]["b"][0]["api_key"] == "***redacted***"
        assert safe["list"] == ["plain"]

    def test_empty_secret_stays_empty_so_unset_is_visible(self) -> None:
        assert redact({"api_key": ""}) == {"api_key": ""}

    def test_loaded_config_can_be_redacted_for_logging(self, mini_pack: Path) -> None:
        cfg = load_pack(mini_pack, "unit", base_env())
        safe = redact(cfg)
        assert cfg["gateway"]["api_key"] == MASTER_KEY      # original untouched
        assert safe["gateway"]["api_key"] == "***redacted***"
        assert MASTER_KEY not in json.dumps(safe)


# =============================================================================
class TestMetadata:
    def test_meta_records_sources_and_env_usage(self, mini_pack: Path) -> None:
        _, meta = load_pack_with_meta(mini_pack, "unit",
                                      base_env(GATEWAY_HOST="gw"))
        assert meta.station == "unit"
        assert meta.base_file.name == "base.yaml"
        assert meta.overlay_file is not None and meta.overlay_file.name == "unit.yaml"
        assert meta.overlay_is_example is False
        assert "solo" in meta.agent_files
        assert "GATEWAY_HOST" in meta.resolved_env_vars
        assert "GATEWAY_PORT" in meta.defaulted_env_vars


# =============================================================================
class TestRealPack:
    """The pack shipped in this repository must load as-is."""

    def test_local_station_loads(self) -> None:
        cfg = load_pack(PACK_ROOT, "local", base_env())
        assert cfg["pack"]["name"] == "pnp-agents-mcp"
        assert cfg["pack"]["station"] == "local"
        assert set(cfg["agents"]["loaded"]) == {"researcher", "coder", "reviewer"}
        assert cfg["mcp"]["servers"]["tools-server"]["transport"] == "stdio"

    def test_prod_station_loads_with_http_transport(self) -> None:
        cfg = load_pack(PACK_ROOT, "prod", base_env())
        server = cfg["mcp"]["servers"]["tools-server"]
        assert server["transport"] == "http"
        assert server["url"].startswith("http://")
        assert server["url"].endswith("/mcp")

    @pytest.mark.parametrize("station", ["local", "prod"])
    def test_every_agent_has_a_prompt_and_an_alias(self, station: str) -> None:
        cfg = load_pack(PACK_ROOT, station, base_env())
        for name, agent in cfg["agents"]["loaded"].items():
            assert agent["system_prompt"].strip(), f"{name} has an empty prompt"
            assert agent["model"]["alias"], f"{name} has no alias"
            assert agent["output_contract"] in {"json", "text"}

    @pytest.mark.parametrize("station", ["local", "prod"])
    def test_master_key_is_the_only_hard_requirement(self, station: str) -> None:
        with pytest.raises(PackConfigError) as caught:
            load_pack(PACK_ROOT, station, {})
        missing = [p for p in caught.value.problems if p.category == "missing_env"]
        assert len(missing) == 1
        assert "GATEWAY_MASTER_KEY" in missing[0].message

    def test_agent_aliases_all_exist_in_the_gateway_config(self) -> None:
        gateway_cfg = yaml.safe_load(
            (PACK_ROOT / "gateway" / "litellm_config.yaml").read_text(encoding="utf-8")
        )
        declared = {m["model_name"] for m in gateway_cfg["model_list"]}
        cfg = load_pack(PACK_ROOT, "local", base_env())
        for name, agent in cfg["agents"]["loaded"].items():
            model = agent["model"]
            for alias in [model["alias"], *model.get("fallback_aliases", [])]:
                assert alias in declared, f"{name} references unknown alias {alias}"

    def test_agent_tools_are_exposed_by_the_tools_server(self) -> None:
        cfg = load_pack(PACK_ROOT, "local", base_env())
        exposed = set(cfg["mcp"]["servers"]["tools-server"]["tools"])
        for name, agent in cfg["agents"]["loaded"].items():
            assert set(agent["tools"]) <= exposed, f"{name} wants a missing tool"

    def test_shipped_schema_is_itself_valid(self) -> None:
        from jsonschema import Draft202012Validator

        schema = json.loads(
            (PACK_ROOT / "config" / "schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)

    def test_redacted_config_never_contains_the_key(self) -> None:
        cfg = load_pack(PACK_ROOT, "prod", base_env())
        assert MASTER_KEY not in json.dumps(redact(cfg), default=str)
