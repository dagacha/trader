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

"""This module contains the tests for valory/fund_managers' behaviour."""

from pathlib import Path
from typing import Any, Dict, cast
from unittest import mock
from unittest.mock import MagicMock

import pytest
from aea.configurations.base import PackageConfiguration
from aea.test_tools.test_skill import BaseSkillTestCase
from eth_abi import encode as abi_encode  # type: ignore[import-not-found]
from web3 import Web3

from packages.valory.skills.funds_manager.behaviours import (
    FundsManagerBehaviour,
    GET_FUNDS_STATUS_METHOD_NAME,
    MULTICALL_ADDR,
)
from packages.valory.skills.funds_manager.models import FundRequirements
from packages.valory.skills.funds_manager.tests import data_for_tests

CURRENT_FILE_PATH = Path(__file__).resolve()
PACKAGE_DIR = CURRENT_FILE_PATH.parents[1]


class TestFundsManagerBehaviour(BaseSkillTestCase):
    """Test `TestFundsManagerBehaviour`."""

    # behaviour: FundsManager  # type: ignore
    path_to_skill = PACKAGE_DIR
    _skill = MagicMock()

    @property
    def behaviour(self) -> FundsManagerBehaviour:
        """Get the behaviour."""
        return cast(FundsManagerBehaviour, self.skill.behaviours["funds_manager"])

    @classmethod
    def setup_class(cls, **kwargs: Any) -> None:
        """Set up the class."""
        kwargs["config_overrides"] = {
            "models": {
                "params": {
                    "args": {
                        "fund_requirements": data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS,
                        "safe_contract_addresses": {
                            "gnosis": data_for_tests.MOCK_SAFE_ADDRESS,
                            "base": data_for_tests.MOCK_SAFE_ADDRESS,
                            "optimism": data_for_tests.MOCK_SAFE_ADDRESS,
                            "mode": data_for_tests.MOCK_SAFE_ADDRESS,
                            "celo": data_for_tests.MOCK_SAFE_ADDRESS_CELO,
                        },
                        "rpc_urls": data_for_tests.MOCK_RPC_URLS,
                    },
                }
            }
        }

        with mock.patch.object(PackageConfiguration, "check_overrides_valid"):
            super().setup_class(**kwargs)

    def setup_method(self, **kwargs: Any) -> None:
        """Setup."""
        super().setup_method(**kwargs)
        self.behaviour.setup()
        for attr in ("_perform_try_aggregate", "_get_web3"):
            self.behaviour.__dict__.pop(attr, None)
        self.behaviour._web3_by_rpc_url = {}

    @pytest.mark.parametrize(
        "account_name, chain_name, expected_address",
        [
            ("agent", "gnosis", data_for_tests.MOCK_AGENT_ADDRESS),
            ("safe", "gnosis", data_for_tests.MOCK_SAFE_ADDRESS),
            ("safe", "celo", data_for_tests.MOCK_SAFE_ADDRESS_CELO),
        ],
    )
    def test_account_name_to_actual_address(
        self, account_name: str, chain_name: str, expected_address: str
    ) -> None:
        """Test the `_account_name_to_actual_address` method."""
        behaviour = self.behaviour
        assert (
            behaviour._account_name_to_actual_address(account_name, chain_name)
            == expected_address
        )

    def test_get_funds_status_exists_in_shared_state(self) -> None:
        """Test the `get_funds_status` method is correctly set in the shared state."""

        assert GET_FUNDS_STATUS_METHOD_NAME in self.behaviour.context.shared_state
        assert callable(
            self.behaviour.context.shared_state[GET_FUNDS_STATUS_METHOD_NAME]
        )

    def test_shared_state_callable_returns_funds_status(
        self, funds_dataset: Dict
    ) -> None:
        """The callable registered in shared_state must reach the same code path."""
        behaviour = self.behaviour
        fund_requirements = funds_dataset["fund_requirements"]
        funds_response = funds_dataset["funds_response"]
        mock_multicall_response = funds_dataset["multicall"]
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            fund_requirements
        )
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=mock_multicall_response
        )

        shared_callable = behaviour.context.shared_state[GET_FUNDS_STATUS_METHOD_NAME]
        funds = shared_callable()

        assert funds.get_response_body() == funds_response

    def test_get_funds_status(self, funds_dataset: Dict) -> None:
        """Test the `get_funds_status` method."""
        behaviour = self.behaviour
        fund_requirements = funds_dataset["fund_requirements"]
        funds_response = funds_dataset["funds_response"]
        mock_multicall_response = funds_dataset["multicall"]
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            fund_requirements
        )

        # patch the instance method
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=mock_multicall_response
        )

        funds = behaviour.get_funds_status()

        assert behaviour._perform_try_aggregate.call_count == len(
            mock_multicall_response
        )

        assert funds.get_response_body() == funds_response

    def test_chain_failure_leaves_other_chains_populated(self) -> None:
        """One chain raising must not abort the loop or wipe other chains' data."""
        behaviour = self.behaviour
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            data_for_tests.OPTIMUS_INITIAL_FUND_REQUIREMENTS
        )
        # 3 chains in OPTIMUS_INITIAL_FUND_REQUIREMENTS; raise on the middle one
        first_chain_results = data_for_tests.OPTIMUS_MULTICALL_RETURN_VALUES[0]
        third_chain_results = data_for_tests.OPTIMUS_MULTICALL_RETURN_VALUES[2]
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=[
                first_chain_results,
                RuntimeError("simulated RPC outage on chain 2"),
                third_chain_results,
            ]
        )

        funds = behaviour.get_funds_status()
        response = funds.get_response_body()

        # First chain: populated as normal.
        first_chain = list(response.values())[0]
        first_token = list(list(first_chain.values())[0].values())[0]
        assert first_token["balance"] is not None
        assert first_token["deficit"] is not None
        assert first_token["decimals"] is not None

        # Second chain: every token has balance=None / deficit=None.
        second_chain = list(response.values())[1]
        for account in second_chain.values():
            for token in account.values():
                assert token["balance"] is None
                assert token["deficit"] is None

        # Third chain: populated, untouched by the second chain's failure.
        third_chain = list(response.values())[2]
        third_token = list(list(third_chain.values())[0].values())[0]
        assert third_token["balance"] is not None
        assert third_token["deficit"] is not None

    def test_sub_call_failure_does_not_become_zero_balance(self) -> None:
        """A None sub-call result must not be treated as a legitimate zero balance."""
        behaviour = self.behaviour
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS
        )
        # Trader chain has 2 native balance calls. Simulate the first reverting.
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=[[None, 2500000000000000000]]
        )

        funds = behaviour.get_funds_status()
        response = funds.get_response_body()
        gnosis = response["gnosis"]

        agent_eth = gnosis[data_for_tests.MOCK_AGENT_ADDRESS][
            "0x0000000000000000000000000000000000000000"
        ]
        safe_eth = gnosis[data_for_tests.MOCK_SAFE_ADDRESS][
            "0x0000000000000000000000000000000000000000"
        ]

        # The reverted sub-call must NOT be reported as "zero balance,
        # deficit=topup"; otherwise the consumer would trigger an
        # unnecessary top-up for a token whose balance is unknown.
        assert agent_eth["balance"] is None
        assert agent_eth["deficit"] is None

        # The other sub-call in the same multicall succeeded and is reported.
        assert safe_eth["balance"] == "2500000000000000000"
        assert safe_eth["deficit"] == "0"

    def test_decimals_failure_marks_token_unknown(self) -> None:
        """A failed ERC20 decimals call must not silently default to 18."""
        behaviour = self.behaviour
        # Two-account ERC20-only-token-on-optimism setup gives us 4 balance
        # calls + 1 decimals call. Make the decimals call fail.
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            {"optimism": data_for_tests.OPTIMUS_INITIAL_FUND_REQUIREMENTS["optimism"]}
        )
        behaviour._perform_try_aggregate = mock.Mock(  # type: ignore[method-assign]
            side_effect=[[0, 2500000000000000, 0, 0, None]]
        )

        funds = behaviour.get_funds_status()
        response = funds.get_response_body()
        usdc_address = "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"

        # Every ERC20 slot (both accounts) must be marked unknown — the shared
        # decimals failure invalidates the token across all accounts.
        for account_address in (
            data_for_tests.MOCK_AGENT_ADDRESS,
            data_for_tests.MOCK_SAFE_ADDRESS,
        ):
            usdc = response["optimism"][account_address][usdc_address]
            assert usdc["balance"] is None
            assert usdc["deficit"] is None
            assert usdc["decimals"] is None

        # Native ETH on the same chain still resolves: it does not need decimals.
        for account_address in (
            data_for_tests.MOCK_AGENT_ADDRESS,
            data_for_tests.MOCK_SAFE_ADDRESS,
        ):
            eth = response["optimism"][account_address][
                "0x0000000000000000000000000000000000000000"
            ]
            assert eth["balance"] == "0"
            assert eth["decimals"] == 18

    def test_get_web3_threads_timeout_and_retry_params(self) -> None:
        """`_get_web3` must build the provider with the configured timeout/retries."""
        behaviour = self.behaviour
        behaviour.context.params.rpc_timeout_seconds = 7
        behaviour.context.params.rpc_max_retries = 4

        w3 = behaviour._get_web3("https://example.invalid/rpc")
        assert isinstance(w3, Web3)

        # The provider should carry the per-request timeout and retry count
        # the skill params declared, not the web3 defaults.
        provider = w3.provider
        assert provider._request_kwargs == {"timeout": 7}
        assert provider.exception_retry_configuration.retries == 4

        # Second call returns the cached instance, not a fresh provider.
        assert behaviour._get_web3("https://example.invalid/rpc") is w3

    def test_perform_try_aggregate_raises_on_length_mismatch(self) -> None:
        """A multicall response with the wrong number of entries must raise."""
        behaviour = self.behaviour
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS
        )

        # Build the two-call list trader/gnosis would dispatch.
        funds = behaviour._switch_out_account_names_for_addresses(
            behaviour.fund_requirements
        )
        balance_calls = []
        for (
            account_address,
            account_requirements,
        ) in funds["gnosis"].accounts.items():
            balance_calls_account, _, _ = behaviour._construct_calls(
                account_address, account_requirements
            )
            balance_calls.extend(balance_calls_account)
        calls = [call for _, _, call in balance_calls]

        # Encode a response with only ONE Result entry for TWO requested calls.
        short_results = [(True, (1).to_bytes(32, "big"))]
        encoded_short_response = abi_encode(["(bool,bytes)[]"], [short_results])

        mock_w3 = MagicMock()
        mock_w3.eth.call.return_value = encoded_short_response
        with mock.patch.object(behaviour, "_get_web3", return_value=mock_w3):
            with pytest.raises(RuntimeError, match="returned 1 entries for 2 calls"):
                behaviour._perform_try_aggregate("https://example.invalid/rpc", calls)

    def test_perform_try_aggregate_real_encode_decode(self) -> None:
        """Exercise the hand-rolled tryAggregate encode/decode against a mocked eth_call."""
        behaviour = self.behaviour
        behaviour.context.params.fund_requirements = FundRequirements.from_dict(
            data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS
        )

        # Build the same call list `get_funds_status` would produce for trader/gnosis:
        # two native balance calls (agent, safe).
        funds = behaviour._switch_out_account_names_for_addresses(
            behaviour.fund_requirements
        )
        gnosis_requirements = funds["gnosis"]
        balance_calls = []
        for (
            account_address,
            account_requirements,
        ) in gnosis_requirements.accounts.items():
            balance_calls_account, _, _ = behaviour._construct_calls(
                account_address, account_requirements
            )
            balance_calls.extend(balance_calls_account)
        calls = [call for _, _, call in balance_calls]

        # Pre-encode a tryAggregate response: first sub-call reverts (success=False),
        # second sub-call returns uint256(2500...) as 32 bytes.
        second_balance = 2500000000000000000
        results_tuple = [
            (False, b""),
            (True, second_balance.to_bytes(32, "big")),
        ]
        encoded_response = abi_encode(["(bool,bytes)[]"], [results_tuple])

        mock_w3 = MagicMock()
        mock_w3.eth.call.return_value = encoded_response
        with mock.patch.object(behaviour, "_get_web3", return_value=mock_w3):
            decoded = behaviour._perform_try_aggregate(
                "https://example.invalid/rpc", calls
            )

        assert decoded == [None, second_balance]
        # The eth_call must have been issued against the multicall contract with
        # the tryAggregate selector as the leading 4 bytes of `data`.
        sent_tx = mock_w3.eth.call.call_args.args[0]
        assert sent_tx["to"] == MULTICALL_ADDR
        assert sent_tx["data"][:4].hex() == "bce38bd7"
