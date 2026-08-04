"""Consensus state for successful Omen placement counting."""

from packages.valory.skills.abstract_round_abci.base import (
    CollectSameUntilThresholdRound,
    get_name,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCountPayload
from packages.valory.skills.decision_maker_abci.states.base import Event, SynchronizedData


class TradeCountRound(CollectSameUntilThresholdRound):
    """Persist one successful Omen placement before redemption begins."""

    payload_class = TradeCountPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    collection_key = get_name(SynchronizedData.participant_to_selection)
    selection_key = get_name(SynchronizedData.successful_trade_count)
