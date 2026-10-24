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
    yes = decide("Will it still hold?", 0.9, [0.6, 0.4], [0.6, 0.4], cfg)
    assert yes.p_hat == pytest.approx(0.875)
    assert yes.side == 0
    assert yes.edge == pytest.approx(0.275)
    assert yes.stake == 4
    no = decide("Will it happen?", 0.1, [0.7, 0.3], [0.7, 0.3], cfg)
    assert no.side == 1
    assert no.stake == 4
    assert decide("other", 0.1, [0.5, 0.5], [0.5, 0.5], cfg).stake == 4
    small = decide("numeric above", 0.6, [0.5, 0.5], [0.5, 0.5], cfg)
    assert small.stake == pytest.approx(2)
    assert (
        decide("numeric above", 0.6, [0.5, 0.5], [0.5, 0.5], replace(cfg, cap=1)).stake
        == 1
    )
    assert (
        decide("other", 0.1, [0.7, 0.3], [0.7, 0.3], replace(cfg, prior_weight=1)).p_hat
        == 0.1
    )


@pytest.mark.parametrize(
    "title,p,prices,requested,reason",
    [
        ("above", 0.5, [0.54, 0.46], [0.54, 0.46], "min_edge"),
        ("still", 1, [0.91, 0.09], [0.91, 0.09], "max_price"),
        ("other", 0.5, [0.9, 0.1], [0.9, 0.1], "no_min_p"),
        ("still", 0.9, [0.65, 0.35], [0.5, 0.5], "max_slip"),
        ("still", 0.9, [0, 1], [0.5, 0.5], "invalid_price"),
    ],
)
def test_skip(title: Any, p: Any, prices: Any, requested: Any, reason: Any) -> None:
    """Skip or abort when a strategy bound fails."""
    result = decide(title, p, prices, requested, FastConfig())
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
