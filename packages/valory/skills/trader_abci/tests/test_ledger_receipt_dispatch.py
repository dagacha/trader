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

"""Test raw receipt lookup through the installed ledger connection."""

# pylint: skip-file

import asyncio
from pathlib import Path
from unittest.mock import ANY, patch

import pytest
from aea.connections.base import Connection
from aea.crypto.registries import ledger_apis_registry
from aea.crypto.wallet import CryptoStore
from aea.identity.base import Identity
from aea.mail.base import Envelope
from web3.datastructures import AttributeDict
from web3.eth import Eth

from packages.valory.protocols.ledger_api import LedgerApiMessage

TX_HASH = "0x" + "ab" * 32
LEDGER_DIR = Path(__file__).resolve().parents[3] / "connections" / "ledger"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 1])
@pytest.mark.parametrize("chain_id,network_id", [("gnosis", 100), ("polygon", 137)])
async def test_raw_receipt_dispatch_routes_chain_without_forwarding_it(
    tmp_path: Path, status: int, chain_id: str, network_id: int
) -> None:
    """Dispatch strips the routing key before Web3 reads either mined status."""
    connection = Connection.from_dir(
        str(LEDGER_DIR),
        identity=Identity("trader", address="agent", public_key="public_key"),
        crypto_store=CryptoStore(),
        data_dir=str(tmp_path),
    )
    config = {"address": f"https://{chain_id}.invalid", "chain_id": network_id}
    connection.api_configs = {chain_id: config}
    request = LedgerApiMessage(
        dialogue_reference=("raw_receipt", ""),
        message_id=1,
        target=0,
        performative=LedgerApiMessage.Performative.GET_STATE,  # type: ignore[arg-type]
        ledger_id="ethereum",
        callable="get_transaction_receipt",
        args=(),
        kwargs=LedgerApiMessage.Kwargs(
            {"transaction_hash": TX_HASH, "chain_id": chain_id}
        ),
    )
    request.sender = "valory/trader_abci:0.1.0"
    request.to = str(connection.connection_id)
    envelope = Envelope(to=request.to, sender=request.sender, message=request)

    # This signature rejects chain_id and any other unexpected Web3 keyword.
    def get_transaction_receipt(eth: Eth, transaction_hash: str) -> AttributeDict:
        assert eth.w3.provider.endpoint_uri == config["address"]
        assert transaction_hash == TX_HASH
        return AttributeDict({"status": status})

    await connection.connect()
    try:
        with (
            patch.object(
                Eth,
                "get_transaction_receipt",
                autospec=True,
                side_effect=get_transaction_receipt,
            ) as receipt,
            patch.object(
                ledger_apis_registry, "make", wraps=ledger_apis_registry.make
            ) as make_api,
        ):
            await connection.send(envelope)
            response = await asyncio.wait_for(connection.receive(), timeout=5)
            assert response is not None
            assert response.message.performative == LedgerApiMessage.Performative.STATE
            assert response.message.state.body == {"status": status}
            assert response.message.state.ledger_id == "ethereum"
            make_api.assert_called_once_with("ethereum", **config)
            receipt.assert_called_once_with(ANY, transaction_hash=TX_HASH)
            assert request.kwargs.body == {"transaction_hash": TX_HASH}
    finally:
        await connection.disconnect()
