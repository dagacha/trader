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

"""Keep the subgraph fallback on the normal fast polling cadence."""

from pathlib import Path
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
    run,
)


@pytest.mark.parametrize("background", [False, True])
def test_fallback_polls_subgraph_at_normal_interval(
    tmp_path: Path, background: bool
) -> None:
    """Poll the subgraph at fifteen seconds after the chain fallback starts."""
    behaviour = make(FreshMarketPoller if background else FreshWatchBehaviour, tmp_path)
    behaviour.context.params.lightning = LightningConfig(
        lightning_chain_detect=True, lightning_preview=True
    )
    behaviour.chain_markets = lambda now: done(None)
    behaviour.preview_poll = lambda now: done(None)
    behaviour._fetch_fresh = MagicMock(side_effect=lambda *args: done([]))
    behaviour.sleep = MagicMock(side_effect=lambda delay: done(None))
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.last_housekeeping = 1050
    queue.save()
    for now, expected in [(1050, 1), (1060, 1), (1065, 2)]:
        with patch(
            "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
            return_value=now,
        ):
            run(behaviour.async_act() if background else behaviour._select(False, True))
        assert behaviour._fetch_fresh.call_count == expected
        behaviour.sleep.assert_called_with(15)
