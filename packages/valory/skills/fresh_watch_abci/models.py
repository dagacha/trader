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

"""Parameters and state for the fast Omen strategy."""

from dataclasses import fields
from typing import Any, Optional, Tuple, Type

from packages.valory.skills.abstract_round_abci.base import AbciApp
from packages.valory.skills.fresh_watch_abci.rounds import FreshWatchAbciApp
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig
from packages.valory.skills.trader_abci.models import (
    AccuracyInfoFields as BaseAccuracyInfoFields,
)
from packages.valory.skills.trader_abci.models import (
    AgentToolsSpecs as BaseAgentToolsSpecs,
)
from packages.valory.skills.trader_abci.models import BenchmarkTool as BaseBenchmarkTool
from packages.valory.skills.trader_abci.models import (
    BenchmarkingMode as BaseBenchmarkingMode,
)
from packages.valory.skills.trader_abci.models import (
    ConditionalTokensSubgraph as BaseConditionalTokensSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    GnosisStakingSubgraph as BaseGnosisStakingSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    MechResponseSpecs as BaseMechResponseSpecs,
)
from packages.valory.skills.trader_abci.models import (
    MechToolsSpecs as BaseMechToolsSpecs,
)
from packages.valory.skills.trader_abci.models import MechsSubgraph as BaseMechsSubgraph
from packages.valory.skills.trader_abci.models import (
    NetworkSubgraph as BaseNetworkSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    OlasAgentsSubgraph as BaseOlasAgentsSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    OlasMechSubgraph as BaseOlasMechSubgraph,
)
from packages.valory.skills.trader_abci.models import OmenSubgraph as BaseOmenSubgraph
from packages.valory.skills.trader_abci.models import (
    OpenMarketsSubgraph as BaseOpenMarketsSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    PolygonMechSubgraph as BasePolygonMechSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    PolygonStakingSubgraph as BasePolygonStakingSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    PolymarketAgentsSubgraph as BasePolymarketAgentsSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    PolymarketBetsSubgraph as BasePolymarketBetsSubgraph,
)
from packages.valory.skills.trader_abci.models import (
    PolymarketQuestionsSubgraph as BasePolymarketQuestionsSubgraph,
)
from packages.valory.skills.trader_abci.models import RandomnessApi as BaseRandomnessApi
from packages.valory.skills.trader_abci.models import (
    RealitioSubgraph as BaseRealitioSubgraph,
)
from packages.valory.skills.trader_abci.models import Requests as BaseRequests
from packages.valory.skills.trader_abci.models import SharedState as TraderSharedState
from packages.valory.skills.trader_abci.models import (
    TraderParams,
)
from packages.valory.skills.trader_abci.models import (
    TradesSubgraph as BaseTradesSubgraph,
)

AccuracyInfoFields = BaseAccuracyInfoFields
AgentToolsSpecs = BaseAgentToolsSpecs
BenchmarkTool = BaseBenchmarkTool
BenchmarkingMode = BaseBenchmarkingMode
ConditionalTokensSubgraph = BaseConditionalTokensSubgraph
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


class FastParams(TraderParams):
    """Declare all fast strategy controls alongside existing trader params."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Read the fast controls and initialize the reused models."""
        controls = {
            item.name: self._ensure(item.name, kwargs, item.type)
            for item in fields(FastConfig)
        }
        self.fast = FastConfig(**controls)
        if kwargs.get("is_running_on_polymarket"):
            raise ValueError("The fast agent supports Omen only.")
        if self.fast.tools != FastConfig().tools:
            raise ValueError("Use only the two fast tools in priority order.")
        dynamic = self._ensure("fast_dynamic_mech", kwargs, bool)
        kwargs["fast_dynamic_mech"] = dynamic
        kwargs["mech_marketplace_config"] = {
            **kwargs["mech_marketplace_config"],
            "use_dynamic_mech_selection": dynamic,
        }
        super().__init__(*args, **kwargs)


class SharedState(TraderSharedState):
    """State for the standalone fresh-watch app."""

    abci_app_cls: Type[AbciApp] = FreshWatchAbciApp

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the poll timestamp."""
        super().__init__(*args, **kwargs)
        self.fast_activity_status: Optional[
            Tuple[float, Optional[int], Tuple[bool, bool, int, int]]
        ] = None
        self.fast_last_poll = 0.0
        self.fast_background_running = False
        self.fast_polling = False
