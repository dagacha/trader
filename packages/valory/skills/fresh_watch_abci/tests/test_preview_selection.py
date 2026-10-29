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

"""Keep preview requests distinct from known and expired market work."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import FreshWatchBehaviour
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.preview import PreviewStore
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)


def setup_watch(tmp_path: Path) -> Any:
    """Use the real queue and preview stores with a local pool quote."""
    behaviour = make(FreshWatchBehaviour, tmp_path)
    behaviour.context.params.lightning = LightningConfig(lightning_preview=True)
    behaviour.context.state.fast_background_running = True
    behaviour._fast_tool = lambda: "superforcaster"
    behaviour._get_pool_balances = lambda *args: done([100 * 10**18] * 2)
    behaviour.sleep = MagicMock(side_effect=lambda delay: done(None))
    return behaviour


def test_late_proposal_for_selected_market_is_not_reserved(tmp_path: Path) -> None:
    """A proposal that arrives after market selection cannot buy another request."""
    behaviour = setup_watch(tmp_path)
    market = {**raw_market(), "title": "Will event X happen?"}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [market]
    queue.save()
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        assert run(behaviour._select(False, True))["lane"] == "market"
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": market["title"]}]}}, 1060, 1060)
    assert behaviour._preview_selection(1060) is None
    assert PreviewStore(store.path).counts == {}
    assert PreviewStore(store.path).entries[market["title"]]["requested_at"] is None


def test_known_preview_title_advances_to_next_candidate(tmp_path: Path) -> None:
    """Skip pending market titles and spend the cap on the next unseen title."""
    behaviour = setup_watch(tmp_path)
    market = {**raw_market(), "title": "Will event X happen?"}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [market]
    queue.save()
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest(
        {
            "result": {
                "questions": [
                    {"question": market["title"]},
                    {"question": "Will event Y happen?"},
                ]
            }
        },
        1050,
        1050,
    )
    selection = behaviour._preview_selection(1060)
    assert selection["lightning_preview_title"] == "Will event Y happen?"
    latest = PreviewStore(store.path)
    assert latest.entries[market["title"]]["requested_at"] is None
    assert sum(latest.counts.values()) == 1


@pytest.mark.parametrize("tool", ["superforcaster", None])
def test_preview_expiring_during_quote_uses_normal_request(
    tmp_path: Path, tool: object
) -> None:
    """The pool quote must not turn an expired answer into an unrequested market."""
    behaviour = setup_watch(tmp_path)
    behaviour._fast_tool = lambda: tool
    market = {
        **raw_market(),
        "title": "Will event X happen?",
        "creationTimestamp": 1800,
    }
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [market]
    queue.save()
    store = PreviewStore(tmp_path / "lightning_preview.json")
    store.ingest({"result": {"questions": [{"question": market["title"]}]}}, 990, 990)
    store.reserve(market["title"], 1000, "night", 15)
    store.answer(market["title"], 0.99, 1100)
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        side_effect=[1898, 1902],
    ):
        selection = run(behaviour._select(False, True))
    if tool is None:
        assert selection == {"lane": "wait"}
        assert FreshQueue(queue.path).pending == [market]
        assert FreshQueue(queue.path).asked == {}
    else:
        assert selection["lane"] == "market"
        assert selection["mech_tool"] == "superforcaster"
        assert selection["requested_at"] == 1902
        assert FreshQueue(queue.path).asked == {market["id"]: 1800}


@pytest.mark.parametrize("balances", [[], [0, 0]])
def test_chain_market_without_funded_pool_is_dropped(
    tmp_path: Path, balances: object
) -> None:
    """Discard a missing chain pool so a funded market behind it can run."""
    behaviour = setup_watch(tmp_path)
    market = {
        **raw_market(),
        "title": "Will event X happen?",
        "block_number": 100,
        "block_hash": "0xabc",
    }
    newer = {**raw_market(), "id": "newer", "creationTimestamp": 1001}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [market, newer]
    queue.save()
    behaviour._get_pool_balances = lambda address, *args: done(
        balances if address == market["id"] else [100 * 10**18] * 2
    )
    behaviour._chain_pool_balances = behaviour._get_pool_balances
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        assert run(behaviour._select(False, True))["lane"] == "wait"
        assert FreshQueue(queue.path).pending == [newer]
        selection = run(behaviour._select(False, True))
    assert selection["lane"] == "market"
    assert behaviour.bets[selection["sampled_bet_index"]].id == "newer"


def test_select_reloads_queue_after_overlap_orphan_cleanup(tmp_path: Path) -> None:
    """Do not restore a pending orphan from the foreground's stale queue copy."""
    behaviour = setup_watch(tmp_path)
    behaviour.context.state.fast_background_running = False
    orphan = {**raw_market(), "block_number": 100, "block_hash": "0xorphan"}
    newer = {**raw_market(), "id": "newer", "creationTimestamp": 1001}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [orphan]
    queue.save()

    def poll(_queue: object, _now: float) -> object:
        latest = FreshQueue(queue.path)
        latest.pending = []
        latest.save()
        return done([newer])

    behaviour._poll_markets = poll
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        selection = run(behaviour._select(False, True))
    assert selection["lane"] == "market"
    assert behaviour.bets[selection["sampled_bet_index"]].id == "newer"
    latest = FreshQueue(queue.path)
    assert latest.pending == []
    assert latest.asked == {"newer": 1001}
