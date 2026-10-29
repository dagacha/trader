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

"""Keep unsupported saved rows out of normal market consumers."""

from pathlib import Path
from string import Template
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

from packages.valory.skills.decision_maker_abci.behaviours.mech_only import (
    MechOnlySelectionBehaviour,
)
from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastDecisionRequestBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.preview import PreviewStore
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.fresh_watch_abci.tests.test_preview_selection import (
    setup_watch,
)


def test_disabled_preview_is_removed_before_capped_mech_only_requests(
    tmp_path: Path,
) -> None:
    """A saved synthetic preview must not be requested again after rollback."""
    watch = setup_watch(tmp_path)
    store = PreviewStore(tmp_path / "lightning_preview.json")
    title = "Will event X happen?"
    store.ingest({"result": {"questions": [{"question": title}]}}, 990, 990)
    assert watch._preview_selection(1000)["lane"] == "preview_request"
    watch.context.params.lightning = LightningConfig()
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.last_housekeeping = 1050
    queue.save()
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        assert run(watch._select(True, False))["lane"] == "mech_only"
    mech_only = make(MechOnlySelectionBehaviour, tmp_path)
    mech_only.context.params.opening_margin = 300
    mech_only.context.params.safe_voting_range = 300
    mech_only.context.params.max_mech_requests_per_cycle = 1
    mech_only.context.params.multisend_batch_size = 1
    mech_only.context.params.prompt_template = Template("$question $yes $no")
    mech_only._select_mech_tool = lambda: "superforcaster"
    sent = []

    def finish(payload: Any) -> Any:
        sent.append(payload)
        yield from ()

    mech_only.finish_behaviour = finish
    synced = SimpleNamespace(
        is_activity_target_met=False, activity_target=1, mech_only_queue=[]
    )
    with (
        patch.object(
            type(mech_only),
            "synchronized_data",
            new_callable=PropertyMock,
            return_value=synced,
        ),
        patch.object(
            type(mech_only),
            "synced_timestamp",
            new_callable=PropertyMock,
            return_value=1050,
        ),
    ):
        run(mech_only.async_act())
    assert sent[0].mech_requests is None
    assert mech_only.bets == []
    assert list(PreviewStore(store.path).counts.values()) == [1]


def test_saved_unfunded_chain_row_cannot_reach_request_setup(tmp_path: Path) -> None:
    """Skip unsupported saved rows without asking them or losing the next market."""
    for index, (amounts, liquidity) in enumerate(
        [
            ([0, 0], 0),
            ([100 * 10**18] * 2, 0),
            ([0, 100 * 10**18], 100),
        ]
    ):
        path = tmp_path / str(index)
        path.mkdir()
        watch = setup_watch(path)
        watch.context.params.lightning = LightningConfig()
        legacy = {
            **raw_market(),
            "outcomeTokenAmounts": amounts,
            "scaledLiquidityMeasure": liquidity,
            "block_number": 100,
            "block_hash": "0xabc",
        }
        newer = {**raw_market(), "id": "newer", "creationTimestamp": 1001}
        queue = FreshQueue(path / "fresh_watch.json")
        queue.pending = [legacy, newer]
        queue.save()
        quote = MagicMock(side_effect=lambda *_args: done([100 * 10**18] * 2))
        watch._chain_pool_balances = quote
        watch._get_pool_balances = quote
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            selection = run(watch._select(False, True))
            assert selection == {"lane": "wait"}
            assert FreshQueue(queue.path).pending == [newer]
            assert FreshQueue(queue.path).asked == {}
            quote.assert_not_called()
            assert watch.bets == []
            selection = run(watch._select(False, True))
        assert selection["lane"] == "market"
        selected = watch.bets[selection["sampled_bet_index"]]
        assert selected.id == newer["id"]
        assert selected.outcomes == ["Yes", "No"]
        request = make(FastDecisionRequestBehaviour, path)
        request.context.params.slot_count = 2
        request.context.params.prompt_template = Template("$question $yes $no")
        with (
            patch.object(
                type(request),
                "sampled_bet",
                new_callable=PropertyMock,
                return_value=selected,
            ),
            patch.object(
                type(request), "synchronized_data", new_callable=PropertyMock
            ) as synced,
        ):
            synced.return_value.mech_tool = "superforcaster"
            request.setup()
        assert request.metadata["prompt"] == newer["title"] + " Yes No"
        assert all(not bet.id.startswith("preview:") for bet in watch.bets)


def test_unfunded_chain_quote_is_not_marked_asked(tmp_path: Path) -> None:
    """A saved funded row that loses funding must not become an asked market."""
    for index, balances in enumerate([[0, 0], [0, 100 * 10**18]]):
        path = tmp_path / str(index)
        path.mkdir()
        watch = setup_watch(path)
        watch.context.params.lightning = LightningConfig()
        market = {**raw_market(), "block_number": 100, "block_hash": "0xabc"}
        newer = {**raw_market(), "id": "newer", "creationTimestamp": 1001}
        latest_market = {**newer, "id": "latest", "creationTimestamp": 1002}
        queue = FreshQueue(path / "fresh_watch.json")
        queue.pending = [market, newer]
        queue.save()

        def quote(
            *_args: Any,
            queue_path: Path = queue.path,
            appended: dict[str, Any] = latest_market,
            quote_balances: list[int] = balances,
        ) -> Any:
            latest = FreshQueue(queue_path)
            latest.pending.append(appended)
            latest.save()
            yield
            return quote_balances

        watch._chain_pool_balances = quote
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            assert run(watch._select(False, True)) == {"lane": "wait"}
            assert FreshQueue(queue.path).pending == [newer, latest_market]
            assert FreshQueue(queue.path).asked == {}
            selection = run(watch._select(False, True))
        assert selection["lane"] == "market"
        assert watch.bets[selection["sampled_bet_index"]].id == "newer"
        assert FreshQueue(queue.path).pending == [latest_market]
