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

"""Tests for TradeCapBehaviour."""

from unittest.mock import MagicMock, PropertyMock, patch

from packages.valory.skills.decision_maker_abci.behaviours.base import (
    TRADE_COUNT_FILENAME,
)
from packages.valory.skills.decision_maker_abci.behaviours.trade_cap import (
    TradeCapBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCapPayload
from packages.valory.skills.decision_maker_abci.states.trade_cap import TradeCapRound

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_behaviour(max_trades, store_path):  # type: ignore[no-untyped-def]
    """Return a TradeCapBehaviour with mocked dependencies."""
    behaviour = object.__new__(TradeCapBehaviour)
    context = MagicMock()
    context.agent_address = "test_agent"
    context.params.max_trades = max_trades
    context.params.store_path = store_path
    behaviour.__dict__["_context"] = context
    return behaviour


def _run_async_act(behaviour, successful_trade_count):  # type: ignore[no-untyped-def]
    """Drive async_act to completion and return the payload."""
    payloads_sent = []  # type: ignore[no-untyped-def]

    def mock_finish(payload) -> None:  # type: ignore[no-untyped-def, misc]
        payloads_sent.append(payload)
        yield  # type: ignore[no-untyped-def]

    behaviour.finish_behaviour = mock_finish  # type: ignore[method-assign]

    with patch.object(
        type(behaviour), "synchronized_data", new_callable=PropertyMock
    ) as mock_sd:
        sd = MagicMock()
        sd.successful_trade_count = successful_trade_count
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


class TestTradeCapBehaviour:
    """Tests for TradeCapBehaviour.async_act cap-gate logic."""

    def test_matching_round(self) -> None:
        """The behaviour is bound to TradeCapRound."""
        assert TradeCapBehaviour.matching_round is TradeCapRound

    def test_cap_disabled_always_normal_mode(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """When max_trades is 0 (disabled), mech_only is False even with placements."""
        behaviour = _make_behaviour(max_trades=0, store_path=tmp_path)
        payload = _run_async_act(behaviour, successful_trade_count=100)
        assert isinstance(payload, TradeCapPayload)
        assert payload.mech_only is False

    def test_below_cap_normal_mode(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """When the count is below a positive cap, mech_only is False."""
        behaviour = _make_behaviour(max_trades=5, store_path=tmp_path)
        payload = _run_async_act(behaviour, successful_trade_count=2)
        assert isinstance(payload, TradeCapPayload)
        assert payload.mech_only is False

    def test_at_cap_enters_mech_only(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """When the count equals the cap, mech_only is True."""
        behaviour = _make_behaviour(max_trades=5, store_path=tmp_path)
        payload = _run_async_act(behaviour, successful_trade_count=5)
        assert isinstance(payload, TradeCapPayload)
        assert payload.mech_only is True

    def test_above_cap_enters_mech_only(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """When the count exceeds the cap, mech_only is True."""
        behaviour = _make_behaviour(max_trades=5, store_path=tmp_path)
        payload = _run_async_act(behaviour, successful_trade_count=7)
        assert isinstance(payload, TradeCapPayload)
        assert payload.mech_only is True

    def test_payload_sender_is_agent_address(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The payload is signed by the agent's address."""
        behaviour = _make_behaviour(max_trades=1, store_path=tmp_path)
        payload = _run_async_act(behaviour, successful_trade_count=1)
        assert payload.sender == "test_agent"

    def test_durable_file_overrides_synchronized_data(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The cap uses the persisted file value, not the (restart-wiped) DB value.

        This is the restart scenario: synchronized data has reset to 0 but the
        durable file still holds the pre-restart count, so the cap must trigger.

        :param tmp_path: per-test durable store directory.
        """
        (tmp_path / TRADE_COUNT_FILENAME).write_text("5")
        behaviour = _make_behaviour(max_trades=5, store_path=tmp_path)
        payload = _run_async_act(behaviour, successful_trade_count=0)
        assert isinstance(payload, TradeCapPayload)
        assert payload.mech_only is True
