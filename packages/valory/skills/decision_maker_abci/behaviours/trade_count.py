"""Behaviour recording successful Omen placements."""

from typing import Generator

from packages.valory.skills.decision_maker_abci.behaviours.base import DecisionMakerBaseBehaviour
from packages.valory.skills.decision_maker_abci.payloads import TradeCountPayload
from packages.valory.skills.decision_maker_abci.states.trade_count import TradeCountRound


class TradeCountBehaviour(DecisionMakerBaseBehaviour):
    """Advance the durable trade counter after a settled placement."""

    matching_round = TradeCountRound

    def async_act(self) -> Generator:
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            payload = TradeCountPayload(
                self.context.agent_address,
                self.synchronized_data.successful_trade_count + 1,
            )
        yield from self.finish_behaviour(payload)
