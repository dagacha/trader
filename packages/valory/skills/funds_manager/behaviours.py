# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2025-2026 Valory AG
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
"""This module contains the behaviours for the 'funds_manager' skill."""

import copy
from typing import Any, Dict, List, Optional, Tuple, cast

import requests
from aea.skills.behaviours import SimpleBehaviour
from eth_abi import decode as abi_decode  # type: ignore[import-not-found]
from eth_abi import encode as abi_encode  # type: ignore[import-not-found]
from eth_abi.exceptions import (  # type: ignore[import-not-found]
    DecodingError,
)
from eth_utils import (  # type: ignore[import-not-found]
    function_signature_to_4byte_selector,
)
from w3multicall.multicall import W3Multicall  # type: ignore[import-not-found]
from web3 import Web3
from web3.providers.rpc.utils import ExceptionRetryConfiguration

from packages.valory.skills.funds_manager.models import (
    AGENT_ACCOUNT_NAME,
    AccountRequirements,
    ChainRequirements,
    FundRequirements,
    Params,
    TokenRequirement,
)

ERC20_DECIMALS_ABI = "decimals()(uint8)"
NATIVE_BALANCE_ABI = "getEthBalance(address)(uint256)"
ERC20_BALANCE_ABI = "balanceOf(address)(uint256)"
NATIVE_DECIMALS = 18

MULTICALL_ADDR = "0xcA11bde05977b3631167028862bE2a173976CA11"

GET_FUNDS_STATUS_METHOD_NAME = "get_funds_status"

_TRY_AGGREGATE_SIGNATURE = "tryAggregate(bool,(address,bytes)[])"
_TRY_AGGREGATE_SELECTOR = function_signature_to_4byte_selector(_TRY_AGGREGATE_SIGNATURE)
_TRY_AGGREGATE_INPUT_TYPES = ["bool", "(address,bytes)[]"]
_TRY_AGGREGATE_OUTPUT_TYPES = ["(bool,bytes)[]"]

_RETRY_ON_EXCEPTIONS: Tuple[type, ...] = (
    ConnectionError,
    requests.HTTPError,
    requests.Timeout,
)


def _decode_call_result(
    call: W3Multicall.Call, returndata: bytes
) -> Tuple[Optional[int], Optional[Exception]]:
    """Decode a single-output sub-call as an int.

    All call signatures dispatched by this skill return one uint, so the
    decoded value is unwrapped to a scalar `int`. Returns ``(None, exc)``
    when decoding raises, so the caller can surface the failure cause.

    :param call: the multicall sub-call whose output is being decoded.
    :param returndata: the raw bytes returned for the sub-call.
    :return: ``(decoded_int, None)`` on success, or ``(None, exc)`` on failure.
    """
    try:
        decoded = abi_decode(call.output_types, returndata)
    except (DecodingError, ValueError, OverflowError) as exc:
        return None, exc
    return int(decoded[0]), None


class FundsManagerBehaviour(SimpleBehaviour):
    """FundsManagerBehaviour"""

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the behaviour."""
        super().__init__(**kwargs)
        self._web3_by_rpc_url: Dict[str, Web3] = {}

    def act(self) -> None:
        """Do the action."""

    def setup(self) -> None:
        """Set up the behaviour."""
        super().setup()
        self.context.shared_state[GET_FUNDS_STATUS_METHOD_NAME] = self.get_funds_status

    def _get_web3(self, rpc_url: str) -> Web3:
        """Return a cached Web3 instance for the given RPC URL."""
        cached = self._web3_by_rpc_url.get(rpc_url)
        if cached is not None:
            return cached

        provider = Web3.HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": self.params.rpc_timeout_seconds},
            exception_retry_configuration=ExceptionRetryConfiguration(
                errors=_RETRY_ON_EXCEPTIONS,
                retries=self.params.rpc_max_retries,
            ),
        )
        w3 = Web3(provider)
        self._web3_by_rpc_url[rpc_url] = w3
        return w3

    def _perform_try_aggregate(
        self, rpc_url: str, calls: List[W3Multicall.Call]
    ) -> List[Optional[int]]:
        """Execute a Multicall3 tryAggregate.

        Returns a list the same length as `calls`. Each entry is the decoded
        uint, or None if the sub-call reverted or failed to decode.

        :param rpc_url: the RPC endpoint to query.
        :param calls: the multicall sub-calls to execute.
        :return: one decoded ``int`` (or ``None``) per input call.
        """
        if not calls:
            return []

        w3 = self._get_web3(rpc_url)

        call_tuples = [(call.address, call.data) for call in calls]
        encoded_args = abi_encode(_TRY_AGGREGATE_INPUT_TYPES, [False, call_tuples])
        data = _TRY_AGGREGATE_SELECTOR + encoded_args

        rpc_response = w3.eth.call({"to": MULTICALL_ADDR, "data": data})
        [results] = abi_decode(_TRY_AGGREGATE_OUTPUT_TYPES, rpc_response)

        if len(results) != len(calls):
            raise RuntimeError(
                f"Multicall returned {len(results)} entries for {len(calls)} calls"
            )

        decoded: List[Optional[int]] = []
        for call, (success, returndata) in zip(calls, results):
            if not success:
                self.context.logger.debug(
                    "funds_manager: sub-call reverted " "(address=%s, output_types=%s)",
                    call.address,
                    call.output_types,
                )
                decoded.append(None)
                continue
            value, exc = _decode_call_result(call, returndata)
            if value is None:
                self.context.logger.debug(
                    "funds_manager: decode failed for call to %s "
                    "(output_types=%s, returndata=%s, error=%r)",
                    call.address,
                    call.output_types,
                    returndata.hex(),
                    exc,
                )
            decoded.append(value)
        return decoded

    @property
    def params(self) -> Params:
        """Return the params."""
        return cast(Params, self.context.params)

    @property
    def fund_requirements(self) -> FundRequirements:
        """Return the fund requirements."""
        return self.params.fund_requirements

    def _account_name_to_actual_address(
        self, account_name: str, chain_name: str
    ) -> str:
        """Get the address for the given account name."""
        if account_name == AGENT_ACCOUNT_NAME:
            return self.context.agent_address
        return self.params.safe_contract_addresses[chain_name]

    def _switch_out_account_names_for_addresses(
        self, funds: FundRequirements
    ) -> FundRequirements:
        """Switch out account names for addresses in the given FundRequirements object."""
        funds_with_addresses = copy.deepcopy(funds)

        for chain_name, chain_requirements in funds_with_addresses.items():
            for account_name in list(chain_requirements.accounts.keys()):
                account_address = self._account_name_to_actual_address(
                    account_name, chain_name
                )
                chain_requirements.accounts[account_address] = (
                    chain_requirements.accounts.pop(account_name)
                )

        return funds_with_addresses

    def _get_native_balance_call_tuple(
        self, account_address: str, token_address: str
    ) -> tuple:
        """Get the balance call tuple for the given account and token."""
        return (
            account_address,
            token_address,
            W3Multicall.Call(
                MULTICALL_ADDR,
                NATIVE_BALANCE_ABI,
                [account_address],
            ),
        )

    def _get_erc20_balance_call_tuple(
        self, account_address: str, token_address: str
    ) -> tuple:
        """Get the balance call tuple for the given account and token."""
        return (
            account_address,
            token_address,
            W3Multicall.Call(token_address, ERC20_BALANCE_ABI, [account_address]),
        )

    def _construct_calls(
        self,
        account_address: str,
        account_requirements: AccountRequirements,
    ) -> Tuple:
        """Construct balance calls, decimals calls, and token mapping for the given account."""
        balance_calls = []
        decimals_calls = {}
        token_mapping = {}  # (account_address, token_address) -> TokenRequirement
        for (
            token_address,
            token_requirements,
        ) in account_requirements.tokens.items():
            token_mapping[(account_address, token_address)] = token_requirements

            if token_requirements.is_native:
                # Native: balance call
                balance_calls.append(
                    self._get_native_balance_call_tuple(account_address, token_address)
                )
            else:
                # ERC20: balance call
                balance_calls.append(
                    self._get_erc20_balance_call_tuple(account_address, token_address)
                )
                # ERC20: decimals call
                decimals_calls[token_address] = W3Multicall.Call(
                    token_address, ERC20_DECIMALS_ABI
                )

        return balance_calls, decimals_calls, token_mapping

    def _populate_chain_status(
        self,
        chain_name: str,
        chain_requirements: ChainRequirements,
    ) -> None:
        """Populate balance, deficit, and decimals for every token on a chain.

        Sub-call failure leaves the token's balance/deficit/decimals at None
        (unknown, distinct from zero).

        :param chain_name: the name of the chain to populate.
        :param chain_requirements: the per-chain accounts and token requirements.
        """
        balance_calls: List = []
        decimals_calls: Dict[str, W3Multicall.Call] = {}
        token_mapping: Dict[Tuple, TokenRequirement] = {}
        for (
            account_address,
            account_requirements,
        ) in chain_requirements.accounts.items():
            (
                balance_calls_account,
                decimals_calls_account,
                token_mapping_account,
            ) = self._construct_calls(account_address, account_requirements)
            balance_calls.extend(balance_calls_account)
            decimals_calls.update(decimals_calls_account)
            token_mapping.update(token_mapping_account)

        all_calls = [call for _, _, call in balance_calls] + list(
            decimals_calls.values()
        )
        if not all_calls:
            return

        results = self._perform_try_aggregate(
            self.params.rpc_urls[chain_name], all_calls
        )

        balance_calls_end_index = len(balance_calls)
        balance_results = results[:balance_calls_end_index]
        decimal_results = results[balance_calls_end_index:]

        decimals_map = dict(zip(decimals_calls.keys(), decimal_results))

        for (account_address, token_address, _), balance in zip(
            balance_calls, balance_results
        ):
            token_requirement = token_mapping[(account_address, token_address)]

            if token_requirement.is_native:
                decimals_value: Optional[int] = NATIVE_DECIMALS
            else:
                decimals_value = decimals_map.get(token_address)

            if balance is None or decimals_value is None:
                token_requirement.balance = None
                token_requirement.deficit = None
                token_requirement.decimals = decimals_value
                continue

            balance_int = int(balance)
            deficit = (
                max(token_requirement.topup - balance_int, 0)
                if balance_int < token_requirement.threshold
                else 0
            )
            token_requirement.balance = balance_int
            token_requirement.deficit = deficit
            token_requirement.decimals = decimals_value

    def get_funds_status(self) -> FundRequirements:
        """Get the current funds status, using chain-level multicalls.

        Chains are queried independently. A failing chain's tokens keep
        balance/deficit/decimals at None.

        :return: the funds requirements with balance/deficit/decimals filled per chain.
        """
        funds = self._switch_out_account_names_for_addresses(self.fund_requirements)

        for chain_name, chain_requirements in funds.items():
            try:
                self._populate_chain_status(chain_name, chain_requirements)
            except Exception as exc:  # pylint: disable=broad-except
                self.context.logger.warning(
                    "funds_manager: chain %s multicall failed (%s: %s); "
                    "balance/deficit/decimals left as None for this chain.",
                    chain_name,
                    type(exc).__name__,
                    exc,
                )

        return funds
