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

"""Recover preview requests that cannot still complete in this process."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import FreshWatchBehaviour
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.models import SharedState
from packages.valory.skills.fresh_watch_abci.preview import PreviewStore
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)


def test_shared_state_records_process_start() -> None:
    """Use one process start time across foreground behaviour replacements."""
    with patch("time.time", return_value=1000):
        state = SharedState(name="state", skill_context=MagicMock())
    assert state.fast_started_at == 1000


@pytest.mark.parametrize(
    "started,requested,now,timeout",
    [
        (1051, 1050, 1060, 300),
        (1050, 1050, 1060, 300),
        (1000, 1050, 1350, 300),
        (1000, 1070, 1060, 300),
        (1000, 1050, 1080, 30),
    ],
)
def test_abandoned_preview_uses_normal_request(
    tmp_path: Path, started: float, requested: float, now: float, timeout: float
) -> None:
    """Release the first market and keep later markets queued after a stale request."""
    behaviour = make(FreshWatchBehaviour, tmp_path)
    behaviour.context.params.lightning = LightningConfig(lightning_preview=True)
    behaviour.context.state.fast_started_at = started
    behaviour.context.state.fast_background_running = True
    behaviour.context.params.mech_marketplace_config.offchain_poll_timeout_seconds = (
        timeout
    )
    behaviour._fast_tool = lambda: "superforcaster"
    behaviour._get_pool_balances = lambda *args: done([100 * 10**18] * 2)
    behaviour.sleep = MagicMock(side_effect=lambda delay: done(None))
    market = {**raw_market(), "title": "Will event X happen?"}
    later = {**raw_market(), "id": "later", "creationTimestamp": 1001}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [market, later]
    queue.save()
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": market["title"]}]}}, 1040, 1040)
    store.reserve(market["title"], requested, "night", 15)
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time", return_value=now
    ):
        selection = run(behaviour._select(False, True))
    assert selection["lane"] == "market"
    assert selection["mech_tool"] == "superforcaster"
    assert FreshQueue(queue.path).pending == [later]
    latest = PreviewStore(store.path)
    assert latest.entries[market["title"]]["failed_at"] == now
    assert latest.match(market["title"], now) is None
    assert latest.counts == {"night": 1}
    behaviour.sleep.assert_not_called()
