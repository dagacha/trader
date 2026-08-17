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

"""This module contains the test for rounds of decision maker"""

from typing import Set, Type
from unittest.mock import MagicMock

import pytest

from packages.valory.skills.abstract_round_abci.base import AbstractRound
from packages.valory.skills.decision_maker_abci.rounds import DecisionMakerAbciApp
from packages.valory.skills.decision_maker_abci.states.base import (
    Event,
    SynchronizedData,
)
from packages.valory.skills.decision_maker_abci.states.bet_placement import (
    BetPlacementRound,
)
from packages.valory.skills.decision_maker_abci.states.blacklisting import (
    BlacklistingRound,
)
from packages.valory.skills.decision_maker_abci.states.check_benchmarking import (
    CheckBenchmarkingModeRound,
)
from packages.valory.skills.decision_maker_abci.states.decision_receive import (
    DecisionReceiveRound,
)
from packages.valory.skills.decision_maker_abci.states.decision_request import (
    DecisionRequestRound,
)
from packages.valory.skills.decision_maker_abci.states.epoch_reset import (
    EpochResetRound,
)
from packages.valory.skills.decision_maker_abci.states.final_states import (
    BenchmarkingModeDisabledRound,
    FinishedDecisionMakerRound,
    FinishedDecisionRequestRound,
    FinishedMechOnlyRequestRound,
    FinishedMechOnlyRound,
    FinishedPolymarketBetPlacementRound,
    FinishedPostBetUpdateRound,
    FinishedWithoutDecisionRound,
)
from packages.valory.skills.decision_maker_abci.states.handle_failed_tx import (
    HandleFailedTxRound,
)
from packages.valory.skills.decision_maker_abci.states.mech_only import (
    MechOnlyReceiveRound,
    MechOnlySelectionRound,
    MechResponseRouterRound,
)
from packages.valory.skills.decision_maker_abci.states.polymarket_bet_placement import (
    PolymarketBetPlacementRound,
)
from packages.valory.skills.decision_maker_abci.states.polymarket_swap import (
    PolymarketSwapUsdcRound,
)
from packages.valory.skills.decision_maker_abci.states.polymarket_sweep import (
    PolymarketSweepRound,
)
from packages.valory.skills.decision_maker_abci.states.polymarket_wrap_collateral import (
    PolymarketWrapCollateralRound,
)
from packages.valory.skills.decision_maker_abci.states.post_bet_update import (
    PostBetUpdateRound,
)
from packages.valory.skills.decision_maker_abci.states.randomness import (
    BenchmarkingRandomnessRound,
    RandomnessRound,
)
from packages.valory.skills.decision_maker_abci.states.redeem import RedeemRound
from packages.valory.skills.decision_maker_abci.states.redeem_router import (
    RedeemRouterRound,
)
from packages.valory.skills.decision_maker_abci.states.sampling import SamplingRound
from packages.valory.skills.decision_maker_abci.states.sell_outcome_tokens import (
    SellOutcomeTokensRound,
)
from packages.valory.skills.decision_maker_abci.states.tool_selection import (
    ToolSelectionRound,
)
from packages.valory.skills.decision_maker_abci.states.trade_cap import TradeCapRound
from packages.valory.skills.decision_maker_abci.states.trade_count import (
    TradeCountRound,
)


@pytest.fixture
def setup_app() -> DecisionMakerAbciApp:
    """Set up the initial app instance for testing."""
    # Create mock objects for the required arguments
    synchronized_data = MagicMock(spec=SynchronizedData)
    logger = MagicMock()  # Mock logger
    context = MagicMock()  # Mock context

    # Initialize the app with the mocked dependencies
    return DecisionMakerAbciApp(synchronized_data, logger, context)


def test_initial_state(setup_app: DecisionMakerAbciApp) -> None:
    """Test the initial round of the application."""
    app = setup_app
    assert app.initial_round_cls == CheckBenchmarkingModeRound
    assert CheckBenchmarkingModeRound in app.initial_states


def test_check_benchmarking_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from CheckBenchmarkingModeRound."""
    app = setup_app
    transition_function = app.transition_function[CheckBenchmarkingModeRound]

    # Transition on benchmarking enabled
    assert (
        transition_function[Event.BENCHMARKING_ENABLED] == BenchmarkingRandomnessRound
    )

    # Transition on benchmarking disabled routes through the wrap round so
    # any USDC.e in the Safe is converted to pUSD before the trading cycle
    # checks bankroll.
    assert (
        transition_function[Event.BENCHMARKING_DISABLED]
        == PolymarketWrapCollateralRound
    )

    # Test no majority
    assert transition_function[Event.NO_MAJORITY] == CheckBenchmarkingModeRound


def test_sampling_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from SamplingRound."""
    app = setup_app
    transition_function = app.transition_function[SamplingRound]

    # Transition on done
    assert transition_function[Event.DONE] == ToolSelectionRound

    # Test none and no majority
    assert transition_function[Event.NONE] == FinishedWithoutDecisionRound
    assert transition_function[Event.NO_MAJORITY] == SamplingRound


def test_randomness_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from RandomnessRound."""
    app = setup_app
    transition_function = app.transition_function[RandomnessRound]

    # Transition on done runs the epoch-cap reset before the cap check.
    assert transition_function[Event.DONE] == EpochResetRound


def test_benchmarking_randomness_round_transition(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """Test transitions from BenchmarkingRandomnessRound."""
    transition_function = setup_app.transition_function[BenchmarkingRandomnessRound]

    assert transition_function[Event.DONE] == EpochResetRound


def test_epoch_reset_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test that EpochResetRound resets before TradeCapRound on DONE."""
    transition_function = setup_app.transition_function[EpochResetRound]
    assert transition_function[Event.DONE] == TradeCapRound
    assert transition_function[Event.NO_MAJORITY] == EpochResetRound
    assert transition_function[Event.ROUND_TIMEOUT] == EpochResetRound


def test_trade_cap_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from TradeCapRound."""
    transition_function = setup_app.transition_function[TradeCapRound]

    assert transition_function[Event.DONE] == SamplingRound
    assert transition_function[Event.MECH_ONLY] == MechOnlySelectionRound
    assert transition_function[Event.NO_MAJORITY] == TradeCapRound


def test_mech_only_selection_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from MechOnlySelectionRound."""
    transition_function = setup_app.transition_function[MechOnlySelectionRound]

    assert transition_function[Event.DONE] == FinishedMechOnlyRequestRound
    assert transition_function[Event.NO_MARKETS] == FinishedMechOnlyRound
    assert transition_function[Event.NO_MAJORITY] == MechOnlySelectionRound


def test_mech_only_receive_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from MechOnlyReceiveRound."""
    transition_function = setup_app.transition_function[MechOnlyReceiveRound]

    # One batch per cycle: DONE ends the cycle at the checkpoint, not back to selection.
    assert transition_function[Event.DONE] == FinishedMechOnlyRound
    assert transition_function[Event.NO_MARKETS] == FinishedMechOnlyRound
    assert transition_function[Event.NO_MAJORITY] == MechOnlyReceiveRound


def test_mech_response_router_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from MechResponseRouterRound."""
    transition_function = setup_app.transition_function[MechResponseRouterRound]

    assert transition_function[Event.DONE] == DecisionReceiveRound
    assert transition_function[Event.MECH_ONLY] == MechOnlyReceiveRound
    assert transition_function[Event.NO_MAJORITY] == MechResponseRouterRound


def test_trade_count_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from TradeCountRound."""
    transition_function = setup_app.transition_function[TradeCountRound]

    # A successful count advances to PostBetUpdateRound (post-bet bookkeeping),
    # never back to sampling or betting
    assert transition_function[Event.DONE] == PostBetUpdateRound
    assert transition_function[Event.NO_MAJORITY] == TradeCountRound
    assert transition_function[Event.ROUND_TIMEOUT] == TradeCountRound


def test_trade_count_round_is_entry_point(setup_app: DecisionMakerAbciApp) -> None:
    """Test that TradeCountRound is an initial state with no db pre-conditions (composed entry)."""
    assert TradeCountRound in setup_app.initial_states
    assert setup_app.db_pre_conditions[TradeCountRound] == set()


def test_trade_count_round_persisted_across_periods(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """The successful trade counter is persisted across reset periods."""
    from packages.valory.skills.abstract_round_abci.base import get_name

    assert (
        get_name(SynchronizedData.successful_trade_count)
        in setup_app.cross_period_persisted_keys
    )


def test_trade_count_round_in_db_post_conditions(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """Test that TradeCountRound writes successful_trade_count to the DB on completion."""
    from packages.valory.skills.abstract_round_abci.base import get_name

    post = setup_app.db_post_conditions
    assert TradeCountRound in post
    assert get_name(SynchronizedData.successful_trade_count) in post[TradeCountRound]


def test_mech_only_selection_round_in_db_post_conditions(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """Test that MechOnlySelectionRound writes mech_only_queue to the DB on completion."""
    from packages.valory.skills.abstract_round_abci.base import get_name

    post = setup_app.db_post_conditions
    assert MechOnlySelectionRound in post
    assert get_name(SynchronizedData.mech_only_queue) in post[MechOnlySelectionRound]


def test_mech_only_receive_round_in_db_post_conditions(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """Test that MechOnlyReceiveRound writes mech_only_queue to the DB on completion."""
    from packages.valory.skills.abstract_round_abci.base import get_name

    post = setup_app.db_post_conditions
    assert MechOnlyReceiveRound in post
    assert get_name(SynchronizedData.mech_only_queue) in post[MechOnlyReceiveRound]


def test_tool_selection_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from ToolSelectionRound."""
    app = setup_app
    transition_function = app.transition_function[ToolSelectionRound]

    # Test transition on done
    assert transition_function[Event.DONE] == PolymarketSwapUsdcRound


def test_decision_request_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from DecisionRequestRound."""
    app = setup_app
    transition_function = app.transition_function[DecisionRequestRound]

    # Test transition on done
    assert transition_function[Event.DONE] == FinishedDecisionRequestRound
    assert transition_function[Event.MOCK_MECH_REQUEST] == DecisionReceiveRound


def test_decision_receive_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from DecisionReceiveRound."""
    app = setup_app
    transition_function = app.transition_function[DecisionReceiveRound]

    # Test transition on done
    assert transition_function[Event.DONE] == BetPlacementRound
    assert transition_function[Event.DONE_SELL] == SellOutcomeTokensRound
    assert transition_function[Event.DONE_NO_SELL] == FinishedDecisionMakerRound


def test_blacklisting_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from BlacklistingRound.

    After the always-redeem-first restructure, blacklisting wraps up the
    cycle directly via `FinishedWithoutDecisionRound` (which is mapped to
    `CallCheckpointRound` at the chain level) instead of detouring through
    the redeem router. Redemption now runs at the start of every cycle,
    so the legacy "redeem after blacklist" detour would be a wasteful
    no-op and would also re-enter the trading flow within the same
    period, breaking the one-bet-attempt-per-cycle invariant.

    :param setup_app: the DecisionMakerAbciApp fixture.
    """
    app = setup_app
    transition_function = app.transition_function[BlacklistingRound]

    assert transition_function[Event.DONE] == FinishedWithoutDecisionRound
    assert transition_function[Event.MOCK_TX] == FinishedWithoutDecisionRound


def test_bet_placement_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from BetPlacementRound."""
    app = setup_app
    transition_function = app.transition_function[BetPlacementRound]

    # Test transition on done
    assert transition_function[Event.DONE] == FinishedDecisionMakerRound


def test_polymarket_bet_placement_round_transition(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """Test transitions from PolymarketBetPlacementRound.

    Polymarket bets are placed off-chain via py-clob-client and therefore
    have no on-chain tx for the multiplexer to route. After the always-
    redeem-first restructure, the post-bet exits go directly to the new
    `FinishedPolymarketBetPlacementRound` final state (mapped to
    `CallCheckpointRound` at the chain level) instead of detouring
    through the redeem router. Any winnings produced by the just-placed
    bet will be picked up by the early-redeem at the start of the next
    cycle.

    :param setup_app: the DecisionMakerAbciApp fixture.
    """
    app = setup_app
    transition_function = app.transition_function[PolymarketBetPlacementRound]

    # CLOB v2: a matched order leaves funds in the DepositWallet, so success
    # routes through the sweep round (which returns them to the Safe) before
    # the cycle wraps up. A mocked tx still exits directly.
    assert transition_function[Event.DONE] == PolymarketSweepRound
    assert transition_function[Event.BET_PLACEMENT_DONE] == PolymarketSweepRound
    assert transition_function[Event.MOCK_TX] == FinishedPolymarketBetPlacementRound


def test_redeem_round_transition(setup_app: DecisionMakerAbciApp) -> None:
    """Test transitions from RedeemRound."""
    app = setup_app
    transition_function = app.transition_function[RedeemRound]

    # Test transition on done
    assert transition_function[Event.DONE] == FinishedDecisionMakerRound


def test_post_bet_update_round_transition(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """Test transitions from PostBetUpdateRound.

    PostBetUpdateRound is the post-tx-settlement bookkeeping hook for
    Omen `BetPlacementRound` and `SellOutcomeTokensRound`. Its DONE exit
    must reach `FinishedPostBetUpdateRound`, which the trader_abci
    composition then maps to `CallCheckpointRound`.

    :param setup_app: the DecisionMakerAbciApp fixture.
    """
    app = setup_app
    transition_function = app.transition_function[PostBetUpdateRound]

    assert transition_function[Event.DONE] == FinishedPostBetUpdateRound


def test_final_states(setup_app: DecisionMakerAbciApp) -> None:
    """Test the final states of the application."""
    app = setup_app
    assert FinishedDecisionMakerRound in app.final_states
    assert BenchmarkingModeDisabledRound in app.final_states
    assert FinishedWithoutDecisionRound in app.final_states
    assert FinishedPolymarketBetPlacementRound in app.final_states
    assert FinishedPostBetUpdateRound in app.final_states


def test_mech_only_finished_rounds_are_final(setup_app: DecisionMakerAbciApp) -> None:
    """The two new post-cap finished rounds are final states."""
    app = setup_app
    assert FinishedMechOnlyRequestRound in app.final_states
    assert FinishedMechOnlyRound in app.final_states


def test_epoch_reset_not_an_initial_state(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """Test that EpochResetRound is no longer a standalone entry point.

    It is reached at the start of every decision cycle via
    ``RandomnessRound`` (and ``BenchmarkingRandomnessRound``), immediately
    before ``TradeCapRound``.  ``MechResponseRouterRound`` remains a
    decision-maker initial state only because it is re-entered mid-cycle
    from the composed trader app via ``FinishedMechResponseRound``.

    :param setup_app: the decision-maker AbciApp fixture.
    """
    assert EpochResetRound not in setup_app.initial_states
    assert MechResponseRouterRound in setup_app.initial_states


def test_decision_receive_no_longer_initial_state(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """Test that DecisionReceiveRound is no longer a composition entry point (routed via the router)."""
    assert DecisionReceiveRound not in setup_app.initial_states


def test_mech_only_queue_persisted_across_periods(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """The post-cap queue is persisted across reset periods."""
    from packages.valory.skills.abstract_round_abci.base import get_name

    assert (
        get_name(SynchronizedData.mech_only_queue)
        in setup_app.cross_period_persisted_keys
    )


# ---------------------------------------------------------------------------
# No-bet invariant: FSM reachability check for the capped path
# ---------------------------------------------------------------------------

# Rounds that must NEVER be reached on the post-cap (mech-only) path.
FORBIDDEN_ROUNDS: Set[Type[AbstractRound]] = {
    BetPlacementRound,
    PolymarketBetPlacementRound,
    SellOutcomeTokensRound,
    BlacklistingRound,
    SamplingRound,
    DecisionReceiveRound,
    DecisionRequestRound,
    ToolSelectionRound,
    PolymarketSwapUsdcRound,
    RedeemRound,
    RedeemRouterRound,
    HandleFailedTxRound,
}


def test_capped_path_never_reaches_bet_or_sell_rounds(
    setup_app: DecisionMakerAbciApp,
) -> None:
    """No state reachable on the capped path reaches any bet/sell/trading round.

    The capped path is entered via ``TradeCapRound[MECH_ONLY]`` and re-enters
    the decision-maker through ``MechResponseRouterRound`` after each Mech
    round-trip.  ``MechResponseRouterRound`` branches on ``mech_only_mode``:
    when capped it emits ``MECH_ONLY`` (to ``MechOnlyReceiveRound``), never

    ``DONE`` (which would lead to ``DecisionReceiveRound`` and the normal
    trading flow).  This test follows only the capped branch.

    :param setup_app: the decision-maker AbciApp fixture.
    """
    transition_function = setup_app.transition_function

    # Entry points on the capped path:
    #   MechOnlySelectionRound  via TradeCapRound Event.MECH_ONLY
    #   MechResponseRouterRound  via composition (FinishedMechResponseRound) after a Mech round-trip
    reachable: Set[Type[AbstractRound]] = {
        MechOnlySelectionRound,
        MechResponseRouterRound,
    }
    frontier: list[Type[AbstractRound]] = list(reachable)

    while frontier:
        state = frontier.pop()
        transitions = transition_function.get(state, {})
        for event, target in transitions.items():
            # On the capped path, MechResponseRouterRound emits MECH_ONLY, not DONE.
            # The DONE edge leads to the normal trading flow (DecisionReceiveRound),
            # which is forbidden on the capped path.  Skip it.
            if state is MechResponseRouterRound and event == Event.DONE:
                continue
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)

    forbidden_reached = reachable & FORBIDDEN_ROUNDS
    assert (
        not forbidden_reached
    ), f"The capped path reaches forbidden trading rounds: {forbidden_reached}"

    # Sanity: the expected capped-path states are all reachable.
    assert MechOnlySelectionRound in reachable
    assert MechOnlyReceiveRound in reachable
    assert MechResponseRouterRound in reachable
    assert FinishedMechOnlyRequestRound in reachable
    assert FinishedMechOnlyRound in reachable
