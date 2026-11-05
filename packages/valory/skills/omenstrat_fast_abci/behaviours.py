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

"""Behaviours for the composed fast Omen agent."""

from dataclasses import replace
from typing import Any, Generator, List, Optional, Tuple, cast

from aea.helpers.transaction.base import RawTransaction

from packages.valory.skills.abstract_round_abci.base import BaseTxPayload
from packages.valory.skills.abstract_round_abci.behaviour_utils import (
    RPCResponseStatus,
)
from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastMechRequestBehaviour,
    FastMechResponseBehaviour,
    FreshMarketPoller,
    FreshWatchRoundBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lane_behaviour import (
    LightningLaneBehaviour,
)
from packages.valory.skills.mech_interact_abci.behaviours.mech_info import (
    MechInformationBehaviour,
)
from packages.valory.skills.omenstrat_fast_abci.composition import (
    FastFinalizationRound,
    FastPreTxSettlementRound,
    OmenstratFastAbciApp,
)
from packages.valory.skills.termination_abci.behaviours import TerminationAbciBehaviours
from packages.valory.skills.trader_abci.behaviours import (
    TraderBackgroundBehaviour,
    TraderConsensusBehaviour,
)
from packages.valory.skills.transaction_settlement_abci.behaviours import (
    FinalizeBehaviour,
    ValidateTransactionBehaviour,
)
from packages.valory.skills.transaction_settlement_abci.payloads import (
    FinalizationTxPayload,
)
from packages.valory.skills.tx_settlement_multiplexer_abci.behaviours import (
    PreTxSettlementBehaviour,
)


class FastMechInformationBehaviour(MechInformationBehaviour):
    """Discover allowed mechs, or the priority mech in static mode."""

    @property
    def eligible_mechs(self) -> List[str]:
        """Use the shared allowlist when dynamic selection is enabled."""
        if self.params.mech_marketplace_config.use_dynamic_mech_selection:
            return super().eligible_mechs
        return [
            (self.params.mech_marketplace_config.priority_mech_address or "").lower()
        ]


class FastPreTxSettlementBehaviour(PreTxSettlementBehaviour):
    """Use the fast transaction entry round with the existing balance checks."""

    matching_round = FastPreTxSettlementRound


class FastFinalizeBehaviour(FinalizeBehaviour):
    """Use the fast finalization round with the existing sender logic."""

    matching_round = FastFinalizationRound

    def __init__(self, **kwargs: Any) -> None:
        """Start the attempt with nothing sent."""
        super().__init__(**kwargs)
        self._broadcast_attempted = False

    def send_raw_transaction(
        self, transaction: RawTransaction, *args: Any, **kwargs: Any
    ) -> Generator[Any, Any, Tuple[Optional[str], RPCResponseStatus]]:
        """Do not broadcast gas 0, because a node can not accept it.

        :param transaction: the raw transaction.
        :param args: the upstream positional arguments.
        :param kwargs: the upstream keyword arguments.
        :yield: signing and ledger messages.
        :return: the transaction hash and the response status.
        """
        if transaction.body.get("gas") == 0:
            self.context.logger.warning("Not sending a transaction with gas 0.")
            return None, RPCResponseStatus.UNCLASSIFIED_ERROR
        self._broadcast_attempted = True
        return (yield from super().send_raw_transaction(transaction, *args, **kwargs))

    def send_a2a_transaction(
        self, payload: BaseTxPayload, resetting: bool = False
    ) -> Generator:
        """Tell the round when this attempt is known as not sent.

        :param payload: the finalization payload.
        :param resetting: the upstream flag for a resetting round.
        :yield: consensus messages.
        """
        tx_data = cast(FinalizationTxPayload, payload).tx_data
        if tx_data is not None and not self._broadcast_attempted:
            payload = replace(
                cast(FinalizationTxPayload, payload),
                tx_data={**tx_data, "not_sent": True},
            )
        yield from super().send_a2a_transaction(payload, resetting)


_REPLACED = {b.matching_round for b in FreshWatchRoundBehaviour.behaviours} | {
    FastMechInformationBehaviour.matching_round,
    FastMechRequestBehaviour.matching_round,
    FastMechResponseBehaviour.matching_round,
}


class OmenstratFastConsensusBehaviour(TraderConsensusBehaviour):
    """Run only the behaviours used by the fast composition."""

    abci_app_cls = OmenstratFastAbciApp
    behaviours = {
        *[
            b
            for b in TraderConsensusBehaviour.behaviours
            if b.matching_round in OmenstratFastAbciApp.transition_function
            and b.matching_round not in _REPLACED
        ],
        *FreshWatchRoundBehaviour.behaviours,
        FastMechInformationBehaviour,
        FastMechRequestBehaviour,
        FastMechResponseBehaviour,
        FastPreTxSettlementBehaviour,
        FastFinalizeBehaviour,
        *TerminationAbciBehaviours.behaviours,
    }
    # Termination also supplies the upstream validator; keep the trader's
    # receipt check so a reverted transaction fails fast here too.
    behaviours.discard(ValidateTransactionBehaviour)
    behaviours = {
        behaviour
        for behaviour in behaviours
        if behaviour.matching_round is not TraderBackgroundBehaviour.matching_round
    } | {TraderBackgroundBehaviour}

    def setup(self) -> None:
        """Set up the foreground FSM and its cooperative market poller."""
        super().setup()
        self.discovery = FreshMarketPoller(
            name="fresh_market_poller", skill_context=self.context
        )
        self.lightning_lane: Optional[LightningLaneBehaviour] = None
        if (
            self.context.params.lightning.lightning_lane is True
            or (self.context.params.store_path / "lightning_lane_state.json").exists()
        ):
            self.lightning_lane = LightningLaneBehaviour(
                name="lightning_lane", skill_context=self.context
            )
        self.context.state.fast_background_running = True

    def _background_act(self) -> None:
        """Drive discovery even while a Mech request or settlement waits."""
        super()._background_act()
        self.discovery.act_wrapper()
        if self.lightning_lane is not None:
            self.lightning_lane.act_wrapper()

    def teardown(self) -> None:
        """Stop the poller when the skill stops."""
        discovery = getattr(self, "discovery", None)
        if discovery is not None:
            discovery.stop()
        lane = getattr(self, "lightning_lane", None)
        if lane is not None:
            lane.stop()
        self.context.state.fast_background_running = False
        super().teardown()
