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

"""Reuse the trader handlers, including the housekeeping UI."""

from packages.valory.skills.trader_abci.handlers import AcnHandler as BaseAcnHandler
from packages.valory.skills.trader_abci.handlers import (
    ContractApiHandler as BaseContractApiHandler,
)
from packages.valory.skills.trader_abci.handlers import HttpHandler as BaseHttpHandler
from packages.valory.skills.trader_abci.handlers import IpfsHandler as BaseIpfsHandler
from packages.valory.skills.trader_abci.handlers import (
    LedgerApiHandler as BaseLedgerApiHandler,
)
from packages.valory.skills.trader_abci.handlers import (
    SigningHandler as BaseSigningHandler,
)
from packages.valory.skills.trader_abci.handlers import SrrHandler as BaseSrrHandler
from packages.valory.skills.trader_abci.handlers import (
    TendermintHandler as BaseTendermintHandler,
)
from packages.valory.skills.trader_abci.handlers import (
    TraderHandler as BaseTraderHandler,
)

AcnHandler = BaseAcnHandler
ContractApiHandler = BaseContractApiHandler
HttpHandler = BaseHttpHandler
IpfsHandler = BaseIpfsHandler
LedgerApiHandler = BaseLedgerApiHandler
SigningHandler = BaseSigningHandler
SrrHandler = BaseSrrHandler
TendermintHandler = BaseTendermintHandler
TraderHandler = BaseTraderHandler
