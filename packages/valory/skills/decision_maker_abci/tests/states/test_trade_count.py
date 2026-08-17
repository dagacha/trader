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

"""Tests for the TradeCountRound consensus state."""

from packages.valory.skills.abstract_round_abci.base import (
    CollectSameUntilThresholdRound,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCountPayload
from packages.valory.skills.decision_maker_abci.states.base import (
    Event,
    SynchronizedData,
)
from packages.valory.skills.decision_maker_abci.states.trade_count import (
    TradeCountRound,
)


class TestTradeCountRound:
    """Tests for the TradeCountRound class."""

    def test_inherits_collect_same_until_threshold_round(self) -> None:
        """Test that TradeCountRound inherits from CollectSameUntilThresholdRound."""
        assert issubclass(TradeCountRound, CollectSameUntilThresholdRound)

    def test_round_properties(self) -> None:
        """Test the round's class-level properties are wired correctly."""
        assert TradeCountRound.payload_class is TradeCountPayload
        assert TradeCountRound.synchronized_data_class is SynchronizedData
        assert TradeCountRound.done_event == Event.DONE
        assert TradeCountRound.none_event == Event.NONE
        assert TradeCountRound.no_majority_event == Event.NO_MAJORITY

    def test_selection_and_collection_keys(self) -> None:
        """Test that the selection key targets the trade counter and the collection key targets the participants' selection."""
        from packages.valory.skills.abstract_round_abci.base import get_name

        assert TradeCountRound.selection_key == get_name(
            SynchronizedData.successful_trade_count
        )
        assert TradeCountRound.collection_key == get_name(
            SynchronizedData.participant_to_selection
        )
