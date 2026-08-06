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

"""Behaviour selecting normal or capped trading mode."""

from typing import Generator

from packages.valory.skills.decision_maker_abci.behaviours.base import (
    DecisionMakerBaseBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import TradeCapPayload
from packages.valory.skills.decision_maker_abci.states.trade_cap import TradeCapRound


class TradeCapBehaviour(DecisionMakerBaseBehaviour):
    """Decide whether successful placements have reached the configured cap."""

    matching_round = TradeCapRound

    def async_act(self) -> Generator:
        """Submit the deterministic capped-mode decision."""
        max_trades = self.params.max_trades
        # Read from the durable file rather than synchronized data: the counter
        # must survive agent restarts, which wipe the ABCI database.
        successful_trade_count = self.durable_trade_count()
        mech_only = max_trades > 0 and successful_trade_count >= max_trades
        self.context.logger.info(
            f"Trade cap check: successful_trade_count={successful_trade_count}, "
            f"max_trades={max_trades}, mech_only={mech_only}"
        )
        payload = TradeCapPayload(self.context.agent_address, mech_only)
        yield from self.finish_behaviour(payload)
