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

"""Check the optional background lane lifecycle."""

from typing import Any, cast
from unittest.mock import MagicMock, patch


def test_background_lane_lifecycle(tmp_path: Any) -> None:
    """Drive and stop the lane through the existing background hooks."""
    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        OmenstratFastConsensusBehaviour,
    )
    from packages.valory.skills.trader_abci.behaviours import TraderConsensusBehaviour

    context = MagicMock()
    context.params.lightning.lightning_lane = True
    b = OmenstratFastConsensusBehaviour(name="test", skill_context=context)
    with (
        patch.object(TraderConsensusBehaviour, "setup"),
        patch(
            "packages.valory.skills.omenstrat_fast_abci.behaviours.FreshMarketPoller"
        ),
        patch(
            "packages.valory.skills.omenstrat_fast_abci.behaviours.LightningLaneBehaviour"
        ) as cls,
    ):
        b.setup()
    assert b.lightning_lane is cls.return_value
    with patch.object(TraderConsensusBehaviour, "_background_act"):
        b._background_act()
    cast(Any, b.lightning_lane.act_wrapper).assert_called_once()
    with patch.object(TraderConsensusBehaviour, "teardown"):
        b.teardown()
    cast(Any, b.lightning_lane.stop).assert_called_once()


def test_background_lane_is_absent_when_disabled() -> None:
    """Keep the existing background loop unchanged with the lane flag off."""
    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        OmenstratFastConsensusBehaviour,
    )
    from packages.valory.skills.trader_abci.behaviours import TraderConsensusBehaviour

    context = MagicMock()
    context.params.lightning.lightning_lane = False
    b = OmenstratFastConsensusBehaviour(name="test", skill_context=context)
    with (
        patch.object(TraderConsensusBehaviour, "setup"),
        patch(
            "packages.valory.skills.omenstrat_fast_abci.behaviours.FreshMarketPoller"
        ),
        patch(
            "packages.valory.skills.omenstrat_fast_abci.behaviours.LightningLaneBehaviour"
        ) as cls,
    ):
        b.setup()
    cls.assert_not_called()
    assert b.lightning_lane is None
    with patch.object(TraderConsensusBehaviour, "_background_act"):
        b._background_act()
    with patch.object(TraderConsensusBehaviour, "teardown"):
        b.teardown()


def test_fast_service_uses_guarded_upstream_wrappers() -> None:
    """Keep every upstream Safe preparation path on a published dependency hash."""
    from packages.valory.skills.omenstrat_fast_abci.behaviours import (
        OmenstratFastConsensusBehaviour,
    )

    selected = (
        OmenstratFastConsensusBehaviour.behaviours
        | OmenstratFastConsensusBehaviour.background_behaviours_cls
    )
    for round_id in (
        "background_round",
        "mech_request_round",
        "mech_purchase_subscription_round",
    ):
        classes = [
            cls for cls in selected if cls.matching_round.auto_round_id() == round_id
        ]
        assert classes
        assert all(
            cls.get_contract_api_response.__module__
            == "packages.valory.skills.staking_abci.lane_guard"
            for cls in classes
        )
