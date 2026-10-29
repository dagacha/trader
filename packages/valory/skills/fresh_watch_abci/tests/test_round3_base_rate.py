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

"""Check base-rate recovery for traders that run only at night."""

from dataclasses import replace
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import FreshWatchBehaviour
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
)
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.fresh_watch_abci.tests.test_lightning import resolved


def test_missing_base_rate_refreshes_at_night_with_bounded_retries(
    tmp_path: Any,
) -> None:
    """Retry a failed night refresh after 30 minutes, including after restart."""
    path = tmp_path / "rate.json"
    cache = BaseRateCache(path)
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=1)
    fast = FastConfig(active_window_utc="00:00-05:30")
    assert cache.refresh_due(3600, cfg, fast)
    cache.reserve_refresh(3600)
    assert not BaseRateCache(path).refresh_due(5399, cfg, fast)
    assert BaseRateCache(path).refresh_due(5400, cfg, fast)
    cache.reserve_refresh(5400)
    assert not cache.refresh_due(5401, cfg, fast)
    assert not cache.refresh_due(7200, LightningConfig(), fast)
    cache.update([resolved()], 7200, cfg)
    assert not cache.refresh_due(9000, cfg, fast)
    assert not cache.refresh_due(21600, cfg, fast)
    assert not cache.refresh_due(3600 + 86400, cfg, fast)
    assert cache.refresh_due(21600 + 86400, cfg, fast)
    cache.reserve_refresh(21600 + 86400)
    assert not cache.refresh_due(21601 + 86400, cfg, fast)


def test_small_completed_base_rate_can_retry_at_night(tmp_path: Any) -> None:
    """Bound retries when a complete refresh still has too little evidence."""
    cache = BaseRateCache(tmp_path / "rate.json")
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=2)
    fast = FastConfig(active_window_utc="00:00-05:30")
    cache.update([resolved()], 3600, cfg)
    assert not cache.refresh_due(5399, cfg, fast)
    assert cache.refresh_due(5400, cfg, fast)


def test_base_rate_remains_usable_for_two_nights(tmp_path: Any) -> None:
    """Accept evidence through 48 hours, then allow an in-window refresh."""
    cache = BaseRateCache(tmp_path / "rate.json")
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=1)
    fast = FastConfig(active_window_utc="00:00-05:30")
    cache.update([resolved()], 3600, cfg)
    assert cache.usable(3600 + 172800, cfg) == 1
    assert cache.usable(3600 + 172801, cfg) is None
    assert cache.refresh_due(3600 + 172801, cfg, fast)


@pytest.mark.parametrize("reason", ["missing", "stale", "small", "below_floor"])
def test_disabled_direct_base_rate_logs_reason_and_uses_mech(
    tmp_path: Any, reason: str
) -> None:
    """Explain why an exact still-hold market falls back to the fast request."""
    now = 3 * 86400 + 1050
    b = make(FreshWatchBehaviour, tmp_path)
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=2)
    b.context.params.lightning = cfg
    cache = BaseRateCache(tmp_path / "lightning_base_rate.json")
    if reason != "missing":
        refreshed = now - 172801 if reason == "stale" else now
        rows = [resolved(created=int(refreshed) - 1)]
        if reason != "small":
            rows.append(
                resolved(
                    created=int(refreshed) - 2,
                    payout=[0, 1] if reason == "below_floor" else [1, 0],
                )
            )
        cache.update(rows, refreshed, cfg)
    market = {
        **raw_market(),
        "creationTimestamp": int(now) - 50,
        "openingTimestamp": int(now) + 10000,
    }
    b._refresh_base_rate = lambda timestamp: done(None)
    b._fetch_fresh = lambda cursor, timestamp: done([market])
    b._get_pool_balances = lambda *args: done([100 * 10**18, 100 * 10**18])
    b._fast_tool = lambda: "superforcaster"
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock),
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=now,
        ),
    ):
        result = run(b._select(False, False))
    assert result["lane"] == "market"
    b.context.logger.info.assert_any_call("[LIGHTNING_BASE_RATE] off reason=%s", reason)


def test_base_rate_recovers_after_failed_night_http_read(tmp_path: Any) -> None:
    """Keep failed night reads bounded and commit a successful retry."""
    b = make(FreshWatchBehaviour, tmp_path)
    b.context.params.fast = replace(
        b.context.params.fast, active_window_utc="00:00-05:30"
    )
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=1)
    b.context.params.lightning = cfg
    b.get_http_response = lambda **kwargs: done(None)
    b.context.omen_subgraph.process_response.side_effect = [None, [resolved()]]
    run(b._refresh_base_rate(3600))
    run(b._refresh_base_rate(5399))
    assert b.context.omen_subgraph.process_response.call_count == 1
    run(b._refresh_base_rate(5400))
    assert b.context.omen_subgraph.process_response.call_count == 2
    assert BaseRateCache(tmp_path / "lightning_base_rate.json").usable(5400, cfg) == 1
