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

"""Check event reads with the real Web3 return types."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aea.contracts.base import Contract
from hexbytes import HexBytes
from web3.datastructures import AttributeDict

from packages.valory.contracts.omen_event_reader.contract import OmenEventReaderContract
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.protocols.contract_api.serialization import ContractApiSerializer
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.protocols.ledger_api.serialization import LedgerApiSerializer


def test_event_reader_loads_as_contract() -> None:
    """Load the local contract class and its ABI through the agent dispatcher."""
    path = Path(__file__).resolve().parents[3] / "contracts" / "omen_event_reader"
    contract = Contract.from_dir(path)
    assert contract.contract_id == OmenEventReaderContract.contract_id


@pytest.mark.parametrize("raw_bytes", [True, False])
def test_event_read_survives_both_protocol_serializers(raw_bytes: bool) -> None:
    """Flatten AttributeDict and HexBytes before sending a STATE message."""
    digest = "0x" + "ab" * 32
    data = HexBytes(digest) if raw_bytes else digest.upper().replace("0X", "0x")
    log = AttributeDict(
        {
            "address": "0x" + "12" * 20,
            "data": data,
            "topics": [data],
            "transactionHash": data,
            "blockNumber": 48997527,
            "logIndex": 3,
            "removed": False,
            "blockHash": HexBytes(digest),
        }
    )
    ledger = MagicMock()
    ledger.api.eth.get_logs.return_value = [log]
    filters = {"address": [log["address"]], "fromBlock": 48997527, "toBlock": 48997627}
    body = OmenEventReaderContract.get_logs(ledger, log["address"], filters)
    ledger.api.eth.get_logs.assert_called_once_with(filters)
    assert body == {
        "logs": [
            {
                "address": log["address"],
                "data": digest,
                "topics": [digest],
                "transactionHash": digest,
                "blockNumber": 48997527,
                "logIndex": 3,
                "removed": False,
            }
        ]
    }
    assert json.loads(json.dumps(body)) == body
    protocols: list[tuple[Any, Any]] = [
        (LedgerApiMessage, LedgerApiSerializer),
        (ContractApiMessage, ContractApiSerializer),
    ]
    for message_class, serializer in protocols:
        message = message_class(
            performative=message_class.Performative.STATE,
            dialogue_reference=("nonce", "reply"),
            message_id=2,
            target=1,
            state=message_class.State(ledger_id="ethereum", body=body),
            **({"ledger_id": "ethereum"} if message_class is LedgerApiMessage else {}),
        )
        assert serializer.decode(serializer.encode(message)).state.body == body


def test_empty_event_read_is_serializable() -> None:
    """Preserve an empty scan without special response shapes."""
    ledger: Any = MagicMock()
    ledger.api.eth.get_logs.return_value = []
    assert OmenEventReaderContract.get_logs(ledger, "0x" + "12" * 20, {}) == {
        "logs": []
    }
