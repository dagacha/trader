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

"""Behaviour recording successful Omen placements."""

from typing import Generator

from packages.valory.skills.decision_maker_abci.behaviours.base import (
    DecisionMakerBaseBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCountPayload
from packages.valory.skills.decision_maker_abci.states.trade_count import (
    TradeCountRound,
)


class TradeCountBehaviour(DecisionMakerBaseBehaviour):
    """Advance the durable trade counter after a settled placement."""

    matching_round = TradeCountRound

    def async_act(self) -> Generator:
        """Increment and persist the durable trade counter after a settled placement."""
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            # Increment the durable file-backed counter so the count survives
            # agent restarts, which reset the ABCI database to its setup state.
            new_count = self.durable_trade_count() + 1
            self.store_trade_count(new_count)
            self.context.logger.info(
                f"Recorded successful Omen placement; "
                f"successful_trade_count is now {new_count}."
            )
            payload = TradeCountPayload(self.context.agent_address, new_count)
        yield from self.finish_behaviour(payload)
