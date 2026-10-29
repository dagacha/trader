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

"""Keep discovery timestamps current after a slow base-rate refresh."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import (
    FreshMarketPoller,
    FreshWatchBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    run,
)
from packages.valory.skills.fresh_watch_abci.tests.test_chain_discovery import (
    CREATED,
    Harness,
    creation_details,
)


@pytest.mark.parametrize("background", [False, True])
def test_slow_base_rate_refresh_keeps_new_chain_market(
    tmp_path: Path, background: bool
) -> None:
    """Discover a creation that lands while resolved evidence is loading."""
    behaviour = make(FreshMarketPoller if background else FreshWatchBehaviour, tmp_path)
    behaviour.context.params.lightning = LightningConfig(
        lightning_still_hold=True, lightning_chain_detect=True
    )
    behaviour.context.state.fast_background_running = False
    behaviour.context.lightning_event_scan = None
    harness = Harness(tmp_path)
    behaviour._chain_rpc = harness._chain_rpc
    behaviour._fast_tool = lambda: "superforcaster"
    behaviour._get_pool_balances = lambda *_args: done([7 * 10**18, 7 * 10**18])
    behaviour.sleep = lambda _delay: done(None)
    clock = [CREATED - 10]

    def refresh(_now: float) -> Any:
        yield
        clock[0] = CREATED + 10

    behaviour._refresh_base_rate = refresh
    with patch("time.time", side_effect=lambda: clock[0]):
        selection = run(
            behaviour.async_act() if background else behaviour._select(False, True)
        )
    market_id = creation_details(harness.logs[0])["id"]
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    if background:
        assert [row["id"] for row in queue.pending] == [market_id]
    else:
        assert selection["lane"] == "market"
        assert market_id in queue.asked
    assert behaviour.context.state.fast_last_poll == CREATED + 10
