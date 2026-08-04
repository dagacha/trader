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

"""Tests for the Mech-only (post-cap) consensus states."""

from typing import Tuple, cast
from unittest.mock import MagicMock, patch

from packages.valory.skills.abstract_round_abci.base import (
    CollectSameUntilThresholdRound,
    VotingRound,
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
from packages.valory.skills.decision_maker_abci.states.mech_only import (
    MechOnlyReceiveRound,
    MechOnlySelectionRound,
    MechResponseRouterRound,
)


class TestMechOnlySelectionRound:
    """Tests for MechOnlySelectionRound."""

    def test_inherits_collect_same_until_threshold_round(self) -> None:
        """Test inheritance."""
        assert issubclass(MechOnlySelectionRound, CollectSameUntilThresholdRound)

    def test_round_properties(self) -> None:
        """Test the round's class-level properties."""
        assert MechOnlySelectionRound.payload_class is MechOnlySelectionPayload
        assert MechOnlySelectionRound.synchronized_data_class is SynchronizedData
        assert MechOnlySelectionRound.done_event == Event.DONE
        assert MechOnlySelectionRound.none_event == Event.NO_MARKETS
        assert MechOnlySelectionRound.no_majority_event == Event.NO_MAJORITY

    def test_end_block_returns_none_when_super_returns_none(self) -> None:
        """Test end_block returns None when the parent returns None."""
        round_instance = MechOnlySelectionRound(MagicMock(), MagicMock())
        with patch.object(
            CollectSameUntilThresholdRound, "end_block", return_value=None
        ):
            result = round_instance.end_block()
        assert result is None

    def test_end_block_emits_no_markets_when_no_requests(self) -> None:
        """When DONE but the batch is empty, end_block emits NO_MARKETS."""
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_requests = []
        round_instance = MechOnlySelectionRound(MagicMock(), mock_synced_data)
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
        assert event == Event.NO_MARKETS

    def test_end_block_emits_done_when_requests_present(self) -> None:
        """When DONE and the batch is non-empty, end_block emits DONE."""
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_requests = ["prompt"]
        round_instance = MechOnlySelectionRound(MagicMock(), mock_synced_data)
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


class TestMechOnlyReceiveRound:
    """Tests for MechOnlyReceiveRound."""

    def test_inherits_collect_same_until_threshold_round(self) -> None:
        """Test inheritance."""
        assert issubclass(MechOnlyReceiveRound, CollectSameUntilThresholdRound)

    def test_round_properties(self) -> None:
        """Test the round's class-level properties."""
        assert MechOnlyReceiveRound.payload_class is MechOnlyReceivePayload
        assert MechOnlyReceiveRound.synchronized_data_class is SynchronizedData
        assert MechOnlyReceiveRound.done_event == Event.DONE
        assert MechOnlyReceiveRound.none_event == Event.NO_MARKETS

    def test_end_block_returns_none_when_super_returns_none(self) -> None:
        """Test end_block returns None when the parent returns None."""
        round_instance = MechOnlyReceiveRound(MagicMock(), MagicMock())
        with patch.object(
            CollectSameUntilThresholdRound, "end_block", return_value=None
        ):
            result = round_instance.end_block()
        assert result is None

    def test_end_block_emits_no_markets_when_queue_empty(self) -> None:
        """When DONE and the queue is exhausted, end_block emits NO_MARKETS."""
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_only_queue = []
        round_instance = MechOnlyReceiveRound(MagicMock(), mock_synced_data)
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
        assert event == Event.NO_MARKETS

    def test_end_block_emits_done_when_queue_remaining(self) -> None:
        """When DONE and the queue still has markets, end_block emits DONE (loop)."""
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_only_queue = ["market_1"]
        round_instance = MechOnlyReceiveRound(MagicMock(), mock_synced_data)
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


class TestMechResponseRouterRound:
    """Tests for MechResponseRouterRound."""

    def test_inherits_voting_round(self) -> None:
        """Test inheritance."""
        assert issubclass(MechResponseRouterRound, VotingRound)

    def test_round_properties(self) -> None:
        """Test the round's class-level properties."""
        assert MechResponseRouterRound.payload_class is RedeemRouterPayload
        assert MechResponseRouterRound.synchronized_data_class is SynchronizedData
        assert MechResponseRouterRound.done_event == Event.DONE
        assert MechResponseRouterRound.no_majority_event == Event.NO_MAJORITY

    def test_end_block_returns_none_when_super_returns_none(self) -> None:
        """Test end_block returns None when the parent returns None."""
        round_instance = MechResponseRouterRound(MagicMock(), MagicMock())
        with patch.object(VotingRound, "end_block", return_value=None):
            result = round_instance.end_block()
        assert result is None

    def test_end_block_routes_to_mech_only_when_capped(self) -> None:
        """When DONE and mech_only_mode is True, end_block emits MECH_ONLY."""
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_only_mode = True
        round_instance = MechResponseRouterRound(MagicMock(), mock_synced_data)
        with patch.object(
            VotingRound,
            "end_block",
            return_value=cast(
                Tuple[SynchronizedData, Event], (mock_synced_data, Event.DONE)
            ),
        ):
            result = round_instance.end_block()
        assert result is not None
        _, event = result
        assert event == Event.MECH_ONLY

    def test_end_block_routes_to_decision_receive_when_not_capped(self) -> None:
        """When DONE and mech_only_mode is False, end_block emits DONE."""
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_only_mode = False
        round_instance = MechResponseRouterRound(MagicMock(), mock_synced_data)
        with patch.object(
            VotingRound,
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
        """Non-DONE events are passed through unchanged."""
        mock_synced_data = MagicMock(spec=SynchronizedData)
        mock_synced_data.mech_only_mode = True
        round_instance = MechResponseRouterRound(MagicMock(), mock_synced_data)
        with patch.object(
            VotingRound,
            "end_block",
            return_value=cast(
                Tuple[SynchronizedData, Event], (mock_synced_data, Event.NO_MAJORITY)
            ),
        ):
            result = round_instance.end_block()
        assert result is not None
        _, event = result
        assert event == Event.NO_MAJORITY
