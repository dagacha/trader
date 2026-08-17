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

"""Tests for the TradeCapRound consensus state and its cap-gate event."""

from typing import Tuple, cast
from unittest.mock import MagicMock, patch

from packages.valory.skills.abstract_round_abci.base import (
    CollectSameUntilThresholdRound,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCapPayload
from packages.valory.skills.decision_maker_abci.states.base import (
    Event,
    SynchronizedData,
)
from packages.valory.skills.decision_maker_abci.states.trade_cap import TradeCapRound


class TestTradeCapRound:
    """Tests for the TradeCapRound class."""

    def test_inherits_collect_same_until_threshold_round(self) -> None:
        """Test that TradeCapRound inherits from CollectSameUntilThresholdRound."""
        assert issubclass(TradeCapRound, CollectSameUntilThresholdRound)

    def test_round_properties(self) -> None:
        """Test the round's class-level properties are wired correctly."""
        assert TradeCapRound.payload_class is TradeCapPayload
        assert TradeCapRound.synchronized_data_class is SynchronizedData
        assert TradeCapRound.done_event == Event.DONE
        assert TradeCapRound.none_event == Event.NONE
        assert TradeCapRound.no_majority_event == Event.NO_MAJORITY

    def test_selection_key_targets_mech_only_mode(self) -> None:
        """Test that the selection key targets the persisted capped-mode flag."""
        from packages.valory.skills.abstract_round_abci.base import get_name

        assert TradeCapRound.selection_key == get_name(SynchronizedData.mech_only_mode)

    def test_end_block_returns_none_when_super_returns_none(self) -> None:
        """Test end_block returns None when the parent returns None."""
        mock_context = MagicMock()
        mock_synced_data = MagicMock(spec=SynchronizedData)
        round_instance = TradeCapRound(
            synchronized_data=mock_synced_data, context=mock_context
        )
        with patch.object(
            CollectSameUntilThresholdRound, "end_block", return_value=None
        ):
            result = round_instance.end_block()
        assert result is None

    def test_end_block_emits_mech_only_when_capped(self) -> None:
        """When consensus is DONE and mech_only_mode is True, end_block emits MECH_ONLY."""
        mock_context = MagicMock()
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_only_mode = True
        round_instance = TradeCapRound(
            synchronized_data=mock_synced_data, context=mock_context
        )
        with patch.object(
            CollectSameUntilThresholdRound,
            "end_block",
            return_value=cast(
                Tuple[SynchronizedData, Event], (mock_synced_data, Event.DONE)
            ),
        ):
            result = round_instance.end_block()
        assert result is not None
        _, event = result
        assert event == Event.MECH_ONLY

    def test_end_block_emits_done_when_not_capped(self) -> None:
        """When consensus is DONE and mech_only_mode is False, end_block emits DONE."""
        mock_context = MagicMock()
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_only_mode = False
        round_instance = TradeCapRound(
            synchronized_data=mock_synced_data, context=mock_context
        )
        with patch.object(
            CollectSameUntilThresholdRound,
            "end_block",
            return_value=cast(
                Tuple[SynchronizedData, Event], (mock_synced_data, Event.DONE)
            ),
        ):
            result = round_instance.end_block()
        assert result is not None
        _, event = result
        assert event == Event.DONE

    def test_end_block_passes_through_non_done_events(self) -> None:
        """Non-DONE events (e.g. NO_MAJORITY) are passed through unchanged."""
        mock_context = MagicMock()
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_only_mode = True
        round_instance = TradeCapRound(
            synchronized_data=mock_synced_data, context=mock_context
        )
        with patch.object(
            CollectSameUntilThresholdRound,
            "end_block",
            return_value=cast(
                Tuple[SynchronizedData, Event], (mock_synced_data, Event.NO_MAJORITY)
            ),
        ):
            result = round_instance.end_block()
        assert result is not None
        _, event = result
        assert event == Event.NO_MAJORITY
