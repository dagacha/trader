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

"""Honor the configured still-hold prior when lightning has no evidence."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.behaviours import FastDecisionBehaviour
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    make,
    raw_market,
    run,
)
from packages.valory.skills.market_manager_abci.bets import Bet


@pytest.mark.parametrize(
    "flag", ["lightning_still_hold", "lightning_chain_detect", "lightning_preview"]
)
def test_configured_prior_is_not_a_lightning_sentinel(
    tmp_path: Path, flag: str
) -> None:
    """Keep the configured .85 prior and skip a YES price of .87 at p_tool .91."""
    behaviour = make(FastDecisionBehaviour, tmp_path)
    behaviour.context.params.lightning = LightningConfig(
        lightning_still_hold=flag == "lightning_still_hold",
        lightning_chain_detect=flag == "lightning_chain_detect",
        lightning_preview=flag == "lightning_preview",
        still_hold_prior=0.85,
    )
    behaviour._get_decision_prediction = lambda: SimpleNamespace(p_yes=0.91)
    behaviour._get_pool_balances = lambda *args: done([13 * 10**27, 87 * 10**27])
    raw = raw_market()
    raw.pop("condition")
    raw.pop("question", None)
    bet = Bet(**raw, market="omen_subgraph")
    values = {
        "question_template_id": 2,
        "requested_at": 1050,
        "request_prices": [0.87, 0.13],
    }
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
        synced.return_value.db.get_strict.side_effect = values.__getitem__
        synced.return_value.db.get.side_effect = lambda key, default=None: values.get(
            key, default
        )
        decision = run(behaviour.evaluate())
    assert decision["p_hat"] == pytest.approx(0.88)
    assert decision["stake"] == 0
    assert decision["reason"] == "min_edge"
