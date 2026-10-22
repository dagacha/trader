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

"""Tests for TradeCountBehaviour."""

import json
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

from packages.valory.skills.agent_performance_summary_abci.activity_goal import (
    read_trades,
)
from packages.valory.skills.decision_maker_abci.behaviours.base import (
    TRADE_COUNT_FILENAME,
)
from packages.valory.skills.decision_maker_abci.behaviours.trade_count import (
    TradeCountBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCountPayload
from packages.valory.skills.decision_maker_abci.states.trade_count import (
    TradeCountRound,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_behaviour(store_path):  # type: ignore[no-untyped-def]
    """Return a TradeCountBehaviour with mocked dependencies."""
    behaviour = object.__new__(TradeCountBehaviour)
    context = MagicMock()
    context.agent_address = "test_agent"
    context.params.store_path = store_path
    context.state.round_sequence.last_round_transition_timestamp.timestamp.return_value = (
        1001
    )
    # `benchmark_tool.measure(...).local()` is used as a context manager
    measure_cm = MagicMock()
    measure_cm.local.return_value.__enter__.return_value = None
    measure_cm.local.return_value.__exit__.return_value = False
    context.benchmark_tool.measure.return_value = measure_cm
    behaviour.__dict__["_context"] = context
    return behaviour


def _run_async_act(behaviour, current_count):  # type: ignore[no-untyped-def]
    """Drive async_act to completion and return the payload."""
    payloads_sent = []  # type: ignore[no-untyped-def]

    def mock_finish(payload) -> None:  # type: ignore[no-untyped-def, misc]
        payloads_sent.append(payload)
        yield  # type: ignore[no-untyped-def]

    behaviour.finish_behaviour = mock_finish  # type: ignore[method-assign]

    with (
        patch.object(
            type(behaviour), "synchronized_data", new_callable=PropertyMock
        ) as mock_sd,
        patch.object(type(behaviour), "sampled_bet", new_callable=PropertyMock) as bet,
    ):
        bet.return_value.id = "market"
        sd = MagicMock()
        sd.successful_trade_count = current_count
        sd.final_tx_hash = "0xsettled"
        sd.sampled_bet_index = 0
        sd.previous_checkpoint = None
        mock_sd.return_value = sd

        gen = behaviour.async_act()
        try:
            while True:
                next(gen)
        except StopIteration:
            pass

    assert len(payloads_sent) == 1
    return payloads_sent[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTradeCountBehaviour:
    """Tests for TradeCountBehaviour.async_act."""

    def test_matching_round(self) -> None:
        """The behaviour is bound to TradeCountRound."""
        assert TradeCountBehaviour.matching_round is TradeCountRound

    def test_increments_counter_from_zero(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The payload carries the incremented count starting from zero."""
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(behaviour, 0)
        assert isinstance(payload, TradeCountPayload)
        assert payload.successful_trade_count == 1
        # The new count is persisted to the durable file.
        assert json.loads((tmp_path / TRADE_COUNT_FILENAME).read_text()) == {
            "count": 1,
            "placement_keys": ["omen:0xsettled"],
        }

    def test_increments_counter_from_positive(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The payload carries the incremented count for a positive baseline."""
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(behaviour, 4)
        assert isinstance(payload, TradeCountPayload)
        assert payload.successful_trade_count == 5
        assert json.loads((tmp_path / TRADE_COUNT_FILENAME).read_text()) == {
            "count": 5,
            "placement_keys": ["omen:0xsettled"],
        }

    def test_increments_from_durable_file_after_restart(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The increment is based on the persisted file, not the wiped DB value.

        After a restart synchronized data resets to 0, but the file still holds
        the real count; the increment must build on the file value.

        :param tmp_path: per-test durable store directory.
        """
        (tmp_path / TRADE_COUNT_FILENAME).write_text("2")
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(behaviour, 0)
        assert payload.successful_trade_count == 3
        assert json.loads((tmp_path / TRADE_COUNT_FILENAME).read_text()) == {
            "count": 3,
            "placement_keys": ["omen:0xsettled"],
        }

    def test_payload_sender_is_agent_address(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The payload is signed by the agent's address."""
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(behaviour, 2)
        assert payload.sender == "test_agent"

    def test_store_failure_is_logged_and_swallowed(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A write failure is logged without breaking the round."""
        # Point the store at a missing directory so the write raises OSError.
        behaviour = _make_behaviour(store_path=tmp_path / "missing_dir")
        payload = _run_async_act(behaviour, 0)
        assert isinstance(payload, TradeCountPayload)
        assert payload.successful_trade_count == 1
        errors = [
            call.args[0] for call in behaviour.context.logger.error.call_args_list
        ]
        assert len(errors) == 2
        assert any("Could not record settled trade" in error for error in errors)
        assert any(
            "Failed to persist successful-trade state" in error for error in errors
        )


def test_settled_omen_trade_updates_both_counters(tmp_path: Path) -> None:
    """A settled Omen placement must update the cap and the activity ledger once."""
    behaviour = _make_behaviour(tmp_path)
    with (
        patch.object(type(behaviour), "sampled_bet", new_callable=PropertyMock) as bet,
        patch.object(
            type(behaviour), "synced_timestamp", new_callable=PropertyMock
        ) as timestamp,
    ):
        bet.return_value.id = "market"
        timestamp.return_value = 1001
        first = _run_async_act(behaviour, 0)
        timestamp.return_value = 1002
        retry = _run_async_act(behaviour, 1)
    assert first.successful_trade_count == retry.successful_trade_count == 1
    assert read_trades(tmp_path) == [
        {"timestamp": 1001, "bet_id": "market", "placement_key": "omen:0xsettled"}
    ]
