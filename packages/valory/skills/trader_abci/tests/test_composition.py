# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2024-2026 Valory AG
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

"""Tests for the composition module of the trader_abci skill."""

# pylint: skip-file

from packages.valory.skills.decision_maker_abci.states.redeem_router import (
    RedeemRouterRound,
)
from packages.valory.skills.decision_maker_abci.states.trade_count import TradeCountRound
from packages.valory.skills.decision_maker_abci.states.final_states import (
    FinishedMechOnlyRequestRound,
    FinishedMechOnlyRound,
)
from packages.valory.skills.decision_maker_abci.states.mech_only import (
    MechResponseRouterRound,
)
from packages.valory.skills.termination_abci.rounds import (
    BackgroundRound,
    Event,
    TerminationAbciApp,
)
from packages.valory.skills.trader_abci.composition import (
    TraderAbciApp,
    abci_app_transition_mapping,
    termination_config,
)
from packages.valory.skills.tx_settlement_multiplexer_abci.rounds import (
    FinishedBetPlacementTxRound,
    FinishedSellOutcomeTokensTxRound,
)

EXPECTED_TRANSITION_MAPPING_LENGTH = 46


def test_abci_app_transition_mapping_type() -> None:
    """Test that abci_app_transition_mapping is a dict."""
    assert isinstance(abci_app_transition_mapping, dict)


def test_abci_app_transition_mapping_length() -> None:
    """Test that abci_app_transition_mapping has the expected number of entries."""
    assert len(abci_app_transition_mapping) == EXPECTED_TRANSITION_MAPPING_LENGTH, (
        f"Expected {EXPECTED_TRANSITION_MAPPING_LENGTH} entries, "
        f"got {len(abci_app_transition_mapping)}"
    )


def test_abci_app_transition_mapping_keys_are_round_classes() -> None:
    """Test that all keys in the transition mapping are round classes (types)."""
    for key in abci_app_transition_mapping:
        assert isinstance(key, type), f"Key {key} is not a class"


def test_abci_app_transition_mapping_values_are_round_classes() -> None:
    """Test that all values in the transition mapping are round classes (types)."""
    for value in abci_app_transition_mapping.values():
        assert isinstance(value, type), f"Value {value} is not a class"


def test_termination_config_round_cls() -> None:
    """Test that termination_config has the correct round_cls."""
    assert termination_config.round_cls is BackgroundRound


def test_termination_config_start_event() -> None:
    """Test that termination_config has the correct start_event."""
    assert termination_config.start_event == Event.TERMINATE


def test_termination_config_abci_app() -> None:
    """Test that termination_config has the correct abci_app."""
    assert termination_config.abci_app is TerminationAbciApp


def test_trader_abci_app_is_type() -> None:
    """Test that TraderAbciApp is a type (class), not an instance."""
    assert isinstance(TraderAbciApp, type)


def test_only_omen_placement_reaches_trade_count() -> None:
    """Only a settled Omen placement (FinishedBetPlacementTxRound) routes to the counter."""
    assert abci_app_transition_mapping[FinishedBetPlacementTxRound] is TradeCountRound


def test_sell_does_not_reach_trade_count() -> None:
    """A settled sell (FinishedSellOutcomeTokensTxRound) bypasses the counter, routing to redemption."""
    assert abci_app_transition_mapping[FinishedSellOutcomeTokensTxRound] is RedeemRouterRound


def test_trade_count_is_only_entry_into_counter() -> None:
    """No other finished round besides FinishedBetPlacementTxRound routes to TradeCountRound."""
    routes_to_counter = [
        round_cls
        for round_cls, target in abci_app_transition_mapping.items()
        if target is TradeCountRound
    ]
    assert routes_to_counter == [FinishedBetPlacementTxRound]


def test_mech_only_request_routes_to_mech_request() -> None:
    """The capped Mech request batch routes into the mech-interact request flow."""
    from packages.valory.skills.mech_interact_abci.states.request import MechRequestRound

    assert abci_app_transition_mapping[FinishedMechOnlyRequestRound] is MechRequestRound


def test_mech_only_finished_routes_to_checkpoint() -> None:
    """An exhausted post-cap queue routes the cycle to the staking checkpoint."""
    from packages.valory.skills.staking_abci.rounds import CallCheckpointRound

    assert abci_app_transition_mapping[FinishedMechOnlyRound] is CallCheckpointRound


def test_mech_response_routes_through_router() -> None:
    """Mech responses now enter the decision maker through the response router, not directly."""
    from packages.valory.skills.mech_interact_abci.states.final_states import (
        FinishedMechResponseRound,
    )

    assert abci_app_transition_mapping[FinishedMechResponseRound] is MechResponseRouterRound
