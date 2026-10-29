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

"""Use natural-YES evidence only for canonical Realitio template two markets."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import (
    FastDecisionBehaviour,
    FreshWatchBehaviour,
)
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
)
from packages.valory.skills.fresh_watch_abci.preview import PreviewStore
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.market_manager_abci.bets import Bet


def test_unknown_and_noncanonical_markets_do_not_use_shortcuts(tmp_path: Path) -> None:
    """Request the actual labels unless outcome zero is a known natural Yes."""
    unknown = raw_market()
    unknown.pop("question")
    rows = [
        unknown,
        {**raw_market(), "question": {"templateId": 0}},
        {**raw_market(), "question": {"templateId": 2}, "outcomes": ["No", "Yes"]},
        {
            **raw_market(),
            "question": {"templateId": 2},
            "outcomes": ["No", "Yes"],
            "title": "Will event X happen?",
        },
        {**raw_market(), "question": {"templateId": "2"}},
    ]
    for index, row in enumerate(rows):
        store_path = tmp_path / str(index)
        store_path.mkdir()
        watch = make(FreshWatchBehaviour, store_path)
        cfg = LightningConfig(
            lightning_still_hold=True,
            lightning_preview=True,
            lightning_base_rate_min_n=1,
        )
        watch.context.params.lightning = cfg
        watch.context.state.fast_background_running = True
        watch._fast_tool = lambda: "superforcaster"
        watch._get_pool_balances = lambda *_args: done([100 * 10**18] * 2)
        BaseRateCache(store_path / "lightning_base_rate.json").update(
            [{**raw_market(), "question": {"templateId": 2}, "payouts": [1, 0]}],
            1050,
            cfg,
        )
        preview = PreviewStore(store_path / "lightning_preview.json")
        preview.ingest(
            {"result": {"questions": [{"question": row["title"]}]}}, 990, 990
        )
        preview.reserve(row["title"], 1000, "night", 15)
        preview.answer(row["title"], 0.99, 1010)
        queue = FreshQueue(store_path / "fresh_watch.json")
        queue.pending = [row]
        queue.save()
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1050,
        ):
            selection = run(watch._select(False, True))
        assert selection["lane"] == ("lightning_decision" if index == 4 else "market")
        assert selection["question_template_id"] == row.get("question", {}).get(
            "templateId"
        )
        if index < 4:
            assert selection["mech_tool"] == "superforcaster"
            assert "lightning_probability" not in selection
            assert "lightning_preview_probability" not in selection
        if index == 3:
            assert PreviewStore(preview.path).entries[row["title"]]["p_yes"] == 0.99


def test_cached_natural_yes_prior_is_not_used_for_reversed_outcomes(
    tmp_path: Path,
) -> None:
    """A natural-YES rate must not become a prior for a No-first Mech answer."""
    behaviour = make(FastDecisionBehaviour, tmp_path)
    cfg = LightningConfig(lightning_still_hold=True, still_hold_prior=0.70)
    behaviour.context.params.lightning = cfg
    BaseRateCache(tmp_path / "lightning_base_rate.json").update(
        [
            {
                **raw_market(),
                "id": str(index),
                "question": {"templateId": 2},
                "payouts": [1, 0] if index < 19 else [0, 1],
            }
            for index in range(20)
        ],
        1050,
        cfg,
    )
    raw = {**raw_market(), "outcomes": ["No", "Yes"]}
    raw.pop("condition")
    raw.pop("question", None)
    bet = Bet(**raw, market="omen_subgraph")
    values = {
        "requested_at": 1050,
        "request_prices": [0.45, 0.55],
        "question_template_id": 2,
    }
    behaviour._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.1)
    behaviour._get_pool_balances = lambda *_args: done([55 * 10**27, 45 * 10**27])
    with (
        patch.object(
            type(behaviour), "sampled_bet", new_callable=PropertyMock, return_value=bet
        ),
        patch.object(
            type(behaviour), "synchronized_data", new_callable=PropertyMock
        ) as synced,
        patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=1100,
        ),
    ):
        synced.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        synced.return_value.db.get_strict.side_effect = values.__getitem__
        result = run(behaviour.evaluate())
    assert result["p_hat"] == pytest.approx(0.475)
    assert result["stake"] == 0


def test_discovery_only_flags_preserve_the_fast_no_bet(tmp_path: Path) -> None:
    """Chain discovery must retain the plain fast strategy and its NO purchase."""
    decisions = []
    for flag in [False, True]:
        behaviour = make(FastDecisionBehaviour, tmp_path)
        behaviour.context.params.fast = replace(behaviour.params.fast, prior_weight=1)
        behaviour.context.params.lightning = LightningConfig(
            lightning_chain_detect=flag, still_hold_prior=0.95
        )
        raw = raw_market()
        raw.pop("condition")
        raw.pop("question", None)
        bet = Bet(**raw, market="omen_subgraph")
        values = {
            "requested_at": 1050,
            "request_prices": [0.9, 0.1],
            "question_template_id": 2,
        }
        behaviour._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.1)
        behaviour._get_pool_balances = lambda *_args: done([10 * 10**27, 90 * 10**27])
        with (
            patch.object(
                type(behaviour),
                "sampled_bet",
                new_callable=PropertyMock,
                return_value=bet,
            ),
            patch.object(
                type(behaviour), "synchronized_data", new_callable=PropertyMock
            ) as synced,
            patch(
                "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
                return_value=1100,
            ),
        ):
            synced.return_value.db.get.side_effect = (
                lambda key, default=None, values=values: values.get(key, default)
            )
            synced.return_value.db.get_strict.side_effect = values.__getitem__
            decisions.append(run(behaviour.evaluate()))
    assert decisions[0]["side"] == 1
    assert decisions[0]["stake"] == 4
    assert decisions[1] == decisions[0]


def test_base_rate_requires_realitio_template_two(tmp_path: Path) -> None:
    """Exclude unknown and boolean-template payouts from natural-YES evidence."""
    cfg = LightningConfig(lightning_base_rate_min_n=1)
    cache = BaseRateCache(tmp_path / "rate.json")
    cache.update(
        [
            {**raw_market(), "id": "unknown", "question": None, "payouts": [1, 0]},
            {
                **raw_market(),
                "id": "boolean",
                "question": {"templateId": 0},
                "payouts": [1, 0],
            },
            {
                **raw_market(),
                "id": "canonical",
                "question": {"templateId": 2},
                "payouts": [0, 1],
            },
        ],
        1050,
        cfg,
    )
    assert cache.sample == 1
    assert cache.rate == 0


def test_legacy_base_rate_requires_template_evidence(tmp_path: Path) -> None:
    """Do not reuse an aggregate that lacks evidence of its Realitio template."""
    import json

    cfg = LightningConfig(lightning_base_rate_min_n=1)
    cache = BaseRateCache(tmp_path / "rate.json")
    rows = [{**raw_market(), "payouts": [1, 0]}]
    cache.update(rows, 1050, cfg)
    legacy = dict(cache.data)
    legacy.pop("template_id", None)
    cache.path.write_text(json.dumps(legacy))
    assert BaseRateCache(cache.path).usable(1051, cfg) is None
    cache.update(rows, 1051, cfg)
    assert BaseRateCache(cache.path).usable(1051, cfg) == 1
