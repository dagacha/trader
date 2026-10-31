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

"""Test still-hold qualification, resolved evidence, and fast sizing."""

from dataclasses import replace
from typing import Any

import pytest

from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
    lightning_config,
    still_hold_rule,
)
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, decide


def resolved(
    title: str = "Will it still hold?", created: int = 1000, payout: Any = None
) -> Any:
    """Build a resolved subgraph market."""
    return dict(
        id=str(created),
        title=title,
        creator=FastConfig().creators[0],
        creationTimestamp=created,
        outcomes=["Yes", "No"],
        question={"templateId": 2},
        payouts=[1, 0] if payout is None else payout,
    )


@pytest.mark.parametrize(
    "title,creator,expected",
    [
        ("Will it STILL HOLD?", FastConfig().creators[0].upper(), "still_hold_phrase"),
        (
            "Will it still hold above ten?",
            FastConfig().creators[1],
            "still_hold_phrase",
        ),
        ("Will it hold?", FastConfig().creators[0], None),
        ("Will it still holder?", FastConfig().creators[0], None),
        ("Will it still hold?", "other", None),
    ],
)
def test_still_hold_rule(title: str, creator: str, expected: Any) -> None:
    """Require the creator and the whole still-hold phrase."""
    assert still_hold_rule(title, creator, LightningConfig()) == expected


def test_base_rate_window_floor_and_sample(tmp_path: Any) -> None:
    """Exclude old, invalid, unrelated, and unresolved markets."""
    cfg = LightningConfig(
        lightning_base_rate_window_days=1, lightning_base_rate_min_n=2
    )
    cache = BaseRateCache(tmp_path / "lightning_base_rate.json")
    now = 100000
    rows = [
        resolved(created=99999),
        resolved(created=99998),
        resolved(created=1),
        resolved(payout=[1, 1]),
        resolved(payout=[]),
        resolved(title="Will it remain?"),
    ]
    cache.update(rows, now, cfg)
    assert cache.sample == 2
    assert cache.rate == 1.0
    assert cache.usable(now, cfg) == 1.0
    assert BaseRateCache(cache.path).usable(now, cfg) == 1.0
    assert cache.usable(now, replace(cfg, lightning_base_rate_min_n=3)) is None
    cache.update([resolved(), resolved(created=999, payout=[0, 1])], 1001, cfg)
    assert cache.usable(1001, cfg) is None
    assert cache.rate == 0.5
    assert cache.usable(1001 + 86401, cfg) is None


def test_usable_base_rate_refresh_outside_window_only(tmp_path: Any) -> None:
    """Refresh once each day and only outside the active window."""
    cache = BaseRateCache(tmp_path / "rate.json")
    cfg = LightningConfig(lightning_still_hold=True, lightning_base_rate_min_n=1)
    fast = FastConfig(active_window_utc="00:00-05:30")
    cache.update([resolved()], 1001, cfg)
    assert not cache.refresh_due(3600, cfg, fast)
    assert not cache.refresh_due(21600, cfg, fast)
    assert cache.refresh_due(21600 + 86400, cfg, fast)
    cache.update([resolved()], 21600 + 86400, cfg)
    assert not cache.refresh_due(21601 + 86400, cfg, fast)
    assert cache.refresh_due(21600 + 172800, cfg, fast)
    assert not cache.refresh_due(21600 + 172800, LightningConfig(), fast)


def test_probability_and_yes_only() -> None:
    """Use the caller's probability and reject an attractive NO side."""
    cfg = replace(FastConfig(), prior_weight=1, max_price=0.93, price_margin=0)
    result = decide(
        "Will it still hold?",
        0.95,
        [10**26, 9 * 10**26],
        [0.9, 0.1],
        cfg,
        0,
        yes_only=True,
    )
    assert result.p_hat == 0.95
    assert result.side == 0
    assert result.stake > 0
    assert result.price <= 0.92
    no = decide(
        "Will it still hold?",
        0.1,
        [10**26, 9 * 10**26],
        [0.9, 0.1],
        cfg,
        0,
        yes_only=True,
    )
    assert no.side == 0
    assert no.stake == 0


def test_prior_parameter_and_off_defaults() -> None:
    """Keep legacy controls by default and apply the named prior when enabled."""
    fast = FastConfig()
    assert lightning_config(fast, LightningConfig()).priors == fast.priors
    adapted = lightning_config(fast, LightningConfig(still_hold_prior=0.95))
    assert adapted.priors["still_hold"] == 0.95
    decision = decide(
        "Will it still hold?", 0.99, [10**26, 10**26], [0.5, 0.5], adapted, 0
    )
    assert decision.p_hat == 0.97
    assert fast.priors["still_hold"] == 0.85


def test_existing_prior_override_is_preserved() -> None:
    """An unused lightning default must preserve a fast prior override."""
    fast = replace(FastConfig(), priors={**FastConfig().priors, "still_hold": 0.8})
    assert lightning_config(fast, LightningConfig()).priors["still_hold"] == 0.8


def test_exact_phrase_overrides_broad_numeric_classifier() -> None:
    """Use the still-hold prior for the exact qualifying phrase."""
    cfg = lightning_config(FastConfig(), LightningConfig(still_hold_prior=0.95))
    result = decide(
        "Will X still hold above ten?",
        0.99,
        [10**26, 10**26],
        [0.5, 0.5],
        cfg,
        0,
        yes_only=True,
    )
    assert result.template == "still_hold"
    assert result.p_hat == 0.97


def test_base_rate_rejects_changed_creators(tmp_path: Any) -> None:
    """Do not reuse evidence for a different creator configuration."""
    cfg = LightningConfig(lightning_base_rate_min_n=1)
    cache = BaseRateCache(tmp_path / "rate.json")
    cache.update([resolved()], 1001, cfg)
    assert cache.usable(1001, cfg) == 1
    assert (
        cache.usable(1001, replace(cfg, lightning_creators=["0x" + "01" * 20])) is None
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lightning_creators": []},
        {"lightning_creators": ["bad"]},
        {"omen_fpmm_factory": "bad"},
    ],
)
def test_invalid_lightning_addresses(kwargs: Any) -> None:
    """Reject empty creators and malformed EVM contract addresses at setup."""
    with pytest.raises(ValueError, match="address"):
        LightningConfig(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lightning_still_hold_max_price": 0},
        {"lightning_still_hold_max_price": 1},
        {"still_hold_prior": -0.01},
        {"still_hold_prior": 1.01},
        {"lightning_base_rate_floor": -0.01},
        {"lightning_base_rate_floor": 1.01},
    ],
)
def test_invalid_lightning_probabilities(kwargs: Any) -> None:
    """Reject invalid price, prior, and evidence probability bounds."""
    with pytest.raises(ValueError, match="probability"):
        LightningConfig(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lightning_base_rate_window_days": 0},
        {"lightning_base_rate_min_n": 0},
        {"lightning_rpc_fallback_s": -1},
        {"lightning_preview_max_per_night": -1},
    ],
)
def test_invalid_lightning_limits(kwargs: Any) -> None:
    """Require a positive evidence sample and nonnegative optional budgets."""
    with pytest.raises(ValueError, match="windows and budgets"):
        LightningConfig(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lightning_poll_step_s": 0},
        {"lightning_poll_offset_s": -0.1},
        {"lightning_lane_tip_gwei": -1},
        {"lightning_lane_tip_cap_gwei": 2001},
        {"lightning_lane_max_age_s": 0},
        {"lightning_lane_gas_limit": 0},
        {"lightning_lane_inflight_timeout_s": 0},
        {"lightning_lane_max_per_night": -2},
    ],
)
def test_invalid_lane_controls(kwargs: Any) -> None:
    """Reject invalid polling, fee, age, gas, and budget controls."""
    with pytest.raises(ValueError, match="Lane fee, polling, and budget"):
        LightningConfig(**kwargs)


def test_lane_gas_limit_requires_at_least_100000() -> None:
    """Reject gas below the minimum and accept the minimum itself."""
    with pytest.raises(ValueError, match="Lane fee, polling, and budget"):
        LightningConfig(lightning_lane_gas_limit=99999)
    assert (
        LightningConfig(lightning_lane_gas_limit=100000).lightning_lane_gas_limit
        == 100000
    )


def test_base_rate_ignores_duplicate_and_invalid_resolutions(tmp_path: Any) -> None:
    """Count each market once and accept only a valid binary resolution."""
    cfg = LightningConfig(lightning_base_rate_min_n=2)
    cache = BaseRateCache(tmp_path / "rate.json")
    rows = [
        resolved(created=1000),
        resolved(created=1000, payout=[0, 1]),
        resolved(created=999, payout=[0, 1]),
        resolved(created=998, payout="1,0"),
        resolved(created=997, payout=[]),
        resolved(created=996, payout=[1, 0, 0]),
        resolved(created=995, payout=["invalid", 0]),
        resolved(created=994, payout=[None, 0]),
        resolved(created=993, payout=[-1, 0]),
        resolved(created=992, payout=[1, 1]),
        resolved(created=991, payout=[0, 0]),
        resolved(created=1002),
    ]
    cache.update(rows, 1001, cfg)
    reloaded = BaseRateCache(cache.path)
    assert reloaded.sample == 2
    assert reloaded.data["yes"] == 1
    assert reloaded.rate == 0.5
    assert reloaded.usable(1001, cfg) is None


def test_base_rate_window_and_clock_must_match(tmp_path: Any) -> None:
    """Use only evidence from the configured window with a valid age."""
    cfg = LightningConfig(lightning_base_rate_min_n=1)
    cache = BaseRateCache(tmp_path / "rate.json")
    cache.update([resolved()], 1001, cfg)
    assert cache.usable(1000, cfg) is None
    assert cache.usable(1001 + 86400, cfg) == 1
    assert cache.usable(1001 + 172800, cfg) == 1
    assert cache.usable(1001 + 172801, cfg) is None
    assert cache.usable(1001, replace(cfg, lightning_base_rate_window_days=20)) is None
