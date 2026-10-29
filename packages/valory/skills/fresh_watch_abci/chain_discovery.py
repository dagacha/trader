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

"""Discover Omen markets through the agent's own ledger connection."""

import json
from typing import Any, Dict, Generator, List, Optional, Tuple, Union, cast

from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak, to_checksum_address  # type: ignore[import-not-found]

from packages.valory.contracts.omen_event_reader.contract import PUBLIC_ID
from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.base import LEDGER_API_ADDRESS
from packages.valory.skills.abstract_round_abci.behaviour_utils import TimeoutException
from packages.valory.skills.abstract_round_abci.dialogues import (
    ContractApiDialogue,
    ContractApiDialogues,
    LedgerApiDialogue,
    LedgerApiDialogues,
)
from packages.valory.skills.fresh_watch_abci.lightning import preview_enabled
from packages.valory.skills.fresh_watch_abci.rpc import request_with_timeout
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue

# Verified against Gnosis creation tx 0x1a1d567d7d7c596895327e53568aacc24378cf45234b8a00fbe98d0c31608c00.
CREATION_TOPIC = "0x92e0912d3d7f3192cad5c7ae3b47fb97f9c465c1dd12a5c24fd901ddb3905f43"
CONDITION_TOPIC = "0xab3760c3bd2bb38b5bcf54dc79802ed67338b4cf29f3054ded67ed24661e4177"
QUESTION_TOPIC = "0xfe2dac156a3890636ce13f65f4fdf41dcaee11526e4a5374531572d92194796c"
PROPOSER = "0x601024e27f1c67b28209e24272ced8a31fc8151f"
REQUEST_TOPIC = (
    "0x"
    + keccak(text="MarketplaceRequest(address,address,uint256,bytes32[],bytes[])").hex()
)
DELIVERY_TOPIC = (
    "0x" + keccak(text="Deliver(address,address,bytes32,uint256,bytes)").hex()
)
MARKET_FIELDS = """id title collateralToken condition { id } creator fee creationTimestamp
openingTimestamp outcomeSlotCount outcomeTokenAmounts outcomeTokenMarginalPrices
outcomes scaledLiquidityMeasure question { templateId }"""


class ChainRPCError(Exception):
    """A chain read did not return usable data."""


def _hex(value: Any) -> str:
    """Normalize an RPC byte value.

    :param value: hex string or raw bytes.
    :return: lowercase hex string with a prefix.
    """
    return value.lower() if isinstance(value, str) else "0x" + bytes(value).hex()


def _number(value: Any) -> int:
    """Normalize an RPC integer.

    :param value: JSON-RPC hex string or integer.
    :return: integer value.
    """
    return (
        int(value, 16)
        if isinstance(value, str) and value.startswith("0x")
        else int(value)
    )


def creation_details(log: Dict[str, Any]) -> Dict[str, Any]:
    """Decode the factory event, whose creator is its only indexed field.

    :param log: raw creation event.
    :return: market fields, including the condition contract address.
    :raises ValueError: if the event has no single binary-market condition.
    """
    topics = [_hex(topic) for topic in log["topics"]]
    if len(topics) != 2 or topics[0] != CREATION_TOPIC:
        raise ValueError("Unexpected factory event")
    market, conditional, collateral, conditions, fee = decode(
        ["address", "address", "address", "bytes32[]", "uint256"],
        bytes.fromhex(_hex(log["data"])[2:]),
    )
    if len(conditions) != 1:
        raise ValueError("Require one market condition")
    return dict(
        id=market.lower(),
        creator="0x" + topics[1][-40:],
        collateralToken=collateral.lower(),
        condition={"id": _hex(conditions[0])},
        conditional_tokens=conditional.lower(),
        fee=fee,
    )


def receipt_question(
    logs: List[Dict[str, Any]], condition_id: str
) -> Optional[Dict[str, Any]]:
    """Match the condition's question ID before reading its Realitio title.

    :param logs: receipt events.
    :param condition_id: the market's condition ID.
    :return: metadata, an empty dict for unsupported questions, or None if missing.
    """
    question_id = None
    for log in logs:
        topics = [_hex(topic) for topic in log["topics"]]
        if (
            len(topics) == 4
            and topics[0] == CONDITION_TOPIC
            and topics[1] == condition_id
        ):
            if decode(["uint256"], bytes.fromhex(_hex(log["data"])[2:]))[0] != 2:
                return {}
            question_id = topics[3]
            break
    if question_id is None:
        return None
    for log in logs:
        topics = [_hex(topic) for topic in log["topics"]]
        if len(topics) != 4 or topics[0] != QUESTION_TOPIC or topics[1] != question_id:
            continue
        template_id, question, _, _, opening, _, _ = decode(
            ["uint256", "string", "address", "uint32", "uint32", "uint256", "uint256"],
            bytes.fromhex(_hex(log["data"])[2:]),
        )
        if template_id != 2:
            return {}
        parts = question.replace("\x1f", "␟").split("␟")
        if len(parts) < 2:
            return None
        outcomes = json.loads("[" + parts[1] + "]")
        if outcomes != ["Yes", "No"]:
            return {}
        return dict(
            title=parts[0],
            openingTimestamp=opening,
            outcomes=outcomes,
            outcomeSlotCount=2,
            question={"templateId": template_id},
        )
    return None


def event_scan(
    reader: Any, now: float
) -> Generator[None, None, Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """Share one head and one event filter between both pollers in a tick.

    :param reader: cooperative chain reader.
    :param now: discovery tick timestamp.
    :yield: ledger or contract API I/O.
    :return: head block and merged raw events.
    :raises ChainRPCError: when the event response is not a list.
    """
    cached = getattr(reader.context, "lightning_event_scan", None)
    if cached is not None and cached["now"] == now:
        if cached.get("error") is not None:
            raise ChainRPCError(str(cached["error"])) from cached["error"]
        return cached["head"], cached["logs"]
    cached = {"now": now}
    reader.context.lightning_event_scan = cached
    try:
        head = yield from reader._chain_rpc("get_block", block_identifier="latest")
        height = _number(head["number"])
        cfg = reader.params.lightning
        firsts = []
        addresses = []
        topics = []
        if cfg.lightning_chain_detect:
            addresses.append(to_checksum_address(cfg.omen_fpmm_factory))
            topics.append(CREATION_TOPIC)
            path = reader.params.store_path / "lightning_chain.json"
            last = json.loads(path.read_text())["last_block"] if path.exists() else None
            earliest = max(0, height - reader.params.fast.fresh_max_age_s // 5 - 1)
            if last is None or height > last:
                firsts.append(earliest if last is None else max(earliest, last - 2))
        if cfg.lightning_preview and preview_enabled(reader):
            addresses.extend(
                [
                    to_checksum_address(
                        reader.params.mech_marketplace_config.mech_marketplace_address
                    ),
                    to_checksum_address(PROPOSER),
                ]
            )
            topics.extend([REQUEST_TOPIC, DELIVERY_TOPIC])
            path = reader.params.store_path / "lightning_preview.json"
            last = json.loads(path.read_text())["last_block"] if path.exists() else 0
            if height > last:
                firsts.append(max(last + 1, height - 180, 0))
        logs = []
        if firsts:
            logs = yield from reader._chain_rpc(
                "get_logs",
                filter_params={
                    "address": addresses,
                    "fromBlock": min(firsts),
                    "toBlock": height,
                    "topics": [topics],
                },
            )
            if not isinstance(logs, list):
                raise ChainRPCError("Invalid getLogs result")
        cached.update(head=head, logs=logs)
        return head, logs
    except (ChainRPCError, TimeoutException, ValueError, KeyError, TypeError) as exc:
        cached["error"] = exc
        raise


class ChainDiscoveryMixin:
    """Supply chain reads to both the foreground and background market pollers."""

    def _chain_rpc(self: Any, method: str, **kwargs: Any) -> Generator[None, None, Any]:
        """Bound a read through the configured ledger API to five seconds.

        :param method: Web3 eth method name.
        :param kwargs: method inputs.
        :yield: ledger API I/O.
        :return: normalized method result.
        :raises ChainRPCError: on an RPC failure or timeout.
        """
        if method in {"get_logs", "get_pool_balances"}:
            message_class = ContractApiMessage
            dialogues = cast(ContractApiDialogues, self.context.contract_api_dialogues)
            inputs = dict(
                contract_id=str(PUBLIC_ID),
                contract_address=kwargs.pop(
                    "contract_address", self.params.lightning.omen_fpmm_factory
                ),
            )
        else:
            message_class = LedgerApiMessage
            dialogues = cast(LedgerApiDialogues, self.context.ledger_api_dialogues)
            inputs = dict(args=tuple())
        message, dialogue = dialogues.create(
            counterparty=LEDGER_API_ADDRESS,
            performative=message_class.Performative.GET_STATE,
            ledger_id=self.context.default_ledger_id,
            callable=method,
            kwargs=message_class.Kwargs(
                dict(kwargs, chain_id=self.params.mech_chain_id)
            ),
            **inputs,
        )
        dialogue = cast(Union[ContractApiDialogue, LedgerApiDialogue], dialogue)
        try:
            response = yield from request_with_timeout(self, message, dialogue, 5.0)
        except TimeoutException as exc:
            raise ChainRPCError("Ledger response timed out") from exc
        if response.performative != message_class.Performative.STATE:
            raise ChainRPCError(str(response.message))
        body = response.state.body
        return (
            body["logs"] if method == "get_logs" else body.get(method + "_result", body)
        )

    def _chain_pool_balances(
        self: Any, market: str, collateral: str, condition: Optional[str]
    ) -> Generator[None, None, Optional[List[int]]]:
        """Bound a pool read and distinguish absent contracts from RPC failures.

        :param market: market maker address.
        :param collateral: collateral token address.
        :param condition: binary market condition ID.
        :yield: contract API I/O.
        :return: balances, an empty list for no contract, or None on read failure.
        """
        try:
            body = yield from self._chain_rpc(
                "get_pool_balances",
                contract_address=market,
                conditional_tokens_address=self.params.conditional_tokens_address,
                collateral_token=collateral,
                condition_id=condition,
            )
            return cast(List[int], body["balances"])
        except (ChainRPCError, KeyError, TypeError):
            return None

    def _chain_subgraph_market(
        self: Any, market_id: str
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Use an exact market lookup when its question is absent from the receipt.

        :param market_id: market address.
        :yield: subgraph HTTP I/O.
        :return: full market row or None until the indexer catches up.
        """
        subgraph = self.context.omen_subgraph
        query = (
            "{ fixedProductMarketMakers(where: {id: "
            + json.dumps(market_id)
            + "}) {"
            + MARKET_FIELDS
            + "} }"
        )
        response = yield from self.get_http_response(
            content=json.dumps({"query": query}).encode(), **subgraph.get_spec()
        )
        rows = subgraph.process_response(response)
        return rows[0] if isinstance(rows, list) and rows else None

    def _chain_market(
        self: Any, log: Dict[str, Any], block: Dict[str, Any], now: float
    ) -> Generator[None, None, Optional[Dict[str, Any]]]:
        """Read one receipt and live pool before adding the market to the queue.

        :param log: creation log.
        :param block: creation block header.
        :param now: detection timestamp.
        :yield: ledger and optional subgraph I/O.
        :return: market row or None for a transient materialization failure.
        :raises ChainRPCError: when the live pool read fails.
        """
        market = creation_details(log)
        created = _number(block["timestamp"])
        if created > now:
            return None
        if now - created > self.params.fast.fresh_max_age_s:
            return {}
        receipt = yield from self._chain_rpc(
            "get_transaction_receipt", transaction_hash=_hex(log["transactionHash"])
        )
        question = receipt_question(receipt["logs"], market["condition"]["id"])
        source = "chain"
        if question is None:
            market = yield from self._chain_subgraph_market(market["id"])
            source = "subgraph"
            if (
                market is None
                or market.get("outcomeSlotCount") is None
                or market.get("outcomes") is None
            ):
                return None
            if market["outcomeSlotCount"] != 2 or market["outcomes"] != ["Yes", "No"]:
                return {}
            template_id = (market.get("question") or {}).get("templateId")
            if template_id is not None and str(template_id) != "2":
                return {}
        elif not question:
            return {}
        else:
            market.update(question, creationTimestamp=created)
            balances = yield from self._chain_pool_balances(
                market["id"], market["collateralToken"], market["condition"]["id"]
            )
            if balances is None:
                raise ChainRPCError("Market pool read failed")
            if not balances:
                return {}
            total = sum(balances)
            market.update(
                outcomeTokenAmounts=balances,
                outcomeTokenMarginalPrices=(
                    [balances[1] / total, balances[0] / total] if total else [0, 0]
                ),
                scaledLiquidityMeasure=min(balances) / 10**18,
            )
            market.pop("conditional_tokens")
        self.context.logger.info(
            "[LIGHTNING_DETECT] market=%s delay_s=%.3f source=%s",
            market["id"],
            max(0, now - created),
            source,
        )
        return market

    def chain_markets(
        self: Any, now: float
    ) -> Generator[None, None, Optional[List[Dict[str, Any]]]]:
        """Fetch creator logs and persist markets before advancing the block cursor.

        Scan each new block once, with a two-block overlap for short reorgs.
        Empty scans also advance the durable block cursor.

        :param now: detection timestamp.
        :yield: cooperative ledger and optional title lookup I/O.
        :return: rows, or None when the standard subgraph poll must run.
        :raises ChainRPCError: for an invalid chain read, caught for fallback.
        """
        cfg = self.params.lightning
        if not cfg.lightning_chain_detect:
            return None
        if not self.params.fast.in_window(now):
            return []
        path = self.params.store_path / "lightning_chain.json"
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {"last_block": None, "rpc_error_since": None}
        )

        def save() -> None:
            """Save this agent's cursor and error interval atomically."""
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state))
            temporary.replace(path)

        try:
            head, logs = yield from event_scan(self, now)
            number = _number(head["number"])
            blocks = {number: head}
            if state["last_block"] is None:
                state["last_block"] = max(
                    0, number - self.params.fast.fresh_max_age_s // 5 - 1
                )
                save()
            elif number <= state["last_block"]:
                state["rpc_error_since"] = None
                save()
                return []
            overlap_start = max(
                0,
                number - self.params.fast.fresh_max_age_s // 5 - 1,
                state["last_block"] - 2,
            )
            scanned_head = state.get("scanned_head", state["last_block"])
            orphan_ids = set()
            queue = FreshQueue(self.params.store_path / "fresh_watch.json")
            for pending in queue.pending:
                if (
                    "block_hash" not in pending
                    or not overlap_start <= pending["block_number"] <= scanned_head
                ):
                    continue
                height = pending["block_number"]
                if height not in blocks:
                    blocks[height] = yield from self._chain_rpc(
                        "get_block", block_identifier=height
                    )
                if pending["block_hash"] != _hex(blocks[height]["hash"]):
                    orphan_ids.add(pending["id"])
            creators = [creator.lower() for creator in cfg.lightning_creators]
            rows = []
            last_block = number
            retry_block = None
            market_error = None
            for log in sorted(
                logs,
                key=lambda row: (_number(row["blockNumber"]), _number(row["logIndex"])),
            ):
                if (
                    log.get("removed")
                    or _hex(log["address"]) != cfg.omen_fpmm_factory.lower()
                    or len(log["topics"]) != 2
                    or _hex(log["topics"][0]) != CREATION_TOPIC
                    or "0x" + _hex(log["topics"][1])[-40:] not in creators
                ):
                    continue
                try:
                    creation_details(log)
                except (ValueError, DecodingError):
                    continue
                number = _number(log["blockNumber"])
                if number not in blocks:
                    blocks[number] = yield from self._chain_rpc(
                        "get_block", block_identifier=number
                    )
                try:
                    row = yield from self._chain_market(log, blocks[number], now)
                except (ChainRPCError, TimeoutException) as exc:
                    row = None
                    market_error = exc
                if row is None:
                    # Keep ready markets moving while this block needs a retry.
                    retry_block = min(retry_block or number, number)
                    continue
                if row:
                    row.update(
                        block_number=number, block_hash=_hex(blocks[number]["hash"])
                    )
                    rows.append(row)
                last_block = max(last_block, number)
            queue = FreshQueue(self.params.store_path / "fresh_watch.json")
            queue.pending = [
                row for row in queue.pending if row["id"] not in orphan_ids
            ]
            queue.ingest(
                rows,
                now,
                creators,
                self.params.fast.fresh_max_age_s,
                self.params.fast.overlap_s,
            )
            queue.save()
            state["scanned_head"] = _number(head["number"])
            state["last_block"] = (
                min(state["last_block"], retry_block - 1) if retry_block else last_block
            )
            if market_error is not None:
                raise ChainRPCError(str(market_error))
            state["rpc_error_since"] = None
            save()
            return rows
        except (
            ChainRPCError,
            TimeoutException,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            if state["rpc_error_since"] is None:
                state["rpc_error_since"] = now
            save()
            self.context.logger.warning("Lightning market RPC error: %s", exc)
            return (
                None
                if now - state["rpc_error_since"] > cfg.lightning_rpc_fallback_s
                else []
            )
