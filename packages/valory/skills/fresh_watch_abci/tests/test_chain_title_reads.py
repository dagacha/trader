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

"""Bound exact-ID subgraph reads during chain materialisation."""

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from packages.valory.protocols.http import HttpMessage
from packages.valory.skills.abstract_round_abci.handlers import HttpHandler
from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import done, make
from packages.valory.skills.fresh_watch_abci.tests.test_chain_discovery import (
    BLOCK,
    CREATED,
    Harness,
)


def test_missing_exact_id_title_reply_releases_real_poller_after_deadline(
    tmp_path: Path,
) -> None:
    """A lost lookup reply must start fallback timing and free the background poller."""
    poller = make(FreshMarketPoller, tmp_path)
    poller.context.params.lightning = LightningConfig(lightning_chain_detect=True)
    poller.setup = lambda: None
    poller.async_act_wrapper = poller.async_act
    poller._refresh_base_rate = lambda _now: done(None)
    chain = Harness(tmp_path)
    chain.receipt = {"logs": []}
    poller._chain_rpc = chain._chain_rpc
    poller.context.omen_subgraph.get_spec.return_value = {
        "method": "POST",
        "url": "https://omen.test",
    }
    poller.context.requests = SimpleNamespace(request_id_to_callback={})
    dialogue = SimpleNamespace(
        dialogue_label=SimpleNamespace(dialogue_reference=("title", ""))
    )
    poller._build_http_request_message = MagicMock(return_value=(MagicMock(), dialogue))
    poller._get_default_terms = lambda: None
    seconds = [0.0]
    epoch = datetime.datetime.fromtimestamp(CREATED + 5)

    class Clock(datetime.datetime):
        """Drive scheduler sleeps and the request deadline with one clock."""

        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            """Return the time of the current scheduler tick."""
            return cls.fromtimestamp(epoch.timestamp() + seconds[0], tz=tz)

    with (
        patch(
            "packages.valory.skills.abstract_round_abci.behaviour_utils.datetime.datetime",
            Clock,
        ),
        patch("time.monotonic", side_effect=lambda: seconds[0]),
        patch("time.time", side_effect=lambda: epoch.timestamp() + seconds[0]),
    ):
        poller.act()
        for tick in range(1, 32):
            seconds[0] = tick * 0.2
            poller.act()
            if poller.context.state.fast_polling is False:
                break
    assert poller.context.state.fast_polling is False
    assert seconds[0] <= 6.2
    state = json.loads((tmp_path / "lightning_chain.json").read_text())
    assert state["last_block"] < BLOCK
    assert state["rpc_error_since"] == CREATED + 5
    assert FreshQueue(tmp_path / "fresh_watch.json").pending == []
    assert "title" in poller.context.requests.request_id_to_callback
    poller.context.http_dialogues.update.return_value = dialogue
    poller.context.behaviours.main.current_behaviour = poller
    late = HttpMessage(
        performative=cast(HttpMessage.Performative, HttpMessage.Performative.RESPONSE),
        dialogue_reference=("title", "reply"),
        message_id=2,
        target=1,
        version="",
        status_code=200,
        status_text="OK",
        headers="",
        body=b"{}",
    )
    HttpHandler(name="", skill_context=poller.context).handle(late)
    assert poller.context.requests.request_id_to_callback == {}
    poller.context.logger.exception.assert_not_called()
    poller.stop()
