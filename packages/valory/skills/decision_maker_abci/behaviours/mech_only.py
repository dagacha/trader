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
from typing import Any, Dict, Generator, List, Optional
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
        """
        if bet.outcomeSlotCount != BINARY_N_SLOTS:
            return False
        if bet.market != OMEN_SUBGRAPH:
            return False
        if not bet.title or bet.yes is None or bet.no is None:
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
        open_bets = [
            bet for bet in self.bets if self.is_open_for_mech(bet, now)
        ]
        open_bets.sort(key=lambda bet: bet.id)
        max_requests = self.params.max_mech_requests_per_cycle
        if max_requests > 0:
            open_bets = open_bets[:max_requests]
        return [bet.id for bet in open_bets]

    def _build_metadata(self, market_ids: List[str]) -> List[MechMetadata]:
        """Build Mech request metadata for the given market ids."""
        bets_by_id: Dict[str, Bet] = {bet.id: bet for bet in self.bets}
        tool = self.synchronized_data.mech_tool
        metadata: List[MechMetadata] = []
        for market_id in market_ids:
            bet = bets_by_id.get(market_id)
            if bet is None:
                # the market may have been resolved between periods; skip it
                continue
            prompt_params = dict(
                question=bet.title, yes=bet.yes, no=bet.no
            )
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

            batch_size = max(1, self.params.multisend_batch_size)
            batch = queue[:batch_size]
            remaining = queue[batch_size:]

            metadata = self._build_metadata(batch)
            if metadata:
                serialized_requests = json.dumps(
                    [asdict(m) for m in metadata], sort_keys=True
                )
            else:
                serialized_requests = None
                remaining = queue  # nothing consumed; keep the queue intact

            serialized_queue = json.dumps(remaining)

            payload = MechOnlySelectionPayload(
                self.context.agent_address,
                serialized_requests,
                serialized_queue,
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
        """Submit the deterministic routing vote based on the persisted cap flag."""
        payload = RedeemRouterPayload(
            sender=self.context.agent_address,
            vote=self.synchronized_data.mech_only_mode,
        )
        yield from self.finish_behaviour(payload)
