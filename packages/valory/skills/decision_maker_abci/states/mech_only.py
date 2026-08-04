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

"""Mech-only (post-cap) consensus states for bounded open-market analysis.

These states implement a dedicated Mech-request/response flow that is entered
exclusively after ``MAX_TRADES`` has been reached.  The flow never touches bet
placement, selling, profitability, voting, or bet-queue bookkeeping; it only
requests Mech analyses for open markets and routes the cycle to the checkpoint
when the bounded queue is exhausted.
"""

from enum import Enum
from typing import Any, Optional, Tuple, cast

from packages.valory.skills.abstract_round_abci.base import (
    CollectSameUntilThresholdRound,
    VotingRound,
    get_name,
)
from packages.valory.skills.decision_maker_abci.payloads import (
    MechOnlyReceivePayload,
    MechOnlySelectionPayload,
    RedeemRouterPayload,
)
from packages.valory.skills.decision_maker_abci.states.base import (
    Event,
    SynchronizedData,
)


class MechOnlySelectionRound(CollectSameUntilThresholdRound):
    """Build a Mech request batch for the next open markets in the post-cap queue."""

    payload_class = MechOnlySelectionPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NO_MARKETS
    no_majority_event = Event.NO_MAJORITY
    selection_key: Any = (
        get_name(SynchronizedData.mech_requests),
        get_name(SynchronizedData.mech_only_queue),
    )
    collection_key = get_name(SynchronizedData.participant_to_selection)

    def end_block(self) -> Optional[Tuple[SynchronizedData, Enum]]:
        """Emit ``NO_MARKETS`` when the batch is empty, otherwise ``DONE``."""
        result = super().end_block()
        if result is None:
            return None

        synchronized_data, event = cast(Tuple[SynchronizedData, Enum], result)
        if event == Event.DONE and not synchronized_data.mech_requests:
            return synchronized_data, Event.NO_MARKETS
        return synchronized_data, event


class MechOnlyReceiveRound(CollectSameUntilThresholdRound):
    """Consume a capped Mech delivery and advance the post-cap queue cursor."""

    payload_class = MechOnlyReceivePayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NO_MARKETS
    no_majority_event = Event.NO_MAJORITY
    selection_key = get_name(SynchronizedData.mech_only_queue)
    collection_key = get_name(SynchronizedData.participant_to_selection)

    def end_block(self) -> Optional[Tuple[SynchronizedData, Enum]]:
        """Loop back to selection while the queue has remaining markets, else finish."""
        result = super().end_block()
        if result is None:
            return None

        synchronized_data, event = cast(Tuple[SynchronizedData, Enum], result)
        if event == Event.DONE and not synchronized_data.mech_only_queue:
            # queue exhausted -> route the cycle to the checkpoint
            return synchronized_data, Event.NO_MARKETS
        return synchronized_data, event


class MechResponseRouterRound(VotingRound):
    """Route a Mech delivery to the normal or the post-cap consumer."""

    payload_class = RedeemRouterPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    negative_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = get_name(SynchronizedData.participant_to_selection)

    def end_block(self) -> Optional[Tuple[SynchronizedData, Enum]]:
        """Branch to the Mech-only receiver when capped, otherwise to the normal receiver."""
        result = super().end_block()
        if result is None:
            return None

        synchronized_data, event = cast(Tuple[SynchronizedData, Enum], result)
        if event == Event.DONE:
            if synchronized_data.mech_only_mode:
                return cast(SynchronizedData, synchronized_data), Event.MECH_ONLY
            return cast(SynchronizedData, synchronized_data), Event.DONE
        return cast(SynchronizedData, synchronized_data), event
