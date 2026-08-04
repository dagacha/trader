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

"""This module contains the rounds for the decision-making."""

from typing import Dict, Set

from packages.valory.skills.abstract_round_abci.base import (
    AbciApp,
    AbciAppTransitionFunction,
    AppState,
    get_name,
)
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
from packages.valory.skills.decision_maker_abci.states.final_states import (
    BenchmarkingDoneRound,
    BenchmarkingModeDisabledRound,
    FinishedDecisionMakerRound,
    FinishedDecisionRequestRound,
    FinishedMechOnlyRequestRound,
    FinishedMechOnlyRound,
    FinishedPolymarketRedeemRound,
    FinishedPolymarketSwapTxPreparationRound,
    FinishedRedeemTxPreparationRound,
    FinishedSetApprovalTxPreparationRound,
    FinishedWithoutDecisionRound,
    FinishedWithoutRedeemingRound,
    ImpossibleRound,
    RefillRequiredRound,
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
from packages.valory.skills.decision_maker_abci.states.polymarket_post_set_approval import (
    PolymarketPostSetApprovalRound,
)
from packages.valory.skills.decision_maker_abci.states.polymarket_redeem import (
    PolymarketRedeemRound,
)
from packages.valory.skills.decision_maker_abci.states.polymarket_set_approval import (
    PolymarketSetApprovalRound,
)
from packages.valory.skills.decision_maker_abci.states.polymarket_swap import (
    PolymarketSwapUsdcRound,
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
from packages.valory.skills.decision_maker_abci.states.trade_count import TradeCountRound
from packages.valory.skills.decision_maker_abci.states.trade_cap import TradeCapRound
from packages.valory.skills.market_manager_abci.rounds import (
    Event as MarketManagerEvent,
)


class DecisionMakerAbciApp(AbciApp[Event]):
    """DecisionMakerAbciApp

    Initial round: CheckBenchmarkingModeRound

    Initial states: {CheckBenchmarkingModeRound, DecisionRequestRound, HandleFailedTxRound, MechResponseRouterRound, PolymarketPostSetApprovalRound, RandomnessRound, RedeemRouterRound, TradeCapRound, TradeCountRound}

    Transition states:
        0. CheckBenchmarkingModeRound
            - benchmarking enabled: 1.
            - benchmarking disabled: 23.
            - set approval: 16.
            - prepare tx: 16.
            - no majority: 0.
            - round timeout: 0.
            - none: 32.
        1. BenchmarkingRandomnessRound
            - done: 3.
            - round timeout: 1.
            - no majority: 1.
            - none: 32.
        2. RandomnessRound
            - done: 3.
            - round timeout: 2.
            - no majority: 2.
            - none: 32.
        3. TradeCapRound
            - done: 7.
            - mech only: 4.
            - round timeout: 3.
            - no majority: 3.
            - none: 32.
        4. MechOnlySelectionRound
            - done: 34.
            - no markets: 35.
            - no majority: 4.
            - round timeout: 4.
            - none: 32.
        5. MechOnlyReceiveRound
            - done: 4.
            - no markets: 35.
            - no majority: 5.
            - round timeout: 5.
            - none: 32.
        6. MechResponseRouterRound
            - done: 12.
            - mech only: 5.
            - no majority: 6.
            - round timeout: 6.
            - none: 32.
        7. SamplingRound
            - done: 8.
            - none: 29.
            - no majority: 7.
            - round timeout: 7.
            - new simulated resample: 7.
            - benchmarking enabled: 8.
            - benchmarking finished: 33.
            - fetch error: 32.
        8. ToolSelectionRound
            - done: 10.
            - none: 8.
            - no majority: 8.
            - round timeout: 8.
        9. TradeCountRound
            - done: 19.
            - no majority: 9.
            - round timeout: 9.
            - none: 32.
        10. PolymarketSwapUsdcRound
            - done: 11.
            - none: 11.
            - prepare tx: 27.
            - no majority: 10.
            - round timeout: 10.
            - mock tx: 11.
        11. DecisionRequestRound
            - done: 24.
            - mock mech request: 12.
            - slots unsupported error: 13.
            - no majority: 11.
            - round timeout: 11.
        12. DecisionReceiveRound
            - done: 14.
            - polymarket done: 15.
            - done no sell: 22.
            - done sell: 36.
            - mech response error: 13.
            - no majority: 12.
            - tie: 13.
            - unprofitable: 13.
            - round timeout: 12.
        13. BlacklistingRound
            - done: 19.
            - mock tx: 29.
            - none: 32.
            - no majority: 13.
            - round timeout: 13.
            - fetch error: 32.
        14. BetPlacementRound
            - done: 22.
            - mock tx: 18.
            - insufficient balance: 31.
            - calc buy amount failed: 21.
            - no majority: 14.
            - round timeout: 14.
            - none: 32.
        15. PolymarketBetPlacementRound
            - done: 19.
            - bet placement done: 19.
            - bet placement failed: 15.
            - bet placement impossible: 13.
            - insufficient balance: 31.
            - mock tx: 19.
            - no majority: 15.
            - round timeout: 15.
            - none: 32.
        16. PolymarketSetApprovalRound
            - done: 17.
            - prepare tx: 28.
            - no majority: 16.
            - round timeout: 16.
            - none: 32.
            - mock tx: 17.
        17. PolymarketPostSetApprovalRound
            - done: 23.
            - approval failed: 16.
            - no majority: 17.
            - round timeout: 17.
            - none: 32.
        18. RedeemRound
            - done: 22.
            - mock tx: 7.
            - no redeeming: 30.
            - no majority: 18.
            - redeem round timeout: 30.
            - none: 32.
        19. RedeemRouterRound
            - done: 18.
            - polymarket done: 20.
            - no majority: 19.
            - none: 19.
        20. PolymarketRedeemRound
            - done: 26.
            - prepare tx: 25.
            - no majority: 20.
            - none: 20.
            - no redeeming: 30.
            - redeem round timeout: 22.
            - mock tx: 26.
        21. HandleFailedTxRound
            - blacklist: 13.
            - no op: 18.
            - no majority: 21.
        22. FinishedDecisionMakerRound
        23. BenchmarkingModeDisabledRound
        24. FinishedDecisionRequestRound
        25. FinishedRedeemTxPreparationRound
        26. FinishedPolymarketRedeemRound
        27. FinishedPolymarketSwapTxPreparationRound
        28. FinishedSetApprovalTxPreparationRound
        29. FinishedWithoutDecisionRound
        30. FinishedWithoutRedeemingRound
        31. RefillRequiredRound
        32. ImpossibleRound
        33. BenchmarkingDoneRound
        34. FinishedMechOnlyRequestRound
        35. FinishedMechOnlyRound
        36. SellOutcomeTokensRound
            - done: 22.
            - calc sell amount failed: 21.
            - mock tx: 14.
            - no majority: 36.
            - round timeout: 36.
            - none: 32.

    Final states: {BenchmarkingDoneRound, BenchmarkingModeDisabledRound, FinishedDecisionMakerRound, FinishedDecisionRequestRound, FinishedMechOnlyRequestRound, FinishedMechOnlyRound, FinishedPolymarketRedeemRound, FinishedPolymarketSwapTxPreparationRound, FinishedRedeemTxPreparationRound, FinishedSetApprovalTxPreparationRound, FinishedWithoutDecisionRound, FinishedWithoutRedeemingRound, ImpossibleRound, RefillRequiredRound}

    Timeouts:
        round timeout: 30.0
        redeem round timeout: 3600.0
    """

    initial_round_cls: AppState = CheckBenchmarkingModeRound
    initial_states: Set[AppState] = {
        CheckBenchmarkingModeRound,
        RandomnessRound,
        HandleFailedTxRound,
        MechResponseRouterRound,
        RedeemRouterRound,
        PolymarketPostSetApprovalRound,
        DecisionRequestRound,
        TradeCountRound,
        TradeCapRound,
    }
    transition_function: AbciAppTransitionFunction = {
        CheckBenchmarkingModeRound: {
            Event.BENCHMARKING_ENABLED: BenchmarkingRandomnessRound,
            Event.BENCHMARKING_DISABLED: BenchmarkingModeDisabledRound,
            Event.SET_APPROVAL: PolymarketSetApprovalRound,
            Event.PREPARE_TX: PolymarketSetApprovalRound,
            Event.NO_MAJORITY: CheckBenchmarkingModeRound,
            Event.ROUND_TIMEOUT: CheckBenchmarkingModeRound,
            # added because of `autonomy analyse fsm-specs`
            # falsely reporting them as missing from the transition
            Event.NONE: ImpossibleRound,
        },
        BenchmarkingRandomnessRound: {
            Event.DONE: TradeCapRound,
            Event.ROUND_TIMEOUT: BenchmarkingRandomnessRound,
            Event.NO_MAJORITY: BenchmarkingRandomnessRound,
            Event.NONE: ImpossibleRound,
        },
        RandomnessRound: {
            Event.DONE: TradeCapRound,
            Event.ROUND_TIMEOUT: RandomnessRound,
            Event.NO_MAJORITY: RandomnessRound,
            Event.NONE: ImpossibleRound,
        },
        TradeCapRound: {
            Event.DONE: SamplingRound,
            Event.MECH_ONLY: MechOnlySelectionRound,
            Event.ROUND_TIMEOUT: TradeCapRound,
            Event.NO_MAJORITY: TradeCapRound,
            Event.NONE: ImpossibleRound,
        },
        MechOnlySelectionRound: {
            Event.DONE: FinishedMechOnlyRequestRound,
            Event.NO_MARKETS: FinishedMechOnlyRound,
            Event.NO_MAJORITY: MechOnlySelectionRound,
            Event.ROUND_TIMEOUT: MechOnlySelectionRound,
            Event.NONE: ImpossibleRound,
        },
        MechOnlyReceiveRound: {
            Event.DONE: MechOnlySelectionRound,
            Event.NO_MARKETS: FinishedMechOnlyRound,
            Event.NO_MAJORITY: MechOnlyReceiveRound,
            Event.ROUND_TIMEOUT: MechOnlyReceiveRound,
            Event.NONE: ImpossibleRound,
        },
        MechResponseRouterRound: {
            Event.DONE: DecisionReceiveRound,
            Event.MECH_ONLY: MechOnlyReceiveRound,
            Event.NO_MAJORITY: MechResponseRouterRound,
            Event.ROUND_TIMEOUT: MechResponseRouterRound,
            Event.NONE: ImpossibleRound,
        },
        SamplingRound: {
            Event.DONE: ToolSelectionRound,
            Event.NONE: FinishedWithoutDecisionRound,
            Event.NO_MAJORITY: SamplingRound,
            Event.ROUND_TIMEOUT: SamplingRound,
            Event.NEW_SIMULATED_RESAMPLE: SamplingRound,
            Event.BENCHMARKING_ENABLED: ToolSelectionRound,
            Event.BENCHMARKING_FINISHED: BenchmarkingDoneRound,
            # this is here because of `autonomy analyse fsm-specs`
            # falsely reporting it as missing from the transition
            MarketManagerEvent.FETCH_ERROR: ImpossibleRound,
        },
        ToolSelectionRound: {
            Event.DONE: PolymarketSwapUsdcRound,
            Event.NONE: ToolSelectionRound,
            Event.NO_MAJORITY: ToolSelectionRound,
            Event.ROUND_TIMEOUT: ToolSelectionRound,
        },
        TradeCountRound: {
            Event.DONE: RedeemRouterRound,
            Event.NO_MAJORITY: TradeCountRound,
            Event.ROUND_TIMEOUT: TradeCountRound,
            Event.NONE: ImpossibleRound,
        },
        PolymarketSwapUsdcRound: {
            Event.DONE: DecisionRequestRound,
            Event.NONE: DecisionRequestRound,
            Event.PREPARE_TX: FinishedPolymarketSwapTxPreparationRound,
            Event.NO_MAJORITY: PolymarketSwapUsdcRound,
            Event.ROUND_TIMEOUT: PolymarketSwapUsdcRound,
            Event.MOCK_TX: DecisionRequestRound,
        },
        DecisionRequestRound: {
            Event.DONE: FinishedDecisionRequestRound,
            # skip the request to the mech
            Event.MOCK_MECH_REQUEST: DecisionReceiveRound,
            Event.SLOTS_UNSUPPORTED_ERROR: BlacklistingRound,
            Event.NO_MAJORITY: DecisionRequestRound,
            Event.ROUND_TIMEOUT: DecisionRequestRound,
        },
        DecisionReceiveRound: {
            Event.DONE: BetPlacementRound,
            Event.POLYMARKET_DONE: PolymarketBetPlacementRound,
            Event.DONE_NO_SELL: FinishedDecisionMakerRound,
            Event.DONE_SELL: SellOutcomeTokensRound,
            Event.MECH_RESPONSE_ERROR: BlacklistingRound,
            Event.NO_MAJORITY: DecisionReceiveRound,
            Event.TIE: BlacklistingRound,
            Event.UNPROFITABLE: BlacklistingRound,
            # loop on the same state until Mech deliver is received
            Event.ROUND_TIMEOUT: DecisionReceiveRound,
        },
        BlacklistingRound: {
            Event.DONE: RedeemRouterRound,
            Event.MOCK_TX: FinishedWithoutDecisionRound,
            # degenerate round on purpose, should never have reached here
            Event.NONE: ImpossibleRound,
            Event.NO_MAJORITY: BlacklistingRound,
            Event.ROUND_TIMEOUT: BlacklistingRound,
            # this is here because of `autonomy analyse fsm-specs`
            # falsely reporting it as missing from the transition
            MarketManagerEvent.FETCH_ERROR: ImpossibleRound,
        },
        BetPlacementRound: {
            Event.DONE: FinishedDecisionMakerRound,
            # skip the bet placement tx
            Event.MOCK_TX: RedeemRound,
            # degenerate round on purpose, owner must refill the safe
            Event.INSUFFICIENT_BALANCE: RefillRequiredRound,
            Event.CALC_BUY_AMOUNT_FAILED: HandleFailedTxRound,
            Event.NO_MAJORITY: BetPlacementRound,
            Event.ROUND_TIMEOUT: BetPlacementRound,
            # this is here because of `autonomy analyse fsm-specs`
            # falsely reporting it as missing from the transition
            Event.NONE: ImpossibleRound,
        },
        PolymarketBetPlacementRound: {
            Event.DONE: RedeemRouterRound,
            Event.BET_PLACEMENT_DONE: RedeemRouterRound,
            Event.BET_PLACEMENT_FAILED: PolymarketBetPlacementRound,
            Event.BET_PLACEMENT_IMPOSSIBLE: BlacklistingRound,
            # degenerate round on purpose, owner must refill the safe
            Event.INSUFFICIENT_BALANCE: RefillRequiredRound,
            # skip the bet placement tx
            Event.MOCK_TX: RedeemRouterRound,
            Event.NO_MAJORITY: PolymarketBetPlacementRound,
            Event.ROUND_TIMEOUT: PolymarketBetPlacementRound,
            # this is here because of `autonomy analyse fsm-specs`
            # falsely reporting it as missing from the transition
            Event.NONE: ImpossibleRound,
        },
        PolymarketSetApprovalRound: {
            Event.DONE: PolymarketPostSetApprovalRound,
            Event.PREPARE_TX: FinishedSetApprovalTxPreparationRound,
            # degenerate round on purpose, owner must refill the safe
            Event.NO_MAJORITY: PolymarketSetApprovalRound,
            Event.ROUND_TIMEOUT: PolymarketSetApprovalRound,
            # this is here because of `autonomy analyse fsm-specs`
            # falsely reporting it as missing from the transition
            Event.NONE: ImpossibleRound,
            Event.MOCK_TX: PolymarketPostSetApprovalRound,
        },
        PolymarketPostSetApprovalRound: {
            Event.DONE: BenchmarkingModeDisabledRound,
            # degenerate round on purpose, owner must refill the safe
            Event.APPROVAL_FAILED: PolymarketSetApprovalRound,
            Event.NO_MAJORITY: PolymarketPostSetApprovalRound,
            Event.ROUND_TIMEOUT: PolymarketPostSetApprovalRound,
            # this is here because of `autonomy analyse fsm-specs`
            # falsely reporting it as missing from the transition
            Event.NONE: ImpossibleRound,
        },
        RedeemRound: {
            Event.DONE: FinishedDecisionMakerRound,
            Event.MOCK_TX: SamplingRound,
            Event.NO_REDEEMING: FinishedWithoutRedeemingRound,
            Event.NO_MAJORITY: RedeemRound,
            # in case of a round timeout, there likely is something wrong with redeeming
            # it could be the RPC, or some other issue.
            # We don't want to be stuck trying to redeem.
            Event.REDEEM_ROUND_TIMEOUT: FinishedWithoutRedeemingRound,
            # this is here because of `autonomy analyse fsm-specs` falsely
            # reporting it as missing from the transition
            Event.NONE: ImpossibleRound,
        },
        RedeemRouterRound: {
            Event.DONE: RedeemRound,
            Event.POLYMARKET_DONE: PolymarketRedeemRound,
            Event.NO_MAJORITY: RedeemRouterRound,
            Event.NONE: RedeemRouterRound,
        },
        PolymarketRedeemRound: {
            Event.DONE: FinishedPolymarketRedeemRound,
            Event.PREPARE_TX: FinishedRedeemTxPreparationRound,
            Event.NO_MAJORITY: PolymarketRedeemRound,
            Event.NONE: PolymarketRedeemRound,
            Event.NO_REDEEMING: FinishedWithoutRedeemingRound,
            Event.REDEEM_ROUND_TIMEOUT: FinishedDecisionMakerRound,
            Event.MOCK_TX: FinishedPolymarketRedeemRound,
        },
        HandleFailedTxRound: {
            Event.BLACKLIST: BlacklistingRound,
            Event.NO_OP: RedeemRound,
            Event.NO_MAJORITY: HandleFailedTxRound,
        },
        FinishedDecisionMakerRound: {},
        BenchmarkingModeDisabledRound: {},
        FinishedDecisionRequestRound: {},
        FinishedRedeemTxPreparationRound: {},
        FinishedPolymarketRedeemRound: {},
        FinishedPolymarketSwapTxPreparationRound: {},
        FinishedSetApprovalTxPreparationRound: {},
        FinishedWithoutDecisionRound: {},
        FinishedWithoutRedeemingRound: {},
        RefillRequiredRound: {},
        ImpossibleRound: {},
        BenchmarkingDoneRound: {},
        FinishedMechOnlyRequestRound: {},
        FinishedMechOnlyRound: {},
        SellOutcomeTokensRound: {
            Event.DONE: FinishedDecisionMakerRound,
            # skip the bet placement tx
            Event.CALC_SELL_AMOUNT_FAILED: HandleFailedTxRound,
            Event.MOCK_TX: BetPlacementRound,
            Event.NO_MAJORITY: SellOutcomeTokensRound,
            Event.ROUND_TIMEOUT: SellOutcomeTokensRound,
            # this is here because of `autonomy analyse fsm-specs` falsely
            # reporting it as missing from the transition
            Event.NONE: ImpossibleRound,
        },
    }
    cross_period_persisted_keys = frozenset(
        {
            get_name(SynchronizedData.available_mech_tools),
            get_name(SynchronizedData.policy),
            get_name(SynchronizedData.utilized_tools),
            get_name(SynchronizedData.redeemed_condition_ids),
            get_name(SynchronizedData.payout_so_far),
            get_name(SynchronizedData.mech_price),
            get_name(SynchronizedData.mocking_mode),
            get_name(SynchronizedData.next_mock_data_row),
            get_name(SynchronizedData.agreement_id),
            get_name(SynchronizedData.successful_trade_count),
            get_name(SynchronizedData.mech_only_queue),
        }
    )
    final_states: Set[AppState] = {
        FinishedDecisionMakerRound,
        BenchmarkingModeDisabledRound,
        FinishedDecisionRequestRound,
        FinishedRedeemTxPreparationRound,
        FinishedPolymarketRedeemRound,
        FinishedPolymarketSwapTxPreparationRound,
        FinishedSetApprovalTxPreparationRound,
        FinishedWithoutDecisionRound,
        FinishedWithoutRedeemingRound,
        RefillRequiredRound,
        ImpossibleRound,
        BenchmarkingDoneRound,
        FinishedMechOnlyRequestRound,
        FinishedMechOnlyRound,
    }
    event_to_timeout: Dict[Event, float] = {
        Event.ROUND_TIMEOUT: 30.0,
        Event.REDEEM_ROUND_TIMEOUT: 3600.0,
    }
    db_pre_conditions: Dict[AppState, Set[str]] = {
        RedeemRouterRound: set(),
        TradeCountRound: set(),
        TradeCapRound: set(),
        # problematic check in `chain` does not allow to set `final_tx_hash` as a precondition here
        MechResponseRouterRound: set(),
        # problematic check in `chain` does not allow to set `bets_hash` as a precondition here
        HandleFailedTxRound: set(),
        RandomnessRound: set(),
        CheckBenchmarkingModeRound: {get_name(SynchronizedData.is_marketplace_v2)},
        PolymarketPostSetApprovalRound: set(),
        DecisionRequestRound: set(),
    }
    db_post_conditions: Dict[AppState, Set[str]] = {
        FinishedDecisionMakerRound: {
            get_name(SynchronizedData.sampled_bet_index),
            get_name(SynchronizedData.tx_submitter),
            get_name(SynchronizedData.most_voted_tx_hash),
        },
        BenchmarkingModeDisabledRound: set(),
        FinishedDecisionRequestRound: set(),
        FinishedRedeemTxPreparationRound: {
            get_name(SynchronizedData.tx_submitter),
            get_name(SynchronizedData.most_voted_tx_hash),
        },
        FinishedPolymarketRedeemRound: set(),
        FinishedPolymarketSwapTxPreparationRound: {
            get_name(SynchronizedData.tx_submitter),
            get_name(SynchronizedData.most_voted_tx_hash),
        },
        FinishedSetApprovalTxPreparationRound: {
            get_name(SynchronizedData.tx_submitter),
            get_name(SynchronizedData.most_voted_tx_hash),
        },
        FinishedWithoutDecisionRound: {get_name(SynchronizedData.sampled_bet_index)},
        FinishedWithoutRedeemingRound: set(),
        RefillRequiredRound: set(),
        ImpossibleRound: set(),
        BenchmarkingDoneRound: {
            get_name(SynchronizedData.mocking_mode),
            get_name(SynchronizedData.next_mock_data_row),
        },
        FinishedMechOnlyRequestRound: set(),
        FinishedMechOnlyRound: set(),
    }
