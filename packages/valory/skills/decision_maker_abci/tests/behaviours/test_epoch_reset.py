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

"""Tests for EpochResetBehaviour."""

import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from packages.valory.skills.agent_performance_summary_abci.activity_goal import (
    count_trades_since,
    record_trade,
)
from packages.valory.skills.decision_maker_abci.behaviours.base import (
    TRADE_COUNT_FILENAME,
)
from packages.valory.skills.decision_maker_abci.behaviours.epoch_reset import (
    EpochResetBehaviour,
)
from packages.valory.skills.decision_maker_abci.behaviours.trade_cap import (
    TradeCapBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import EpochResetPayload
from packages.valory.skills.decision_maker_abci.states.epoch_reset import (
    EpochResetRound,
)
from packages.valory.skills.staking_abci.behaviours import (
    CHECKPOINT_FILENAME,
    CallCheckpointBehaviour,
)


def _make_behaviour(store_path):  # type: ignore[no-untyped-def]
    """Return an EpochResetBehaviour with mocked dependencies."""
    behaviour = object.__new__(EpochResetBehaviour)
    context = MagicMock()
    context.agent_address = "test_agent"
    context.params.store_path = store_path
    context.state.synchronized_data.db.get.return_value = None
    measure_cm = MagicMock()
    measure_cm.local.return_value.__enter__.return_value = None
    measure_cm.local.return_value.__exit__.return_value = False
    context.benchmark_tool.measure.return_value = measure_cm
    behaviour.__dict__["_context"] = context
    return behaviour


def _run_async_act(
    behaviour: Any,
    is_checkpoint: bool,
    current_count: int,
    current_queue: list,
    period_start: Optional[int] = None,
) -> Any:
    """Drive async_act to completion and return the payload."""
    payloads_sent = []

    def mock_finish(payload) -> None:  # type: ignore[no-untyped-def, misc]
        payloads_sent.append(payload)
        yield

    behaviour.finish_behaviour = mock_finish

    with patch.object(
        type(behaviour), "synchronized_data", new_callable=PropertyMock
    ) as mock_sd:
        sd = MagicMock()
        sd.is_checkpoint_reached = is_checkpoint
        sd.successful_trade_count = current_count
        sd.previous_checkpoint = period_start
        sd.mech_only_queue = current_queue
        mock_sd.return_value = sd

        gen = behaviour.async_act()
        try:
            while True:
                next(gen)
        except StopIteration:
            pass

    assert len(payloads_sent) == 1
    return payloads_sent[0]


class TestEpochResetBehaviour:
    """Tests for EpochResetBehaviour.async_act."""

    def test_matching_round(self) -> None:
        """The behaviour is bound to EpochResetRound."""
        assert EpochResetBehaviour.matching_round is EpochResetRound

    def test_resets_on_new_epoch(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A changed counter period resets the count and clears the queue."""
        (tmp_path / TRADE_COUNT_FILENAME).write_text(
            json.dumps({"count": 5, "placement_keys": ["old"], "period_start": 999})
        )
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(
            behaviour,
            is_checkpoint=True,
            current_count=5,
            current_queue=["m1", "m2"],
            period_start=1000,
        )
        assert isinstance(payload, EpochResetPayload)
        assert payload.successful_trade_count == 0
        assert json.loads(payload.mech_only_queue) == []
        # The durable file is reset too, so the cap does not re-arm on restart.
        assert json.loads((tmp_path / TRADE_COUNT_FILENAME).read_text()) == {
            "count": 0,
            "placement_keys": [],
            "period_start": 1000,
        }

    def test_noop_when_no_epoch_change(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """When is_checkpoint_reached is False, existing values are preserved."""
        (tmp_path / TRADE_COUNT_FILENAME).write_text("3")
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(
            behaviour, is_checkpoint=False, current_count=3, current_queue=["m1"]
        )
        assert isinstance(payload, EpochResetPayload)
        # The durable file value wins over the (possibly restart-wiped) DB value.
        assert payload.successful_trade_count == 3
        assert json.loads(payload.mech_only_queue) == ["m1"]
        assert (tmp_path / TRADE_COUNT_FILENAME).read_text() == "3"

    def test_noop_rehydrates_from_file_after_restart(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The no-op path re-hydrates the count from the file after a restart."""
        (tmp_path / TRADE_COUNT_FILENAME).write_text("4")
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(
            behaviour, is_checkpoint=False, current_count=0, current_queue=[]
        )
        assert payload.successful_trade_count == 4

    def test_resets_even_when_counter_is_zero(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A changed period resets the state even when the count is zero."""
        (tmp_path / TRADE_COUNT_FILENAME).write_text(
            json.dumps({"count": 0, "placement_keys": ["old"], "period_start": 999})
        )
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(
            behaviour,
            is_checkpoint=True,
            current_count=0,
            current_queue=[],
            period_start=1000,
        )
        assert isinstance(payload, EpochResetPayload)
        assert payload.successful_trade_count == 0
        assert json.loads(payload.mech_only_queue) == []
        assert json.loads((tmp_path / TRADE_COUNT_FILENAME).read_text()) == {
            "count": 0,
            "placement_keys": [],
            "period_start": 1000,
        }

    def test_payload_sender_is_agent_address(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The payload is signed by the agent's address."""
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(
            behaviour, is_checkpoint=False, current_count=0, current_queue=[]
        )
        assert payload.sender == "test_agent"


def test_activity_ledger_and_trade_cap_reset_at_same_epoch(tmp_path: Path) -> None:
    """The checkpoint removes old placements from both epoch counts."""
    period_start = 1000
    record_trade(tmp_path, period_start - 1, "previous_epoch")
    (tmp_path / TRADE_COUNT_FILENAME).write_text(
        json.dumps({"count": 1, "placement_keys": [], "period_start": period_start - 1})
    )
    behaviour = _make_behaviour(tmp_path)
    payload = _run_async_act(behaviour, True, 1, [], period_start)
    assert payload.successful_trade_count == 0
    assert count_trades_since(tmp_path, period_start, MagicMock()) == 0

    record_trade(tmp_path, period_start, "current_epoch")
    assert behaviour.record_successful_placement("current_epoch") == 1
    payload = _run_async_act(behaviour, False, 1, [], period_start)
    assert payload.successful_trade_count == 1
    assert count_trades_since(tmp_path, period_start, MagicMock()) == 1


@pytest.mark.parametrize(
    "state",
    [
        {"count": 3, "placement_keys": ["bet"], "period_start": 1000},
        3,
        {"count": 3, "placement_keys": ["bet"]},
    ],
    ids=["dated", "legacy_integer", "legacy_dict"],
)
def test_restart_with_stale_checkpoint_keeps_cap(tmp_path: Path, state) -> None:  # type: ignore[no-untyped-def]
    """A stale checkpoint flag cannot erase a current or legacy counter."""
    (tmp_path / CHECKPOINT_FILENAME).write_text("999")
    counter = tmp_path / TRADE_COUNT_FILENAME
    counter.write_text(json.dumps(state))
    checkpoint = object.__new__(CallCheckpointBehaviour)
    checkpoint._checkpoint_filepath = tmp_path / CHECKPOINT_FILENAME
    checkpoint.ts_checkpoint = 1000
    checkpoint.__dict__["_context"] = MagicMock()

    def ready(*_args):  # type: ignore[no-untyped-def]
        yield from ()

    with (
        patch.object(
            type(checkpoint),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=MagicMock(period_count=0, previous_checkpoint=None),
        ),
        patch.object(checkpoint, "wait_for_condition_with_sleep", ready),
    ):
        with pytest.raises(StopIteration) as result:
            next(checkpoint.check_new_epoch())
    assert result.value.value is True

    for _ in range(2):
        # Rebuild behaviours and clear synchronized counts as a restart does.
        behaviour = _make_behaviour(tmp_path)
        payload = _run_async_act(behaviour, True, 0, [], 1000)
        assert payload.successful_trade_count == 3
        stored = json.loads(counter.read_text())
        assert stored["count"] == 3
        assert stored["period_start"] == 1000
        assert stored["placement_keys"] == ([] if state == 3 else ["bet"])

        cap = object.__new__(TradeCapBehaviour)
        cap.__dict__["_context"] = behaviour.context
        cap.context.params.max_trades = 3
        assert _run_async_act(cap, False, 0, [], 1000).mech_only is True


@pytest.mark.parametrize("is_checkpoint", [False, True])
def test_counter_period_controls_reset(tmp_path: Path, is_checkpoint: bool) -> None:
    """A changed checkpoint resets the durable count regardless of the flag."""
    counter = tmp_path / TRADE_COUNT_FILENAME
    counter.write_text(
        json.dumps({"count": 3, "placement_keys": ["old"], "period_start": 999})
    )
    behaviour = _make_behaviour(tmp_path)
    payload = _run_async_act(behaviour, is_checkpoint, 3, [], 1000)
    assert payload.successful_trade_count == 0
    assert json.loads(counter.read_text()) == {
        "count": 0,
        "placement_keys": [],
        "period_start": 1000,
    }
