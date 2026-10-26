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

"""Test standalone loading and the fast service defaults."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from aea.components.base import _CheckUsedDependencies
from aea.configurations.base import ComponentType
from aea.configurations.loader import load_component_configuration

from packages.valory.skills.omenstrat_fast_abci.behaviours import (
    OmenstratFastConsensusBehaviour,
)
from packages.valory.skills.omenstrat_fast_abci.composition import OmenstratFastAbciApp

ROOT = Path(__file__).resolve().parents[5]


def test_behaviour_coverage() -> None:
    """Each live round has exactly one matching behaviour."""
    covered = [b.matching_round for b in OmenstratFastConsensusBehaviour.behaviours]
    assert len(covered) == len(set(covered))
    for round_, transitions in OmenstratFastAbciApp.transition_function.items():
        if transitions:
            assert round_ in covered


def test_dependencies_and_params() -> None:
    """Declare all package imports and override parameters."""
    for name in ("fresh_watch_abci", "omenstrat_fast_abci"):
        directory = ROOT / "packages/valory/skills" / name
        config = load_component_configuration(
            ComponentType.SKILL, directory, skip_consistency_check=True
        )
        config.directory = directory
        _CheckUsedDependencies(config).run_check()
    skill = yaml.safe_load(
        (ROOT / "packages/valory/skills/omenstrat_fast_abci/skill.yaml").read_text()
    )
    for relative in (
        "agents/omenstrat_fast/aea-config.yaml",
        "services/omenstrat_fast/service.yaml",
    ):
        docs = list(
            yaml.safe_load_all((ROOT / "packages/valory" / relative).read_text())
        )
        override = next(
            doc
            for doc in docs[1:]
            if doc["public_id"].startswith("valory/omenstrat_fast_abci:")
        )
        args = override["models"]["params"]["args"]
        assert set(args) <= set(skill["models"]["params"]["args"])
        assert args["overlap_s"] == "${OVERLAP_S:int:120}"
        assert "USE_OFFCHAIN:bool:true" in args["use_offchain"]
        assert (
            "STOP_TRADING_IF_STAKING_KPI_MET:bool:false"
            in args["stop_trading_if_staking_kpi_met"]
        )


def test_params_and_state(tmp_path: Any) -> None:
    """Load fast controls through the real parameter model."""
    import importlib
    from unittest.mock import MagicMock, patch

    from packages.valory.skills.fresh_watch_abci.models import (
        FastParams,
    )
    from packages.valory.skills.fresh_watch_abci.models import SharedState as FreshState
    from packages.valory.skills.omenstrat_fast_abci.models import SharedState
    from packages.valory.skills.trader_abci.models import SharedState as TraderState

    for skill in ("fresh_watch_abci", "omenstrat_fast_abci"):
        for module in ("models", "handlers", "dialogues"):
            assert importlib.import_module(f"packages.valory.skills.{skill}.{module}")
    config = yaml.safe_load(
        (ROOT / "packages/valory/skills/omenstrat_fast_abci/skill.yaml").read_text()
    )
    context = MagicMock()
    args = config["models"]["params"]["args"]
    args["store_path"] = str(tmp_path)
    params = FastParams(name="params", skill_context=context, **args)
    assert params.fast.overlap_s == 120
    assert params.fast.cap == 4
    assert params.fast.min_stake == 0.1
    assert params.fast.max_slip == 0.2
    assert params.fast.price_margin == 0.01
    custom = FastParams(
        name="custom",
        skill_context=context,
        **{**args, "min_stake": 0.25, "price_margin": 0.0},
    )
    assert custom.fast.min_stake == 0.25
    assert custom.fast.price_margin == 0.0
    assert params.use_offchain is True
    assert params.fast_dynamic_mech is True
    assert params.mech_marketplace_config.use_dynamic_mech_selection is True
    assert params.mech_marketplace_config.offchain_poll_interval_seconds == 1.5
    assert params.fast.tools == [
        "superforcaster_calibrated_full_search",
        "superforcaster",
    ]
    for invalid, value in (("is_running_on_polymarket", True), ("tools", ["other"])):
        with pytest.raises(ValueError):
            FastParams(name="params", skill_context=context, **{**args, invalid: value})
    state = SharedState(name="state", skill_context=context)
    assert state.fast_last_poll == 0
    context.params = params
    with patch.object(TraderState, "setup"):
        state.setup()
    assert OmenstratFastAbciApp.event_to_timeout
    fresh = FreshState(name="state", skill_context=context)
    assert fresh.fast_last_poll == 0


@pytest.mark.parametrize("dynamic", [False, True])
def test_priority_mech_only(dynamic: bool) -> None:
    """Ignore the broad allowlist and discover only the configured priority."""
    from unittest.mock import MagicMock

    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        FastMechInformationBehaviour,
    )

    context = MagicMock()
    context.params.mech_marketplace_config.priority_mech_address = "0xABC"
    context.params.mech_marketplace_config.use_dynamic_mech_selection = dynamic
    context.params.valid_mechs = ["0xselected"]
    context.state.synchronized_data.approved_mechs = []
    behaviour = FastMechInformationBehaviour(name="info", skill_context=context)
    assert behaviour.eligible_mechs == (["0xselected"] if dynamic else ["0xabc"])


def test_only_fast_composition_is_active() -> None:
    """The imported trader package must not start a second trading app."""
    fresh = yaml.safe_load(
        (ROOT / "packages/valory/skills/fresh_watch_abci/skill.yaml").read_text()
    )
    assert fresh["is_abstract"] is True
    docs = list(
        yaml.safe_load_all(
            (ROOT / "packages/valory/agents/omenstrat_fast/aea-config.yaml").read_text()
        )
    )
    trader = next(
        doc for doc in docs[1:] if doc["public_id"].startswith("valory/trader_abci:")
    )
    assert trader["is_abstract"] is True


def test_funds_manager_override_is_declared() -> None:
    """The inherited Omen service override must pass AEA validation."""
    funds = yaml.safe_load(
        (ROOT / "packages/valory/skills/funds_manager/skill.yaml").read_text()
    )
    for name in ("omenstrat_fast", "trader_pearl"):
        docs = list(
            yaml.safe_load_all(
                (ROOT / "packages/valory/services" / name / "service.yaml").read_text()
            )
        )
        override = next(
            doc
            for doc in docs[1:]
            if doc["public_id"].startswith("valory/funds_manager:")
        )
        args = override["models"]["params"]["args"]
        assert set(args) <= set(funds["models"]["params"]["args"])


def test_discovery_lifecycle(tmp_path: Any) -> None:
    """Drive the poller beside a busy foreground behaviour and stop it on exit."""
    from unittest.mock import MagicMock, patch

    from packages.valory.skills.trader_abci.behaviours import TraderConsensusBehaviour

    context = MagicMock()
    context.params.store_path = tmp_path
    b = OmenstratFastConsensusBehaviour(name="main", skill_context=context)
    with patch.object(TraderConsensusBehaviour, "setup"):
        b.setup()
    assert context.state.fast_background_running is True
    with (
        patch.object(TraderConsensusBehaviour, "_background_act"),
        patch.object(b.discovery, "act_wrapper") as tick,
    ):
        b._background_act()
        tick.assert_called_once()
    with (
        patch.object(TraderConsensusBehaviour, "teardown"),
        patch.object(b.discovery, "stop") as stop,
    ):
        b.teardown()
        stop.assert_called_once()
        del b.discovery
        b.teardown()
    assert context.state.fast_background_running is False


def test_overlap_defaults() -> None:
    """Declare the overlap in both skills and the quickstart template."""
    import json

    for name in ("fresh_watch_abci", "omenstrat_fast_abci"):
        config = yaml.safe_load(
            (ROOT / "packages/valory/skills" / name / "skill.yaml").read_text()
        )
        assert config["models"]["params"]["args"]["overlap_s"] == 120
    config = json.loads((ROOT / "configs/config_omenstrat_fast.json").read_text())
    assert config["env_variables"]["OVERLAP_S"]["value"] == "120"


def test_dynamic_defaults() -> None:
    """Expose dynamic selection in both deployment entry points."""
    import json

    for relative in (
        "agents/omenstrat_fast/aea-config.yaml",
        "services/omenstrat_fast/service.yaml",
    ):
        docs = list(
            yaml.safe_load_all((ROOT / "packages/valory" / relative).read_text())
        )
        args = next(
            doc
            for doc in docs[1:]
            if doc["public_id"].startswith("valory/omenstrat_fast_abci:")
        )["models"]["params"]["args"]
        assert args["fast_dynamic_mech"] == "${FAST_DYNAMIC_MECH:bool:true}"
        assert "0xdb78159e9246ec738f51c2c9cb1169b5c0e45fee" in args["valid_mechs"]
    config = json.loads((ROOT / "configs/config_omenstrat_fast.json").read_text())
    assert config["env_variables"]["FAST_DYNAMIC_MECH"]["value"] == "true"


def test_fast_uses_shared_timing_behaviours() -> None:
    """The fast composition keeps the shared request and answer logging."""
    from packages.valory.skills.trader_abci.mech_latency import (
        TraderMechRequestBehaviour,
        TraderMechResponseBehaviour,
        TraderPostTxSettlementBehaviour,
    )

    assert {
        TraderMechRequestBehaviour,
        TraderMechResponseBehaviour,
        TraderPostTxSettlementBehaviour,
    } <= OmenstratFastConsensusBehaviour.behaviours


def test_dynamic_param_declared_in_overriding_skills() -> None:
    """Declare the control in every composed parameter model."""
    for skill, default in (
        ("trader_abci", False),
        ("fresh_watch_abci", True),
        ("omenstrat_fast_abci", True),
    ):
        config = yaml.safe_load(
            (ROOT / "packages/valory/skills" / skill / "skill.yaml").read_text()
        )
        assert config["models"]["params"]["args"]["fast_dynamic_mech"] is default


def test_slip_and_minimum_stake_defaults() -> None:
    """Keep fast controls consistent across each deployment entry point."""
    import json
    import runpy

    from packages.valory.skills.fresh_watch_abci.strategy import FastConfig

    defaults = {"max_slip": 0.2, "min_stake": 0.1, "price_margin": 0.01}
    for key, value in defaults.items():
        assert getattr(FastConfig(), key) == value
    for skill in ("fresh_watch_abci", "omenstrat_fast_abci"):
        config = yaml.safe_load(
            (ROOT / "packages/valory/skills" / skill / "skill.yaml").read_text()
        )
        for key, value in defaults.items():
            assert config["models"]["params"]["args"][key] == value
    for relative in (
        "agents/omenstrat_fast/aea-config.yaml",
        "services/omenstrat_fast/service.yaml",
    ):
        docs = list(
            yaml.safe_load_all((ROOT / "packages/valory" / relative).read_text())
        )
        args = next(
            doc
            for doc in docs[1:]
            if doc["public_id"].startswith("valory/omenstrat_fast_abci:")
        )["models"]["params"]["args"]
        for key, value in defaults.items():
            assert args[key] == "${" + key.upper() + ":float:" + str(value) + "}"
    config = json.loads((ROOT / "configs/config_omenstrat_fast.json").read_text())
    migration = runpy.run_path(str(ROOT / "scripts/migrate_trader_config.py"))
    for key, value in defaults.items():
        assert config["env_variables"][key.upper()]["value"] == str(value)
        assert key.upper() in migration["FAST_ENV"]


@pytest.mark.parametrize(
    "margin,max_slip,max_price",
    [(-0.01, 0.2, 0.9), (0.2, 0.2, 0.9), (0.3, 0.2, 0.9), (0.1, 0.2, 0.1)],
)
def test_invalid_price_margin(
    tmp_path: Any, margin: float, max_slip: float, max_price: float
) -> None:
    """Reject a margin that leaves no permitted price or slip range."""
    from unittest.mock import MagicMock

    from packages.valory.skills.fresh_watch_abci.models import FastParams

    config = yaml.safe_load(
        (ROOT / "packages/valory/skills/omenstrat_fast_abci/skill.yaml").read_text()
    )
    args = {
        **config["models"]["params"]["args"],
        "store_path": str(tmp_path),
        "price_margin": margin,
        "max_slip": max_slip,
        "max_price": max_price,
    }
    with pytest.raises(ValueError, match="0 <= price_margin < min"):
        FastParams(name="params", skill_context=MagicMock(), **args)


def list_prefix_collisions(lists: Any, names: Any) -> Any:
    """Return (list name, other name) pairs that aea's parse_list would mix.

    :param lists: names of list placeholders.
    :param names: all env names.
    :return: sorted collisions.
    """
    return sorted(
        (name, other)
        for name in lists
        for other in names
        if other != name and other.startswith(name)
    )


def test_list_prefix_rule_matches_aea() -> None:
    """aea collects every name that starts with the list name, with or without "_"."""
    assert list_prefix_collisions({"FAST_TOOLS"}, {"FAST_TOOLS", "FAST_TOOLSET"}) == [
        ("FAST_TOOLS", "FAST_TOOLSET")
    ]


def test_list_env_names_do_not_prefix_other_names() -> None:
    """A missing list variable must not collect another variable's value.

    When a named list variable X is not set, aea builds the list from every
    variable whose name starts with "X". A second such name then breaks the
    agent start with a JSONDecodeError.
    """
    import json
    import re

    files = [
        *(ROOT / "packages/valory/agents").glob("*/aea-config.yaml"),
        *(ROOT / "packages/valory/services").glob("*/service.yaml"),
    ]
    templates = list((ROOT / "configs").glob("config_*.json"))
    assert files and templates
    names = set()
    lists = set()
    for path in files:
        text = path.read_text()
        names.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*):", text))
        lists.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*):list:", text))
    for path in templates:
        names.update(json.loads(path.read_text()).get("env_variables", {}))
    assert lists
    assert not list_prefix_collisions(lists, names)
