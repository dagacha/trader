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
        """Test that MechOnlySelectionPayload does not contain any betting field."""
        payload_fields = {f.name for f in fields(MechOnlySelectionPayload)}
        assert not (payload_fields & NO_BET_FIELDS)

    def test_receive_payload_has_no_bet_fields(self) -> None:
        """Test that MechOnlyReceivePayload does not contain any betting field."""
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

    def test_outcomes_none_rejected_without_raising(self) -> None:
        """A market with outcomes=None (e.g. blacklisted) is rejected without raising.

        The real ``Bet.yes`` and ``Bet.no`` properties raise ``ValueError`` when
        ``outcomes`` is ``None`` (set by ``blacklist_forever``).  The guard
        checks ``bet.outcomes`` directly to avoid triggering that exception.
        """
        behaviour = self._make_selection_behaviour()
        bet = MagicMock()
        bet.id = "market_none"
        bet.market = "omen_subgraph"
        bet.outcomeSlotCount = BINARY_N_SLOTS
        bet.title = "Will it rain?"
        bet.outcomes = None
        bet.openingTimestamp = 10_000_000_000
        # Simulate the real Bet.yes / Bet.no: they raise ValueError when
        # outcomes is None (via get_outcome -> raise ValueError).
        type(bet).yes = PropertyMock(  # type: ignore[method-assign]
            side_effect=ValueError("outcomes is None")
        )
        type(bet).no = PropertyMock(  # type: ignore[method-assign]
            side_effect=ValueError("outcomes is None")
        )
        # Must return False, not raise ValueError
        assert behaviour.is_open_for_mech(bet, now=0) is False


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

        with (
            patch.object(
                type(behaviour), "synchronized_data", new_callable=PropertyMock
            ) as mock_sd,
            patch.object(
                type(behaviour), "synced_timestamp", new_callable=PropertyMock
            ) as mock_ts,
        ):
            sd = MagicMock()
            sd.mech_only_queue = []
            sd.has_tool_selection_run = True
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

    def test_builds_queue_with_unbounded_requests(self) -> None:
        """max_mech_requests_per_cycle<=0 means no per-cycle cap on the queue."""
        behaviour = _make_behaviour(MechOnlySelectionBehaviour)
        behaviour.context.params.opening_margin = 100
        behaviour.context.params.safe_voting_range = 3600
        behaviour.context.params.multisend_batch_size = 1
        behaviour.context.params.max_mech_requests_per_cycle = 0
        behaviour.context.params.prompt_template.substitute.return_value = "prompt"

        behaviour.read_bets = MagicMock()  # type: ignore[method-assign]
        behaviour.bets = [_mock_bet(bet_id="m_a"), _mock_bet(bet_id="m_b")]

        with (
            patch.object(
                type(behaviour), "synchronized_data", new_callable=PropertyMock
            ) as mock_sd,
            patch.object(
                type(behaviour), "synced_timestamp", new_callable=PropertyMock
            ) as mock_ts,
        ):
            sd = MagicMock()
            sd.mech_only_queue = []
            sd.has_tool_selection_run = True
            sd.mech_tool = "tool1"
            mock_sd.return_value = sd
            mock_ts.return_value = 10_000_000_000 - 100_000

            payload = _run_async_act(behaviour)

        # both markets survive (no per-cycle cap); batch_size=1 consumes m_a,
        # leaving m_b queued.
        assert json.loads(payload.mech_only_queue) == ["m_b"]

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

        with (
            patch.object(
                type(behaviour), "synchronized_data", new_callable=PropertyMock
            ) as mock_sd,
            patch.object(
                type(behaviour), "synced_timestamp", new_callable=PropertyMock
            ) as mock_ts,
        ):
            sd = MagicMock()
            sd.mech_only_queue = ["m_x"]
            sd.has_tool_selection_run = True
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

        with (
            patch.object(
                type(behaviour), "synchronized_data", new_callable=PropertyMock
            ) as mock_sd,
            patch.object(
                type(behaviour), "synced_timestamp", new_callable=PropertyMock
            ) as mock_ts,
        ):
            sd = MagicMock()
            sd.mech_only_queue = []
            sd.has_tool_selection_run = True
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

        with (
            patch.object(
                type(behaviour), "synchronized_data", new_callable=PropertyMock
            ) as mock_sd,
            patch.object(
                type(behaviour), "synced_timestamp", new_callable=PropertyMock
            ) as mock_ts,
        ):
            sd = MagicMock()
            sd.mech_only_queue = ["m_gone", "m_alive"]
            sd.has_tool_selection_run = True
            sd.mech_tool = "tool1"
            mock_sd.return_value = sd
            mock_ts.return_value = 0

            payload = _run_async_act(behaviour)

        # "m_gone" was at the front of the queue but couldn't be resolved;
        # it must be dropped, leaving ["m_alive"] — not retained as ["m_gone", "m_alive"]
        remaining = json.loads(payload.mech_only_queue)
        assert remaining == ["m_alive"]
        assert payload.mech_requests is None

    def test_blacklisted_bet_in_queue_skipped(self) -> None:
        """A bet that was blacklisted (outcomes=None) between periods is skipped.

        The bet exists in ``self.bets`` (so it's not a dead market), but its
        ``outcomes`` were set to ``None`` by ``blacklist_forever``.  The
        ``_build_metadata`` method must skip it instead of crashing when
        accessing ``bet.yes`` / ``bet.no``.
        """
        behaviour = _make_behaviour(MechOnlySelectionBehaviour)
        behaviour.context.params.opening_margin = 100
        behaviour.context.params.safe_voting_range = 3600
        behaviour.context.params.multisend_batch_size = 1
        behaviour.context.params.max_mech_requests_per_cycle = 10
        behaviour.context.params.prompt_template.substitute.return_value = "prompt"

        # "m_blacklisted" exists in bets but has outcomes=None
        blacklisted = _mock_bet(bet_id="m_blacklisted")
        blacklisted.outcomes = None
        type(blacklisted).yes = PropertyMock(  # type: ignore[method-assign]
            side_effect=ValueError("outcomes is None")
        )
        type(blacklisted).no = PropertyMock(  # type: ignore[method-assign]
            side_effect=ValueError("outcomes is None")
        )
        alive = _mock_bet(bet_id="m_alive")
        behaviour.read_bets = MagicMock()  # type: ignore[method-assign]
        behaviour.bets = [blacklisted, alive]

        with (
            patch.object(
                type(behaviour), "synchronized_data", new_callable=PropertyMock
            ) as mock_sd,
            patch.object(
                type(behaviour), "synced_timestamp", new_callable=PropertyMock
            ) as mock_ts,
        ):
            sd = MagicMock()
            sd.mech_only_queue = ["m_blacklisted", "m_alive"]
            sd.has_tool_selection_run = True
            sd.mech_tool = "tool1"
            mock_sd.return_value = sd
            mock_ts.return_value = 0

            payload = _run_async_act(behaviour)

        # batch_size=1 -> batch is ["m_blacklisted"], remaining is ["m_alive"]
        # "m_blacklisted" is skipped (outcomes=None), so metadata is empty ->
        # the dead batch is dropped (mech_requests=None), same as the
        # resolved-market case.
        remaining = json.loads(payload.mech_only_queue)
        assert remaining == ["m_alive"]
        assert payload.mech_requests is None


class TestMechOnlySelectionToolFallback:
    """Tests for tool selection when ToolSelectionRound was bypassed.

    In mech-only mode the FSM reaches ``MechOnlySelectionRound`` without
    passing through ``ToolSelectionRound``, so ``mech_tool`` is unset (always
    the case right after a restart, at period 0).  Reading it strictly used to
    raise ``ValueError`` and crash the agent.  These tests cover the policy
    fallback that keeps the capped flow running.
    """

    def _make_selection_behaviour(self, sd):  # type: ignore[no-untyped-def]
        """Return a selection behaviour wired to build a single-market batch."""
        behaviour = _make_behaviour(MechOnlySelectionBehaviour)
        behaviour.context.params.opening_margin = 100
        behaviour.context.params.safe_voting_range = 3600
        behaviour.context.params.multisend_batch_size = 1
        behaviour.context.params.max_mech_requests_per_cycle = 10
        behaviour.context.params.prompt_template.substitute.return_value = "prompt"
        behaviour.read_bets = MagicMock()  # type: ignore[method-assign]
        behaviour.bets = [_mock_bet(bet_id="m_a")]
        return behaviour

    def _run(self, sd):  # type: ignore[no-untyped-def]
        """Drive async_act with the given synchronized_data mock."""
        behaviour = self._make_selection_behaviour(sd)
        with (
            patch.object(
                type(behaviour), "synchronized_data", new_callable=PropertyMock
            ) as mock_sd,
            patch.object(
                type(behaviour), "synced_timestamp", new_callable=PropertyMock
            ) as mock_ts,
        ):
            mock_sd.return_value = sd
            mock_ts.return_value = 0
            return _run_async_act(behaviour)

    def test_uses_policy_best_tool_when_selection_not_run(self) -> None:
        """When mech_tool is unset, the policy's best available tool is used."""
        policy = MagicMock()
        policy.weighted_accuracy = {"toolA": 0.1, "toolB": 0.9}
        policy.best_tool = "toolB"
        sd = MagicMock()
        sd.mech_only_queue = ["m_a"]
        sd.has_tool_selection_run = False
        sd.is_policy_set = True
        sd.available_mech_tools = {"toolA", "toolB"}
        sd.policy = policy

        payload = self._run(sd)

        requests = json.loads(payload.mech_requests)
        assert len(requests) == 1
        assert requests[0]["tool"] == "toolB"
        # The chosen tool must be persisted so mech_interact's strict read works.
        assert payload.mech_tool == "toolB"

    def test_falls_back_to_deterministic_tool_when_best_unavailable(self) -> None:
        """If the best tool is no longer offered, pick the first available one."""
        policy = MagicMock()
        policy.weighted_accuracy = {"gone": 0.9}
        policy.best_tool = "gone"
        sd = MagicMock()
        sd.mech_only_queue = ["m_a"]
        sd.has_tool_selection_run = False
        sd.is_policy_set = True
        sd.available_mech_tools = {"z_tool", "a_tool"}
        sd.policy = policy

        payload = self._run(sd)

        requests = json.loads(payload.mech_requests)
        assert requests[0]["tool"] == "a_tool"
        assert payload.mech_tool == "a_tool"

    def test_empty_weighted_accuracy_uses_deterministic_tool(self) -> None:
        """When the policy has no weighted accuracy yet, pick a deterministic tool."""
        policy = MagicMock()
        policy.weighted_accuracy = {}
        sd = MagicMock()
        sd.mech_only_queue = ["m_a"]
        sd.has_tool_selection_run = False
        sd.is_policy_set = True
        sd.available_mech_tools = {"z_tool", "a_tool"}
        sd.policy = policy

        payload = self._run(sd)

        requests = json.loads(payload.mech_requests)
        assert requests[0]["tool"] == "a_tool"

    def test_no_policy_drops_batch(self) -> None:
        """With no tool_selection and no policy, the batch is dropped (no crash)."""
        sd = MagicMock()
        sd.mech_only_queue = ["m_a"]
        sd.has_tool_selection_run = False
        sd.is_policy_set = False

        payload = self._run(sd)

        assert payload.mech_requests is None

    def test_empty_available_tools_drops_batch(self) -> None:
        """With a policy set but no available tools, the batch is dropped."""
        sd = MagicMock()
        sd.mech_only_queue = ["m_a"]
        sd.has_tool_selection_run = False
        sd.is_policy_set = True
        sd.available_mech_tools = set()
        sd.policy = MagicMock()

        payload = self._run(sd)

        assert payload.mech_requests is None

    def test_available_tools_lookup_raises_drops_batch(self) -> None:
        """If reading available_mech_tools/policy raises, the batch is dropped."""

        # A dedicated MagicMock subclass so the raising PropertyMock is scoped
        # to this instance's type and does not leak into other MagicMocks.
        class _RaisingSD(MagicMock):
            pass

        sd = _RaisingSD()
        sd.mech_only_queue = ["m_a"]
        sd.has_tool_selection_run = False
        sd.is_policy_set = True
        type(sd).available_mech_tools = PropertyMock(  # type: ignore[assignment]
            side_effect=ValueError("not set")
        )

        payload = self._run(sd)

        assert payload.mech_requests is None

    def test_picks_served_tool_when_no_policy_set(self) -> None:
        """Mech-only picks a served tool even when no policy is persisted.

        ``policy`` is only written to synchronized data when the redeem flow
        finds redeemable winnings; on a fresh/empty redeem cycle ("No winnings
        to redeem") it is absent, so selection must fall back to the tools
        actually served by the fetched mechs instead of bailing out.
        """
        sd = MagicMock()
        sd.mech_only_queue = ["m_a"]
        sd.has_tool_selection_run = False
        sd.is_policy_set = False
        sd.available_mech_tools = set()
        sd.mech_tools = {"factual_research", "superforcaster"}

        payload = self._run(sd)

        requests = json.loads(payload.mech_requests)
        assert len(requests) == 1
        # deterministic pick among the served tools
        assert requests[0]["tool"] == "factual_research"
        assert payload.mech_tool == "factual_research"

    def test_prefers_served_tool_over_unserved_curated(self) -> None:
        """A curated tool no mech serves must not win over a served one."""
        sd = MagicMock()
        sd.mech_only_queue = ["m_a"]
        sd.has_tool_selection_run = False
        sd.is_policy_set = False
        sd.available_mech_tools = {"only_curated_no_mech"}
        sd.mech_tools = {"superforcaster"}

        payload = self._run(sd)

        requests = json.loads(payload.mech_requests)
        assert requests[0]["tool"] == "superforcaster"
        assert payload.mech_tool == "superforcaster"
