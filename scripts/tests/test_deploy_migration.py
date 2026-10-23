# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2023-2026 Valory AG
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

"""Check release config migration using temporary trader directories."""

import io
import json
import os
import runpy
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "migrate_trader_config.py"
OLD = {
    "priority_mech_address": "0x552c",
    "priority_mech_service_id": 975,
    "use_offchain": False,
    "offchain_deposit_target_calls": 7,
    "offchain_url": "https://example.test/a?b=c",
}


def entry(value):
    """Make a quickstart env entry."""
    return {
        "name": "Mech config",
        "description": "keep",
        "value": value,
        "provision_type": "computed",
    }


def run(qs, *args):
    """Run the helper as the release script does."""
    output = io.StringIO()
    previous = sys.argv
    try:
        sys.argv = [str(SCRIPT), str(qs), *args]
        with redirect_stdout(output):
            runpy.run_path(str(SCRIPT), run_name="__main__")
    finally:
        sys.argv = previous
    return output.getvalue()


def fixture(qs):
    """Write a quickstart config and two operate caches."""
    config = {
        "hash": "unchanged",
        "env_variables": {
            "MECH_MARKETPLACE_CONFIG": entry(json.dumps(OLD)),
            "USE_OFFCHAIN": entry("true"),
        },
        "untouched": [1, 2],
    }
    paths = [qs / "configs/config_predict_trader.json"]
    for name in ("sc-one", "sc-two"):
        paths.append(qs / ".operate/services" / name / "config.json")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=4) + "\n")
    return paths


def test_json_migration(tmp_path):
    """Migrate all configs with backups and no unrelated text edits."""
    paths = fixture(tmp_path)
    before = {p: p.read_bytes() for p in paths}
    preview = run(tmp_path, "--dry-run")
    assert "offchain_deposit_target_calls" in preview
    assert all(p.read_bytes() == before[p] for p in paths)
    assert not list(tmp_path.rglob("*.bak-*"))
    output = run(tmp_path)
    for path in paths:
        assert str(path) in output
        text = path.read_text()
        env = json.loads(text)["env_variables"]
        assert json.loads(env["MECH_MARKETPLACE_CONFIG"]["value"]) == {
            k: v
            for k, v in OLD.items()
            if k not in ("use_offchain", "offchain_deposit_target_calls")
        }
        assert env["USE_OFFCHAIN"] == entry("true")
        assert env["OFFCHAIN_DEPOSIT_TARGET_CALLS"]["value"] == "7"
        assert set(env["OFFCHAIN_DEPOSIT_TARGET_CALLS"]) == set(entry("7"))
        assert (
            list(path.parent.glob(path.name + ".bak-*"))[0].read_bytes() == before[path]
        )
        assert '    "untouched": [\n        1,\n        2\n    ]' in text
        assert (
            '        "USE_OFFCHAIN": '
            + json.dumps(entry("true"), indent=4).replace("\n", "\n        ")
            in text
        )
    after = {p: p.read_bytes() for p in paths}
    run(tmp_path)
    assert all(p.read_bytes() == after[p] for p in paths)
    assert len(list(tmp_path.rglob("*.bak-*"))) == 3


@pytest.mark.parametrize("style", ["env", "mapping", "list", "kubernetes"])
def test_deployment_files(tmp_path, style):
    """Migrate each env format without changing other lines."""
    fixture(tmp_path)
    service = tmp_path / ".operate/services/sc-one"
    if style == "env":
        path = service / "deployment/agent_0.env"
        line = "MECH_MARKETPLACE_CONFIG=" + json.dumps(OLD)
        existing = "USE_OFFCHAIN=true\n"
    elif style == "mapping":
        path = service / "deployment/other/build.yaml"
        line = "    MECH_MARKETPLACE_CONFIG: '" + json.dumps(OLD) + "'"
        existing = "    USE_OFFCHAIN: 'true'\n"
    elif style == "list":
        path = service / "deployment/build.yaml"
        line = "    - MECH_MARKETPLACE_CONFIG=" + json.dumps(OLD)
        existing = "    - USE_OFFCHAIN=true\n"
    else:
        path = service / "deployment/abci_build_k8s/build.yaml"
        line = (
            "    - name: MECH_MARKETPLACE_CONFIG\n      value: '"
            + json.dumps(OLD)
            + "'"
        )
        existing = "    - name: USE_OFFCHAIN\n      value: 'true'\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "# keep this line\n\n" + existing + line + "\n# keep the tail\n"
    path.write_text(original)
    run(tmp_path, "--dry-run")
    assert path.read_text() == original
    run(tmp_path)
    result = path.read_text()
    assert "use_offchain" not in result
    assert "offchain_deposit_target_calls" not in result
    assert "OFFCHAIN_DEPOSIT_TARGET_CALLS" in result
    assert existing in result
    assert result.startswith("# keep this line\n")
    assert result.endswith("# keep the tail\n")
    assert list(path.parent.glob(path.name + ".bak-*"))[0].read_text() == original
    run(tmp_path)
    assert path.read_text() == result


def test_priority_warning_and_no_cache(tmp_path):
    """Report different stored priority values and absent caches."""
    paths = fixture(tmp_path)
    changed = json.loads(paths[1].read_text())
    mech = dict(OLD, priority_mech_address="0xC05e", priority_mech_service_id=2182)
    changed["env_variables"]["MECH_MARKETPLACE_CONFIG"]["value"] = json.dumps(mech)
    paths[1].write_text(json.dumps(changed))
    output = run(tmp_path, "--dry-run")
    assert "WARNING" in output
    assert "0xC05e" in output
    assert "2182" in output
    assert "0x552c" in output
    assert "975" in output
    assert str(paths[0]) in output
    assert str(paths[1]) in output
    for path in paths[1:]:
        path.unlink()
        path.parent.rmdir()
    assert "no operate" in run(tmp_path, "--dry-run").lower()


def test_no_final_newline_and_missing_top_level_values(tmp_path):
    """Add both settings when an env file has no final newline."""
    fixture(tmp_path)
    path = tmp_path / ".operate/services/sc-one/agent_0.env"
    path.write_text("MECH_MARKETPLACE_CONFIG=" + json.dumps(OLD))
    run(tmp_path)
    values = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert (
        json.loads(values["MECH_MARKETPLACE_CONFIG"])["priority_mech_service_id"] == 975
    )
    assert values["USE_OFFCHAIN"] == "false"
    assert values["OFFCHAIN_DEPOSIT_TARGET_CALLS"] == "7"


def test_crlf_and_nested_operate_config(tmp_path):
    """Keep line endings and migrate a nested operate env mapping."""
    paths = fixture(tmp_path)
    path = paths[1]
    config = {"user_params": json.loads(path.read_text()), "other": "keep"}
    original = (json.dumps(config, indent=2) + "\n").replace("\n", "\r\n").encode()
    path.write_bytes(original)
    run(tmp_path)
    result = path.read_bytes()
    assert result.count(b"\n") == result.count(b"\r\n")
    assert b'"other": "keep"\r\n' in result
    assert list(path.parent.glob(path.name + ".bak-*"))[0].read_bytes() == original


def test_warning_preflight_does_not_edit(tmp_path):
    """Check stored priority values before the release starts."""
    paths = fixture(tmp_path)
    before = {path: path.read_bytes() for path in paths}
    run(tmp_path, "--warn-only")
    assert all(path.read_bytes() == before[path] for path in paths)
    assert not list(tmp_path.rglob("*.bak-*"))


def test_empty_top_level_entry_keeps_metadata(tmp_path):
    """Fill an unset template entry without changing its metadata."""
    paths = fixture(tmp_path)
    data = json.loads(paths[0].read_text())
    data["env_variables"]["USE_OFFCHAIN"] = entry("")
    paths[0].write_text(json.dumps(data))
    run(tmp_path)
    env = json.loads(paths[0].read_text())["env_variables"]
    assert env["USE_OFFCHAIN"] == entry("false")


@pytest.mark.parametrize("kubernetes", [False, True])
def test_wrapped_yaml_value(tmp_path, kubernetes):
    """Read the wrapped scalar emitted by the deployment YAML writer."""
    import yaml

    fixture(tmp_path)
    path = tmp_path / ".operate/services/sc-one/deployment/build.yaml"
    path.parent.mkdir(parents=True)
    env = (
        [{"name": "MECH_MARKETPLACE_CONFIG", "value": json.dumps(OLD)}]
        if kubernetes
        else {"MECH_MARKETPLACE_CONFIG": json.dumps(OLD)}
    )
    path.write_text(yaml.safe_dump({"environment": env, "unchanged": "keep"}, width=60))
    run(tmp_path)
    actual = yaml.safe_load(path.read_text())
    assert actual["unchanged"] == "keep"
    values = (
        {item["name"]: item["value"] for item in actual["environment"]}
        if kubernetes
        else actual["environment"]
    )
    assert (
        json.loads(values["MECH_MARKETPLACE_CONFIG"])["offchain_url"]
        == OLD["offchain_url"]
    )
    assert values["USE_OFFCHAIN"] == "false"
    assert values["OFFCHAIN_DEPOSIT_TARGET_CALLS"] == "7"


def test_unset_config_and_missing_cache_files(tmp_path):
    """Skip unset templates and absent cache files without backups."""
    paths = fixture(tmp_path)
    paths[1].unlink()
    data = {
        "env_variables": {"MECH_MARKETPLACE_CONFIG": entry("")},
        "user_args": {"priority_mech_address": "0x552c"},
    }
    paths[0].write_text(json.dumps(data))
    paths[2].write_text(json.dumps(data))
    output = run(tmp_path)
    assert "missing; skipping" in output
    assert not list(tmp_path.rglob("*.bak-*"))


@pytest.mark.parametrize(
    "text",
    [
        "MECH_MARKETPLACE_CONFIG=not-json\n",
        "MECH_MARKETPLACE_CONFIG=not-json\nOTHER=keep\n",
        "- name: MECH_MARKETPLACE_CONFIG\n  invalid: keep\n",
    ],
)
def test_invalid_env_does_not_edit(tmp_path, text):
    """Leave an invalid deployment file intact and report the parse error."""
    fixture(tmp_path)
    path = tmp_path / ".operate/services/sc-one/agent_0.env"
    path.write_text(text)
    with pytest.raises(ValueError, match="Expecting|Missing marketplace"):
        run(tmp_path)
    assert path.read_text() == text
    assert not list(path.parent.glob(path.name + ".bak-*"))


def test_double_quoted_yaml_value(tmp_path):
    """Keep escaped JSON inside a double-quoted YAML scalar valid."""
    import yaml

    fixture(tmp_path)
    path = tmp_path / ".operate/services/sc-one/build.yaml"
    path.write_text("MECH_MARKETPLACE_CONFIG: " + json.dumps(json.dumps(OLD)) + "\n")
    run(tmp_path)
    values = yaml.safe_load(path.read_text())
    assert "use_offchain" not in json.loads(values["MECH_MARKETPLACE_CONFIG"])
    assert values["USE_OFFCHAIN"] == "false"


def test_json_edit_rejects_an_unrelated_change():
    """Reject an edit when it does not give the expected document."""
    helper = runpy.run_path(str(SCRIPT))
    with pytest.raises(ValueError, match="Could not locate"):
        helper["edit_json"]('{"a": 1}', {"b": 2}, value=2)


def test_settings_are_scoped_to_each_yaml_environment(tmp_path):
    """An entry in another container must not suppress a missing setting."""
    import yaml

    fixture(tmp_path)
    path = tmp_path / ".operate/services/sc-one/build.yaml"
    first = {"env": [{"name": "MECH_MARKETPLACE_CONFIG", "value": json.dumps(OLD)}]}
    second = {
        "env": [
            {"name": "USE_OFFCHAIN", "value": "true"},
            {"name": "MECH_MARKETPLACE_CONFIG", "value": json.dumps(OLD)},
        ]
    }
    path.write_text(yaml.safe_dump_all([first, second]))
    run(tmp_path)
    actual = list(yaml.safe_load_all(path.read_text()))
    values = [{item["name"]: item["value"] for item in doc["env"]} for doc in actual]
    assert values[0]["USE_OFFCHAIN"] == "false"
    assert values[1]["USE_OFFCHAIN"] == "true"
    assert all(value["OFFCHAIN_DEPOSIT_TARGET_CALLS"] == "7" for value in values)


@pytest.mark.parametrize("existing", [None, 0, 123456789, 10**17])
@pytest.mark.parametrize("as_mapping", [False, True])
def test_strategy_json_migration(tmp_path, existing, as_mapping):
    """Add the Omen threshold once and keep explicit values and metadata."""
    paths = fixture(tmp_path)
    strategy = {"floor_balance": 0, "fee_per_trade": 10**16}
    if existing is not None:
        strategy["min_expected_profit"] = existing
    for path in paths:
        data = json.loads(path.read_text())
        data["env_variables"] = {
            "STRATEGIES_KWARGS": entry(strategy if as_mapping else json.dumps(strategy))
        }
        path.write_text(json.dumps({"user_params": data}, indent=2) + "\n")
    before = {path: path.read_bytes() for path in paths}
    preview = run(tmp_path, "--dry-run")
    if existing is None:
        assert "STRATEGIES_KWARGS.min_expected_profit=60000000000000000" in preview
    assert all(path.read_bytes() == before[path] for path in paths)
    assert not list(tmp_path.rglob("*.bak-*"))
    run(tmp_path, "--warn-only")
    assert all(path.read_bytes() == before[path] for path in paths)
    run(tmp_path)
    for path in paths:
        actual_entry = json.loads(path.read_text())["user_params"]["env_variables"][
            "STRATEGIES_KWARGS"
        ]
        value = actual_entry["value"]
        actual = value if as_mapping else json.loads(value)
        assert actual == dict(
            strategy, min_expected_profit=6 * 10**16 if existing is None else existing
        )
        assert {k: v for k, v in actual_entry.items() if k != "value"} == {
            k: v for k, v in entry("").items() if k != "value"
        }
        backups = list(path.parent.glob(path.name + ".bak-*"))
        assert len(backups) == (1 if existing is None else 0)
        if backups:
            assert backups[0].read_bytes() == before[path]
    after = {path: path.read_bytes() for path in paths}
    run(tmp_path)
    assert all(path.read_bytes() == after[path] for path in paths)


@pytest.mark.parametrize("style", ["env", "mapping", "list", "kubernetes"])
@pytest.mark.parametrize("existing", [None, 0, 123456789, 10**17])
def test_strategy_deployment_migration(tmp_path, style, existing):
    """Migrate deployment scalars with the same backup and dry-run rules."""
    import yaml

    fixture(tmp_path)
    strategy = {"floor_balance": 0, "fee_per_trade": 10**16, "grid_points": 500}
    if existing is not None:
        strategy["min_expected_profit"] = existing
    raw = json.dumps(strategy)
    service = tmp_path / ".operate/services/sc-one"
    if style == "env":
        path = service / "agent_0.env"
        original = "# keep\r\nSTRATEGIES_KWARGS=" + raw
    elif style == "list":
        path = service / "build.yaml"
        original = (
            "environment:\n  - STRATEGIES_KWARGS="
            + json.dumps(strategy, separators=(",", ":"))
            + "\n"
        )
    else:
        path = service / "build.yaml"
        env = (
            [{"name": "STRATEGIES_KWARGS", "value": raw}]
            if style == "kubernetes"
            else {"STRATEGIES_KWARGS": raw}
        )
        original = yaml.safe_dump({"environment": env, "untouched": "keep"}, width=60)
    path.write_bytes(original.encode())
    preview = run(tmp_path, "--dry-run")
    assert path.read_bytes() == original.encode()
    assert not list(path.parent.glob(path.name + ".bak-*"))
    run(tmp_path)
    with path.open(newline="") as source:
        result = source.read()
    if style == "env":
        assert result.startswith("# keep\r\n")
        value = result.split("STRATEGIES_KWARGS=", 1)[1]
    else:
        env = yaml.safe_load(result)["environment"]
        if style == "list":
            value = env[0].split("=", 1)[1]
        elif style == "kubernetes":
            value = env[0]["value"]
        else:
            value = env["STRATEGIES_KWARGS"]
    assert json.loads(value) == dict(
        strategy, min_expected_profit=6 * 10**16 if existing is None else existing
    )
    backups = list(path.parent.glob(path.name + ".bak-*"))
    assert len(backups) == (1 if existing is None else 0)
    if existing is None:
        assert str(path) in preview
        assert "STRATEGIES_KWARGS.min_expected_profit=60000000000000000" in preview
        assert backups[0].read_bytes() == original.encode()
    run(tmp_path)
    assert path.read_bytes() == result.encode()


@pytest.mark.parametrize("value", [{}, "", None])
def test_empty_strategy_entry(tmp_path, value):
    """Migrate an empty dict but leave unset templates alone."""
    paths = fixture(tmp_path)
    for path in paths:
        path.write_text(
            json.dumps({"env_variables": {"STRATEGIES_KWARGS": entry(value)}})
        )
    run(tmp_path)
    for path in paths:
        actual = json.loads(path.read_text())["env_variables"]["STRATEGIES_KWARGS"]
        assert actual == entry(
            {"min_expected_profit": 6 * 10**16} if value == {} else value
        )


@pytest.mark.parametrize("raw", ["", "''", '""', "{}"])
@pytest.mark.parametrize("style", ["env", "mapping", "list", "kubernetes"])
def test_empty_strategy_deployment_entry(tmp_path, raw, style):
    """Keep unset strategy scalars and continue to the next cache file."""
    paths = fixture(tmp_path)
    service = paths[1].parent
    path = service / ("agent_0.env" if style == "env" else "build.yaml")
    prefix = {
        "env": "STRATEGIES_KWARGS=",
        "mapping": "    STRATEGIES_KWARGS: ",
        "list": "    - STRATEGIES_KWARGS=",
        "kubernetes": "    - name: STRATEGIES_KWARGS\n      value: ",
    }[style]
    original = prefix + raw + "\n"
    path.write_text(original)
    run(tmp_path, "--dry-run")
    run(tmp_path, "--warn-only")
    assert path.read_text() == original
    assert not list(path.parent.glob(path.name + ".bak-*"))
    run(tmp_path)
    result = path.read_text()
    if raw == "{}":
        assert "min_expected_profit" in result
        assert list(path.parent.glob(path.name + ".bak-*"))[0].read_text() == original
    else:
        assert result == original
        assert not list(path.parent.glob(path.name + ".bak-*"))
    later_env = json.loads(paths[2].read_text())["env_variables"]
    assert later_env["OFFCHAIN_DEPOSIT_TARGET_CALLS"]["value"] == "7"
    run(tmp_path)
    assert path.read_text() == result


def test_strategy_and_offchain_migrate_together(tmp_path):
    """Apply both migrations with one backup for each file."""
    paths = fixture(tmp_path)
    for path in paths:
        data = json.loads(path.read_text())
        data["env_variables"]["STRATEGIES_KWARGS"] = entry("{}")
        path.write_text(json.dumps(data))
    env_path = tmp_path / ".operate/services/sc-one/agent_0.env"
    env_path.write_text(
        "STRATEGIES_KWARGS={}\nMECH_MARKETPLACE_CONFIG=" + json.dumps(OLD) + "\n"
    )
    output = run(tmp_path)
    assert "STRATEGIES_KWARGS.min_expected_profit" in output
    assert "OFFCHAIN_DEPOSIT_TARGET_CALLS" in output
    for path in paths:
        env = json.loads(path.read_text())["env_variables"]
        assert (
            json.loads(env["STRATEGIES_KWARGS"]["value"])["min_expected_profit"]
            == 6 * 10**16
        )
        assert env["OFFCHAIN_DEPOSIT_TARGET_CALLS"]["value"] == "7"
    env = dict(line.split("=", 1) for line in env_path.read_text().splitlines())
    assert json.loads(env["STRATEGIES_KWARGS"])["min_expected_profit"] == 6 * 10**16
    assert env["OFFCHAIN_DEPOSIT_TARGET_CALLS"] == "7"
    assert len(list(tmp_path.rglob("*.bak-*"))) == 4


@pytest.mark.parametrize(
    "style", ["json-string", "json-mapping", "env", "mapping", "list", "kubernetes"]
)
@pytest.mark.parametrize(
    "case", ["old", "current", "unknown", "missing", "empty", "unset"]
)
def test_strategy_hash_migration(tmp_path, style, case):
    """Update known strategy hashes with backups, dry runs, and repeat safety."""
    import yaml

    packages = json.loads((SCRIPT.parents[1] / "packages/packages.json").read_text())
    current = {
        name: packages["dev"][f"custom/valory/{name}/0.1.0"]
        for name in ("kelly_criterion", "fixed_bet")
    }
    unknown = {
        "unknown-hash": ["unknown"],
        "shared-hash": ["kelly_criterion", "fixed_bet"],
        "empty-hash": [],
    }
    mapping = dict(unknown)
    expected = dict(unknown)
    if case in ("old", "current"):
        for name, cid in current.items():
            mapping[f"old-{name}" if case == "old" else cid] = [name]
            expected[cid] = [name]
    elif case == "empty":
        mapping = expected = {}
    variable = "FILE_HASH_TO_STRATEGIES"
    value = "" if case == "unset" else json.dumps(mapping)
    if style.startswith("json"):
        paths = fixture(tmp_path)
        env = (
            {}
            if case == "missing"
            else {
                variable: entry(mapping if style == "json-mapping" and value else value)
            }
        )
        original = json.dumps({"user_params": {"env_variables": env}}, indent=2) + "\n"
    else:
        paths = []
        for service in ("sc-one", "sc-two"):
            name = "agent_0.env" if style == "env" else "build.yaml"
            paths.append(tmp_path / ".operate/services" / service / "deployment" / name)
        if case == "missing":
            original = "# keep\nOTHER=keep\n" if style == "env" else "other: keep\n"
        elif style in ("env", "list"):
            prefix = "# keep\r\n" if style == "env" else "environment:\n  - "
            original = prefix + variable + "=" + (value or '""') + "\n"
        else:
            env = (
                [{"name": variable, "value": value}]
                if style == "kubernetes"
                else {variable: value}
            )
            original = yaml.safe_dump({"environment": env, "other": "keep"}, width=60)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original.encode())
    preview = run(tmp_path, "--dry-run")
    run(tmp_path, "--warn-only")
    assert all(path.read_bytes() == original.encode() for path in paths)
    assert not list(tmp_path.rglob("*.bak-*"))
    output = run(tmp_path)
    for path in paths:
        if case == "old":
            assert str(path) in preview
            assert str(path) in output
            for name, cid in current.items():
                assert f"{variable}: old-{name} -> {cid}" in preview
            result = path.read_text()
            if style.startswith("json"):
                actual = json.loads(result)["user_params"]["env_variables"][variable]
                assert actual == entry(
                    expected if style == "json-mapping" else json.dumps(expected)
                )
            else:
                if style == "env":
                    value = result.split(variable + "=", 1)[1]
                else:
                    env = yaml.safe_load(result)["environment"]
                    value = (
                        env[0].split("=", 1)[1]
                        if style == "list"
                        else env[0]["value"] if style == "kubernetes" else env[variable]
                    )
                assert json.loads(value) == expected
            backups = list(path.parent.glob(path.name + ".bak-*"))
            assert len(backups) == 1
            assert backups[0].read_bytes() == original.encode()
        else:
            assert path.read_bytes() == original.encode()
            assert not list(path.parent.glob(path.name + ".bak-*"))
    after = {path: path.read_bytes() for path in paths}
    backups = list(tmp_path.rglob("*.bak-*"))
    run(tmp_path)
    assert all(path.read_bytes() == after[path] for path in paths)
    assert list(tmp_path.rglob("*.bak-*")) == backups


def test_strategy_hashes_follow_package_manifest(tmp_path, monkeypatch):
    """Read replacement hashes from the repo that contains the helper."""
    script = tmp_path / "repo/scripts/migrate_trader_config.py"
    script.parent.mkdir(parents=True)
    script.write_bytes(SCRIPT.read_bytes())
    packages = script.parents[1] / "packages/packages.json"
    packages.parent.mkdir()
    packages.write_text(
        json.dumps(
            {
                "dev": {
                    f"custom/valory/{name}/0.1.0": f"current-{name}"
                    for name in ("kelly_criterion", "fixed_bet")
                }
            }
        )
    )
    monkeypatch.setattr(sys.modules[__name__], "SCRIPT", script)
    path = fixture(tmp_path / "quickstart")[0]
    path.write_text(
        json.dumps(
            {
                "env_variables": {
                    "FILE_HASH_TO_STRATEGIES": entry(
                        {"old-kelly": ["kelly_criterion"], "old-fixed": ["fixed_bet"]}
                    )
                }
            }
        )
    )
    run(path.parents[1])
    assert json.loads(path.read_text())["env_variables"][
        "FILE_HASH_TO_STRATEGIES"
    ] == entry(
        {
            "current-kelly_criterion": ["kelly_criterion"],
            "current-fixed_bet": ["fixed_bet"],
        }
    )


V1_MARKETPLACE = "0x4554fE75c1f5576c1d7F765B2A036c199Adae329"
V2_MARKETPLACE = "0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB"
ZERO_ADDRESS = "0x" + "0" * 40


@pytest.mark.parametrize("address", [V1_MARKETPLACE, V1_MARKETPLACE.lower()])
@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize("plain_enabled", [False, True])
def test_marketplace_json_migration(tmp_path, address, mapping, plain_enabled):
    """Replace the old address in quickstart and every cached config."""
    paths = fixture(tmp_path)
    mech = {
        "mech_marketplace_address": address,
        "requester_staking_instance_address": ZERO_ADDRESS,
        "response_timeout": 300,
    }
    for path in paths:
        data = {
            "env_variables": {
                "MECH_MARKETPLACE_CONFIG": entry(mech if mapping else json.dumps(mech)),
                "USE_MECH_MARKETPLACE": "true" if plain_enabled else entry("true"),
            },
            "keep": "unchanged",
        }
        path.write_text(json.dumps(data, indent=2) + "\n")
    before = {path: path.read_bytes() for path in paths}
    preview = run(tmp_path, "--dry-run")
    assert V1_MARKETPLACE.lower() in preview.lower()
    assert V2_MARKETPLACE in preview
    assert "requester_staking_instance_address" in preview
    assert all(path.read_bytes() == before[path] for path in paths)
    assert not list(tmp_path.rglob("*.bak-*"))
    output = run(tmp_path)
    for path in paths:
        assert str(path) in output
        data = json.loads(path.read_text())
        actual = data["env_variables"]["MECH_MARKETPLACE_CONFIG"]["value"]
        assert isinstance(actual, dict if mapping else str)
        actual = json.loads(actual) if isinstance(actual, str) else actual
        assert actual == dict(mech, mech_marketplace_address=V2_MARKETPLACE)
        assert data["env_variables"]["USE_MECH_MARKETPLACE"] == (
            "true" if plain_enabled else entry("true")
        )
        assert data["keep"] == "unchanged"
        assert (
            list(path.parent.glob(path.name + ".bak-*"))[0].read_bytes() == before[path]
        )
    after = {path: path.read_bytes() for path in paths}
    run(tmp_path)
    assert all(path.read_bytes() == after[path] for path in paths)
    assert len(list(tmp_path.rglob("*.bak-*"))) == len(paths)


@pytest.mark.parametrize("style", ["env", "mapping", "list", "kubernetes"])
def test_marketplace_deployment_migration(tmp_path, style):
    """Replace only the old marketplace in each deployment format."""
    import yaml

    service = tmp_path / ".operate/services/sc-one"
    service.mkdir(parents=True)
    mech = {
        "mech_marketplace_address": V1_MARKETPLACE.lower(),
        "requester_staking_instance_address": ZERO_ADDRESS,
    }
    if style == "env":
        path = service / "agent_0.env"
        original = (
            "USE_MECH_MARKETPLACE=true\nMECH_MARKETPLACE_CONFIG="
            + json.dumps(mech)
            + "\n"
        )
    else:
        path = service / "build.yaml"
        env = {
            "USE_MECH_MARKETPLACE": "true",
            "MECH_MARKETPLACE_CONFIG": json.dumps(mech),
        }
        if style == "list":
            env = [key + "=" + value for key, value in env.items()]
            original = "environment:\n" + "\n".join("- " + item for item in env) + "\n"
        elif style == "kubernetes":
            env = [{"name": key, "value": value} for key, value in env.items()]
        if style != "list":
            original = yaml.safe_dump(
                {"environment": env, "keep": "unchanged"}, width=60
            )
    path.write_text(original)
    preview = run(tmp_path, "--dry-run")
    assert V2_MARKETPLACE in preview
    assert "WARNING" in preview and "requester_staking_instance_address" in preview
    assert path.read_text() == original
    run(tmp_path)
    result = path.read_text()
    assert V1_MARKETPLACE.lower() not in result.lower()
    assert V2_MARKETPLACE in result
    assert ZERO_ADDRESS in result
    assert list(service.glob(path.name + ".bak-*"))[0].read_text() == original
    run(tmp_path)
    assert path.read_text() == result
    assert len(list(service.glob("*.bak-*"))) == 1


@pytest.mark.parametrize("enabled", ["true", "false", True, False])
@pytest.mark.parametrize("address", [V2_MARKETPLACE, "0x" + "a" * 40])
@pytest.mark.parametrize("plain_enabled", [False, True])
def test_marketplace_warnings_do_not_edit(tmp_path, enabled, address, plain_enabled):
    """Warn for unknown addresses and enabled zero requester staking only."""
    path = tmp_path / "configs/config_predict_trader.json"
    path.parent.mkdir()
    mech = {
        "mech_marketplace_address": address,
        "requester_staking_instance_address": ZERO_ADDRESS,
    }
    path.write_text(
        json.dumps(
            {
                "env_variables": {
                    "MECH_MARKETPLACE_CONFIG": entry(json.dumps(mech)),
                    "USE_MECH_MARKETPLACE": (
                        enabled if plain_enabled else entry(enabled)
                    ),
                }
            }
        )
    )
    before = path.read_bytes()
    for args in [(), ("--dry-run",), ("--warn-only",)]:
        output = run(tmp_path, *args)
        assert ("requester_staking_instance_address" in output) == (
            str(enabled).lower() == "true"
        )
        assert (address in output) == (address != V2_MARKETPLACE)
        if address != V2_MARKETPLACE:
            assert "WARNING" in output and str(path) in output
        assert path.read_bytes() == before
        assert not list(tmp_path.rglob("*.bak-*"))


def test_requester_warning_is_scoped_to_yaml_environment(tmp_path):
    """An enabled container must not trigger a warning for another container."""
    import yaml

    path = tmp_path / ".operate/services/sc-one/build.yaml"
    path.parent.mkdir(parents=True)
    docs = [
        {"environment": {"USE_MECH_MARKETPLACE": "true"}},
        {
            "environment": {
                "USE_MECH_MARKETPLACE": "false",
                "MECH_MARKETPLACE_CONFIG": json.dumps(
                    {
                        "mech_marketplace_address": V2_MARKETPLACE,
                        "requester_staking_instance_address": ZERO_ADDRESS,
                    }
                ),
            }
        },
    ]
    path.write_text(yaml.safe_dump_all(docs))
    assert "requester_staking_instance_address" not in run(tmp_path)


@pytest.mark.parametrize("address", [V1_MARKETPLACE, V2_MARKETPLACE])
@pytest.mark.parametrize("mapping", [False, True])
def test_direct_operate_marketplace_config(tmp_path, address, mapping):
    """Migrate stored user args and warn without changing requester staking."""
    paths = fixture(tmp_path)
    path = paths[1]
    mech = {
        "mech_marketplace_address": address,
        "requester_staking_instance_address": ZERO_ADDRESS,
    }
    path.write_text(
        json.dumps(
            {
                "user_params": {
                    "use_mech_marketplace": True,
                    "mech_marketplace_config": mech if mapping else json.dumps(mech),
                }
            }
        )
    )
    before = path.read_bytes()
    for args in [("--dry-run",), ("--warn-only",)]:
        assert "requester_staking_instance_address" in run(tmp_path, *args)
        assert path.read_bytes() == before
        assert not list(tmp_path.rglob("*.bak-*"))
    run(tmp_path)
    actual = json.loads(path.read_text())["user_params"]["mech_marketplace_config"]
    assert isinstance(actual, dict if mapping else str)
    actual = actual if mapping else json.loads(actual)
    assert actual == dict(mech, mech_marketplace_address=V2_MARKETPLACE)
    backups = list(path.parent.glob(path.name + ".bak-*"))
    assert len(backups) == (1 if address == V1_MARKETPLACE else 0)
    if backups:
        assert backups[0].read_bytes() == before
    later = json.loads(paths[2].read_text())["env_variables"]
    assert "use_offchain" not in json.loads(later["MECH_MARKETPLACE_CONFIG"]["value"])
    after = {path: path.read_bytes() for path in paths}
    backups = list(tmp_path.rglob("*.bak-*"))
    run(tmp_path)
    assert all(path.read_bytes() == after[path] for path in paths)
    assert list(tmp_path.rglob("*.bak-*")) == backups


@pytest.mark.parametrize(
    "config", ["agents/trader/aea-config.yaml", "services/trader_pearl/service.yaml"]
)
def test_gnosis_marketplace_default(config):
    """Use the V2 marketplace in both Gnosis defaults."""
    text = (SCRIPT.parents[1] / "packages/valory" / config).read_text()
    assert '"mech_marketplace_address":"' + V2_MARKETPLACE + '"' in text


@pytest.mark.parametrize(
    "style", ["json-string", "json-mapping", "env", "mapping", "list", "kubernetes"]
)
def test_marketplace_and_strategy_migrate_together(tmp_path, style):
    """Apply all migrations in one pass with one backup and stable repeat runs."""
    import yaml

    packages = json.loads((SCRIPT.parents[1] / "packages/packages.json").read_text())
    values = {
        "MECH_MARKETPLACE_CONFIG": dict(
            OLD,
            mech_marketplace_address=V1_MARKETPLACE.lower(),
            requester_staking_instance_address=ZERO_ADDRESS,
        ),
        "STRATEGIES_KWARGS": {},
        "FILE_HASH_TO_STRATEGIES": {"old-kelly": ["kelly_criterion"]},
        "USE_MECH_MARKETPLACE": True,
    }
    expected = dict(values)
    expected["MECH_MARKETPLACE_CONFIG"] = {
        key: value
        for key, value in dict(
            values["MECH_MARKETPLACE_CONFIG"], mech_marketplace_address=V2_MARKETPLACE
        ).items()
        if key not in ("use_offchain", "offchain_deposit_target_calls")
    }
    expected["STRATEGIES_KWARGS"] = {"min_expected_profit": 6 * 10**16}
    expected["FILE_HASH_TO_STRATEGIES"] = {
        packages["dev"]["custom/valory/kelly_criterion/0.1.0"]: ["kelly_criterion"]
    }
    expected.update(USE_OFFCHAIN=False, OFFCHAIN_DEPOSIT_TARGET_CALLS=7)
    if style.startswith("json"):
        paths = fixture(tmp_path)
        original = json.dumps(
            {
                "env_variables": {
                    key: entry(value if style == "json-mapping" else json.dumps(value))
                    for key, value in values.items()
                }
            }
        )
    else:
        path = (
            tmp_path
            / ".operate/services/sc-one"
            / ("agent_0.env" if style == "env" else "build.yaml")
        )
        path.parent.mkdir(parents=True)
        paths = [path]
        env = {key: json.dumps(value) for key, value in values.items()}
        if style in ("env", "list"):
            original = ("environment:\n" if style == "list" else "") + "".join(
                ("  - " if style == "list" else "") + key + "=" + value + "\n"
                for key, value in env.items()
            )
        else:
            original = yaml.safe_dump(
                {
                    "environment": (
                        [{"name": key, "value": value} for key, value in env.items()]
                        if style == "kubernetes"
                        else env
                    )
                },
                width=60,
            )
    for path in paths:
        path.write_text(original)
    preview = run(tmp_path, "--dry-run")
    assert V2_MARKETPLACE in preview
    assert "min_expected_profit" in preview
    assert "FILE_HASH_TO_STRATEGIES: old-kelly" in preview
    assert "requester_staking_instance_address is zero" in preview
    run(tmp_path, "--warn-only")
    assert all(path.read_text() == original for path in paths)
    assert not list(tmp_path.rglob("*.bak-*"))
    run(tmp_path)
    for path in paths:
        result = path.read_text()
        if style.startswith("json"):
            env = {
                key: item["value"]
                for key, item in json.loads(result)["env_variables"].items()
            }
        elif style == "env":
            env = dict(line.split("=", 1) for line in result.splitlines())
        else:
            env = yaml.safe_load(result)["environment"]
            if style == "kubernetes":
                env = {item["name"]: item["value"] for item in env}
            elif style == "list":
                env = dict(item.split("=", 1) for item in env)
        assert {
            key: json.loads(value) if isinstance(value, str) else value
            for key, value in env.items()
        } == expected
        backups = list(path.parent.glob(path.name + ".bak-*"))
        assert len(backups) == 1
        assert backups[0].read_text() == original
    after = {path: path.read_bytes() for path in paths}
    run(tmp_path)
    assert all(path.read_bytes() == after[path] for path in paths)
    assert len(list(tmp_path.rglob("*.bak-*"))) == len(paths)


@pytest.mark.parametrize("style", ["env", "mapping", "list", "kubernetes"])
def test_quickstart_controls_replace_stale_operate_cache(tmp_path, style):
    """Sync explicit quickstart controls to cached config and deployment envs."""
    import yaml

    quickstart = tmp_path / "configs/config_predict_trader.json"
    quickstart.parent.mkdir()
    desired = {
        "MAX_TRADES": "1",
        "IRRELEVANT_TOOLS": '["factual_research","propose-question"]',
        "DEFAULT_ACTIVITY_GOAL": "8",
    }
    quickstart.write_text(
        json.dumps(
            {"env_variables": {key: entry(value) for key, value in desired.items()}}
        )
    )
    service = tmp_path / ".operate/services/sc-one"
    service.mkdir(parents=True)
    config = service / "config.json"
    config.write_text(
        json.dumps(
            {
                "env_variables": {"MAX_TRADES": entry("2")},
                "user_params": {"max_trades": 2},
                "keep": "value",
            }
        )
    )
    env = {
        "MAX_TRADES": "2",
        "SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_MAX_TRADES": "2",
        "KEEP": "unchanged",
    }
    path = service / ("agent_0.env" if style == "env" else "build.yaml")
    if style == "env":
        path.write_text("".join(key + "=" + value + "\n" for key, value in env.items()))
    else:
        values = (
            list(key + "=" + value for key, value in env.items())
            if style == "list"
            else (
                [{"name": key, "value": value} for key, value in env.items()]
                if style == "kubernetes"
                else env
            )
        )
        path.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "agent": {"environment": values},
                        "tendermint": {"environment": {"KEEP": "other"}},
                    }
                }
            )
        )
    originals = {p: p.read_bytes() for p in [quickstart, config, path]}
    for flag in ["--dry-run", "--warn-only"]:
        run(tmp_path, flag)
        assert all(p.read_bytes() == original for p, original in originals.items())
    assert not list(tmp_path.rglob("*.bak-*"))
    run(tmp_path)
    cached = json.loads(config.read_text())
    assert cached["env_variables"]["MAX_TRADES"]["value"] == "1"
    assert (
        cached["env_variables"]["IRRELEVANT_TOOLS"]["value"]
        == desired["IRRELEVANT_TOOLS"]
    )
    assert cached["user_params"]["max_trades"] == 1
    if style == "env":
        actual = dict(line.split("=", 1) for line in path.read_text().splitlines())
    else:
        services = yaml.safe_load(path.read_text())["services"]
        assert services["tendermint"]["environment"] == {"KEEP": "other"}
        actual = services["agent"]["environment"]
        if style == "list":
            actual = dict(value.split("=", 1) for value in actual)
        elif style == "kubernetes":
            actual = {value["name"]: value["value"] for value in actual}
    for key, value in desired.items():
        assert actual[key] == value
    assert actual["SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_MAX_TRADES"] == "1"
    assert actual["KEEP"] == "unchanged"
    assert quickstart.read_bytes() == originals[quickstart]
    backups = list(tmp_path.rglob("*.bak-*"))
    assert len(backups) == 2
    for p in [config, path]:
        assert list(p.parent.glob(p.name + ".bak-*"))[0].read_bytes() == originals[p]
    after = {p: p.read_bytes() for p in originals}
    run(tmp_path)
    assert all(p.read_bytes() == value for p, value in after.items())
    assert list(tmp_path.rglob("*.bak-*")) == backups


@pytest.mark.parametrize("current", ["1", "2"])
def test_sync_plain_cached_controls(tmp_path, current):
    """Plain cached values and stored string args must follow quickstart."""
    paths = fixture(tmp_path)
    source = {"MAX_TRADES": entry("1")}
    paths[0].write_text(json.dumps({"env_variables": source}))
    for path in paths[1:]:
        path.write_text(
            json.dumps(
                {
                    "env_variables": {"MAX_TRADES": current},
                    "user_params": {"max_trades": current},
                }
            )
        )
    run(tmp_path)
    for path in paths[1:]:
        assert json.loads(path.read_text()) == {
            "env_variables": {"MAX_TRADES": "1"},
            "user_params": {"max_trades": "1"},
        }


def test_sync_rejects_kubernetes_control_without_value(tmp_path):
    """A malformed env entry must fail before the cache is written."""
    source = tmp_path / "configs/config_predict_trader.json"
    source.parent.mkdir()
    source.write_text(json.dumps({"env_variables": {"MAX_TRADES": entry("1")}}))
    path = tmp_path / ".operate/services/sc-one/build.yaml"
    path.parent.mkdir(parents=True)
    text = "env:\n- name: MAX_TRADES\n  broken: true\n"
    path.write_text(text)
    with pytest.raises(ValueError, match="Missing value for MAX_TRADES"):
        run(tmp_path)
    assert path.read_text() == text
    assert not list(tmp_path.rglob("*.bak-*"))


@pytest.mark.parametrize(
    "filename", ["Trader Agent-quickstart-config.json", "local_config.json"]
)
@pytest.mark.parametrize("saved", [{"MAX_TRADES": "2"}, {}])
def test_sync_saved_quickstart_inputs(tmp_path, filename, saved):
    """Prevent saved user inputs from restoring stale values on redeploy."""
    source = fixture(tmp_path)[0]
    source.write_text(json.dumps({"env_variables": {"MAX_TRADES": entry("1")}}))
    path = tmp_path / ".operate" / filename
    untouched = {
        "KEEP": "value",
        "STRATEGIES_KWARGS": '{"min_edge": 0.2}',
        "MECH_MARKETPLACE_CONFIG": json.dumps(OLD),
    }
    original = json.dumps({"user_provided_args": dict(saved, **untouched)}) + "\n"
    path.write_text(original)
    for flag in ["--dry-run", "--warn-only"]:
        run(tmp_path, flag)
        assert path.read_text() == original
    assert not list(tmp_path.rglob("*.bak-*"))
    run(tmp_path)
    assert json.loads(path.read_text())["user_provided_args"] == dict(
        untouched, MAX_TRADES="1"
    )
    backups = list(path.parent.glob(path.name + ".bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    after = path.read_bytes()
    run(tmp_path)
    assert path.read_bytes() == after
    assert list(path.parent.glob(path.name + ".bak-*")) == backups


def test_sync_kubernetes_container_env(tmp_path):
    """Sync the list-item env block emitted by the Kubernetes generator."""
    import yaml

    source = fixture(tmp_path)[0]
    source.write_text(json.dumps({"env_variables": {"MAX_TRADES": entry("1")}}))
    path = tmp_path / ".operate/services/sc-one/deployment/abci_build_k8s/build.yaml"
    path.parent.mkdir(parents=True)
    original = """apiVersion: v1
kind: Service
---
kind: Deployment
spec:
  template:
    spec:
      containers:
      - env:
        - name: KEEP
          value: tendermint
        name: node0
      - env:
        - name: SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_MAX_TRADES
          value: '2'
        - name: KEEP
          value: agent
        image: agent-image
        name: abci0
"""
    path.write_text(original)
    run(tmp_path, "--dry-run")
    assert path.read_text() == original
    run(tmp_path)
    documents = list(yaml.safe_load_all(path.read_text()))
    containers = documents[1]["spec"]["template"]["spec"]["containers"]
    assert containers[0]["env"] == [{"name": "KEEP", "value": "tendermint"}]
    assert containers[1]["env"] == [
        {"name": "SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_MAX_TRADES", "value": "1"},
        {"name": "KEEP", "value": "agent"},
        {"name": "MAX_TRADES", "value": "1"},
    ]
    assert containers[1]["image"] == "agent-image"
    assert list(path.parent.glob("*.bak-*"))[0].read_text() == original
    after = path.read_bytes()
    run(tmp_path)
    assert path.read_bytes() == after
    assert len(list(path.parent.glob("*.bak-*"))) == 1


@pytest.mark.parametrize("released", [False, True])
def test_release_sync_survives_saved_input_reload(tmp_path, released):
    """Release must sync the inputs that operate uses to rebuild the cache."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in [
        "release_service.sh",
        "scripts/migrate_trader_config.py",
        "packages/packages.json",
        "packages/valory/services/trader_pearl/service.yaml",
    ]:
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SCRIPT.parents[1] / relative, target)
    binaries = repo / ".venv/bin"
    binaries.mkdir(parents=True)
    for command in ["autonomy", "aea", "docker", "curl"]:
        target = binaries / command
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
    qs = tmp_path / "quickstart"
    paths = fixture(qs)
    cid = json.loads((repo / "packages/packages.json").read_text())["dev"][
        "service/valory/trader_pearl/0.1.0"
    ]
    paths[0].write_text(
        json.dumps(
            {
                "hash": cid if released else "old-service",
                "agent_release": {"repository": {"owner": "dagacha"}},
                "env_variables": {"MAX_TRADES": entry("1")},
            }
        )
    )
    saved = qs / ".operate/Trader Agent-quickstart-config.json"
    saved.write_text(json.dumps({"user_provided_args": {"MAX_TRADES": "2"}}))
    env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ["PATH"])
    subprocess.run(
        [
            "bash",
            str(repo / "release_service.sh"),
            "--skip-publish",
            "--quickstart",
            str(qs),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    # Operate applies saved user inputs to the template before it builds envs.
    value = json.loads(saved.read_text())["user_provided_args"]["MAX_TRADES"]
    assert value == "1"
    for path in paths[1:]:
        assert (
            json.loads(path.read_text())["env_variables"]["MAX_TRADES"]["value"]
            == value
        )
    assert json.loads(paths[0].read_text())["hash"] == cid


@pytest.mark.parametrize("style", ["env", "mapping", "list", "kubernetes"])
@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize("stale", [False, True])
def test_priority_inputs_sync_everywhere(tmp_path, style, mapping, stale):
    """Sync saved priority inputs and the dict used by the agent."""
    import yaml

    desired = {"PRIORITY_MECH_ADDRESS": "0x552c", "PRIORITY_MECH_SERVICE_ID": "975"}
    current = (
        {"PRIORITY_MECH_ADDRESS": "0xC05e", "PRIORITY_MECH_SERVICE_ID": "2182"}
        if stale
        else desired
    )
    mech = {
        "priority_mech_address": current["PRIORITY_MECH_ADDRESS"],
        "priority_mech_service_id": int(current["PRIORITY_MECH_SERVICE_ID"]),
        "response_timeout": 300,
        "requester_staking_instance_address": "keep-requester",
    }
    expected = dict(mech, priority_mech_address="0x552c", priority_mech_service_id=975)
    encoded = mech if mapping else json.dumps(mech)
    source = tmp_path / "configs/config_predict_trader.json"
    source.parent.mkdir()
    source.write_text(
        json.dumps(
            {
                "env_variables": {
                    **{
                        key: dict(entry(value), provision_type="user")
                        for key, value in desired.items()
                    },
                    "MECH_MARKETPLACE_CONFIG": entry(json.dumps(expected)),
                }
            }
        )
    )
    service = tmp_path / ".operate/services/sc-one"
    service.mkdir(parents=True)
    saved = tmp_path / ".operate/Trader Agent-quickstart-config.json"
    saved.write_text(
        json.dumps(
            {
                "user_provided_args": dict(
                    current, MECH_MARKETPLACE_CONFIG=encoded, KEEP="saved"
                )
            }
        )
    )
    config = service / "config.json"
    config.write_text(
        json.dumps(
            {
                "env_variables": {
                    **{key: entry(value) for key, value in current.items()},
                    "MECH_MARKETPLACE_CONFIG": entry(encoded),
                },
                "user_params": {"mech_marketplace_config": encoded},
            }
        )
    )
    env = dict(
        current,
        MECH_MARKETPLACE_CONFIG=json.dumps(mech, separators=(",", ":")),
        KEEP="agent",
    )
    env.update(
        {
            "SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_" + key: value
            for key, value in current.items()
        }
    )
    env["SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_MECH_MARKETPLACE_CONFIG"] = env[
        "MECH_MARKETPLACE_CONFIG"
    ]
    agent = service / "agent_0.env"
    agent.write_text("".join(key + "=" + value + "\n" for key, value in env.items()))
    build = service / "build.yaml"
    values = (
        [key + "=" + value for key, value in env.items()]
        if style == "list"
        else (
            [{"name": key, "value": value} for key, value in env.items()]
            if style == "kubernetes"
            else env
        )
    )
    build.write_text(
        "environment:\n"
        + "".join("  - " + key + "=" + value + "\n" for key, value in env.items())
        if style == "env"
        else yaml.safe_dump({"environment": values}, width=60)
    )
    paths = [source, saved, config, agent, build]
    before = {path: path.read_bytes() for path in paths}
    for flag in ["--dry-run", "--warn-only"]:
        output = run(tmp_path, flag)
        assert ("WARNING: priority mech differs" in output) == stale
        assert all(path.read_bytes() == before[path] for path in paths)
        assert not list(tmp_path.rglob("*.bak-*"))
    assert "WARNING: priority mech differs" not in run(tmp_path)
    actual_saved = json.loads(saved.read_text())["user_provided_args"]
    assert actual_saved == dict(
        desired,
        MECH_MARKETPLACE_CONFIG=expected if mapping else json.dumps(expected),
        KEEP="saved",
    )
    actual_config = json.loads(config.read_text())
    for key, value in desired.items():
        assert actual_config["env_variables"][key]["value"] == value
    for value in [
        actual_config["env_variables"]["MECH_MARKETPLACE_CONFIG"]["value"],
        actual_config["user_params"]["mech_marketplace_config"],
    ]:
        assert (value if mapping else json.loads(value)) == expected
    actual_env = dict(line.split("=", 1) for line in agent.read_text().splitlines())
    actual_build = yaml.safe_load(build.read_text())["environment"]
    if style in ("env", "list"):
        actual_build = dict(value.split("=", 1) for value in actual_build)
    elif style == "kubernetes":
        actual_build = {item["name"]: item["value"] for item in actual_build}
    for actual in [actual_env, actual_build]:
        assert json.loads(actual["MECH_MARKETPLACE_CONFIG"]) == expected
        assert (
            json.loads(
                actual["SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_MECH_MARKETPLACE_CONFIG"]
            )
            == expected
        )
        assert actual["KEEP"] == "agent"
        for key, value in desired.items():
            assert actual[key] == value
            assert actual["SKILL_TRADER_ABCI_MODELS_PARAMS_ARGS_" + key] == value
    after = {path: path.read_bytes() for path in paths}
    backups = list(tmp_path.rglob("*.bak-*"))
    assert len(backups) == (4 if stale else 0)
    assert "WARNING: priority mech differs" not in run(tmp_path)
    assert all(path.read_bytes() == after[path] for path in paths)
    assert list(tmp_path.rglob("*.bak-*")) == backups
    if not stale:
        assert after == before


@pytest.mark.parametrize("value", ["", None, {}])
def test_priority_sync_preserves_empty_marketplace(tmp_path, value):
    """Sync priority inputs without making a missing marketplace dict."""
    source = fixture(tmp_path)[0]
    source.write_text(
        json.dumps({"env_variables": {"PRIORITY_MECH_SERVICE_ID": entry("975")}})
    )
    saved = tmp_path / ".operate/local_config.json"
    saved.write_text(
        json.dumps({"user_provided_args": {"MECH_MARKETPLACE_CONFIG": value}})
    )
    run(tmp_path)
    assert json.loads(saved.read_text())["user_provided_args"] == {
        "MECH_MARKETPLACE_CONFIG": value,
        "PRIORITY_MECH_SERVICE_ID": "975",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [("PRIORITY_MECH_ADDRESS", "0x552c"), ("PRIORITY_MECH_SERVICE_ID", "975")],
)
def test_priority_sync_uses_only_explicit_inputs(tmp_path, key, value):
    """Keep an unset priority field and add an explicit field when absent."""
    paths = fixture(tmp_path)
    paths[0].write_text(json.dumps({"env_variables": {key: entry(value)}}))
    mech = {"response_timeout": 300}
    paths[1].write_text(
        json.dumps({"env_variables": {"MECH_MARKETPLACE_CONFIG": entry(mech)}})
    )
    run(tmp_path)
    expected = dict(mech, **{key.lower(): int(value) if key.endswith("ID") else value})
    assert (
        json.loads(paths[1].read_text())["env_variables"]["MECH_MARKETPLACE_CONFIG"][
            "value"
        ]
        == expected
    )


def test_skip_fast_template_and_caches(tmp_path):
    """Keep the fast strategy out of the normal trader migration."""
    template = tmp_path / "configs/config_omenstrat_fast.json"
    cache = tmp_path / ".operate/services/sc-fast/config.json"
    env = tmp_path / ".operate/services/sc-fast/agent_0.env"
    for path in (template, cache, env):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "SKILL_OMENSTRAT_FAST_ABCI_MODELS_PARAMS_ARGS_SERVICE_ID=omenstrat_fast\n"
            if path == env
            else json.dumps(
                {
                    "name": "omenstrat_fast",
                    "env_variables": {
                        "STRATEGIES_KWARGS": entry(
                            '{"absolute_max_bet_size":4000000000000000000}'
                        )
                    },
                }
            )
        )
    before = {path: path.read_bytes() for path in (template, cache, env)}
    from scripts import migrate_trader_config

    output_stream = io.StringIO()
    with redirect_stdout(output_stream):
        migrate_trader_config.migrate(tmp_path)
    output = output_stream.getvalue()
    assert "omenstrat_fast: skipping trader migration" in output
    assert all(path.read_bytes() == data for path, data in before.items())
    assert not list(tmp_path.rglob("*.bak-*"))
