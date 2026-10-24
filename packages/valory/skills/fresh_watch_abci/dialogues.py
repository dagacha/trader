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

"""Reuse the trader dialogues."""

from packages.valory.skills.trader_abci.dialogues import (
    AbciDialogues as BaseAbciDialogues,
)
from packages.valory.skills.trader_abci.dialogues import (
    AcnDialogues as BaseAcnDialogues,
)
from packages.valory.skills.trader_abci.dialogues import (
    ContractApiDialogues as BaseContractApiDialogues,
)
from packages.valory.skills.trader_abci.dialogues import (
    HttpDialogues as BaseHttpDialogues,
)
from packages.valory.skills.trader_abci.dialogues import (
    IpfsDialogues as BaseIpfsDialogues,
)
from packages.valory.skills.trader_abci.dialogues import (
    LedgerApiDialogues as BaseLedgerApiDialogues,
)
from packages.valory.skills.trader_abci.dialogues import (
    SigningDialogues as BaseSigningDialogues,
)
from packages.valory.skills.trader_abci.dialogues import (
    SrrDialogues as BaseSrrDialogues,
)
from packages.valory.skills.trader_abci.dialogues import (
    TendermintDialogues as BaseTendermintDialogues,
)

AbciDialogues = BaseAbciDialogues
AcnDialogues = BaseAcnDialogues
ContractApiDialogues = BaseContractApiDialogues
HttpDialogues = BaseHttpDialogues
IpfsDialogues = BaseIpfsDialogues
LedgerApiDialogues = BaseLedgerApiDialogues
SigningDialogues = BaseSigningDialogues
SrrDialogues = BaseSrrDialogues
TendermintDialogues = BaseTendermintDialogues
