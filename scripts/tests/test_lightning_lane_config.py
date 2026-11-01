# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2026 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""Check opt-in lane controls and reversible migration."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from scripts.tests.test_deploy_migration import cached_package, entry, fixture, run
from scripts.tests.test_lightning_config import params

from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.models import FastParams

ROOT = Path(__file__).resolve().parents[2]
CONTROLS = {
    "lightning_lane": ("bool", False),
    "lightning_rpc_url": ("str", ""),
    "lightning_poll_offset_s": ("float", 0.3),
    "lightning_poll_step_s": ("float", 0.25),
    "lightning_lane_max_age_s": ("int", 6),
    "lightning_lane_gas_limit": ("int", 400000),
    "lightning_lane_tip_gwei": ("float", 0.0),
    "lightning_lane_tip_cap_gwei": ("float", 1000.0),
    "lightning_lane_inflight_timeout_s": ("int", 60),
    "lightning_lane_max_per_night": ("int", -1),
}


@pytest.mark.parametrize(
    "relative",
    [
        "skills/fresh_watch_abci/skill.yaml",
        "skills/omenstrat_fast_abci/skill.yaml",
        "agents/omenstrat_fast/aea-config.yaml",
        "agents/omenstrat_lightning/aea-config.yaml",
        "services/omenstrat_fast/service.yaml",
        "services/omenstrat_lightning/service.yaml",
    ],
)
def test_lane_controls_declared_off(relative):
    """Keep the direct lane off in each deployment entry point."""
    args = params(relative)
    for key, (kind, default) in CONTROLS.items():
        if relative.startswith("skills/"):
            assert args[key] == default
        else:
            encoded = "null" if key == "lightning_rpc_url" else json.dumps(default)
            assert args[key] == "${" + key.upper() + ":" + kind + ":" + encoded + "}"


@pytest.mark.parametrize("name", ["omenstrat_fast", "omenstrat_lightning"])
def test_lane_quickstart_defaults(name):
    """Require an explicit opt-in, endpoint, and fixed tip."""
    env = json.loads((ROOT / f"configs/config_{name}.json").read_text())[
        "env_variables"
    ]
    for key, (_, default) in CONTROLS.items():
        raw = env[key.upper()]["value"]
        assert (raw if isinstance(default, str) else json.loads(raw)) == default
    assert env["LIGHTNING_RPC_URL"]["is_secret"] is True


def test_lane_params_resolve_empty_endpoint_and_trade_cap(tmp_path):
    """Use MAX_TRADES when no separate shot budget is set."""
    args = params("skills/omenstrat_fast_abci/skill.yaml")
    args.update(store_path=str(tmp_path), max_trades=7, lightning_rpc_url=None)
    model = FastParams(name="params", skill_context=MagicMock(), **args)
    assert model.lightning.lightning_lane is False
    assert model.lightning.lightning_rpc_url == ""
    assert model.lightning.lightning_lane_max_per_night == 7


@pytest.mark.parametrize(
    "overrides",
    [
        {"lightning_lane_tip_gwei": 1001.0},
        {"lightning_lane_tip_cap_gwei": 2001.0},
        {"lightning_lane_tip_gwei": -1.0},
        {"lightning_lane_tip_gwei": float("nan")},
        {"lightning_poll_step_s": 0.0},
        {"lightning_poll_offset_s": -0.1},
        {"lightning_lane_gas_limit": 0},
        {"lightning_lane_max_age_s": 0},
        {"lightning_lane_inflight_timeout_s": 0},
        {"lightning_lane_max_per_night": -2},
    ],
)
def test_lane_rejects_invalid_controls(overrides):
    """Reject unsafe fees and unusable polling bounds at startup."""
    with pytest.raises(ValueError, match="[Ll]ane"):
        LightningConfig(**overrides)


@pytest.mark.parametrize("existing", [False, True])
def test_lane_migration_records_missing_defaults_and_rollback(tmp_path, existing):
    """Preserve explicit lane controls until rollback removes them."""
    target = fixture(tmp_path)[0]
    config = json.loads(target.read_text())
    if existing:
        config["env_variables"]["LIGHTNING_LANE"] = entry("true")
        config["env_variables"]["LIGHTNING_RPC_URL"] = entry(
            "https://key.invalid/private"
        )
    target.write_text(json.dumps(config))
    output = run(tmp_path, "--service", "omenstrat_lightning")
    migrated = json.loads(target.read_text())["env_variables"]
    assert migrated["LIGHTNING_LANE"]["value"] == ("true" if existing else "false")
    assert migrated["LIGHTNING_RPC_URL"]["value"] == (
        "https://key.invalid/private" if existing else ""
    )
    record = json.loads(
        (tmp_path / "configs/.lightning_defaults_added.json").read_text()
    )
    expected = {key.upper() for key in CONTROLS}
    if existing:
        expected -= {"LIGHTNING_LANE", "LIGHTNING_RPC_URL"}
    assert expected <= set(record["added"])
    assert "https://key.invalid/private" not in output
    run(tmp_path, "--service", "omenstrat_fast")
    rolled_back = json.loads(target.read_text())["env_variables"]
    assert not (expected & set(rolled_back))
    assert "LIGHTNING_LANE" not in rolled_back
    assert "LIGHTNING_RPC_URL" not in rolled_back


@pytest.mark.parametrize("service", ["omenstrat_fast", "trader_pearl"])
@pytest.mark.parametrize("style", ["env", "mapping", "list", "kubernetes"])
@pytest.mark.parametrize("source_controls", [False, True])
def test_lane_rollback_removes_operator_controls(
    tmp_path, capsys, service, style, source_controls
):
    """Remove lane controls from all active copies without logging the endpoint."""
    paths = fixture(tmp_path)
    saved = tmp_path / ".operate/Trader Agent-quickstart-config.json"
    saved.write_text(json.dumps({"user_provided_args": {}}))
    run(tmp_path, "--service", "omenstrat_lightning")
    url = "https://key.invalid/private-rollback-token"
    controls = {"LIGHTNING_LANE": "true", "LIGHTNING_RPC_URL": url}
    values = {**controls, "LIGHTNING_STILL_HOLD_MAX_PRICE": "0.87"}
    prefixes = (
        "",
        "SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_",
        "SKILL_OMENSTRAT_FAST_ABCI_MODELS_PARAMS_ARGS_",
    )
    cached = {
        prefix + key: value for prefix in prefixes for key, value in values.items()
    }
    for path in paths:
        data = json.loads(path.read_text())
        data["env_variables"].update(
            {key: entry(value) for key, value in cached.items()}
        )
        if path == paths[0] and not source_controls:
            for prefix in prefixes:
                for key in controls:
                    data["env_variables"].pop(prefix + key)
        path.write_text(json.dumps(data))
    saved.write_text(json.dumps({"user_provided_args": cached}))
    deployments = []
    for config in paths[1:]:
        path = config.parent / ("agent_0.env" if style == "env" else "build.yaml")
        if style == "env":
            text = "".join(f"{key}={value}\n" for key, value in cached.items())
        else:
            environment = cached
            if style == "list":
                environment = [f"{key}={value}" for key, value in cached.items()]
            elif style == "kubernetes":
                environment = [
                    {"name": key, "value": value} for key, value in cached.items()
                ]
            text = yaml.safe_dump({"environment": environment})
        path.write_text(text)
        deployments.append(path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    preview = run(tmp_path, "--service", service, "--dry-run")
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before
    output = run(tmp_path, "--service", service)
    captured = capsys.readouterr()
    assert url not in preview + output + captured.out + captured.err
    for path in (*paths, saved, *deployments):
        text = path.read_text()
        assert all(key not in text for key in controls), path
        assert "LIGHTNING_STILL_HOLD_MAX_PRICE" in text
        assert "0.87" in text
        for report in (preview, output):
            line = next(line for line in report.splitlines() if f"{path}:" in line)
            if path != paths[0] or source_controls:
                for key in controls:
                    assert f"remove lightning lane control {key}" in line
    assert "keep operator value LIGHTNING_STILL_HOLD_MAX_PRICE" in output


@pytest.mark.parametrize("service", ["omenstrat_fast", "trader_pearl"])
def test_lane_rollback_with_empty_provenance(tmp_path, service):
    """Remove explicit lane controls when no lightning defaults were added."""
    target = fixture(tmp_path)[0]
    data = json.loads(target.read_text())
    data["env_variables"].update(
        {
            "LIGHTNING_LANE": entry("true"),
            "LIGHTNING_RPC_URL": entry("https://key.invalid/empty-record"),
        }
    )
    target.write_text(json.dumps(data))
    marker = tmp_path / "configs/.lightning_defaults_added.json"
    marker.write_text(json.dumps({"added": [], "service": "omenstrat_lightning"}))
    run(tmp_path, "--service", service)
    env = json.loads(target.read_text())["env_variables"]
    assert "LIGHTNING_LANE" not in env
    assert "LIGHTNING_RPC_URL" not in env


def lane_copies(tmp_path, service):
    """Write lane controls into quickstart and each active cache format."""
    paths = fixture(tmp_path)
    packages = json.loads((ROOT / "packages/packages.json").read_text())
    url = "https://key.invalid/stale-lane-token"
    controls = {"LIGHTNING_LANE": "true", "LIGHTNING_RPC_URL": url}
    values = {
        prefix + key: value
        for prefix in (
            "",
            "SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_",
            "SKILL_OMENSTRAT_FAST_ABCI_MODELS_PARAMS_ARGS_",
        )
        for key, value in controls.items()
    }
    for path in paths:
        data = json.loads(path.read_text())
        data["hash"] = packages["dev"][f"service/valory/{service}/0.1.0"]
        data["env_variables"].update(
            {key: entry(value) for key, value in values.items()}
        )
        path.write_text(json.dumps(data))
    saved = tmp_path / ".operate/Trader Agent-quickstart-config.json"
    saved.write_text(json.dumps({"user_provided_args": values}))
    deployments = []
    for config in paths[1:]:
        agent = config.parent / "agent_0.env"
        agent.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        build = config.parent / "build.yaml"
        build.write_text(yaml.safe_dump({"environment": values}))
        deployments.extend((agent, build))
    return (*paths, saved, *deployments), url


@pytest.mark.parametrize("rpc", [None, ""])
def test_lane_rollback_clears_stale_caches_without_provenance(tmp_path, capsys, rpc):
    """Remove cached lane controls when quickstart no longer has lane inputs."""
    paths, url = lane_copies(tmp_path, "omenstrat_lightning")
    data = json.loads(paths[0].read_text())
    for key in list(data["env_variables"]):
        if key.endswith(("LIGHTNING_LANE", "LIGHTNING_RPC_URL")):
            del data["env_variables"][key]
    if rpc is not None:
        data["env_variables"]["LIGHTNING_RPC_URL"] = entry(rpc)
    paths[0].write_text(json.dumps(data))
    output = run(tmp_path, "--service", "omenstrat_fast")
    captured = capsys.readouterr()
    assert url not in output + captured.out + captured.err
    for path in paths:
        text = path.read_text()
        assert "LIGHTNING_LANE" not in text, path
        assert "LIGHTNING_RPC_URL" not in text, path
        assert url not in text, path


@pytest.mark.parametrize("service", ["omenstrat_fast", "trader_pearl"])
@pytest.mark.parametrize("rpc", [None, ""])
@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_",
        "SKILL_OMENSTRAT_FAST_ABCI_MODELS_PARAMS_ARGS_",
    ],
)
def test_lane_cleanup_removes_kubernetes_secret_without_provenance(
    tmp_path, service, rpc, prefix
):
    """Remove complete lane items and keep the next secret entry unchanged."""
    paths = fixture(tmp_path)
    if rpc is not None:
        data = json.loads(paths[0].read_text())
        data["env_variables"]["LIGHTNING_RPC_URL"] = entry(rpc)
        paths[0].write_text(json.dumps(data))
    assert not (tmp_path / "configs/.lightning_defaults_added.json").exists()
    path = paths[1].parent / "build.yaml"
    sibling = (
        "- name: KEEP_SECRET\n"
        "  valueFrom:\n"
        "    secretKeyRef:\n"
        "      name: keep-secret # Keep this comment.\n"
        "      key: value\n"
    )
    path.write_text(
        "env:\n"
        f"- name: {prefix}LIGHTNING_LANE\n"
        '  value: "true"\n'
        f"- name: {prefix}LIGHTNING_RPC_URL\n"
        "  valueFrom:\n"
        "    secretKeyRef:\n"
        "      name: lightning-rpc\n"
        "      key: url\n" + sibling
    )
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    run(tmp_path, "--service", service, "--dry-run")
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before
    run(tmp_path, "--service", service)
    assert path.read_text() == "env:\n" + sibling


@pytest.mark.parametrize("service", ["omenstrat_fast", "trader_pearl"])
@pytest.mark.parametrize("style", [">-", "|", ""])
@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_",
        "SKILL_OMENSTRAT_FAST_ABCI_MODELS_PARAMS_ARGS_",
    ],
)
def test_lane_cleanup_removes_mapping_values(tmp_path, service, style, prefix):
    """Remove each full lane value and keep the next mapping entry."""
    paths = fixture(tmp_path)
    path = paths[1].parent / "build.yaml"
    url = "https://key.invalid/multiline-lane-token"
    sibling = '      KEEP: "yes" # Keep this comment.\n'
    header = "services:\n  agent:\n    environment:\n"
    path.write_text(
        header
        + f"      {prefix}LIGHTNING_LANE: {style}\n"
        + "        true\n"
        + f"      {prefix}LIGHTNING_RPC_URL: {style}\n"
        + f"        {url}\n"
        + sibling
    )
    yaml.safe_load(path.read_text())
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    run(tmp_path, "--service", service, "--dry-run")
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before
    run(tmp_path, "--service", service)
    text = path.read_text()
    assert yaml.safe_load(text)["services"]["agent"]["environment"] == {"KEEP": "yes"}
    assert "LIGHTNING_LANE" not in text
    assert "LIGHTNING_RPC_URL" not in text
    assert url not in text
    assert text == header + sibling


@pytest.mark.parametrize("service", ["omenstrat_fast", "trader_pearl"])
@pytest.mark.parametrize("explicit", [False, True])
def test_lane_migration_clears_non_lightning_controls(
    tmp_path, capsys, service, explicit
):
    """Remove lane controls on normal migration runs without a rollback record."""
    paths, url = lane_copies(tmp_path, service)
    args = ("--service", service) if explicit else ()
    output = run(tmp_path, *args)
    captured = capsys.readouterr()
    assert url not in output + captured.out + captured.err
    for path in paths:
        text = path.read_text()
        line = next(
            line
            for line in output.splitlines()
            if line.startswith(">>") and f"{path}:" in line
        )
        for key in ("LIGHTNING_LANE", "LIGHTNING_RPC_URL"):
            assert key not in text, path
            assert f"remove lightning lane control {key}" in line
        assert url not in text, path


@pytest.mark.parametrize("explicit", [False, True])
def test_lane_migration_keeps_lightning_controls(tmp_path, capsys, explicit):
    """Keep lane inputs when quickstart still points at the lightning service."""
    paths, url = lane_copies(tmp_path, "omenstrat_lightning")
    args = ("--service", "omenstrat_lightning") if explicit else ()
    output = run(tmp_path, *args)
    captured = capsys.readouterr()
    assert url not in output + captured.out + captured.err
    assert "remove lightning lane control" not in output
    for path in paths:
        text = path.read_text()
        assert "LIGHTNING_LANE" in text, path
        assert url in text, path
        if path.suffix == ".json":
            data = json.loads(text)
            values = data.get("env_variables", data.get("user_provided_args"))
        elif path.suffix == ".yaml":
            values = yaml.safe_load(text)["environment"]
        else:
            values = dict(line.split("=", 1) for line in text.splitlines())
        for key, value in values.items():
            if key.endswith(("LIGHTNING_LANE", "LIGHTNING_RPC_URL")):
                expected = "true" if key.endswith("LIGHTNING_LANE") else url
                assert (
                    value["value"] if isinstance(value, dict) else value
                ) == expected


@pytest.mark.parametrize(
    "source,recorded,cached,explicit,keep",
    [
        pytest.param(
            "unknown", "omenstrat_lightning", None, None, True, id="older-provenance"
        ),
        pytest.param(
            "unknown", None, "omenstrat_lightning", None, True, id="older-cache"
        ),
        pytest.param(
            "unknown",
            "omenstrat_lightning",
            "omenstrat_fast",
            None,
            True,
            id="provenance-before-cache",
        ),
        pytest.param(
            "omenstrat_fast",
            "omenstrat_lightning",
            "omenstrat_lightning",
            None,
            False,
            id="fast-before-provenance",
        ),
        pytest.param(
            "trader_pearl",
            "omenstrat_lightning",
            "omenstrat_lightning",
            None,
            False,
            id="pearl-before-provenance",
        ),
        pytest.param("unknown", None, None, None, False, id="unknown"),
        pytest.param(
            "unknown", None, "omenstrat_fast", None, False, id="unknown-fast-cache"
        ),
        pytest.param(
            "omenstrat_lightning",
            "omenstrat_lightning",
            None,
            "omenstrat_fast",
            False,
            id="explicit-fast",
        ),
        pytest.param(
            "omenstrat_lightning",
            "omenstrat_lightning",
            None,
            "trader_pearl",
            False,
            id="explicit-pearl",
        ),
        pytest.param(
            "omenstrat_fast",
            "omenstrat_fast",
            None,
            "omenstrat_lightning",
            True,
            id="explicit-lightning",
        ),
    ],
)
def test_lane_cleanup_resolves_service_identity(
    tmp_path, source, recorded, cached, explicit, keep
):
    """Keep the lane only when the effective service is lightning."""
    paths, url = lane_copies(
        tmp_path, "omenstrat_lightning" if source == "unknown" else source
    )
    if source == "unknown":
        for path in paths[:3]:
            data = json.loads(path.read_text())
            data["hash"] = "older-lightning-service-cid"
            path.write_text(json.dumps(data))
    original_hash = json.loads(paths[0].read_text())["hash"]
    if recorded:
        marker = tmp_path / "configs/.lightning_defaults_added.json"
        marker.write_text(json.dumps({"added": [], "service": recorded}))
    if cached:
        for path in paths[1:3]:
            cached_package(path, cached)
    args = ("--service", explicit) if explicit else ()
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    run(tmp_path, *args, "--dry-run")
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before
    run(tmp_path, *args)
    if not explicit:
        assert json.loads(paths[0].read_text())["hash"] == original_hash
    for path in paths:
        text = path.read_text()
        assert ("LIGHTNING_LANE" in text) == keep, path
        assert ("LIGHTNING_RPC_URL" in text) == keep, path
        assert (url in text) == keep, path


@pytest.mark.parametrize("explicit", [False, True])
def test_lane_cleanup_dry_run_reports_without_writes(tmp_path, capsys, explicit):
    """Report lane cleanup without changing files or creating backups."""
    paths, url = lane_copies(tmp_path, "omenstrat_fast")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    args = ("--service", "omenstrat_fast") if explicit else ()
    output = run(tmp_path, *args, "--dry-run")
    captured = capsys.readouterr()
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before
    assert url not in output + captured.out + captured.err
    for path in paths:
        line = next(
            line
            for line in output.splitlines()
            if line.startswith(">>") and f"{path}:" in line
        )
        assert "(dry-run) would update" in line
        for key in ("LIGHTNING_LANE", "LIGHTNING_RPC_URL"):
            assert f"remove lightning lane control {key}" in line
