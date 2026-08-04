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
        mech_only = (
            max_trades > 0
            and self.synchronized_data.successful_trade_count >= max_trades
        )
        payload = TradeCapPayload(self.context.agent_address, mech_only)
        yield from self.finish_behaviour(payload)
