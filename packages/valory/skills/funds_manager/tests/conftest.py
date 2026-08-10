# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2025 Valory AG
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

"""This module contains the conftest for the 'funds_manager' skill tests."""

from typing import Generator
from unittest import mock

import pytest

from packages.valory.skills.funds_manager.tests import data_for_tests


@pytest.fixture(params=["trader", "optimus"])
def funds_dataset(request: pytest.FixtureRequest) -> dict:
    """Provide parameterized datasets for funds tests."""
    if request.param == "trader":
        return {
            "fund_requirements": data_for_tests.TRADER_INITIAL_FUND_REQUIREMENTS,
            "funds_response": data_for_tests.TRADER_FUNDS_RESPONSE,
            "multicall": data_for_tests.TRADER_MULTICALL_RETURN_VALUES,
            "id": request.param,
        }
    if request.param == "optimus":
        return {
            "fund_requirements": data_for_tests.OPTIMUS_INITIAL_FUND_REQUIREMENTS,
            "funds_response": data_for_tests.OPTIMUS_FUNDS_RESPONSE,
            "multicall": data_for_tests.OPTIMUS_MULTICALL_RETURN_VALUES,
            "id": request.param,
        }
    raise ValueError(f"Unsupported param: {request.param}")


@pytest.fixture
def mock_safe_address() -> str:
    """Return the mock safe address for tests."""
    return data_for_tests.MOCK_SAFE_ADDRESS


@pytest.fixture(autouse=True)
def patch_agent_address(request: pytest.FixtureRequest) -> Generator:
    """Automatically patch `agent_address` for all tests in the class."""
    test_instance = getattr(request, "instance", None)
    if test_instance is None or not hasattr(test_instance, "behaviour"):
        # skip fixture for tests without behaviour
        yield
        return

    with mock.patch.object(
        type(test_instance.behaviour.context),
        "agent_address",
        new_callable=mock.PropertyMock,
        return_value=data_for_tests.MOCK_AGENT_ADDRESS,
    ):
        yield
