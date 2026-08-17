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

"""Behaviours for the post-cap Mech-only analysis flow.

These behaviours implement a dedicated, bounded Mech-request/response loop that
runs **only** after ``MAX_TRADES`` has been reached.  The loop never opens a
position: it does not set a vote, bet amount, or profitability flag, and it does
not mutate bet-queue bookkeeping.  Open markets are analyzed in deterministic
batches and the cycle routes to the checkpoint when the queue is exhausted.
"""

import json
from dataclasses import asdict
from typing import Any, Dict, Generator, List, Optional, Set
from uuid import uuid4

from packages.valory.skills.decision_maker_abci.behaviours.base import (
    DecisionMakerBaseBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import (
    MechOnlyReceivePayload,
    MechOnlySelectionPayload,
    RedeemRouterPayload,
)
from packages.valory.skills.decision_maker_abci.states.mech_only import (
    MechOnlyReceiveRound,
    MechOnlySelectionRound,
    MechResponseRouterRound,
)
from packages.valory.skills.market_manager_abci.bets import BINARY_N_SLOTS, Bet
from packages.valory.skills.mech_interact_abci.states.base import MechMetadata

OMEN_SUBGRAPH = "omen_subgraph"


class MechOnlySelectionBehaviour(DecisionMakerBaseBehaviour):
    """Build a Mech request batch for the next open markets in the post-cap queue."""

    matching_round = MechOnlySelectionRound

    def is_open_for_mech(self, bet: Bet, now: int) -> bool:
        """Whether a market is eligible for post-cap Mech analysis.

        This predicate is deliberately narrower than
        ``SamplingBehaviour.processable_bet``: it only requires a binary Omen
        market that is still within the safe voting window and has the fields
        needed to build a prompt.  It intentionally excludes trading-specific
        conditions (liquidity, invested amounts, queue status, multi-bet mode).

        :param bet: the candidate market bet.
        :param now: the current timestamp, in seconds.
        :return: ``True`` if the market is eligible for post-cap Mech analysis.
        """
        if bet.outcomeSlotCount != BINARY_N_SLOTS:
            return False
        if bet.market != OMEN_SUBGRAPH:
            return False
        # ``bet.yes`` and ``bet.no`` raise ``ValueError`` when ``bet.outcomes``
        # is ``None`` (e.g. blacklisted bets via ``blacklist_forever``).  Check
        # ``outcomes`` directly instead of catching the property access.
        if not bet.title or bet.outcomes is None:
            return False
        within_safe_range = (
            now
            < bet.openingTimestamp
            - self.params.opening_margin
            - self.params.safe_voting_range
        )
        return within_safe_range

    def _build_queue(self) -> List[str]:
        """Build the deterministic post-cap queue of open market ids."""
        now = self.synced_timestamp
        open_bets = [bet for bet in self.bets if self.is_open_for_mech(bet, now)]
        open_bets.sort(key=lambda bet: bet.id)
        max_requests = self.params.max_mech_requests_per_cycle
        if max_requests > 0:
            open_bets = open_bets[:max_requests]
        return [bet.id for bet in open_bets]

    def _select_mech_tool(self) -> Optional[str]:
        """Return a Mech tool to use for post-cap analysis.

        ``MechOnlySelectionRound`` is only reached after the trade cap fires,
        and the FSM gets there without passing through ``ToolSelectionRound``.
        Consequently ``mech_tool`` is not set for the period -- and it is
        *always* unset on a fresh agent restart, where the period counter is
        back at 0.  Reading it via ``get_strict`` therefore raises.

        Pick a tool that is actually on offer.  Prefer the e-greedy policy's
        best tool when a policy is available and still offers one of the
        candidates; otherwise fall back to a stable, deterministic tool so
        the request set is reproducible.

        The tool selection is deliberately **not** gated on the policy being
        set: ``policy`` is only written to synchronized data when the redeem
        flow finds redeemable winnings, so on an empty redeem cycle ("No
        winnings to redeem") there is no persisted policy to consult and a
        tool must still be chosen.  Candidate tools come from the curated
        set (``available_mech_tools``) and the tools actually served by the
        fetched mechs (``mech_tools``), which ``MechInformationRound``
        refreshes every period.  Returns ``None`` only when no tool at all is
        on offer.

        :return: a Mech tool id, or ``None`` when no tool is on offer.
        """
        if self.synchronized_data.has_tool_selection_run:
            return self.synchronized_data.mech_tool

        def safe_tools(accessor: Any) -> Set[str]:  # pragma: no cover
            """Resolve a tool-set accessor, tolerating an unset synced key."""
            try:
                return set(accessor())
            except (TypeError, ValueError):
                return set()

        available = safe_tools(lambda: self.synchronized_data.available_mech_tools)
        served = safe_tools(lambda: self.synchronized_data.mech_tools)

        # Prefer tools both curated and served; otherwise prefer the served
        # set (guarantees the chosen tool has at least one mech to route to),
        # falling back to the curated set when the registry is empty.
        candidates = (available & served) or served or available
        if not candidates:
            return None

        policy = None
        if self.synchronized_data.is_policy_set:
            try:
                policy = self.synchronized_data.policy
            except ValueError:
                policy = None

        if policy is not None and policy.weighted_accuracy:
            best = policy.best_tool
            if best is not None and best in candidates:
                return best

        # No usable policy signal, or the best tool is no longer offered;
        # pick a stable, deterministic tool so the request set is reproducible.
        return sorted(candidates)[0]

    def _build_metadata(self, market_ids: List[str], tool: str) -> List[MechMetadata]:
        """Build Mech request metadata for the given market ids using ``tool``."""
        bets_by_id: Dict[str, Bet] = {bet.id: bet for bet in self.bets}
        self.context.logger.info(f"Mech-only analysis using tool {tool!r}.")
        metadata: List[MechMetadata] = []
        for market_id in market_ids:
            bet = bets_by_id.get(market_id)
            if bet is None:
                # the market may have been resolved between periods; skip it
                continue
            if bet.outcomes is None:
                # the market may have been blacklisted between periods; skip
                continue
            prompt_params = dict(question=bet.title, yes=bet.yes, no=bet.no)
            prompt = self.params.prompt_template.substitute(prompt_params)
            nonce = str(uuid4())
            request_context = bet.to_request_context()
            metadata.append(
                MechMetadata(prompt, tool, nonce, request_context=request_context)
            )
        return metadata

    def async_act(self) -> Generator:
        """Select the next batch and prepare the Mech request payload."""
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            self.read_bets()

            queue = self.synchronized_data.mech_only_queue
            if not queue:
                # first entry into the capped flow this period, or the
                # persisted queue from a previous period was exhausted
                queue = self._build_queue()

            # ``multisend_batch_size`` is the correct batch size here because
            # ``MechRequestBehaviour`` processes at most that many requests per
            # multisend tx.  Putting more in ``mech_requests`` would cause the
            # excess to be dropped.  ``max_mech_requests_per_cycle`` caps the
            # *total* per cycle (via the queue length), not the per-tx batch.
            batch_size = max(1, self.params.multisend_batch_size)
            batch = queue[:batch_size]
            remaining = queue[batch_size:]

            tool = self._select_mech_tool()
            if tool is None:
                # No tool could be determined (tool selection has not run and
                # no policy/available tools are set yet). Emit an empty batch
                # so ``MechOnlySelectionRound`` routes to ``NO_MARKETS`` and
                # the flow skips the Mech request (which requires ``mech_tool``).
                self.context.logger.warning(
                    "Mech-only selection could not determine a Mech tool "
                    "(tool selection has not run and no policy/available tools "
                    "are set); skipping this batch."
                )
                metadata: List[MechMetadata] = []
            else:
                metadata = self._build_metadata(batch, tool)
            if metadata:
                serialized_requests = json.dumps(
                    [asdict(m) for m in metadata], sort_keys=True
                )
            else:
                # The batch was non-empty but every market has disappeared
                # (resolved between periods).  Drop the dead batch so the
                # queue drains instead of livelocking on unrecoverable ids.
                serialized_requests = None

            serialized_queue = json.dumps(remaining)

            payload = MechOnlySelectionPayload(
                self.context.agent_address,
                serialized_requests,
                serialized_queue,
                tool,
            )
        yield from self.finish_behaviour(payload)


class MechOnlyReceiveBehaviour(DecisionMakerBaseBehaviour):
    """Consume a capped Mech delivery and re-assert the remaining queue."""

    matching_round = MechOnlyReceiveRound

    def async_act(self) -> Generator:
        """Acknowledge the delivery without setting any vote or bet field."""
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            # The queue was already advanced by the selection round.  We only
            # re-assert it here so consensus is reached on the current cursor.
            # Tool-health / delivery tracking may be added here without affecting
            # betting policy or market queue state.
            queue = self.synchronized_data.mech_only_queue
            serialized_queue = json.dumps(queue)

            payload = MechOnlyReceivePayload(
                self.context.agent_address,
                serialized_queue,
            )
        yield from self.finish_behaviour(payload)


class MechResponseRouterBehaviour(DecisionMakerBaseBehaviour):
    """Vote on whether the Mech delivery belongs to the capped or normal path."""

    matching_round = MechResponseRouterRound

    def async_act(self) -> Generator:
        """Submit a positive consensus vote; the routing decision is made in end_block.

        All agents vote ``True`` so that ``VotingRound`` reaches the positive
        threshold and emits ``done_event`` (``Event.DONE``).  The actual
        capped-vs-normal branch is then decided in
        ``MechResponseRouterRound.end_block`` based on the consensus-confirmed
        ``mech_only_mode`` flag — the vote itself does not carry the routing
        decision.  This mirrors ``RedeemRouterBehaviour`` which also always
        votes ``True`` and lets ``end_block`` do the routing.
        """
        payload = RedeemRouterPayload(
            sender=self.context.agent_address,
            vote=True,
        )
        yield from self.finish_behaviour(payload)
