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

import pytest

from packages.valory.skills.check_stop_trading_abci.rounds import CheckStopTradingRound
from packages.valory.skills.decision_maker_abci.states.final_states import (
    FinishedMechOnlyRequestRound,
    FinishedMechOnlyRound,
    FinishedPostBetUpdateRound,
)
from packages.valory.skills.decision_maker_abci.states.handle_failed_tx import (
    HandleFailedTxRound,
)
from packages.valory.skills.decision_maker_abci.states.mech_only import (
    MechResponseRouterRound,
)
from packages.valory.skills.decision_maker_abci.states.post_bet_update import (
    PostBetUpdateRound,
)
from packages.valory.skills.decision_maker_abci.states.redeem_router import (
    RedeemRouterRound,
)
from packages.valory.skills.decision_maker_abci.states.trade_count import (
    TradeCountRound,
)
from packages.valory.skills.market_manager_abci.rounds import (
    FetchMarketsRouterRound,
    FinishedMarketManagerRound,
    FinishedPolymarketFetchMarketRound,
)
from packages.valory.skills.mech_interact_abci.states.final_states import (
    FailedOffchainMechRequestRound,
    FinishedOffchainMechDepositNeededRound,
    FinishedOffchainMechRequestRound,
)
from packages.valory.skills.mech_interact_abci.states.request import MechRequestRound
from packages.valory.skills.mech_interact_abci.states.response import MechResponseRound
from packages.valory.skills.staking_abci.rounds import CallCheckpointRound
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
    FinishedOffchainMechDepositSettledRound,
    FinishedPolymarketWrapCollateralTxRound,
    FinishedRedeemingTxRound,
    FinishedSellOutcomeTokensTxRound,
    PreTxSettlementRound,
)

EXPECTED_TRANSITION_MAPPING_LENGTH = 63

# Transitions introduced or rewired by the always-redeem-first /
# `PostBetUpdateRound` FSM restructure (PR #904). Each pair must hold
# exactly; a typo, swap, or accidental retarget will trip the matching
# parametrised assertion. The count tripwire above stays in place to
# catch additions/removals that preserve every listed edge.
RESTRUCTURE_TRANSITIONS = {
    # Always-redeem-first: market fetch → redeem router, so any unclaimed
    # winnings are redeemed before the next mech/bet cycle.
    FinishedMarketManagerRound: RedeemRouterRound,
    FinishedPolymarketFetchMarketRound: RedeemRouterRound,
    # Redeem terminals now feed CheckStopTrading (previously the other
    # way around).
    FinishedRedeemingTxRound: CheckStopTradingRound,
    # Omen on-chain bet settlement now goes through TradeCountRound first
    # (to increment the trade counter), which then routes to PostBetUpdateRound
    # internally via DecisionMakerAbciApp's transition function.
    FinishedBetPlacementTxRound: TradeCountRound,
    FinishedSellOutcomeTokensTxRound: PostBetUpdateRound,
    FinishedPostBetUpdateRound: CallCheckpointRound,
}

# Off-chain mech-interact transitions. Pinned individually because a
# flat value-set assertion would let a silent swap pass — e.g. routing
# ``FinishedOffchainMechDepositSettledRound`` to ``MechResponseRound``
# instead of ``MechRequestRound`` would break ``_retry_pending``,
# and routing ``FinishedOffchainMechDepositNeededRound`` to
# ``RandomnessTransactionSubmissionRound`` would skip the
# refill-required check that ``PreTxSettlementRound`` runs.
OFFCHAIN_TRANSITIONS = {
    FinishedOffchainMechRequestRound: MechResponseRound,
    FinishedOffchainMechDepositNeededRound: PreTxSettlementRound,
    FinishedOffchainMechDepositSettledRound: MechRequestRound,
    FailedOffchainMechRequestRound: HandleFailedTxRound,
}


@pytest.mark.parametrize(
    "src,dst",
    list(OFFCHAIN_TRANSITIONS.items()),
    ids=lambda cls: getattr(cls, "__name__", str(cls)),
)
def test_offchain_transition(src: type, dst: type) -> None:
    """Each off-chain mech-interact transition must resolve to its exact target round."""
    assert src in abci_app_transition_mapping, f"{src.__name__} missing from mapping"
    assert abci_app_transition_mapping[src] is dst, (
        f"{src.__name__} -> {abci_app_transition_mapping[src].__name__}, "
        f"expected {dst.__name__}"
    )


@pytest.mark.parametrize(
    "src,dst",
    list(RESTRUCTURE_TRANSITIONS.items()),
    ids=lambda cls: getattr(cls, "__name__", str(cls)),
)
def test_restructure_transition(src: type, dst: type) -> None:
    """Each PR-#904 transition must resolve to its exact target round."""
    assert src in abci_app_transition_mapping, f"{src.__name__} missing from mapping"
    assert abci_app_transition_mapping[src] is dst, (
        f"{src.__name__} -> {abci_app_transition_mapping[src].__name__}, "
        f"expected {dst.__name__}"
    )


def test_wrap_tx_terminal_skips_to_trading_cycle() -> None:
    """Wrap tx settlement routes directly into the trading cycle.

    Safe multisend is atomic and the wrap doesn't touch CLOB-exchange
    approvals, so the legacy post-wrap ``PolymarketPostSetApprovalRound``
    hop (1 SRR + 6 chain reads) added no observable safety. The
    settlement terminal now targets ``FetchMarketsRouterRound`` directly.
    """
    assert (
        abci_app_transition_mapping[FinishedPolymarketWrapCollateralTxRound]
        is FetchMarketsRouterRound
    )


def test_only_expected_edges_enter_post_bet_update() -> None:
    """Exactly the Omen bet / sell tx-settlement terminals feed PostBetUpdateRound.

    An accidental additional route into PostBetUpdateRound would let
    non-bet/sell flows trigger the post-bet bookkeeping helpers.
    """
    edges_into = {
        src
        for src, dst in abci_app_transition_mapping.items()
        if dst is PostBetUpdateRound
    }
    # In the merged FSM, FinishedBetPlacementTxRound goes through TradeCountRound
    # first (to increment the counter), then TradeCountRound DONE -> PostBetUpdateRound.
    # Only FinishedSellOutcomeTokensTxRound enters PostBetUpdateRound directly.
    assert edges_into == {
        FinishedSellOutcomeTokensTxRound,
    }


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
    """A settled sell (FinishedSellOutcomeTokensTxRound) bypasses the counter, routing to PostBetUpdateRound."""
    assert (
        abci_app_transition_mapping[FinishedSellOutcomeTokensTxRound]
        is PostBetUpdateRound
    )


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
    from packages.valory.skills.mech_interact_abci.states.request import (
        MechRequestRound,
    )

    assert abci_app_transition_mapping[FinishedMechOnlyRequestRound] is MechRequestRound


def test_mech_only_finished_routes_to_checkpoint() -> None:
    """An exhausted post-cap queue routes the cycle to the staking checkpoint."""
    from packages.valory.skills.staking_abci.rounds import CallCheckpointRound

    assert abci_app_transition_mapping[FinishedMechOnlyRound] is CallCheckpointRound


def test_mech_response_routes_to_router() -> None:
    """Mech responses enter the decision maker straight through MechResponseRouterRound.

    The epoch-cap reset no longer lives on this mid-cycle path; it runs at
    the start of each cycle (RandomnessRound -> EpochResetRound -> TradeCapRound).
    """
    from packages.valory.skills.mech_interact_abci.states.final_states import (
        FinishedMechResponseRound,
    )

    assert (
        abci_app_transition_mapping[FinishedMechResponseRound]
        is MechResponseRouterRound
    )
