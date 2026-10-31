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

"""Keep Safe transaction preparation behind an in-flight lightning shot."""

import json
import time
from typing import Any, Generator, Optional, cast

from packages.valory.protocols.contract_api import ContractApiMessage
from packages.valory.skills.abstract_round_abci.behaviour_utils import TimeoutException
from packages.valory.skills.staking_abci.rpc import request_with_timeout


class LaneGuardMixin:
    """Guard local Safe behaviours without changing upstream packages."""

    def _lane_guard_rpc(
        self: Any, method: str, params: Any
    ) -> Generator[None, None, Any]:
        """Read one private RPC result without exposing endpoint credentials.

        :param method: JSON-RPC method.
        :param params: positional method arguments.
        :yield: control during the response wait.
        :return: decoded RPC result.
        :raises ValueError: when the HTTP request or receipt result is invalid.
        """
        message, dialogue = self._build_http_request_message(
            method="POST",
            url=self.params.lightning.lightning_rpc_url,
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            ).encode(),
        )
        response = yield from request_with_timeout(self, message, dialogue, timeout=5.0)
        if response.status_code != 200:
            raise ValueError("RPC response failed")
        result = json.loads(response.body)["result"]
        if (
            method == "eth_getTransactionReceipt"
            and result is not None
            and not isinstance(result, dict)
        ):
            raise ValueError("Invalid receipt")
        return result

    def wait_for_lightning_lane(
        self: Any, reserve: bool = False
    ) -> Generator[None, None, None]:
        """Wait before preparing a Safe transaction while a lane shot is pending.

        The background receipt watcher clears the flag after bookkeeping.
        After timeout, require a mined EOA nonce before checking replacement.
        Keep waiting if that read fails, and do not log the private RPC URL.

        :param reserve: also exclude shots while a whitelisted round prepares a Safe tx.
        :yield: control during the half-second poll or nonce read.
        """
        config: Any = getattr(self.params, "lightning", None)
        if getattr(config, "lightning_lane", False) is not True:
            return
        shared = self.context.shared_state
        while shared.get("lightning_lane_inflight"):
            inflight = shared["lightning_lane_inflight"]
            if (
                inflight.get("receipt")
                or time.time() - inflight["sent_at"]
                < config.lightning_lane_inflight_timeout_s
            ):
                yield from self.sleep(0.5)
                continue
            try:
                value = yield from self._lane_guard_rpc(
                    "eth_getTransactionCount", [self.context.agent_address, "latest"]
                )
                nonce = int(value, 16)
                if shared.get("lightning_lane_inflight") is not inflight:
                    continue
                if nonce <= inflight["eoa_nonce"]:
                    now = time.time()
                    if now - inflight.get("pending_logged_at", now - 60) >= 60:
                        self.context.logger.info(
                            "[LIGHTNING_LANE] tx=%s status=pending Safe remains blocked",
                            inflight.get("tx"),
                        )
                        inflight["pending_logged_at"] = now
                    yield from self.sleep(0.5)
                    continue
                receipt = yield from self._lane_guard_rpc(
                    "eth_getTransactionReceipt", [inflight["tx"]]
                )
                if shared.get("lightning_lane_inflight") is not inflight:
                    continue
                if receipt is None:
                    shared.pop("lightning_lane_inflight", None)
                    shared.pop("lightning_lane_eoa_nonce", None)
                    self.context.logger.info(
                        "[LIGHTNING_LANE_RESULT] tx=%s status=dropped reason=dropped_replaced",
                        inflight.get("tx"),
                    )
                else:
                    if (
                        receipt.get("blockNumber") is not None
                        and receipt.get("status") is not None
                    ):
                        inflight["receipt"] = receipt
                    yield from self.sleep(0.5)
            except (TimeoutException, ValueError, KeyError, TypeError):
                self.context.logger.warning(
                    "[LIGHTNING_LANE] timeout nonce refresh failed; Safe remains blocked"
                )
                yield from self.sleep(0.5)
        if reserve:
            shared["lightning_lane_fsm_preparing"] = True
            matching_round = getattr(self, "matching_round", None)
            shared["lightning_lane_fsm_preparing_round"] = (
                matching_round.auto_round_id() if matching_round is not None else None
            )

    def async_act_wrapper(self: Any) -> Generator:
        """Wait before upstream Safe transaction behaviours start.

        :yield: control during the guard and normal behaviour.
        """
        config: Any = getattr(self.params, "lightning", None)
        if getattr(
            config, "lightning_lane", False
        ) is True and self.matching_round.auto_round_id() in {
            "mech_request_round",
            "mech_purchase_subscription_round",
            "background_round",
        }:
            yield from self.wait_for_lightning_lane()
        yield from cast(Any, super()).async_act_wrapper()

    def get_contract_api_response(
        self: Any,
        performative: ContractApiMessage.Performative,
        contract_address: Optional[str],
        contract_id: str,
        contract_callable: str,
        ledger_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Generator[None, None, ContractApiMessage]:
        """Reserve the Safe before reading the nonce for a transaction hash.

        :param performative: request performative.
        :param contract_address: target contract address.
        :param contract_id: contract package ID.
        :param contract_callable: contract method.
        :param ledger_id: ledger ID override.
        :param kwargs: contract method inputs.
        :yield: control during the guard and contract read.
        :return: contract API response.
        """
        if contract_callable == "get_raw_safe_transaction_hash":
            yield from self.wait_for_lightning_lane(reserve=True)
        response = yield from cast(Any, super()).get_contract_api_response(
            performative,
            contract_address,
            contract_id,
            contract_callable,
            ledger_id=ledger_id,
            **kwargs,
        )
        if (
            contract_callable == "get_raw_safe_transaction_hash"
            and response.performative != ContractApiMessage.Performative.STATE
        ):
            self.context.shared_state.pop("lightning_lane_fsm_preparing", None)
            self.context.shared_state.pop("lightning_lane_fsm_preparing_round", None)
        return response
