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

"""Test fresh-watch component bindings, parameters, and initial state."""

from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from packages.valory.skills.fresh_watch_abci import dialogues, handlers
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.models import FastParams, SharedState
from packages.valory.skills.fresh_watch_abci.rounds import FreshWatchAbciApp
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig
from packages.valory.skills.trader_abci import dialogues as trader_dialogues
from packages.valory.skills.trader_abci import handlers as trader_handlers


@pytest.fixture
def config() -> Any:
    """Read this skill's component configuration."""
    return yaml.safe_load((Path(__file__).parents[1] / "skill.yaml").read_text())


@pytest.fixture
def params_args(config: Any, tmp_path: Path) -> Any:
    """Use the declared controls with a writable local store."""
    args = config["models"]["params"]["args"]
    args["store_path"] = str(tmp_path)
    return args


def test_declared_components_reuse_trader_bindings(config: Any) -> None:
    """Resolve each declared handler and dialogue to the reused trader class."""
    for entry in config["handlers"].values():
        name = entry["class_name"]
        assert getattr(handlers, name) is getattr(trader_handlers, name)
    for key, entry in config["models"].items():
        if key.endswith("_dialogues"):
            name = entry["class_name"]
            assert getattr(dialogues, name) is getattr(trader_dialogues, name)


@pytest.mark.parametrize("dynamic", [False, True])
def test_declared_params(params_args: Any, dynamic: bool) -> None:
    """Load the declared controls and apply dynamic mech selection."""
    params_args["fast_dynamic_mech"] = dynamic
    params_args["use_offchain"] = dynamic
    params = FastParams(name="params", skill_context=MagicMock(), **params_args)
    assert params.fast == FastConfig()
    assert params.lightning == LightningConfig()
    assert params.fast_dynamic_mech is dynamic
    assert params.use_offchain is dynamic
    assert params.mech_marketplace_config.use_dynamic_mech_selection is dynamic


def test_missing_lightning_params_keep_fast_defaults(params_args: Any) -> None:
    """Load older fast configurations with all lightning controls off."""
    defaults = LightningConfig()
    for item in fields(defaults):
        params_args.pop(item.name)
    params = FastParams(name="params", skill_context=MagicMock(), **params_args)
    assert params.lightning == defaults
    assert params.lightning.lightning_still_hold is False
    assert params.lightning.lightning_chain_detect is False
    assert params.lightning.lightning_preview is False


def test_lane_inherits_uncapped_global_trade_budget(params_args: Any) -> None:
    """Keep the inherited nightly budget unlimited for an unlimited service."""
    params_args["max_trades"] = 0
    params_args["lightning_lane_max_per_night"] = -1
    params = FastParams(name="params", skill_context=MagicMock(), **params_args)
    assert params.lightning.lightning_lane_max_per_night == -1


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"price_margin": -0.01}, "0 <= price_margin < min"),
        ({"price_margin": 0.2}, "0 <= price_margin < min"),
        ({"is_running_on_polymarket": True}, "supports Omen only"),
        ({"tools": ["other"]}, "two fast tools in priority order"),
    ],
)
def test_invalid_params(params_args: Any, overrides: Any, message: str) -> None:
    """Reject controls that cannot run the fixed Omen strategy."""
    with pytest.raises(ValueError, match=message):
        FastParams(
            name="params", skill_context=MagicMock(), **{**params_args, **overrides}
        )


def test_initial_state() -> None:
    """Start the poller with no prior activity and no running background task."""
    state = SharedState(name="state", skill_context=MagicMock())
    assert state.abci_app_cls is FreshWatchAbciApp
    assert state.fast_activity_status is None
    assert state.fast_last_poll == 0
    assert state.fast_background_running is False
    assert state.fast_polling is False
