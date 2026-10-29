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
from unittest.mock import MagicMock, patch

import pytest
from aea.contracts.base import Contract
from hexbytes import HexBytes
from web3.datastructures import AttributeDict

from packages.valory.contracts.omen_event_reader.contract import OmenEventReaderContract
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.protocols.contract_api.serialization import ContractApiSerializer
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.protocols.ledger_api.serialization import LedgerApiSerializer
from packages.valory.skills.fresh_watch_abci.tests.test_chain_discovery import (
    CREATED,
    CREATION,
    Harness,
    done,
    run,
)


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


def test_missing_contract_is_reported_without_conditional_token_reads(
    tmp_path: Path,
) -> None:
    """An address with no code is confirmed missing rather than an RPC failure."""
    ledger: Any = MagicMock()
    ledger.api.eth.get_code.return_value = HexBytes("0x")
    body = OmenEventReaderContract.get_pool_balances(
        ledger, "0x" + "12" * 20, "0x" + "34" * 20, "0x" + "56" * 20, "0x" + "78" * 32
    )
    assert body == {"balances": []}
    ledger.api.eth.contract.assert_not_called()
    harness: Any = Harness(tmp_path)
    read = harness._chain_rpc

    def rpc(method: str, **kwargs: Any) -> Any:
        """Pass the actual no-code result through market materialisation."""
        if method == "get_pool_balances":
            return (yield from done(body))
        return (yield from read(method, **kwargs))

    harness._chain_rpc = rpc
    assert run(harness._chain_market(CREATION, {"timestamp": CREATED}, CREATED)) == {}


def test_present_contract_reads_real_conditional_token_balances() -> None:
    """Keep funded and zero balances distinct from connection errors."""
    ledger: Any = MagicMock()
    ledger.api.eth.get_code.return_value = HexBytes("0x6000")
    contract = ledger.get_contract_instance.return_value
    contract.functions.getOutcomeSlotCount.return_value.call.return_value = 2
    contract.functions.getCollectionId.return_value.call.return_value = bytes(32)
    contract.functions.balanceOf.return_value.call.side_effect = [7, 0]
    ledger.api.solidity_keccak.return_value = bytes(32)
    with patch(
        "packages.valory.contracts.market_maker.contract.ConditionalTokensContract.get_instance",
        return_value=contract,
    ):
        body = OmenEventReaderContract.get_pool_balances(
            ledger,
            "0x" + "12" * 20,
            "0x" + "34" * 20,
            "0x" + "56" * 20,
            "0x" + "78" * 32,
        )
    assert body == {"balances": [7, 0]}
