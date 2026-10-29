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

"""Consensus rounds for the fast Omen agent."""

import json
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Set, Tuple

from packages.valory.skills.abstract_round_abci.base import (
    AbciApp,
    AppState,
    BaseSynchronizedData,
    BaseTxPayload,
    CollectSameUntilThresholdRound,
    DegenerateRound,
)
from packages.valory.skills.decision_maker_abci.states.base import Event as D
from packages.valory.skills.decision_maker_abci.states.base import SynchronizedData
from packages.valory.skills.decision_maker_abci.states.bet_placement import (
    BetPlacementRound,
)
from packages.valory.skills.decision_maker_abci.states.decision_request import (
    DecisionRequestRound,
)
from packages.valory.skills.decision_maker_abci.states.mech_only import (
    MechOnlySelectionRound,
)
from packages.valory.skills.decision_maker_abci.states.post_bet_update import (
    PostBetUpdateRound,
)
from packages.valory.skills.decision_maker_abci.states.redeem import RedeemRound
from packages.valory.skills.decision_maker_abci.states.trade_count import (
    TradeCountRound,
)


class Event(Enum):
    """Fast-lane events."""

    DONE = "done"
    MARKET = "market"
    LIGHTNING = "lightning"
    MECH_ONLY = "mech_only"
    HOUSEKEEPING = "housekeeping"
    WAIT = "wait"
    BET = "bet"
    SKIP = "skip"
    NO_MAJORITY = "no_majority"
    ROUND_TIMEOUT = "round_timeout"


@dataclass(frozen=True)
class SelectionPayload(BaseTxPayload):
    """A watch selection or a strategy decision."""

    selection: str


class FreshWatchRound(CollectSameUntilThresholdRound):
    """Agree on the next market or housekeeping entry."""

    payload_class = SelectionPayload
    synchronized_data_class = SynchronizedData
    selection_key = "fresh_selection"
    collection_key = "participant_to_fresh_selection"
    done_event = Event.MARKET
    no_majority_event = Event.NO_MAJORITY
    none_event = Event.WAIT

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Apply the selection and clear the previous request and transaction."""
        result = super().end_block()
        if result is None:
            return None
        data, event = result
        if event != self.done_event:
            return data, event
        selection = json.loads(data.db.get_strict(self.selection_key))
        lane = selection.pop("lane")
        if selection.get("lightning_probability") is not None:
            selection.update(mech_tool=None, selected_tool_key=None)
        data = data.update(
            synchronized_data_class=SynchronizedData,
            tx_submitter=None,
            final_tx_hash=None,
            most_voted_tx_hash=None,
            mech_responses="[]",
            offchain_pending_request=None,
            is_marketplace_v2=True,
            review_bets_for_selling=False,
            policy=None,
            lightning_probability=selection.pop("lightning_probability", None),
            lightning_rule=selection.pop("lightning_rule", None),
            lightning_preview_probability=selection.pop(
                "lightning_preview_probability", None
            ),
            lightning_preview_title=selection.pop("lightning_preview_title", None),
            lightning_preview_request=selection.pop("lightning_preview_request", False),
            question_template_id=selection.pop("question_template_id", None),
            **selection,
        )
        if lane == "lightning_decision":
            return data, Event.LIGHTNING
        if lane in ("market", "preview_request"):
            return data, Event.MARKET
        if lane == "mech_only":
            return data, Event.MECH_ONLY
        if lane == "housekeeping":
            return data, Event.HOUSEKEEPING
        return data, Event.WAIT


class FastDecisionRound(CollectSameUntilThresholdRound):
    """Agree on the fast strategy result."""

    payload_class = SelectionPayload
    synchronized_data_class = SynchronizedData
    selection_key = "fast_decision"
    collection_key = "participant_to_fast_decision"
    done_event = Event.BET
    no_majority_event = Event.NO_MAJORITY
    none_event = Event.SKIP

    def end_block(self) -> Optional[Tuple[BaseSynchronizedData, Enum]]:
        """Set the inputs used by the existing bet-placement round."""
        result = super().end_block()
        if result is None:
            return None
        data, event = result
        if event != self.done_event:
            return data, event
        decision = json.loads(data.db.get_strict(self.selection_key))
        amount = int(decision["stake"] * 10**18)
        data = data.update(
            synchronized_data_class=SynchronizedData,
            bet_amount=amount,
            vote=decision["side"],
            confidence=1.0,
            is_profitable=amount > 0,
            decision_receive_timestamp=decision.get("answered_at"),
        )
        return data, Event.BET if amount > 0 else Event.SKIP


class FinishedRequestRound(DegenerateRound):
    """Hand a request to mech-interact."""


class FinishedFastTxRound(DegenerateRound):
    """Hand a prepared transaction to settlement."""


class FinishedHousekeepingRound(DegenerateRound):
    """Continue housekeeping after redeem."""


class FreshWatchAbciApp(AbciApp[Event]):
    """FreshWatchAbciApp

    Initial round: FreshWatchRound

    Initial states: {FastDecisionRound, FreshWatchRound, PostBetUpdateRound, TradeCountRound}

    Transition states:
        0. FreshWatchRound
            - market: 2.
            - lightning: 3.
            - mech only: 1.
            - housekeeping: 7.
            - wait: 0.
            - no majority: 0.
            - round timeout: 0.
        1. MechOnlySelectionRound
            - done: 8.
            - no markets: 0.
            - no majority: 1.
            - round timeout: 0.
        2. DecisionRequestRound
            - done: 8.
            - mock mech request: 3.
            - slots unsupported error: 0.
            - no majority: 2.
            - round timeout: 2.
        3. FastDecisionRound
            - bet: 4.
            - skip: 0.
            - no majority: 3.
            - round timeout: 3.
        4. BetPlacementRound
            - done: 9.
            - mock tx: 6.
            - insufficient balance: 0.
            - calc buy amount failed: 0.
            - no majority: 4.
            - round timeout: 0.
        5. TradeCountRound
            - done: 6.
            - none: 5.
            - no majority: 5.
            - round timeout: 5.
        6. PostBetUpdateRound
            - done: 0.
            - none: 6.
            - no majority: 6.
            - round timeout: 6.
        7. RedeemRound
            - done: 9.
            - mock tx: 10.
            - no redeeming: 10.
            - no majority: 7.
            - redeem round timeout: 10.
        8. FinishedRequestRound
        9. FinishedFastTxRound
        10. FinishedHousekeepingRound

    Final states: {FinishedFastTxRound, FinishedHousekeepingRound, FinishedRequestRound}

    Timeouts:
        round timeout: 350.0
        round timeout: 350.0
        redeem round timeout: 3600.0
    """

    initial_round_cls = FreshWatchRound
    initial_states = {
        FreshWatchRound,
        FastDecisionRound,
        PostBetUpdateRound,
        TradeCountRound,
    }
    transition_function = {
        FreshWatchRound: {
            Event.MARKET: DecisionRequestRound,
            Event.LIGHTNING: FastDecisionRound,
            Event.MECH_ONLY: MechOnlySelectionRound,
            Event.HOUSEKEEPING: RedeemRound,
            Event.WAIT: FreshWatchRound,
            Event.NO_MAJORITY: FreshWatchRound,
            Event.ROUND_TIMEOUT: FreshWatchRound,
        },
        MechOnlySelectionRound: {
            D.DONE: FinishedRequestRound,
            D.NO_MARKETS: FreshWatchRound,
            D.NO_MAJORITY: MechOnlySelectionRound,
            D.ROUND_TIMEOUT: FreshWatchRound,
        },
        DecisionRequestRound: {
            D.DONE: FinishedRequestRound,
            D.MOCK_MECH_REQUEST: FastDecisionRound,
            D.SLOTS_UNSUPPORTED_ERROR: FreshWatchRound,
            D.NO_MAJORITY: DecisionRequestRound,
            D.ROUND_TIMEOUT: DecisionRequestRound,
        },
        FastDecisionRound: {
            Event.BET: BetPlacementRound,
            Event.SKIP: FreshWatchRound,
            Event.NO_MAJORITY: FastDecisionRound,
            Event.ROUND_TIMEOUT: FastDecisionRound,
        },
        BetPlacementRound: {
            D.DONE: FinishedFastTxRound,
            D.MOCK_TX: PostBetUpdateRound,
            D.INSUFFICIENT_BALANCE: FreshWatchRound,
            D.CALC_BUY_AMOUNT_FAILED: FreshWatchRound,
            D.NO_MAJORITY: BetPlacementRound,
            D.ROUND_TIMEOUT: FreshWatchRound,
        },
        TradeCountRound: {
            D.DONE: PostBetUpdateRound,
            D.NONE: TradeCountRound,
            D.NO_MAJORITY: TradeCountRound,
            D.ROUND_TIMEOUT: TradeCountRound,
        },
        PostBetUpdateRound: {
            D.DONE: FreshWatchRound,
            D.NONE: PostBetUpdateRound,
            D.NO_MAJORITY: PostBetUpdateRound,
            D.ROUND_TIMEOUT: PostBetUpdateRound,
        },
        RedeemRound: {
            D.DONE: FinishedFastTxRound,
            D.MOCK_TX: FinishedHousekeepingRound,
            D.NO_REDEEMING: FinishedHousekeepingRound,
            D.NO_MAJORITY: RedeemRound,
            D.REDEEM_ROUND_TIMEOUT: FinishedHousekeepingRound,
        },
        FinishedRequestRound: {},
        FinishedFastTxRound: {},
        FinishedHousekeepingRound: {},
    }
    final_states = {
        FinishedRequestRound,
        FinishedFastTxRound,
        FinishedHousekeepingRound,
    }
    event_to_timeout = {
        Event.ROUND_TIMEOUT: 350.0,
        D.ROUND_TIMEOUT: 350.0,
        D.REDEEM_ROUND_TIMEOUT: 3600.0,
    }
    db_pre_conditions: Dict[AppState, Set[str]] = {
        state: set() for state in initial_states
    }
    db_post_conditions: Dict[AppState, Set[str]] = {
        FinishedRequestRound: set(),
        FinishedFastTxRound: {"tx_submitter", "most_voted_tx_hash"},
        FinishedHousekeepingRound: set(),
    }
