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

"""Test the fast strategy and durable fresh-market queue."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from packages.valory.skills.fresh_watch_abci.strategy import (
    FastConfig,
    FreshQueue,
    decide,
    housekeeping_due,
    template,
)


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Will it stay above 10?", "numeric_threshold"),
        ("Will it still hold?", "still_hold"),
        ("Will it remain?", "still_hold"),
        ("Will the keeper win?", "other_event"),
        *[
            (f"Will it {word} 10?", "numeric_threshold")
            for word in (
                "above",
                "below",
                "at least",
                "at most",
                "more than",
                "less than",
                "or more",
                "or higher",
                "exceed",
            )
        ],
        *[
            (f"Will it {word}?", "still_hold")
            for word in (
                "still",
                "remain",
                "continue",
                "maintain",
                "hold",
                "stay",
                "keep",
            )
        ],
    ],
)
def test_template(title: Any, expected: Any) -> None:
    """Use whole words and give the numeric template priority."""
    assert template(title.upper()) == expected


def test_decision() -> None:
    """Blend the prior, select a side, and size with capped Kelly."""
    cfg = FastConfig()

    def decide_at_spot(
        title: Any, p: Any, prices: Any, requested: Any, cfg: Any
    ) -> Any:
        """Keep price-rule fixtures free of material pool impact."""
        return decide(
            title, p, [int(v * 10**27) for v in reversed(prices)], requested, cfg, 0
        )

    yes = decide_at_spot("Will it still hold?", 0.9, [0.6, 0.4], [0.6, 0.4], cfg)
    assert yes.p_hat == pytest.approx(0.875)
    assert yes.side == 0
    assert yes.edge == pytest.approx(0.275)
    assert yes.stake == 4
    no = decide_at_spot("Will it happen?", 0.1, [0.7, 0.3], [0.7, 0.3], cfg)
    assert no.side == 1
    assert no.stake == 4
    assert decide_at_spot("other", 0.1, [0.5, 0.5], [0.5, 0.5], cfg).stake == 4
    small = decide_at_spot("numeric above", 0.6, [0.5, 0.5], [0.5, 0.5], cfg)
    assert small.stake == pytest.approx(2)
    assert (
        decide_at_spot(
            "numeric above", 0.6, [0.5, 0.5], [0.5, 0.5], replace(cfg, cap=1)
        ).stake
        == 1
    )
    assert (
        decide_at_spot(
            "other", 0.1, [0.7, 0.3], [0.7, 0.3], replace(cfg, prior_weight=1)
        ).p_hat
        == 0.1
    )


@pytest.mark.parametrize(
    "title,p,prices,requested,reason",
    [
        ("above", 0.5, [0.54, 0.46], [0.54, 0.46], "min_edge"),
        ("still", 1, [0.91, 0.09], [0.91, 0.09], "max_price"),
        ("other", 0.5, [0.9, 0.1], [0.9, 0.1], "no_min_p"),
        ("still", 0.9, [0.75, 0.25], [0.5, 0.5], "max_slip"),
        ("still", 0.9, [0, 1], [0.5, 0.5], "invalid_price"),
    ],
)
def test_skip(title: Any, p: Any, prices: Any, requested: Any, reason: Any) -> None:
    """Skip or abort when a strategy bound fails."""
    result = decide(
        title,
        p,
        [int(v * 10**27) for v in reversed(prices)],
        requested,
        FastConfig(),
        0,
    )
    assert result.reason == reason
    assert result.stake == 0


def market(id_: Any, timestamp: Any, creator: str = "allowed") -> Any:
    """Build a small raw market."""
    return dict(
        id=id_, creationTimestamp=timestamp, creator=creator, openingTimestamp=100000
    )


def test_queue(tmp_path: Any) -> None:
    """Filter, deduplicate, sort, and retain asked IDs across restarts."""
    path = tmp_path / "fresh.json"
    queue = FreshQueue(path)
    queue.ingest(
        [
            market("b", 950),
            market("a", 940),
            market("c", 1),
            market("d", 990, "other"),
            market("a", 940),
            market("future", 1001),
        ],
        1000,
        ["ALLOWED"],
        600,
    )
    assert [row["id"] for row in queue.pending] == ["a", "b"]
    assert queue.last_seen == 990
    queue.save()
    assert (FreshQueue(path).pop() or {})["id"] == "a"
    queue = FreshQueue(path)
    assert queue.asked == {"a": 940}
    queue.ingest(
        [market("a", 999), market("b", 950), market("e", 1000)], 1000, ["allowed"], 600
    )
    queue.save()
    assert (queue.pop() or {})["id"] == "b"
    assert (FreshQueue(path).pop() or {})["id"] == "e"
    assert FreshQueue(path).pop() is None


def test_age_boundary(tmp_path: Any) -> None:
    """Accept the maximum age and reject expired markets."""
    queue = FreshQueue(tmp_path / "fresh.json")
    queue.last_seen = 1
    queue.ingest([market("old", 1), market("edge", 400)], 1000, ["allowed"], 600)
    assert [row["id"] for row in queue.pending] == ["edge"]


def test_housekeeping() -> None:
    """Queued work wins; inside the window, housekeeping is rate limited."""
    cfg = FastConfig()
    inside = datetime(2026, 10, 23, 1, tzinfo=timezone.utc).timestamp()
    outside = datetime(2026, 10, 23, 6, tzinfo=timezone.utc).timestamp()
    assert housekeeping_due(inside, 0, False, cfg)
    assert not housekeeping_due(inside, inside - 599, False, cfg)
    assert housekeeping_due(inside, inside - 600, False, cfg)
    assert not housekeeping_due(outside, 0, True, cfg)
    assert housekeeping_due(outside, outside, False, cfg)
    assert not cfg.in_window(
        datetime(2026, 10, 23, 5, 30, tzinfo=timezone.utc).timestamp()
    )
    assert replace(cfg, active_window_utc="23:00-01:00").in_window(inside - 1800)


@pytest.mark.parametrize("created", [1000, 999, 880])
def test_late_market_once(tmp_path: Any, created: int) -> None:
    """Queue a late result once, including at the overlap boundary."""
    path = tmp_path / "fresh.json"
    queue = FreshQueue(path)
    queue.last_seen = 1000
    late = market("late", created)
    queue.ingest([late, late], 1050, ["allowed"], 600)
    assert queue.pending == [late]
    queue.save()
    queue = FreshQueue(path)
    queue.ingest([late], 1060, ["allowed"], 600)
    assert queue.pending == [late]
    assert queue.pop() == late
    queue = FreshQueue(path)
    queue.ingest([late], 1070, ["allowed"], 600)
    assert queue.pending == []
    assert "late" in queue.asked


@pytest.mark.parametrize("overlap", [120, 300])
def test_prune_asked(tmp_path: Any, overlap: int) -> None:
    """Prune only beyond the age plus margin, with restart deduplication."""
    path = tmp_path / "fresh.json"
    queue = FreshQueue(path)
    cutoff = 1000 - 600 - overlap
    rows = [
        market("expired", cutoff - 1),
        market("boundary", cutoff),
        market("fresh", 400),
    ]
    queue.ingest(rows, 400, ["allowed"], 600)
    for row in rows:
        assert queue.pop() == row
    queue = FreshQueue(path)
    queue.ingest([], 1000, ["allowed"], 600, overlap_s=overlap)
    assert set(queue.asked) == {"boundary", "fresh"}
    queue.save()
    queue = FreshQueue(path)
    queue.ingest(
        rows + [market("late", 990)], 1000, ["allowed"], 600, overlap_s=overlap
    )
    assert [row["id"] for row in queue.pending] == ["late"]
    assert (queue.pop() or {})["id"] == "late"
    queue = FreshQueue(path)
    queue.ingest(
        rows + [market("late", 990)], 1001, ["allowed"], 600, overlap_s=overlap
    )
    assert queue.pending == []
    assert set(queue.asked) == {"fresh", "late"}


def test_legacy_asked_state(tmp_path: Any) -> None:
    """Load old asked lists and retain IDs until their watermark expires."""
    path = tmp_path / "fresh.json"
    path.write_text(json.dumps(dict(asked=["asked"], last_seen=1000)))
    queue = FreshQueue(path)
    queue.ingest([market("asked", 990)], 1050, ["allowed"], 600)
    assert queue.pending == []
    queue.save()
    queue = FreshQueue(path)
    queue.ingest([], 1720, ["allowed"], 600)
    assert "asked" in queue.asked
    queue.ingest([market("asked", 990)], 1721, ["allowed"], 600)
    assert not queue.asked
    assert queue.pending == []


@pytest.mark.parametrize("opening,queued", [(999, False), (1000, False), (1001, True)])
def test_queue_realitio_opening_is_trading_close(
    tmp_path: Any, opening: int, queued: bool
) -> None:
    """Realitio answering opens when the Omen trading window closes."""
    queue = FreshQueue(tmp_path / "fresh.json")
    row = {**market("market", 950), "openingTimestamp": str(opening)}
    queue.ingest([row], 1000, ["allowed"], 600)
    queue.save()
    assert bool(FreshQueue(queue.path).pending) is queued


@pytest.mark.parametrize(
    "balances,fee,requested,cfg,reason",
    [
        ([3.5, 6.5], 2 * 10**16, [0.65, 0.35], FastConfig(), ""),
        ([35, 65], 2 * 10**16, [0.65, 0.35], replace(FastConfig(), max_price=0.67), ""),
        ([35, 65], 2 * 10**16, [0.65, 0.35], replace(FastConfig(), min_edge=0.14), ""),
        ([35, 65], 2 * 10**17, [0.65, 0.35], FastConfig(), "min_edge"),
        (
            [35, 65],
            2 * 10**16,
            [0.65, 0.35],
            replace(FastConfig(), max_price=0.66),
            "max_price",
        ),
        ([35, 65], 2 * 10**16, [0.45, 0.55], FastConfig(), "max_slip"),
    ],
)
def test_executed_kelly(
    balances: Any, fee: int, requested: Any, cfg: Any, reason: str
) -> None:
    """Size against fees and impact, including a trader-35-like slip failure."""
    reserves = [int(value * 10**18) for value in balances]
    result = decide("still", 0.78, reserves, requested, cfg, fee)
    assert result.p_hat == pytest.approx(0.815)
    assert result.reason == reason
    if reason:
        assert result.stake == 0
        return
    # Independent binary invariant: net collateral increases both reserves.
    net = result.stake * (1 - fee / 10**18)
    tokens = balances[0] + net - balances[0] * balances[1] / (balances[1] + net)
    assert result.price == pytest.approx(result.stake / tokens, abs=1e-12)
    assert result.edge == pytest.approx(result.p_hat - result.price)
    assert result.price <= cfg.max_price
    assert result.price - requested[0] <= cfg.max_slip + 1e-12
    assert result.edge + 1e-12 >= cfg.min_edge
    assert 0 < result.stake < 4
    kelly = cfg.kelly_bankroll * result.edge / (1 - result.price)
    assert result.stake <= kelly + 1e-9
    assert (
        result.stake == pytest.approx(kelly, abs=1e-8)
        or result.price == pytest.approx(cfg.max_price, abs=1e-8)
        or result.edge == pytest.approx(cfg.min_edge, abs=1e-8)
        or result.price - requested[0] == pytest.approx(cfg.max_slip, abs=1e-8)
    )


@pytest.mark.parametrize("fee", [-1, 10**18])
def test_invalid_fee(fee: int) -> None:
    """Fail closed when the market fee cannot give a valid quote."""
    result = decide("still", 0.78, [10**18, 10**18], [0.5, 0.5], FastConfig(), fee)
    assert result.reason == "invalid_fee"
    assert result.stake == 0


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("fee", [0, 2 * 10**16, 3 * 10**17])
@pytest.mark.parametrize(
    "reserves,investment",
    [([35, 65], 4), ([10**19, 3 * 10**19], 4 * 10**18), ([1, 99], 1)],
)
def test_quote_contract_math(
    side: int, fee: int, reserves: Any, investment: int
) -> None:
    """Compare with calcBuyAmount's binary invariant, including wei rounding."""
    from fractions import Fraction
    from math import ceil

    from packages.valory.skills.fresh_watch_abci.strategy import buy_amount

    # calcBuyAmount: https://github.com/gnosis/conditional-tokens-market-makers/
    # blob/master/contracts/FixedProductMarketMaker.sol
    # The binary invariant gives the ending reserve directly as an exact ratio.
    net = investment - investment * fee // 10**18
    ending = Fraction(reserves[0] * reserves[1], reserves[1 - side] + net)
    expected = reserves[side] + net - ceil(ending)
    assert buy_amount(investment, reserves, side, fee) == expected


def test_no_outcome_execution_price() -> None:
    """Use the selected NO reserve and probability when sizing a buy."""
    from packages.valory.skills.fresh_watch_abci.strategy import buy_amount

    reserves = [7 * 10**18, 3 * 10**18]
    result = decide("other", 0.1, reserves, [0.3, 0.7], FastConfig(), 2 * 10**16)
    assert result.side == 1
    assert result.stake > 0
    investment = int(result.stake * 10**18)
    assert result.price == investment / buy_amount(investment, reserves, 1, 2 * 10**16)
    assert result.edge == pytest.approx(1 - result.p_hat - result.price)


def test_trader_35_quote_replay() -> None:
    """Lower the stake for a synthetic pool with the reported 0.7607 quote."""
    from packages.valory.skills.fresh_watch_abci.strategy import buy_amount

    # The task supplies no reserves or request price. This fixture uses a 0.65
    # request and reserves chosen to reproduce its 2% fee and 0.7607 buy quote.
    reserves = [3657009488301950024, 6791589049703621473]
    fee = 2 * 10**16
    old_price = 4 * 10**18 / buy_amount(4 * 10**18, reserves, 0, fee)
    assert old_price == pytest.approx(0.7607, abs=1e-12)
    assert old_price - 0.65 > 0.1
    assert old_price - 0.65 <= FastConfig().max_slip
    result = decide("still", 0.78, reserves, [0.65, 0.35], FastConfig(), fee)
    assert result.reason == ""
    assert result.stake == pytest.approx(2.8965418874612787)
    assert result.price == pytest.approx(0.7395634674420816)
    assert result.price - 0.65 <= FastConfig().max_slip


@pytest.mark.parametrize(
    "reserves,cap,stake,reason",
    [
        ([1, 1], 4.0, 0, "min_stake"),
        ([1, 1], 10**9, 0, "min_stake"),
        ([10**9, 10**9], 4.0, 0, "min_stake"),
        ([10**20, 10**20], 0.0, 0, "min_stake"),
        ([10**20, 10**20], 0.1, 0.1, ""),
        ([10**20, 10**20], 4.0, 4.0, ""),
    ],
)
def test_minimum_stake(reserves: Any, cap: float, stake: float, reason: str) -> None:
    """Skip empty and dust results, and keep normal and minimum-size bets."""
    result = decide(
        "still", 0.9, reserves, [0.5, 0.5], replace(FastConfig(), cap=cap), 0
    )
    assert result.stake == stake
    assert result.reason == reason


@pytest.mark.parametrize(
    "minimum,cap,expected,reason",
    [(0.25, 0.2, 0, "min_stake"), (0.01, 0.05, 0.05, ""), (0.0, 0.0, 0, "min_stake")],
)
def test_configured_minimum_stake(
    minimum: float, cap: float, expected: float, reason: str
) -> None:
    """Apply the configured minimum and always explain an empty result."""
    cfg = replace(FastConfig(), min_stake=minimum, cap=cap)
    result = decide("still", 0.9, [10**20, 10**20], [0.5, 0.5], cfg, 0)
    assert result.stake == expected
    assert result.reason == reason


def test_higher_slip_default() -> None:
    """Allow a 0.15 price rise by default and retain explicit tighter limits."""
    reserves = [35 * 10**18, 65 * 10**18]
    cfg = FastConfig()
    result = decide("still", 0.9, reserves, [0.5, 0.5], cfg, 2 * 10**16)
    assert result.reason == ""
    assert result.stake == 4
    assert 0.1 < result.price - 0.5 < 0.2
    strict = decide(
        "still", 0.9, reserves, [0.5, 0.5], replace(cfg, max_slip=0.1), 2 * 10**16
    )
    assert strict.reason == "max_slip"
    assert strict.stake == 0


@pytest.mark.parametrize("bounded", [False, True])
def test_stop_when_quote_amount_converges(bounded: bool) -> None:
    """Stop once the next quote repeats the integer investment amount."""
    from unittest.mock import patch

    from packages.valory.skills.fresh_watch_abci.strategy import buy_amount

    cfg = replace(FastConfig(), max_price=0.67) if bounded else FastConfig()
    with patch(
        "packages.valory.skills.fresh_watch_abci.strategy.buy_amount", wraps=buy_amount
    ) as quotes:
        result = decide(
            "still", 0.9, [35 * 10**18, 65 * 10**18], [0.65, 0.35], cfg, 2 * 10**16
        )
    assert result.stake > 0
    amounts = [call.args[0] for call in quotes.call_args_list]
    assert len(amounts) < 64
    assert all(a != b for a, b in zip(amounts, amounts[1:]))
    if not bounded:
        assert result.stake == cfg.cap
        assert len(amounts) == 1
