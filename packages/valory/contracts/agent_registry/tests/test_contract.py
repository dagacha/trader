# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2023-2026 Valory AG
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

"""Test for contract module."""

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aea_test_autonomy.base_test_classes.contracts import BaseRegistriesContractsTest
from aea_test_autonomy.docker.base import skip_docker_tests
from aea_test_autonomy.docker.registries import AGENT_REGISTRY

from packages.valory.contracts.agent_registry.contract import AgentRegistryContract

PACKAGE_DIR = Path(__file__).parent.parent


@skip_docker_tests
class TestAgentRegistry(
    BaseRegistriesContractsTest
):  # pylint: disable=too-few-public-methods
    """Test agent registry."""

    contract: AgentRegistryContract
    contract_address = AGENT_REGISTRY
    contract_directory = PACKAGE_DIR

    def test_get_token_uri(self) -> None:
        """Test get token URI method."""

        token_uri = self.contract.get_token_uri(
            ledger_api=self.ledger_api,
            contract_address=self.contract_address,
            token_id=1,
        )

        assert (
            token_uri
            == "https://gateway.autonolas.tech/ipfs/f01701220985b4c36158b51f8a865faceff8141dbc0989c349a1a41ba1e2ac8e5b24536b2"  # nosec
        )


def test_registry_calls_exist_in_build_abi() -> None:
    """Every call on a registry instance must exist in its build ABI."""
    tree = ast.parse((PACKAGE_DIR / "contract.py").read_text())
    instances = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "get_instance"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    calls = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "functions"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id in instances
    }
    abi = json.loads((PACKAGE_DIR / "build/AgentRegistry.json").read_text())["abi"]
    functions = {item["name"] for item in abi if item["type"] == "function"}
    assert calls
    assert calls <= functions, f"Registry ABI is missing: {calls - functions}"


@pytest.mark.parametrize(
    "response, expected",
    [
        ([1], "unexpected format"),
        ([0, []], "no hashes"),
        ([1, ["not bytes"]], "non-bytes"),
        ([2, [b"old", b"new"]], None),
    ],
)
def test_get_hash(response, expected) -> None:
    """Return the latest hash or a clear error for invalid contract data."""
    instance = MagicMock()
    instance.functions.getHashes.return_value.call.return_value = response
    with patch.object(AgentRegistryContract, "get_instance", return_value=instance):
        result = AgentRegistryContract.get_hash(MagicMock(), "registry", 9)
    instance.functions.getHashes.assert_called_once_with(9)
    if expected is None:
        assert result == {"hash": b"new".hex()}
    else:
        assert expected in result["error"]


@pytest.mark.parametrize("updates", [[], [b"old", b"new"]])
def test_token_uri(updates) -> None:
    """Use the latest update, or the token URI when there are no updates."""
    instance = MagicMock()
    instance.functions.getUpdatedHashes.return_value.call.return_value = (
        len(updates),
        updates,
    )
    instance.functions.tokenURI.return_value.call.return_value = "original"
    with patch.object(AgentRegistryContract, "get_instance", return_value=instance):
        result = AgentRegistryContract.get_token_uri(MagicMock(), "registry", 9)
    assert result == (
        "https://gateway.autonolas.tech/ipfs/f01701220" + b"new".hex()
        if updates
        else "original"
    )


@pytest.mark.parametrize(
    "owner, owners, valid",
    [("sender", [], True), ("safe", ["sender"], True), ("safe", ["other"], False)],
)
def test_authenticate_sender(owner, owners, valid) -> None:
    """Accept the agent owner or an owner of its Safe."""
    ledger = MagicMock()
    ledger.api.to_checksum_address.side_effect = lambda address: address
    mech, safe, registry = MagicMock(), MagicMock(), MagicMock()
    mech.functions.tokenId.return_value.call.return_value = 9
    registry.functions.ownerOf.return_value.call.return_value = owner
    safe.functions.getOwners.return_value.call.return_value = owners
    ledger.api.eth.contract.side_effect = [mech, safe]
    with patch.object(AgentRegistryContract, "get_instance", return_value=registry):
        result = AgentRegistryContract.authenticate_sender(
            ledger, "registry", "sender", "mech"
        )
    assert result == {"is_valid": valid}


def test_event_helpers() -> None:
    """Read registry events and return an empty list for an unknown event."""
    instance = MagicMock()
    instance.events.Missing = None
    instance.events.CreateUnit.return_value.process_receipt.return_value = ["created"]
    instance.events.UpdateUnitHash.return_value.process_receipt.return_value = [
        "updated"
    ]
    with patch.object(AgentRegistryContract, "get_instance", return_value=instance):
        assert AgentRegistryContract.get_events(
            MagicMock(), "registry", "Missing", {}
        ) == {"events": []}
        assert AgentRegistryContract.get_events(
            MagicMock(), "registry", "CreateUnit", {}
        ) == {"events": ["created"]}
        assert AgentRegistryContract.get_create_events(MagicMock(), "registry", {}) == [
            "created"
        ]
        assert AgentRegistryContract.get_update_hash_events(
            MagicMock(), "registry", {}
        ) == ["updated"]


def test_authenticate_sender_contract_error() -> None:
    """Return the contract error when authentication cannot complete."""
    ledger = MagicMock()
    ledger.api.eth.contract.side_effect = ValueError("not a mech")
    with patch.object(AgentRegistryContract, "get_instance", return_value=MagicMock()):
        result = AgentRegistryContract.authenticate_sender(
            ledger, "registry", "sender", "mech"
        )
    assert result == {"is_valid": False, "error": "not a mech"}
