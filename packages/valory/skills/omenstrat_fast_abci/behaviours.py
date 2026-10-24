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

from typing import List

from packages.valory.skills.fresh_watch_abci.behaviours import (
    FreshMarketPoller,
    FreshWatchRoundBehaviour,
)
from packages.valory.skills.mech_interact_abci.behaviours.mech_info import (
    MechInformationBehaviour,
)
from packages.valory.skills.omenstrat_fast_abci.composition import (
    FastPreTxSettlementRound,
    OmenstratFastAbciApp,
)
from packages.valory.skills.termination_abci.behaviours import TerminationAbciBehaviours
from packages.valory.skills.trader_abci.behaviours import TraderConsensusBehaviour
from packages.valory.skills.transaction_settlement_abci.behaviours import (
    ValidateTransactionBehaviour,
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


_REPLACED = {b.matching_round for b in FreshWatchRoundBehaviour.behaviours} | {
    FastMechInformationBehaviour.matching_round
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
        FastPreTxSettlementBehaviour,
        *TerminationAbciBehaviours.behaviours,
    }
    # Termination also supplies the upstream validator; keep the trader's
    # receipt check so a reverted transaction fails fast here too.
    behaviours.discard(ValidateTransactionBehaviour)

    def setup(self) -> None:
        """Set up the foreground FSM and its cooperative market poller."""
        super().setup()
        self.discovery = FreshMarketPoller(
            name="fresh_market_poller", skill_context=self.context
        )
        self.context.state.fast_background_running = True

    def _background_act(self) -> None:
        """Drive discovery even while a Mech request or settlement waits."""
        super()._background_act()
        self.discovery.act_wrapper()

    def teardown(self) -> None:
        """Stop the poller when the skill stops."""
        discovery = getattr(self, "discovery", None)
        if discovery is not None:
            discovery.stop()
        self.context.state.fast_background_running = False
        super().teardown()
