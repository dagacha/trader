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

"""Read raw Omen events as protocol-safe JSON values."""

from typing import Any, Dict, cast

from aea.common import JSONLike
from aea.configurations.base import PublicId
from aea.contracts.base import Contract
from aea.crypto.base import LedgerApi
from aea_ledger_ethereum import EthereumApi

PUBLIC_ID = PublicId.from_str("valory/omen_event_reader:0.1.0")


def _hex(value: Any) -> str:
    """Convert a Web3 byte field to a lower-case hex string."""
    return value.lower() if isinstance(value, str) else "0x" + bytes(value).hex()


class OmenEventReaderContract(Contract):
    """Read factory and proposer events without Web3 container types."""

    contract_id = PUBLIC_ID

    @classmethod
    def get_logs(
        cls,
        ledger_api: LedgerApi,
        contract_address: str,
        filter_params: Dict[str, Any],
    ) -> JSONLike:
        """Read events and return only the fields used by the lightning poller.

        :param ledger_api: configured chain ledger.
        :param contract_address: package dispatch address; filters set each source.
        :param filter_params: Web3 log filter, including address and topic lists.
        :return: events made of plain dictionaries, strings, integers and booleans.
        """
        logs = cast(EthereumApi, ledger_api).api.eth.get_logs(filter_params)
        return {
            "logs": [
                {
                    "address": str(log["address"]).lower(),
                    "data": _hex(log["data"]),
                    "topics": [_hex(topic) for topic in log["topics"]],
                    "transactionHash": _hex(log["transactionHash"]),
                    "blockNumber": int(log["blockNumber"]),
                    "logIndex": int(log["logIndex"]),
                    "removed": bool(log.get("removed", False)),
                }
                for log in logs
            ]
        }
