"""Consensus state selecting normal or capped trading mode."""

from typing import Optional, Tuple

from packages.valory.skills.abstract_round_abci.base import (
    CollectSameUntilThresholdRound,
    get_name,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCapPayload
from packages.valory.skills.decision_maker_abci.states.base import Event, SynchronizedData


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

        synchronized_data, event = result
        if event == Event.DONE and synchronized_data.mech_only_mode:
            return synchronized_data, Event.MECH_ONLY
        return synchronized_data, event
