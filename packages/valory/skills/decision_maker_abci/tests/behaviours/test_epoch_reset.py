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
from unittest.mock import MagicMock, PropertyMock, patch

from packages.valory.skills.decision_maker_abci.behaviours.base import (
    TRADE_COUNT_FILENAME,
)
from packages.valory.skills.decision_maker_abci.behaviours.epoch_reset import (
    EpochResetBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import EpochResetPayload
from packages.valory.skills.decision_maker_abci.states.epoch_reset import (
    EpochResetRound,
)


def _make_behaviour(store_path):  # type: ignore[no-untyped-def]
    """Return an EpochResetBehaviour with mocked dependencies."""
    behaviour = object.__new__(EpochResetBehaviour)
    context = MagicMock()
    context.agent_address = "test_agent"
    context.params.store_path = store_path
    measure_cm = MagicMock()
    measure_cm.local.return_value.__enter__.return_value = None
    measure_cm.local.return_value.__exit__.return_value = False
    context.benchmark_tool.measure.return_value = measure_cm
    behaviour.__dict__["_context"] = context
    return behaviour


def _run_async_act(behaviour, is_checkpoint, current_count, current_queue):  # type: ignore[no-untyped-def]
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
        """When is_checkpoint_reached is True, counter resets to 0 and queue is cleared."""
        (tmp_path / TRADE_COUNT_FILENAME).write_text("5")
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(
            behaviour, is_checkpoint=True, current_count=5, current_queue=["m1", "m2"]
        )
        assert isinstance(payload, EpochResetPayload)
        assert payload.successful_trade_count == 0
        assert json.loads(payload.mech_only_queue) == []
        # The durable file is reset too, so the cap does not re-arm on restart.
        assert json.loads((tmp_path / TRADE_COUNT_FILENAME).read_text()) == {
            "count": 0,
            "placement_keys": [],
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
        """Reset still fires when counter is already 0 (idempotent)."""
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(
            behaviour, is_checkpoint=True, current_count=0, current_queue=[]
        )
        assert isinstance(payload, EpochResetPayload)
        assert payload.successful_trade_count == 0
        assert json.loads(payload.mech_only_queue) == []
        assert json.loads((tmp_path / TRADE_COUNT_FILENAME).read_text()) == {
            "count": 0,
            "placement_keys": [],
        }

    def test_payload_sender_is_agent_address(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The payload is signed by the agent's address."""
        behaviour = _make_behaviour(store_path=tmp_path)
        payload = _run_async_act(
            behaviour, is_checkpoint=False, current_count=0, current_queue=[]
        )
        assert payload.sender == "test_agent"
