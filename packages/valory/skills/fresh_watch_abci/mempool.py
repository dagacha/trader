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

"""Decode pending Omen creation calls without network access."""

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak  # type: ignore[import-not-found]

CREATOR_PAIRS = {
    "0x1cac05cf0bb39945bb0c6446a36b3fd8e26be285": "0x89c5cc945dd550bcffb72fe42bff002429f46fec",
    "0xebff9a4079292adddc3d639cf39bfe7f9e4740ab": "0xffc8029154ecd55abed15bd428ba596e7d23f557",
}
EXEC_SELECTOR = bytes.fromhex("6a761202")
MULTISEND_SELECTOR = bytes.fromhex("8d80ff0a")
FACTORY_SELECTOR = bytes.fromhex("82e90fa0")
QUESTION_SELECTOR = bytes.fromhex("762c38fd")
WXDAI = "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
# implementationMaster() on the Gnosis deterministic factory.
IMPLEMENTATION = "0x907e5e6e8391395f6d518ae29bfe593051bbec52"
SAFE_TYPES = [
    "address",
    "uint256",
    "bytes",
    "uint8",
    "uint256",
    "uint256",
    "uint256",
    "address",
    "address",
    "bytes",
]
FACTORY_TYPES = [
    "uint256",
    "address",
    "address",
    "bytes32[]",
    "uint256",
    "uint256",
    "uint256[]",
]
QUESTION_TYPES = ["uint256", "string", "address", "uint32", "uint32", "uint256"]


@dataclass(frozen=True)
class CreationCall:
    """The arguments from one Safe's pending market creation."""

    creator: str
    salt_nonce: int
    conditional_tokens: str
    collateral_token: str
    condition_ids: Tuple[bytes, ...]
    fee: int
    initial_funds: int
    distribution_hint: Tuple[int, ...]
    template_id: int
    question: str
    opening_ts: int


@dataclass(frozen=True)
class PendingCreation:
    """A supported pending market and the creator's unchanged fees."""

    market: Dict[str, Any]
    creation_tx: str
    seen_at: float
    priority_fee: int
    max_fee: int
    salt_nonce: int
    initial_funds: int


def question_metadata(
    template_id: int, question: str, opening: int
) -> Optional[Dict[str, Any]]:
    """Parse the same Realitio fields for pending and mined creations.

    :param template_id: Realitio template number.
    :param question: Realitio question text with JSON-escaped fields.
    :param opening: question opening timestamp.
    :return: supported metadata, an empty dict if unsupported, or None if absent.
    """
    if template_id != 2:
        return {}
    parts = question.replace("\x1f", "␟").split("␟")
    if len(parts) < 2:
        return None
    outcomes = json.loads("[" + parts[1] + "]")
    if outcomes != ["Yes", "No"]:
        return {}
    return dict(
        title=json.loads('"' + parts[0] + '"'),
        openingTimestamp=opening,
        outcomes=outcomes,
        outcomeSlotCount=2,
        question={"templateId": template_id},
    )


def creator_transaction(tx: Dict[str, Any]) -> bool:
    """Filter full transaction bodies before ABI decoding.

    :param tx: pending JSON-RPC transaction.
    :return: whether it has a known creator pair and Safe selector.
    """
    sender, safe, data = tx.get("from"), tx.get("to"), tx.get("input")
    return (
        isinstance(sender, str)
        and isinstance(safe, str)
        and CREATOR_PAIRS.get(sender.lower()) == safe.lower()
        and isinstance(data, str)
        and data[:10].lower() == "0x" + EXEC_SELECTOR.hex()
    )


def _entries(data: bytes) -> List[Tuple[int, str, bytes]]:
    """Read bounded MultiSend entries.

    :param data: packed MultiSend calls.
    :return: operation, target and calldata for each entry.
    :raises ValueError: if an entry is truncated.
    """
    entries = []
    while data:
        if len(data) < 85:
            raise ValueError("Short MultiSend header")
        length = int.from_bytes(data[53:85], "big")
        if length > len(data) - 85:
            raise ValueError("Short MultiSend data")
        entries.append((data[0], "0x" + data[1:21].hex(), data[85 : 85 + length]))
        data = data[85 + length :]
    return entries


def decode_calls(
    tx: Dict[str, Any], multisend: str, factory: str
) -> Optional[CreationCall]:
    """Read creation arguments without inferring whether the pool is supported.

    :param tx: full JSON-RPC transaction.
    :param multisend: configured MultiSend delegate target.
    :param factory: configured deterministic factory target.
    :return: decoded arguments, or None for another or malformed transaction.
    """
    try:
        if not creator_transaction(tx):
            return None
        outer = decode(SAFE_TYPES, bytes.fromhex(tx["input"][10:]))
        target, _, data, operation = outer[:4]
        if (
            target.lower() != multisend.lower()
            or operation != 1
            or data[:4] != MULTISEND_SELECTOR
        ):
            return None
        entries = _entries(decode(["bytes"], data[4:])[0])
        factories = [
            call
            for op, address, call in entries
            if op == 0 and address == factory.lower() and call[:4] == FACTORY_SELECTOR
        ]
        questions = [
            call for op, _, call in entries if op == 0 and call[:4] == QUESTION_SELECTOR
        ]
        if len(factories) != 1 or len(questions) != 1:
            return None
        salt, conditional, collateral, conditions, fee, funds, hint = decode(
            FACTORY_TYPES, factories[0][4:]
        )
        template, question, _, _, opening, _ = decode(QUESTION_TYPES, questions[0][4:])
        return CreationCall(
            tx["to"].lower(),
            salt,
            conditional,
            collateral,
            conditions,
            fee,
            funds,
            hint,
            template,
            question,
            opening,
        )
    except (ValueError, TypeError, KeyError, DecodingError):
        return None


def predict_market(call: CreationCall, factory: str) -> str:
    """Use Gnosis Create2CloneFactory's constructor and caller-derived salt.

    Source: gnosis/conditional-tokens-market-makers, Create2CloneFactory.sol.
    The constructor bytes are part of the init code, after its 99-byte prefix.

    :param call: decoded factory arguments and the calling Safe.
    :param factory: deterministic factory address.
    :return: lowercase predicted market address.
    """
    constructor = encode(
        ["address", "address", "bytes32[]", "uint256"],
        [call.conditional_tokens, call.collateral_token, call.condition_ids, call.fee],
    )
    payload = keccak(text="cloneConstructor(bytes)")[:4] + encode(
        ["bytes"], [constructor]
    )
    factory_bytes = bytes.fromhex(factory[2:])
    init_code = (
        bytes.fromhex("3d3d606380380380913d393d73")
        + factory_bytes
        + bytes.fromhex(
            "5af4602a57600080fd5b602d8060366000396000f3363d3d373d3d3d363d73"
        )
        + bytes.fromhex(IMPLEMENTATION[2:])
        + bytes.fromhex("5af43d82803e903d91602b57fd5bf3")
        + payload
    )
    salt = keccak(encode(["address", "uint256"], [call.creator, call.salt_nonce]))
    return "0x" + keccak(b"\xff" + factory_bytes + salt + keccak(init_code))[-20:].hex()


def _number(value: Any) -> int:
    """Read an integer or JSON-RPC hex quantity.

    :param value: integer or hexadecimal string.
    :return: integer quantity.
    """
    return int(value, 16) if isinstance(value, str) else int(value)


def decode_pending(
    tx: Dict[str, Any], cfg: Any, multisend: str, seen_at: float
) -> Optional[PendingCreation]:
    """Build a complete row only for the supported pending binary pool.

    :param tx: pending JSON-RPC transaction with EIP-1559 fee fields.
    :param cfg: lightning controls, including factory and creator allowlist.
    :param multisend: configured MultiSend delegate target.
    :param seen_at: first local sight time, in seconds since the epoch.
    :return: pending market and copied fees, or None when unsupported or malformed.
    """
    try:
        if _number(tx["type"]) != 2:
            return None
        priority, maximum = _number(tx["maxPriorityFeePerGas"]), _number(
            tx["maxFeePerGas"]
        )
        if priority < 0 or maximum < priority or not isinstance(tx["hash"], str):
            return None
        call = decode_calls(tx, multisend, cfg.omen_fpmm_factory)
        if (
            call is None
            or call.creator not in {value.lower() for value in cfg.lightning_creators}
            or call.collateral_token != WXDAI
            or len(call.condition_ids) != 1
            or call.distribution_hint not in ((), (1, 1))
            or call.initial_funds <= 0
        ):
            return None
        metadata = question_metadata(call.template_id, call.question, call.opening_ts)
        if not metadata:
            return None
        market = dict(
            metadata,
            id=predict_market(call, cfg.omen_fpmm_factory),
            creator=call.creator,
            collateralToken=call.collateral_token,
            condition={"id": "0x" + call.condition_ids[0].hex()},
            conditional_tokens=call.conditional_tokens,
            fee=call.fee,
            creationTimestamp=int(seen_at),
            outcomeTokenAmounts=[call.initial_funds] * 2,
            outcomeTokenMarginalPrices=[0.5, 0.5],
            scaledLiquidityMeasure=call.initial_funds / 10**18,
        )
        return PendingCreation(
            market,
            tx["hash"].lower(),
            seen_at,
            priority,
            maximum,
            call.salt_nonce,
            call.initial_funds,
        )
    except (ValueError, TypeError, KeyError, OverflowError, DecodingError):
        return None
