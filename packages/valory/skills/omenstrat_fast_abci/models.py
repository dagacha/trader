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

"""Models for the composed fast Omen agent."""

from packages.valory.skills.fresh_watch_abci.models import (
    AccuracyInfoFields as BaseAccuracyInfoFields,
)
from packages.valory.skills.fresh_watch_abci.models import (
    AgentToolsSpecs as BaseAgentToolsSpecs,
)
from packages.valory.skills.fresh_watch_abci.models import (
    BenchmarkTool as BaseBenchmarkTool,
)
from packages.valory.skills.fresh_watch_abci.models import (
    BenchmarkingMode as BaseBenchmarkingMode,
)
from packages.valory.skills.fresh_watch_abci.models import (
    ConditionalTokensSubgraph as BaseConditionalTokensSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import FastParams as BaseFastParams
from packages.valory.skills.fresh_watch_abci.models import (
    GnosisStakingSubgraph as BaseGnosisStakingSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    MechResponseSpecs as BaseMechResponseSpecs,
)
from packages.valory.skills.fresh_watch_abci.models import (
    MechToolsSpecs as BaseMechToolsSpecs,
)
from packages.valory.skills.fresh_watch_abci.models import (
    MechsSubgraph as BaseMechsSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    NetworkSubgraph as BaseNetworkSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    OlasAgentsSubgraph as BaseOlasAgentsSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    OlasMechSubgraph as BaseOlasMechSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    OmenSubgraph as BaseOmenSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    OpenMarketsSubgraph as BaseOpenMarketsSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    PolygonMechSubgraph as BasePolygonMechSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    PolygonStakingSubgraph as BasePolygonStakingSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    PolymarketAgentsSubgraph as BasePolymarketAgentsSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    PolymarketBetsSubgraph as BasePolymarketBetsSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    PolymarketQuestionsSubgraph as BasePolymarketQuestionsSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import (
    RandomnessApi as BaseRandomnessApi,
)
from packages.valory.skills.fresh_watch_abci.models import (
    RealitioSubgraph as BaseRealitioSubgraph,
)
from packages.valory.skills.fresh_watch_abci.models import Requests as BaseRequests
from packages.valory.skills.fresh_watch_abci.models import (
    SharedState as FreshSharedState,
)
from packages.valory.skills.fresh_watch_abci.models import (
    TradesSubgraph as BaseTradesSubgraph,
)
from packages.valory.skills.fresh_watch_abci.rounds import Event
from packages.valory.skills.omenstrat_fast_abci.composition import OmenstratFastAbciApp
from packages.valory.skills.trader_abci.composition import TraderAbciApp

AccuracyInfoFields = BaseAccuracyInfoFields
AgentToolsSpecs = BaseAgentToolsSpecs
BenchmarkTool = BaseBenchmarkTool
BenchmarkingMode = BaseBenchmarkingMode
ConditionalTokensSubgraph = BaseConditionalTokensSubgraph
FastParams = BaseFastParams
GnosisStakingSubgraph = BaseGnosisStakingSubgraph
MechResponseSpecs = BaseMechResponseSpecs
MechToolsSpecs = BaseMechToolsSpecs
MechsSubgraph = BaseMechsSubgraph
NetworkSubgraph = BaseNetworkSubgraph
OlasAgentsSubgraph = BaseOlasAgentsSubgraph
OlasMechSubgraph = BaseOlasMechSubgraph
OmenSubgraph = BaseOmenSubgraph
OpenMarketsSubgraph = BaseOpenMarketsSubgraph
PolygonMechSubgraph = BasePolygonMechSubgraph
PolygonStakingSubgraph = BasePolygonStakingSubgraph
PolymarketAgentsSubgraph = BasePolymarketAgentsSubgraph
PolymarketBetsSubgraph = BasePolymarketBetsSubgraph
PolymarketQuestionsSubgraph = BasePolymarketQuestionsSubgraph
RandomnessApi = BaseRandomnessApi
RealitioSubgraph = BaseRealitioSubgraph
Requests = BaseRequests
TradesSubgraph = BaseTradesSubgraph


class SharedState(FreshSharedState):
    """Apply the existing timeout setup to this composed app."""

    abci_app_cls = OmenstratFastAbciApp

    def setup(self) -> None:
        """Set the fast app timeouts after the reused state setup."""
        super().setup()
        for event in OmenstratFastAbciApp.event_to_timeout:
            if event in TraderAbciApp.event_to_timeout:
                OmenstratFastAbciApp.event_to_timeout[event] = (
                    TraderAbciApp.event_to_timeout[event]
                )
        OmenstratFastAbciApp.event_to_timeout[Event.ROUND_TIMEOUT] = (
            self.context.params.round_timeout_seconds
        )
