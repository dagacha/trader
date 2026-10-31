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

"""Run an optional direct-send lane outside the foreground FSM."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Generator, Optional
from urllib.parse import urlparse

from aea.helpers.transaction.base import RawTransaction, Terms
from eth_abi import decode, encode
from eth_utils import keccak  # type: ignore[import-not-found]

from packages.open_aea.protocols.signing import SigningMessage
from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.base import LEDGER_API_ADDRESS
from packages.valory.skills.abstract_round_abci.behaviour_utils import TimeoutException
from packages.valory.skills.agent_performance_summary_abci.activity_goal import (
    record_trade,
)
from packages.valory.skills.agent_performance_summary_abci.models import (
    write_json_atomically,
)
from packages.valory.skills.decision_maker_abci.behaviours.base import WXDAI
from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller
from packages.valory.skills.fresh_watch_abci.chain_discovery import (
    creation_details,
    receipt_question,
)
from packages.valory.skills.fresh_watch_abci.lane import (
    RPCStats,
    SlotPoller,
    build_safe_calldata,
    buy_result,
    creation_filter,
    fixed_transaction,
    funding_balances,
    lane_decision,
    number,
    pool_balances_call,
)
from packages.valory.skills.fresh_watch_abci.lightning import BaseRateCache
from packages.valory.skills.fresh_watch_abci.preview import preview_night
from packages.valory.skills.fresh_watch_abci.rpc import request_with_timeout
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.market_manager_abci.bets import (
    Bet,
    BetsEncoder,
    PredictionResponse,
    QueueStatus,
)


class LaneRPCError(Exception):
    """A raw RPC request failed without exposing endpoint credentials."""


class LightningLaneBehaviour(FreshMarketPoller):
    """Poll slots and send one owner-signed Safe bet without consensus."""

    def __init__(self, **kwargs: Any) -> None:
        """Keep scheduling and cached balances local to this agent.

        :param kwargs: behaviour constructor arguments.
        """
        super().__init__(**kwargs)
        self._poller: Optional[SlotPoller] = None
        self._stats = RPCStats(started_at=time.time())
        self._checked = False
        self._safe_ok = False
        self._off_logged = False
        self._balances: Dict[str, int] = {}
        self._balance_at = 0.0
        self._was_fresh = False
        self._night = ""
        self._shots = 0
        self._rpc_id = 0
        self._journal_ok = True
        self._saved_state = ""
        path = self.params.store_path / "lightning_lane_state.json"
        if path.exists():
            try:
                saved = json.loads(path.read_text())
                self._night, self._shots = saved["night"], saved["shots"]
                if saved.get("inflight"):
                    if saved["inflight"].get("tx") is None:
                        self._log_drop(saved["inflight"], "abandoned_signing")
                        self._persist()
                    else:
                        self.context.shared_state["lightning_lane_inflight"] = saved[
                            "inflight"
                        ]
            except (OSError, ValueError, KeyError, TypeError):
                self._journal_ok = False

    def _persist(self) -> None:
        """Save the nonce lock and nightly budget before either broadcast."""
        state = dict(
            night=self._night,
            shots=self._shots,
            inflight=self.context.shared_state.get("lightning_lane_inflight"),
        )
        serialized = json.dumps(state, sort_keys=True)
        if serialized != self._saved_state:
            write_json_atomically(
                self.params.store_path / "lightning_lane_state.json", state
            )
            self._saved_state = serialized

    def _enabled(self) -> bool:
        """Require all explicit lane controls before any RPC work.

        :return: whether the direct lane has enough configuration.
        """
        cfg = self.params.lightning
        if not cfg.lightning_lane:
            return False
        reason = "" if cfg.lightning_rpc_url else "missing_rpc_url"
        if not self._journal_ok:
            reason = "invalid_lane_state"
        if cfg.lightning_lane_tip_gwei <= 0:
            reason = "zero_tip"
        if reason and not self._off_logged:
            self.context.logger.info("[LIGHTNING_LANE] off reason=%s", reason)
            self._off_logged = True
        return not reason

    def _round_name(self) -> str:
        """Read the foreground round without changing its state.

        :return: current round class name.
        """
        return type(self.context.state.round_sequence.current_round).__name__

    def _round_allowed(self) -> bool:
        """Allow only rounds that hold no prepared Safe transaction.

        :return: whether a new direct bet may be prepared.
        """
        if self.context.shared_state.get("lightning_lane_fsm_preparing"):
            return False
        name = self._round_name()
        if name in {"MechRequestRound", "MechResponseRound"}:
            return self.params.use_offchain is True
        return name in {
            "FreshWatchRound",
            "FastDecisionRound",
            "DecisionRequestRound",
            "TradeCountRound",
            "MechOnlySelectionRound",
        }

    def _request(self, method: str, params: Any) -> Any:
        """Build one raw JSON-RPC POST through the existing HTTP connection.

        :param method: JSON-RPC method.
        :param params: positional method arguments.
        :return: HTTP message and dialogue.
        """
        self._rpc_id += 1
        return self._build_http_request_message(
            method="POST",
            url=self.params.lightning.lightning_rpc_url,
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self._rpc_id,
                    "method": method,
                    "params": params,
                }
            ).encode(),
        )

    def _rpc(self, method: str, params: Any) -> Generator[None, None, Any]:
        """Bound one raw read and count errors without logging the URL.

        :param method: JSON-RPC method.
        :param params: positional method arguments.
        :yield: HTTP I/O.
        :return: JSON-RPC result, including a legitimate null result.
        :raises LaneRPCError: when the response is absent or unusable.
        """
        try:
            if not self.params.lightning.lightning_rpc_url:
                return (yield from self._lane_guard_rpc(method, params))
            message, dialogue = self._request(method, params)
            response = yield from request_with_timeout(
                self, message, dialogue, timeout=2.0
            )
            body = json.loads(response.body)
            if response.status_code != 200 or "error" in body or "result" not in body:
                raise ValueError("RPC result unavailable")
        except (TimeoutException, ValueError, TypeError, KeyError) as exc:
            self._stats.called(time.time(), error=True)
            if not self._stats.ready(time.time()):
                self.context.logger.warning(
                    "[LIGHTNING_LANE_RPC] paused_s=60 reason=consecutive_errors"
                )
            raise LaneRPCError("Lane RPC request failed") from exc
        self._stats.called(time.time())
        return body["result"]

    def _startup(self) -> Generator[None, None, bool]:
        """Check that this EOA can submit the single-owner Safe call.

        :yield: raw Safe reads.
        :return: whether the Safe supports the lane signature.
        """
        safe = self.synchronized_data.safe_contract_address
        threshold = yield from self._rpc(
            "eth_call",
            [
                {"to": safe, "data": "0x" + keccak(text="getThreshold()")[:4].hex()},
                "latest",
            ],
        )
        owner = yield from self._rpc(
            "eth_call",
            [
                {
                    "to": safe,
                    "data": "0x"
                    + (
                        keccak(text="isOwner(address)")[:4]
                        + encode(["address"], [self.context.agent_address])
                    ).hex(),
                },
                "latest",
            ],
        )
        self._safe_ok = number(threshold) == 1 and bool(
            decode(["bool"], bytes.fromhex(owner[2:]))[0]
        )
        self._checked = True
        self.context.logger.info(
            "[LIGHTNING_LANE] startup=%s rpc_host=%s",
            "ready" if self._safe_ok else "unsupported_safe",
            urlparse(self.params.lightning.lightning_rpc_url).hostname,
        )
        return self._safe_ok

    def _refresh_idle(self, now: float) -> Generator[None, None, None]:
        """Refresh the EOA nonce and spendable balances while no shot waits.

        :param now: local Unix timestamp.
        :yield: raw RPC reads.
        """
        shared = self.context.shared_state
        fresh = self._round_name() == "FreshWatchRound"
        entered = fresh and not self._was_fresh
        self._was_fresh = fresh
        if (
            entered
            and shared.get("lightning_lane_fsm_preparing_round") != "background_round"
        ):
            shared.pop("lightning_lane_fsm_preparing", None)
            shared.pop("lightning_lane_fsm_preparing_round", None)
        if shared.get("lightning_lane_inflight") or not self._round_allowed():
            return
        nonce = shared.get("lightning_lane_eoa_nonce", {})
        settlement = shared.get("lightning_lane_last_fsm_settlement", 0)
        if (
            entered
            or now - nonce.get("read_at", 0) >= 30
            or nonce.get("read_at", 0) <= settlement
        ):
            value = yield from self._rpc(
                "eth_getTransactionCount", [self.context.agent_address, "pending"]
            )
            shared["lightning_lane_eoa_nonce"] = {
                "nonce": number(value),
                "read_at": time.time(),
            }
        if now - self._balance_at < 30 and self._balance_at > settlement:
            return
        safe = self.synchronized_data.safe_contract_address
        eoa = yield from self._rpc(
            "eth_getBalance", [self.context.agent_address, "latest"]
        )
        native = yield from self._rpc("eth_getBalance", [safe, "latest"])
        wrapped = yield from self._rpc(
            "eth_call",
            [
                {
                    "to": WXDAI,
                    "data": "0x"
                    + (
                        keccak(text="balanceOf(address)")[:4]
                        + encode(["address"], [safe])
                    ).hex(),
                },
                "latest",
            ],
        )
        self._balances = {
            "eoa": number(eoa),
            "native": number(native),
            "wrapped": number(wrapped),
        }
        self._balance_at = time.time()

    def _broadcast(self, signed: Any, raw: str) -> None:
        """Send through both connections before waiting for either result.

        :param signed: decision-maker signed transaction response.
        :param raw: signed EIP-1559 transaction bytes as hex.
        """
        info = self.context.shared_state.get("lightning_lane_inflight", {})
        tx = info.get("tx")
        message, dialogue = self._request("eth_sendRawTransaction", [raw])
        nonce = self._get_request_nonce_from_dialogue(dialogue)
        callbacks = self.context.requests.request_id_to_callback
        callbacks[nonce] = lambda response, _behaviour: self._receive_send(response, tx)
        self.context.outbox.put_message(message=message)
        self._stats.called(time.time())
        message, dialogue = self.context.ledger_api_dialogues.create(
            counterparty=LEDGER_API_ADDRESS,
            performative=LedgerApiMessage.Performative.SEND_SIGNED_TRANSACTION,
            signed_transaction=signed.signed_transaction,
            kwargs=LedgerApiMessage.Kwargs({"chain_id": self.params.mech_chain_id}),
        )
        nonce = self._get_request_nonce_from_dialogue(dialogue)
        callbacks[nonce] = lambda response, _behaviour: self._receive_ledger_send(
            response, tx
        )
        self.context.outbox.put_message(message=message)

    def _receive_send(self, response: Any, tx: str) -> None:
        """Keep malformed RPC responses within the independent lane.

        :param response: HTTP broadcast response.
        :param tx: transaction hash bound to this callback.
        """
        if self.context.shared_state.get("lightning_lane_inflight", {}).get("tx") != tx:
            return
        try:
            body = json.loads(response.body)
            if not isinstance(body, dict) or not isinstance(
                body.get("error", {}), dict
            ):
                raise ValueError("Invalid send result")
            if not isinstance(body.get("error", {}).get("message", ""), str):
                raise ValueError("Invalid send error")
            self._handle_send_result(body, tx)
        except (OSError, ValueError, TypeError, KeyError):
            self._stats.called(time.time(), error=True)
            self.context.logger.warning("[LIGHTNING_LANE] broadcast_error=true")

    def _log_drop(self, info: Dict[str, Any], reason: str) -> None:
        """Keep all result fields for a transaction that has no receipt.

        :param info: saved shot metadata.
        :param reason: local drop reason.
        """
        created = info.get("row", {}).get("creationTimestamp")
        age = time.time() - number(created) if created is not None else "unknown"
        self.context.logger.info(
            "[LIGHTNING_LANE_RESULT] market=%s tx=%s status=dropped "
            "gas_used=0 fee_xdai=0 index=unknown block_age=%s reason=%s",
            info.get("market", "unknown"),
            info.get("tx"),
            age,
            reason,
        )

    def _receive_ledger_send(self, response: Any, tx: str) -> None:
        """Reconcile ledger errors against the same shot as the RPC callback.

        :param response: ledger broadcast response.
        :param tx: transaction hash bound to this callback.
        """
        if response.performative == LedgerApiMessage.Performative.ERROR:
            self._handle_send_result({"error": {"message": response.message}}, tx)

    def _handle_send_result(self, body: Dict[str, Any], tx: str) -> None:
        """Keep duplicate send errors locked until the shot is reconciled.

        :param body: decoded JSON-RPC send response.
        :param tx: transaction hash bound to this callback.
        """
        info = self.context.shared_state.get("lightning_lane_inflight")
        if not info or info.get("tx") != tx:
            return
        error = body.get("error", {}).get("message", "").lower()
        if "nonce too low" in error:
            info["reconcile"] = True
        elif error and "already known" not in error:
            self._stats.called(time.time(), error=True)
            self.context.logger.warning("[LIGHTNING_LANE] broadcast_error=true")

    def _send(
        self,
        market: Dict[str, Any],
        decision: Any,
        bounds: Any,
        block: Dict[str, Any],
        seen: float,
    ) -> Generator[None, None, bool]:
        """Sign locally encoded calldata and reserve both nonce domains.

        :param market: complete receipt market row.
        :param decision: pure YES strategy result.
        :param bounds: execution ceiling and minimum tokens.
        :param block: market creation block.
        :param seen: local block detection time.
        :yield: decision-maker signing response.
        :return: whether both broadcast requests were issued.
        """
        shared = self.context.shared_state
        cfg = self.params.lightning
        nonce = shared.get("lightning_lane_eoa_nonce", {})
        if (
            shared.get("lightning_lane_inflight")
            or not self._round_allowed()
            or not nonce
            or nonce["read_at"] <= shared.get("lightning_lane_last_fsm_settlement", 0)
        ):
            self.context.logger.info(
                "[LIGHTNING_LANE] market=%s reason=fsm_busy round=%s",
                market["id"],
                self._round_name(),
            )
            return False
        if (
            time.time() - number(market["creationTimestamp"])
            > cfg.lightning_lane_max_age_s
        ):
            return False
        amount = int(decision.stake * 10**18)
        try:
            data, _ = build_safe_calldata(
                self.context.agent_address,
                self.params.multisend_address,
                market["collateralToken"],
                market["id"],
                amount,
                bounds[1],
                self._balances["wrapped"],
                self._balances["native"],
            )
            tx = fixed_transaction(
                self.context.agent_address,
                self.synchronized_data.safe_contract_address,
                data,
                nonce["nonce"],
                number(block.get("baseFeePerGas", 0)),
                cfg.lightning_lane_gas_limit,
                cfg.lightning_lane_tip_gwei,
                self._balances["eoa"],
                cfg.lightning_lane_tip_cap_gwei,
            )
        except (KeyError, ValueError):
            self.context.logger.info(
                "[LIGHTNING_LANE] market=%s reason=insufficient_funds_or_tip",
                market["id"],
            )
            return False
        info = dict(
            tx=None,
            market=market["id"],
            eoa_nonce=nonce["nonce"],
            sent_at=time.time(),
            row=market,
            amount=amount,
            probability=decision.p_hat,
        )
        shared["lightning_lane_inflight"] = info
        try:
            self._persist()
        except OSError:
            shared.pop("lightning_lane_inflight", None)
            raise
        terms = Terms(
            self.params.mech_chain_id,
            self.context.agent_address,
            counterparty_address="",
            amount_by_currency_id={},
            quantities_by_good_id={},
            nonce="",
        )
        message, dialogue = self.context.signing_dialogues.create(
            counterparty=self.context.decision_maker_address,
            performative=SigningMessage.Performative.SIGN_TRANSACTION,
            raw_transaction=RawTransaction(self.context.default_ledger_id, tx),
            terms=terms,
        )
        try:
            response = yield from request_with_timeout(
                self,
                message,
                dialogue,
                timeout=2.0,
                send=self.context.decision_maker_message_queue.put_nowait,
            )
            if response.performative != SigningMessage.Performative.SIGNED_TRANSACTION:
                raise ValueError("Signing failed")
            body = response.signed_transaction.body
            raw = body["raw_transaction"]
            info["tx"] = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
            signed_at = time.time()
            if (
                signed_at - number(market["creationTimestamp"])
                > cfg.lightning_lane_max_age_s
                or not self.params.fast.in_window(signed_at)
                or number(market["openingTimestamp"]) <= signed_at
            ):
                shared.pop("lightning_lane_inflight", None)
                self._persist()
                return False
            info["sent_at"] = time.time()
            self._shots += 1
            try:
                self._persist()
            except OSError:
                shared.pop("lightning_lane_inflight", None)
                self._shots -= 1
                raise
            self._broadcast(response, raw)
        except (TimeoutException, ValueError, KeyError, TypeError):
            shared.pop("lightning_lane_inflight", None)
            self._persist()
            return False
        self.context.logger.info(
            "[LIGHTNING_LANE] market=%s block=%s seen_s=%.3f sent_s=%.3f tip_gwei=%s stake=%s local_price=%s ceiling=%s min_out=%s tx=%s",
            market["id"],
            number(block["number"]),
            seen - number(market["creationTimestamp"]),
            time.time() - seen,
            cfg.lightning_lane_tip_gwei,
            decision.stake,
            decision.price,
            bounds[0],
            bounds[1],
            info["tx"],
        )
        return True

    def _complete(
        self, info: Dict[str, Any], receipt: Dict[str, Any], block: Dict[str, Any]
    ) -> None:
        """Apply settled bet bookkeeping once and release the nonce guard.

        :param info: saved signed shot and market metadata.
        :param receipt: mined transaction receipt.
        :param block: receipt block timestamp context.
        """
        shared = self.context.shared_state
        info["receipt"], info["receipt_block"] = receipt, block
        self._persist()
        gas = number(receipt.get("gasUsed", 0))
        fee = gas * number(receipt.get("effectiveGasPrice", 0)) / 10**18
        age = number(block["timestamp"]) - number(info["row"]["creationTimestamp"])
        if number(receipt["status"]) == 1:
            key = "omen:" + info["tx"]
            counted = key in self.read_counted_placement_keys()
            if not counted:
                row = info["row"].copy()
                for field in (
                    "block_hash",
                    "block_number",
                    "conditional_tokens",
                    "question",
                ):
                    row.pop(field, None)
                condition = row.pop("condition", None)
                row["condition_id"] = condition["id"] if condition else None
                bet = Bet(**row, market="omen_subgraph")
                self._update_bet_strategy(bet)
                strategy = bet.strategy
                self.read_bets()
                existing = next(
                    (stored for stored in self.bets if stored.id == bet.id), None
                )
                if existing is None:
                    self.bets.append(bet)
                else:
                    bet = existing
                bet.strategy_vote = 0
                bet.prediction_response = PredictionResponse(
                    p_yes=info["probability"],
                    p_no=1 - info["probability"],
                    confidence=1.0,
                    info_utility=1.0,
                )
                bet.processed_timestamp = number(block["timestamp"])
                bet.queue_status = QueueStatus.PROCESSED
                bet.set_investment_amount(0, info["amount"])
                bet.strategy = strategy
                # Keep the saved receipt if the investment write fails.
                write_json_atomically(
                    Path(self.multi_bets_filepath),
                    json.loads(json.dumps(self.bets, cls=BetsEncoder)),
                )
            record_trade(
                self.params.store_path, number(block["timestamp"]), info["market"], key
            )
            if not counted:
                self.record_successful_placement(key)
            if key not in self.read_counted_placement_keys():
                self.context.logger.warning(
                    "[LIGHTNING_LANE] completion_pending=true reason=trade_count_write"
                )
                return
            shared.setdefault("lightning_lane_bet_markets", set()).add(info["market"])
            queue = FreshQueue(self.params.store_path / "fresh_watch.json")
            queue.pending = [
                row for row in queue.pending if row["id"] != info["market"]
            ]
            queue.asked.pop(info["market"], None)
            queue.save()
            shared["fast_activity_status"] = None
            self.context.state.fast_activity_status = None
            fill = buy_result(receipt.get("logs", []), info["market"]) or {}
            self.context.logger.info(
                "[LIGHTNING_LANE_RESULT] market=%s tx=%s status=mined index=%s block_age=%s fill_price=%s tokens=%s gas_used=%s fee_xdai=%s",
                info["market"],
                info["tx"],
                number(receipt.get("transactionIndex", 0)),
                age,
                fill.get("fill_price"),
                fill.get("tokens"),
                gas,
                fee,
            )
        else:
            self.context.logger.info(
                "[LIGHTNING_LANE_RESULT] market=%s tx=%s status=reverted gas_used=%s fee_xdai=%s index=%s block_age=%s",
                info["market"],
                info["tx"],
                gas,
                fee,
                number(receipt.get("transactionIndex", 0)),
                age,
            )
        shared.pop("lightning_lane_inflight", None)
        shared.pop("lightning_lane_eoa_nonce", None)
        self._balance_at = 0
        self._persist()

    def _watch_receipt(self, block: Dict[str, Any]) -> Generator[None, None, None]:
        """Keep a shot locked until its receipt or a confirmed replacement.

        :param block: newly detected block.
        :yield: receipt and confirmed nonce reads.
        """
        shared = self.context.shared_state
        info = shared.get("lightning_lane_inflight")
        if not info:
            return
        reconcile = info.pop("reconcile", False)
        receipt = info.get("receipt")
        if receipt is None and info.get("tx"):
            receipt = yield from self._rpc("eth_getTransactionReceipt", [info["tx"]])
        if receipt is None and (
            time.time() - info["sent_at"]
            >= self.params.lightning.lightning_lane_inflight_timeout_s
            or reconcile
        ):
            nonce = yield from self._rpc(
                "eth_getTransactionCount", [self.context.agent_address, "latest"]
            )
            if shared.get("lightning_lane_inflight") is not info:
                return
            if number(nonce) > info["eoa_nonce"]:
                if info.get("tx"):
                    receipt = yield from self._rpc(
                        "eth_getTransactionReceipt", [info["tx"]]
                    )
                if shared.get("lightning_lane_inflight") is not info:
                    return
                if receipt is None:
                    shared.pop("lightning_lane_inflight", None)
                    shared.pop("lightning_lane_eoa_nonce", None)
                    self._persist()
                    self._log_drop(info, "dropped_replaced")
                    return
            elif time.time() - info.get("pending_logged_at", 0) >= 60:
                info["pending_logged_at"] = time.time()
                self.context.logger.info(
                    "[LIGHTNING_LANE] market=%s tx=%s status=pending",
                    info.get("market"),
                    info.get("tx"),
                )
        if receipt is not None:
            if receipt.get("blockNumber") != block.get("number"):
                block = yield from self._rpc(
                    "eth_getBlockByNumber", [receipt["blockNumber"], False]
                )
            if shared.get("lightning_lane_inflight") is info:
                self._complete(info, receipt, block)

    def _market(
        self, log: Dict[str, Any], block: Dict[str, Any], seen: float
    ) -> Generator[None, None, None]:
        """Decode one creation receipt and decide within the detection tick.

        :param log: raw factory creation event.
        :param block: creation block.
        :param seen: local block detection time.
        :yield: receipt, optional balance read, and signing I/O.
        """
        if self.context.shared_state.get("lightning_lane_inflight"):
            return
        market = creation_details(log)
        if market["collateralToken"].lower() != WXDAI.lower():
            return
        if not self._round_allowed():
            self.context.logger.info(
                "[LIGHTNING_LANE] market=%s reason=fsm_busy round=%s",
                market["id"],
                self._round_name(),
            )
            return
        receipt = yield from self._rpc(
            "eth_getTransactionReceipt", [log["transactionHash"]]
        )
        if not receipt:
            return
        question = receipt_question(receipt["logs"], market["condition"]["id"])
        if not question:
            return
        market.update(
            question,
            creationTimestamp=number(block["timestamp"]),
            block_hash=block["hash"],
            block_number=number(block["number"]),
        )
        balances = funding_balances(receipt["logs"], market["id"])
        if balances is None:
            target, data = pool_balances_call(
                market["id"],
                market["conditional_tokens"],
                market["collateralToken"],
                market["condition"]["id"],
            )
            raw = yield from self._rpc(
                "eth_call", [{"to": target, "data": data}, "latest"]
            )
            balances = list(decode(["uint256[]"], bytes.fromhex(raw[2:]))[0])
        if len(balances) != 2 or min(balances) <= 0:
            return
        market.update(
            outcomeTokenAmounts=balances,
            outcomeTokenMarginalPrices=[
                balances[1] / sum(balances),
                balances[0] / sum(balances),
            ],
            scaledLiquidityMeasure=min(balances) / 10**18,
        )
        self.read_bets()
        if market["id"] in self.context.shared_state.get(
            "lightning_lane_bet_markets", set()
        ) or any(b.id == market["id"] and b.invested_amount > 0 for b in self.bets):
            return
        result, bounds, reason = lane_decision(
            market,
            balances,
            time.time(),
            self.params.fast,
            self.params.lightning,
            BaseRateCache(self.params.store_path / "lightning_base_rate.json"),
            self.durable_trade_count(),
            self.params.max_trades,
            self.params.placement_min_edge,
        )
        if reason or result is None or bounds is None:
            self.context.logger.info(
                "[LIGHTNING_LANE] market=%s reason=%s", market["id"], reason
            )
            return
        yield from self._send(market, result, bounds, block, seen)

    def async_act(self) -> Generator:
        """Drive one slot-aligned step without a consensus payload."""
        now = time.time()
        cfg = self.params.lightning
        info = self.context.shared_state.get("lightning_lane_inflight")
        if info:
            try:
                if info.get("receipt") and info.get("receipt_block"):
                    self._complete(info, info["receipt"], info["receipt_block"])
                else:
                    yield from self._watch_receipt({})
            except (LaneRPCError, OSError, ValueError, KeyError, TypeError):
                self.context.logger.warning("[LIGHTNING_LANE] tick_failed=true")
            if self.context.shared_state.get("lightning_lane_inflight"):
                yield from self.sleep(0.5)
            return
        if not self._enabled() or not self.params.fast.in_window(now):
            yield from self.sleep(1)
            return
        report = self._stats.report(now)
        if report:
            self.context.logger.info(report)
        if not self._stats.ready(now):
            yield from self.sleep(0.5)
            return
        try:
            self._persist()
            if not self._checked and not (yield from self._startup()):
                return
            if not self._safe_ok:
                yield from self.sleep(1)
                return
            if self._poller is None:
                self._poller = SlotPoller(
                    cfg.lightning_poll_offset_s, cfg.lightning_poll_step_s
                )
            request = self._poller.request(time.time())
            if request is None:
                yield from self._refresh_idle(time.time())
                yield from self.sleep(
                    min(0.25, max(0, self._poller.next_at - time.time()))
                )
                return
            method, params = request
            result = yield from self._rpc(method, params)
            if method == "eth_blockNumber":
                self._poller.height(result, time.time())
                yield from self._refresh_idle(time.time())
                return
            block = self._poller.block(result, time.time())
            if block is None:
                return
            seen = time.time()
            self._stats.detected(number(block["timestamp"]), seen)
            yield from self._watch_receipt(block)
            if self.context.shared_state.get(
                "lightning_lane_inflight"
            ) or not self.params.fast.in_window(time.time()):
                return
            night = preview_night(time.time(), self.params.fast.active_window_utc)
            if night != self._night:
                self._night, self._shots = night, 0
                self._persist()
            if 0 <= cfg.lightning_lane_max_per_night <= self._shots:
                return
            logs = yield from self._rpc(
                "eth_getLogs",
                [
                    creation_filter(
                        block["hash"], cfg.omen_fpmm_factory, cfg.lightning_creators
                    )
                ],
            )
            for log in logs:
                if log.get("removed", False):
                    continue
                yield from self._market(log, block, seen)
                if self.context.shared_state.get("lightning_lane_inflight"):
                    break
            yield from self._refresh_idle(time.time())
        except (LaneRPCError, OSError, ValueError, KeyError, TypeError):
            self.context.logger.warning("[LIGHTNING_LANE] tick_failed=true")
            yield from self.sleep(0.25)
