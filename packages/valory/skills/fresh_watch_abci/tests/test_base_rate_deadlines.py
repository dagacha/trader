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

"""Bound optional evidence reads and check their age at authorisation."""

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

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
    raw_market,
    run,
)


@pytest.mark.parametrize("slow_stage", ["discovery", "quote"])
def test_expired_base_rate_cannot_authorise_direct_bet(
    tmp_path: Path, slow_stage: str
) -> None:
    """A cache with one second left must expire during a three-second read."""
    b = make(FreshWatchBehaviour, tmp_path)
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=1)
    b.context.params.lightning = cfg
    b.context.state.fast_background_running = False
    clock = [200000.0]
    (tmp_path / "lightning_base_rate.json").write_text(
        json.dumps(
            dict(
                n=1,
                yes=1,
                rate=1.0,
                template_id=2,
                refreshed_at=clock[0] - 172799,
                window_days=cfg.lightning_base_rate_window_days,
                creators=sorted(cfg.lightning_creators),
            )
        )
    )
    market = {
        **raw_market(),
        "creationTimestamp": int(clock[0]) - 10,
        "openingTimestamp": int(clock[0]) + 1000,
    }
    b._refresh_base_rate = lambda _now: done(None)
    b._fast_tool = lambda: "superforcaster"

    def poll(_queue: Any, _now: float) -> Any:
        yield
        if slow_stage == "discovery":
            clock[0] += 3
        return [market]

    def quote(*_args: Any) -> Any:
        yield
        if slow_stage == "quote":
            clock[0] += 3
        return [100 * 10**18, 100 * 10**18]

    b._poll_markets = poll
    b._get_pool_balances = quote
    with patch("time.time", side_effect=lambda: clock[0]):
        selection = run(b._select(False, True))
    assert selection["lane"] == "market"
    assert selection["mech_tool"] == "superforcaster"
    assert "lightning_probability" not in selection


def test_missing_base_rate_http_reply_does_not_block_discovery(tmp_path: Path) -> None:
    """A lost refresh reply must leave the poller free within five seconds."""
    b = make(FreshMarketPoller, tmp_path)
    b.context.params.lightning = LightningConfig(
        lightning_still_hold=True, lightning_chain_detect=True
    )
    b.setup = lambda: None
    b.async_act_wrapper = b.async_act
    b.context.omen_subgraph.get_spec.return_value = {
        "method": "POST",
        "url": "https://omen.test",
    }
    b.context.requests = SimpleNamespace(request_id_to_callback={})
    dialogue = SimpleNamespace(
        dialogue_label=SimpleNamespace(dialogue_reference=("refresh", ""))
    )
    b._build_http_request_message = MagicMock(return_value=(MagicMock(), dialogue))
    b._get_default_terms = lambda: None
    b.chain_markets = MagicMock(side_effect=lambda _now: done([]))
    seconds = [0.0]
    epoch = datetime.datetime(2026, 1, 1, 1)

    class Clock(datetime.datetime):
        """Drive the real scheduler's sleep and request deadline together."""

        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            """Return the current scheduler time."""
            return cls.fromtimestamp(epoch.timestamp() + seconds[0], tz=tz)

    with (
        patch(
            "packages.valory.skills.abstract_round_abci.behaviour_utils.datetime.datetime",
            Clock,
        ),
        patch("time.monotonic", side_effect=lambda: seconds[0]),
        patch("time.time", side_effect=lambda: epoch.timestamp() + seconds[0]),
    ):
        b.act()
        for tick in range(1, 32):
            seconds[0] = tick * 0.2
            b.act()
            if b.chain_markets.called:
                break
    b.chain_markets.assert_called_once()
    assert seconds[0] <= 6.2
    assert "refresh" in b.context.requests.request_id_to_callback
    b.context.requests.request_id_to_callback.pop("refresh")(None, b)
    assert b.context.requests.request_id_to_callback == {}
    assert FreshQueue(tmp_path / "fresh_watch.json").pending == []
    b.stop()


@pytest.mark.parametrize("background", [False, True])
def test_discovery_commit_uses_clock_after_io(tmp_path: Path, background: bool) -> None:
    """A market that closes during discovery must not enter the queue."""
    b = make(FreshMarketPoller if background else FreshWatchBehaviour, tmp_path)
    b.context.params.lightning = LightningConfig(lightning_chain_detect=True)
    b.context.state.fast_background_running = False
    b._refresh_base_rate = lambda _now: done(None)
    b._fast_tool = lambda: "superforcaster"
    b._get_pool_balances = MagicMock(side_effect=lambda *_args: done([1, 1]))
    b.sleep = lambda _delay: done(None)
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.last_housekeeping = 1050
    queue.save()
    clock = [1050.0]
    market = {**raw_market(), "openingTimestamp": 1052}

    def poll(_queue: Any, _now: float) -> Any:
        yield
        clock[0] = 1053
        return [market]

    b._poll_markets = poll
    with patch("time.time", side_effect=lambda: clock[0]):
        run(b.async_act() if background else b._select(False, True))
    saved = FreshQueue(queue.path)
    assert saved.pending == []
    assert saved.asked == {}
    b._get_pool_balances.assert_not_called()
