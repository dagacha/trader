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

"""This module contains the tests for valory/fund_managers' models."""

import re
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import pytest

from packages.valory.skills.funds_manager.models import FundRequirements, Params
from packages.valory.skills.funds_manager.tests import data_for_tests

CURRENT_FILE_PATH = Path(__file__).resolve()
PACKAGE_DIR = CURRENT_FILE_PATH.parents[1]


class TestFundRequirementsModel:
    """Test `TestFundRequirementsModel`"""

    @pytest.mark.parametrize(
        "fund_requirements, error_message",
        [
            (
                data_for_tests.INVALID_TRADER_FUND_REQUIREMENTS,
                "chain can only have accounts",
            ),
            ({}, "Fund requirements cannot be empty."),
            (
                data_for_tests.INVALID_TRADER_TOKEN_REQUIREMENTS,
                "1 validation error for TokenRequirement",
            ),
        ],
    )
    def test_initialization_fails_with_wrong_fund_requirements(
        self, fund_requirements: Dict, error_message: str
    ) -> None:
        """Test that the model fails to initialize with wrong fund requirements."""

        with pytest.raises(ValueError, match=re.escape(error_message)):
            FundRequirements.from_dict(fund_requirements)

    def test_initialization_valid(self, funds_dataset: Dict) -> None:
        """Test that valid fund requirements initialize correctly."""
        fund_requirements = funds_dataset["fund_requirements"]
        model = FundRequirements.from_dict(fund_requirements)
        assert isinstance(model, FundRequirements)
        for chain, accounts in fund_requirements.items():
            for account_name, tokens in accounts.items():
                for addr in tokens.keys():
                    assert addr in model.get_response_body()[chain][account_name]


def _build_params_kwargs(**overrides: Any) -> Dict[str, Any]:
    """Build a minimal Params kwargs dict, with optional per-key overrides."""
    kwargs: Dict[str, Any] = {
        "name": "params",
        "skill_context": mock.MagicMock(skill_id="valory/funds_manager:0.1.0"),
        "fund_requirements": data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS,
        "rpc_urls": {"gnosis": "https://mock-rpc.com/gnosis"},
        "safe_contract_addresses": {"gnosis": data_for_tests.MOCK_SAFE_ADDRESS},
    }
    kwargs.update(overrides)
    return kwargs


class TestParamsChainKeyValidation:
    """Cross-validate that every fund chain has a matching rpc + safe entry."""

    def test_accepts_aligned_chains(self) -> None:
        """When every fund chain has rpc_urls and safe entries, init succeeds."""
        Params(**_build_params_kwargs())

    def test_rejects_missing_rpc_url(self) -> None:
        """A chain declared in fund_requirements but absent from rpc_urls is rejected."""
        with pytest.raises(ValueError, match="rpc_urls is missing"):
            Params(**_build_params_kwargs(rpc_urls={}))

    def test_rejects_missing_safe_address(self) -> None:
        """A chain declared in fund_requirements but absent from safe is rejected."""
        with pytest.raises(ValueError, match="safe_contract_addresses is missing"):
            Params(**_build_params_kwargs(safe_contract_addresses={}))

    def test_extra_rpc_url_chains_are_allowed(self) -> None:
        """rpc_urls / safe may list extra chains beyond what fund_requirements needs."""
        Params(
            **_build_params_kwargs(
                rpc_urls={
                    "gnosis": "https://mock-rpc.com/gnosis",
                    "base": "https://mock-rpc.com/base",
                },
                safe_contract_addresses={
                    "gnosis": data_for_tests.MOCK_SAFE_ADDRESS,
                    "base": data_for_tests.MOCK_SAFE_ADDRESS,
                },
            )
        )
