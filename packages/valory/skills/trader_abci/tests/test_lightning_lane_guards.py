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

"""Verify each Safe transaction entry point waits for the lightning lane."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.decision_maker_abci.behaviours.bet_placement import (
    BetPlacementBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.omen_withdraw import (
    OmenWithdrawBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.polymarket_reedem import (
    PolymarketRedeemBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.polymarket_set_approval import (
    PolymarketSetApprovalBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.polymarket_swap import (
    PolymarketSwapUsdcBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.polymarket_sweep import (
    PolymarketSweepBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.polymarket_top_up import (
    PolymarketTopUpBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.polymarket_withdraw import (
    PolymarketWithdrawBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.polymarket_withdraw_top_up import (
    PolymarketWithdrawTopUpBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.polymarket_wrap_collateral import (
    PolymarketWrapCollateralBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.reedem import RedeemBehaviour
from packages.valory.skills.decision_maker_abci.behaviours.sell_outcome_tokens import (
    SellOutcomeTokensBehaviour,
)
from packages.valory.skills.staking_abci.behaviours import CallCheckpointBehaviour
from packages.valory.skills.trader_abci.behaviours import (
    TraderBackgroundBehaviour as BackgroundBehaviour,
)
from packages.valory.skills.trader_abci.mech_latency import (
    TraderMechPurchaseSubscriptionBehaviour as MechPurchaseSubscriptionBehaviour,
)
from packages.valory.skills.trader_abci.mech_latency import (
    TraderMechRequestBehaviour as MechRequestBehaviour,
)


@pytest.mark.parametrize(
    "behaviour_cls",
    [
        BetPlacementBehaviour,
        RedeemBehaviour,
        SellOutcomeTokensBehaviour,
        OmenWithdrawBehaviour,
        PolymarketWrapCollateralBehaviour,
        PolymarketSwapUsdcBehaviour,
        PolymarketTopUpBehaviour,
        PolymarketWithdrawBehaviour,
        PolymarketWithdrawTopUpBehaviour,
        PolymarketSweepBehaviour,
        PolymarketSetApprovalBehaviour,
        PolymarketRedeemBehaviour,
        CallCheckpointBehaviour,
    ],
)
def test_safe_preparation_waits_at_start(behaviour_cls: Any) -> None:
    """Do not read or prepare Safe state before the shared lane guard exits."""
    behaviour = object.__new__(behaviour_cls)
    sentinel = object()
    with patch.object(
        behaviour, "wait_for_lightning_lane", return_value=iter([sentinel])
    ):
        assert next(behaviour.async_act()) is sentinel


def test_offchain_deposit_waits_and_reserves_safe() -> None:
    """Guard the deposit path even when its Mech request round is whitelisted."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from packages.valory.skills.trader_abci.mech_latency import (
        TimedOffchainRequestExecutor,
    )

    sentinel = object()
    behaviour = SimpleNamespace(
        wait_for_lightning_lane=MagicMock(return_value=iter([sentinel]))
    )
    executor = TimedOffchainRequestExecutor(behaviour)
    challenge: Any = None
    assert next(executor._build_deposit_tx(challenge, b"", 0, 0)) is sentinel
    behaviour.wait_for_lightning_lane.assert_called_once_with(reserve=True)


def test_termination_preparation_reserves_safe() -> None:
    """Exclude background termination preparation from otherwise idle rounds."""
    behaviour = object.__new__(BackgroundBehaviour)
    sentinel = object()
    with patch.object(
        behaviour, "wait_for_lightning_lane", return_value=iter([sentinel])
    ) as guard:
        assert (
            next(
                behaviour.get_contract_api_response(
                    cast(
                        ContractApiMessage.Performative,
                        ContractApiMessage.Performative.GET_STATE,
                    ),
                    "safe",
                    "valory/gnosis_safe:0.1.0",
                    "get_raw_safe_transaction_hash",
                )
            )
            is sentinel
        )
        guard.assert_called_once_with(reserve=True)


@pytest.mark.parametrize(
    "behaviour_cls",
    [MechRequestBehaviour, MechPurchaseSubscriptionBehaviour, BackgroundBehaviour],
)
def test_upstream_safe_preparation_waits_in_scheduler_wrapper(
    behaviour_cls: Any,
) -> None:
    """Apply the shared guard before an upstream Safe behaviour starts."""
    behaviour = object.__new__(behaviour_cls)
    behaviour._context = SimpleNamespace(
        params=SimpleNamespace(lightning=SimpleNamespace(lightning_lane=True))
    )
    sentinel = object()
    with patch.object(
        behaviour, "wait_for_lightning_lane", return_value=iter([sentinel])
    ):
        assert next(behaviour.async_act_wrapper()) is sentinel


@pytest.mark.parametrize(
    "round_id",
    ["background_round", "mech_request_round", "mech_purchase_subscription_round"],
)
def test_service_selects_local_guarded_upstream_behaviours(round_id: str) -> None:
    """Ship Safe guards without a changed upstream package hash."""
    from packages.valory.skills.trader_abci.behaviours import TraderConsensusBehaviour

    selected = (
        TraderConsensusBehaviour.behaviours
        | TraderConsensusBehaviour.background_behaviours_cls
    )
    behaviour_cls = next(
        cls for cls in selected if cls.matching_round.auto_round_id() == round_id
    )
    assert (
        behaviour_cls.get_contract_api_response.__module__
        == "packages.valory.skills.staking_abci.lane_guard"
    )


@pytest.mark.parametrize(
    "behaviour_cls,expected",
    [
        (BackgroundBehaviour, "background_behaviour"),
        (MechPurchaseSubscriptionBehaviour, "mech_purchase_subscription_behaviour"),
    ],
)
def test_guarded_wrappers_keep_existing_behaviour_ids(
    behaviour_cls: Any, expected: str
) -> None:
    """Keep the existing scheduler and log names when the lane is off."""
    assert behaviour_cls.auto_behaviour_id() == expected
