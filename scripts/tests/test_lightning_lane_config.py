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
from scripts.tests.test_deploy_migration import entry, fixture, run
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
    """Preserve explicit controls and remove only defaults added by migration."""
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
    if existing:
        assert (
            rolled_back["LIGHTNING_LANE"] == config["env_variables"]["LIGHTNING_LANE"]
        )
        assert (
            rolled_back["LIGHTNING_RPC_URL"]
            == config["env_variables"]["LIGHTNING_RPC_URL"]
        )
