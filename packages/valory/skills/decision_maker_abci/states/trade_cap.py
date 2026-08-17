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

"""Consensus state selecting normal or capped trading mode."""

from typing import Optional, Tuple, cast

from packages.valory.skills.abstract_round_abci.base import (
    CollectSameUntilThresholdRound,
    get_name,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCapPayload
from packages.valory.skills.decision_maker_abci.states.base import (
    Event,
    SynchronizedData,
)


class TradeCapRound(CollectSameUntilThresholdRound):
    """Persist whether the successful-placement cap has been reached."""

    payload_class = TradeCapPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    no_majority_event = Event.NO_MAJORITY
    none_event = Event.NONE
    collection_key = get_name(SynchronizedData.participant_to_selection)
    selection_key = get_name(SynchronizedData.mech_only_mode)

    def end_block(self) -> Optional[Tuple[SynchronizedData, Event]]:
        """Emit the capped-mode event after a consensus decision."""
        result = super().end_block()
        if result is None:
            return None

        synchronized_data, event = cast(Tuple[SynchronizedData, Event], result)
        if event == Event.DONE and synchronized_data.mech_only_mode:
            return synchronized_data, Event.MECH_ONLY
        return synchronized_data, event
