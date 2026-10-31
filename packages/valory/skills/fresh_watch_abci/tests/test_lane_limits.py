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

"""Check the runtime meaning of the lane's nightly budget."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import done, run
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MODULE,
    lane,
)


@pytest.mark.parametrize("budget,reads_logs", [(-1, True), (0, False)])
def test_lane_nightly_budget_uncapped_and_explicit_zero(
    tmp_path: Any, budget: int, reads_logs: bool
) -> None:
    """Allow inherited unlimited shots and keep an explicit zero closed."""
    b = lane(tmp_path, lightning_lane_max_per_night=budget)
    b._checked = b._safe_ok = True
    b._night, b._shots = "1970-01-01", 2
    block = {"hash": "0xblock", "timestamp": "0x3e8"}
    b._poller = SimpleNamespace(
        request=lambda _now: ("eth_getBlockByNumber", ["0x1", False]),
        block=lambda _result, _now: block,
    )
    calls = []

    def rpc(method: str, _params: Any) -> Any:
        calls.append(method)
        return done([] if method == "eth_getLogs" else block)

    b._rpc = rpc
    b._watch_receipt = MagicMock(side_effect=lambda _block: done(None))
    b._refresh_idle = MagicMock(side_effect=lambda _now: done(None))
    with patch(MODULE + ".time.time", return_value=1001):
        run(b.async_act())
    assert ("eth_getLogs" in calls) is reads_logs
