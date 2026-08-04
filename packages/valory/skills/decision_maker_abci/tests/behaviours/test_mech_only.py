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

"""Tests for the Mech-only (post-cap) behaviours."""

import json
from dataclasses import fields
from unittest.mock import MagicMock, PropertyMock, patch

from packages.valory.skills.decision_maker_abci.behaviours.mech_only import (
    MechOnlyReceiveBehaviour,
    MechOnlySelectionBehaviour,
    MechResponseRouterBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import (
    MechOnlyReceivePayload,
    MechOnlySelectionPayload,
)
from packages.valory.skills.decision_maker_abci.states.mech_only import (
    MechOnlyReceiveRound,
    MechOnlySelectionRound,
    MechResponseRouterRound,
)
from packages.valory.skills.market_manager_abci.bets import BINARY_N_SLOTS

# Forbidden field names that must never appear on a Mech-only payload.
NO_BET_FIELDS = frozenset(
    {"vote", "bet_amount", "is_profitable", "confidence", "should_be_sold"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_behaviour(behaviour_cls):  # type: ignore[no-untyped-def]
    """Return a behaviour instance with a mocked context (no __init__)."""
    behaviour = object.__new__(behaviour_cls)
    context = MagicMock()
    context.agent_address = "test_agent"
    measure_cm = MagicMock()
    measure_cm.local.return_value.__enter__.return_value = None
    measure_cm.local.return_value.__exit__.return_value = False
    context.benchmark_tool.measure.return_value = measure_cm
    behaviour.__dict__["_context"] = context
    return behaviour


def _run_async_act(behaviour):  # type: ignore[no-untyped-def]
    """Drive async_act to completion and return the payload."""
    payloads_sent = []  # type: ignore[no-untyped-def]

    def mock_finish(payload) -> None:  # type: ignore[no-untyped-def, misc]
        payloads_sent.append(payload)
        yield  # type: ignore[no-untyped-def]

    behaviour.finish_behaviour = mock_finish  # type: ignore[method-assign]

    gen = behaviour.async_act()
    try:
        while True:
            next(gen)
    except StopIteration:
        pass

    assert len(payloads_sent) == 1
    return payloads_sent[0]


def _mock_bet(  # type: ignore[no-untyped-def]
    bet_id="market_1",
    market="omen_subgraph",
    outcome_slot_count=BINARY_N_SLOTS,
    title="Will it rain?",
    yes="Yes",
    no="No",
    opening_timestamp=10_000_000_000,
):
    """Create a mock Bet with the attributes used by the Mech-only flow."""
    bet = MagicMock()
    bet.id = bet_id
    bet.market = market
    bet.outcomeSlotCount = outcome_slot_count
    bet.title = title
    bet.yes = yes
    bet.no = no
    bet.openingTimestamp = opening_timestamp
    bet.to_request_context.return_value = {"market_id": bet_id}
    return bet


# ---------------------------------------------------------------------------
# Payload invariant tests
# ---------------------------------------------------------------------------


class TestMechOnlyPayloadNoBetFields:
    """The Mech-only payloads must never carry vote/bet/profitability fields."""

    def test_selection_payload_has_no_bet_fields(self) -> None:
        """MechOnlySelectionPayload must not contain any betting field."""
        payload_fields = {f.name for f in fields(MechOnlySelectionPayload)}
        assert not (payload_fields & NO_BET_FIELDS)

    def test_receive_payload_has_no_bet_fields(self) -> None:
        """MechOnlyReceivePayload must not contain any betting field."""
        payload_fields = {f.name for f in fields(MechOnlyReceivePayload)}
        assert not (payload_fields & NO_BET_FIELDS)


# ---------------------------------------------------------------------------
# MechResponseRouterBehaviour
# ---------------------------------------------------------------------------


class TestMechResponseRouterBehaviour:
    """Tests for MechResponseRouterBehaviour.async_act."""

    def test_matching_round(self) -> None:
        """The behaviour is bound to MechResponseRouterRound."""
        assert MechResponseRouterBehaviour.matching_round is MechResponseRouterRound

    def test_always_votes_true(self) -> None:
        """The router always votes True; the routing decision is made in end_block."""
        behaviour = _make_behaviour(MechResponseRouterBehaviour)
        payload = _run_async_act(behaviour)
        assert payload.vote is True


# ---------------------------------------------------------------------------
# MechOnlyReceiveBehaviour
# ---------------------------------------------------------------------------


class TestMechOnlyReceiveBehaviour:
    """Tests for MechOnlyReceiveBehaviour.async_act."""

    def test_matching_round(self) -> None:
        """The behaviour is bound to MechOnlyReceiveRound."""
        assert MechOnlyReceiveBehaviour.matching_round is MechOnlyReceiveRound

    def test_reasserts_queue_remaining(self) -> None:
        """The payload carries the current (non-empty) queue unchanged."""
        behaviour = _make_behaviour(MechOnlyReceiveBehaviour)
        with patch.object(
            type(behaviour), "synchronized_data", new_callable=PropertyMock
        ) as mock_sd:
            sd = MagicMock()
            sd.mech_only_queue = ["market_2", "market_3"]
            mock_sd.return_value = sd
            payload = _run_async_act(behaviour)
        assert json.loads(payload.mech_only_queue) == ["market_2", "market_3"]

    def test_reasserts_empty_queue(self) -> None:
        """The payload carries an empty queue when exhausted."""
        behaviour = _make_behaviour(MechOnlyReceiveBehaviour)
        with patch.object(
            type(behaviour), "synchronized_data", new_callable=PropertyMock
        ) as mock_sd:
            sd = MagicMock()
            sd.mech_only_queue = []
            mock_sd.return_value = sd
            payload = _run_async_act(behaviour)
        assert json.loads(payload.mech_only_queue) == []


# ---------------------------------------------------------------------------
# MechOnlySelectionBehaviour
# ---------------------------------------------------------------------------


class TestMechOnlySelectionIsOpenForMech:
    """Tests for the is_open_for_mech eligibility predicate."""

    def _make_selection_behaviour(self):  # type: ignore[no-untyped-def]
        """Return a MechOnlySelectionBehaviour with mocked params."""
        behaviour = _make_behaviour(MechOnlySelectionBehaviour)
        behaviour.context.params.opening_margin = 100
        behaviour.context.params.safe_voting_range = 3600
        return behaviour

    def test_eligible_binary_omen_market(self) -> None:
        """A binary Omen market within the safe voting range is eligible."""
        behaviour = self._make_selection_behaviour()
        bet = _mock_bet()
        # now is well before openingTimestamp - margins
        assert behaviour.is_open_for_mech(bet, now=10_000_000_000 - 100_000) is True

    def test_non_binary_rejected(self) -> None:
        """A non-binary market is rejected."""
        behaviour = self._make_selection_behaviour()
        bet = _mock_bet(outcome_slot_count=3)
        assert behaviour.is_open_for_mech(bet, now=0) is False

    def test_polymarket_rejected(self) -> None:
        """A Polymarket market is rejected (Omen-only scope)."""
        behaviour = self._make_selection_behaviour()
        bet = _mock_bet(market="polymarket_client")
        assert behaviour.is_open_for_mech(bet, now=0) is False

    def test_no_title_rejected(self) -> None:
        """A market without a title is rejected."""
        behaviour = self._make_selection_behaviour()
        bet = _mock_bet(title="")
        assert behaviour.is_open_for_mech(bet, now=0) is False

    def test_outside_safe_range_rejected(self) -> None:
        """A market whose opening is too close to now is rejected."""
        behaviour = self._make_selection_behaviour()
        bet = _mock_bet(opening_timestamp=5_000)
        # now is after openingTimestamp - margins -> not within safe range
        assert behaviour.is_open_for_mech(bet, now=5_000) is False


class TestMechOnlySelectionBehaviourAsyncAct:
    """Tests for MechOnlySelectionBehaviour.async_act queue and batch logic."""

    def test_matching_round(self) -> None:
        """The behaviour is bound to MechOnlySelectionRound."""
        assert MechOnlySelectionBehaviour.matching_round is MechOnlySelectionRound

    def test_builds_queue_and_batch_from_open_markets(self) -> None:
        """A fresh queue is built from open markets and the first batch is consumed."""
        behaviour = _make_behaviour(MechOnlySelectionBehaviour)
        behaviour.context.params.opening_margin = 100
        behaviour.context.params.safe_voting_range = 3600
        behaviour.context.params.multisend_batch_size = 1
        behaviour.context.params.max_mech_requests_per_cycle = 10
        behaviour.context.params.prompt_template.substitute.return_value = "prompt"

        bets = [
            _mock_bet(bet_id="m_b"),
            _mock_bet(bet_id="m_a"),
            _mock_bet(bet_id="m_c"),
        ]

        behaviour.read_bets = MagicMock()  # type: ignore[method-assign]
        behaviour.bets = bets

        with patch.object(
            type(behaviour), "synchronized_data", new_callable=PropertyMock
        ) as mock_sd, patch.object(
            type(behaviour), "synced_timestamp", new_callable=PropertyMock
        ) as mock_ts:
            sd = MagicMock()
            sd.mech_only_queue = []
            sd.mech_tool = "tool1"
            mock_sd.return_value = sd
            mock_ts.return_value = 10_000_000_000 - 100_000

            payload = _run_async_act(behaviour)

        # the queue is built sorted by id: [m_a, m_b, m_c]
        # batch_size=1 -> first batch is [m_a], remaining is [m_b, m_c]
        remaining = json.loads(payload.mech_only_queue)
        assert remaining == ["m_b", "m_c"]
        # mech_requests is a non-null JSON list with one entry
        assert payload.mech_requests is not None
        requests = json.loads(payload.mech_requests)
        assert len(requests) == 1
        assert requests[0]["prompt"] == "prompt"
        assert requests[0]["tool"] == "tool1"

    def test_uses_persisted_queue_when_non_empty(self) -> None:
        """When a persisted queue exists, it is used instead of rebuilding."""
        behaviour = _make_behaviour(MechOnlySelectionBehaviour)
        behaviour.context.params.opening_margin = 100
        behaviour.context.params.safe_voting_range = 3600
        behaviour.context.params.multisend_batch_size = 1
        behaviour.context.params.max_mech_requests_per_cycle = 10
        behaviour.context.params.prompt_template.substitute.return_value = "prompt"

        bets = [_mock_bet(bet_id="m_x")]
        behaviour.read_bets = MagicMock()  # type: ignore[method-assign]
        behaviour.bets = bets

        with patch.object(
            type(behaviour), "synchronized_data", new_callable=PropertyMock
        ) as mock_sd, patch.object(
            type(behaviour), "synced_timestamp", new_callable=PropertyMock
        ) as mock_ts:
            sd = MagicMock()
            sd.mech_only_queue = ["m_x"]
            sd.mech_tool = "tool1"
            mock_sd.return_value = sd
            mock_ts.return_value = 0

            payload = _run_async_act(behaviour)

        # the persisted queue [m_x] is consumed; remaining is empty
        remaining = json.loads(payload.mech_only_queue)
        assert remaining == []
        assert payload.mech_requests is not None
        requests = json.loads(payload.mech_requests)
        assert len(requests) == 1

    def test_no_open_markets_produces_empty_requests(self) -> None:
        """When there are no open markets, mech_requests is None and NO_MARKETS is implied."""
        behaviour = _make_behaviour(MechOnlySelectionBehaviour)
        behaviour.context.params.opening_margin = 100
        behaviour.context.params.safe_voting_range = 3600
        behaviour.context.params.multisend_batch_size = 1
        behaviour.context.params.max_mech_requests_per_cycle = 10

        behaviour.read_bets = MagicMock()  # type: ignore[method-assign]
        behaviour.bets = []

        with patch.object(
            type(behaviour), "synchronized_data", new_callable=PropertyMock
        ) as mock_sd, patch.object(
            type(behaviour), "synced_timestamp", new_callable=PropertyMock
        ) as mock_ts:
            sd = MagicMock()
            sd.mech_only_queue = []
            sd.mech_tool = "tool1"
            mock_sd.return_value = sd
            mock_ts.return_value = 0

            payload = _run_async_act(behaviour)

        assert payload.mech_requests is None

    def test_dead_batch_dropped_not_retained(self) -> None:
        """When batch markets have disappeared, the batch is dropped to avoid livelock."""
        behaviour = _make_behaviour(MechOnlySelectionBehaviour)
        behaviour.context.params.opening_margin = 100
        behaviour.context.params.safe_voting_range = 3600
        behaviour.context.params.multisend_batch_size = 1
        behaviour.context.params.max_mech_requests_per_cycle = 10
        behaviour.context.params.prompt_template.substitute.return_value = "prompt"

        # bets list does NOT contain "m_gone" — the market was resolved
        behaviour.read_bets = MagicMock()  # type: ignore[method-assign]
        behaviour.bets = [_mock_bet(bet_id="m_alive")]

        with patch.object(
            type(behaviour), "synchronized_data", new_callable=PropertyMock
        ) as mock_sd, patch.object(
            type(behaviour), "synced_timestamp", new_callable=PropertyMock
        ) as mock_ts:
            sd = MagicMock()
            sd.mech_only_queue = ["m_gone", "m_alive"]
            sd.mech_tool = "tool1"
            mock_sd.return_value = sd
            mock_ts.return_value = 0

            payload = _run_async_act(behaviour)

        # "m_gone" was at the front of the queue but couldn't be resolved;
        # it must be dropped, leaving ["m_alive"] — not retained as ["m_gone", "m_alive"]
        remaining = json.loads(payload.mech_only_queue)
        assert remaining == ["m_alive"]
        assert payload.mech_requests is None
