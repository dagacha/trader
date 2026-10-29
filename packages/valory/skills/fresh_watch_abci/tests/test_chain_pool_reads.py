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

"""Check bounded chain pool reads and retryable quote failures."""

import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator
from unittest.mock import MagicMock, patch

from packages.valory.skills.abstract_round_abci.behaviour_utils import AsyncBehaviour
from packages.valory.skills.fresh_watch_abci.chain_discovery import ChainDiscoveryMixin
from packages.valory.skills.fresh_watch_abci.strategy import FreshQueue
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    raw_market,
    run,
)
from packages.valory.skills.fresh_watch_abci.tests.test_chain_discovery import (
    CREATED,
    Harness,
)
from packages.valory.skills.fresh_watch_abci.tests.test_preview_selection import (
    setup_watch,
)


class ScheduledChainPool(Harness, AsyncBehaviour):
    """Deliver event and receipt reads but leave the pool request unanswered."""

    def __init__(self, path: Path) -> None:
        """Prepare healthy discovery reads and a hung pool connection."""
        Harness.__init__(self, path)
        AsyncBehaviour.__init__(self)
        self.context.default_ledger_id = "ethereum"
        self.context.contract_api_dialogues = MagicMock()
        dialogue = MagicMock()
        dialogue.dialogue_label.dialogue_reference = ("pool", "")
        self.context.contract_api_dialogues.create.return_value = (
            MagicMock(),
            dialogue,
        )
        self.context.requests = SimpleNamespace(request_id_to_callback={})
        self.context.outbox = MagicMock()
        self.params.conditional_tokens_address = "0x" + "12" * 20
        self.completed = False
        self.result: Any = "pending"

    _chain_pool_balances = ChainDiscoveryMixin._chain_pool_balances

    def _get_default_terms(self) -> None:
        """Use no payment terms for this local read."""

    @staticmethod
    def _get_request_nonce_from_dialogue(_dialogue: Any) -> str:
        """Use a stable nonce for the one unanswered pool read."""
        return "pool"

    def _chain_rpc(self, method: str, **kwargs: Any) -> Any:
        """Use healthy saved reads except for the real pool request wait."""
        if method == "get_pool_balances":
            return (yield from ChainDiscoveryMixin._chain_rpc(self, method, **kwargs))
        return (yield from Harness._chain_rpc(self, method, **kwargs))

    def _get_pool_balances(self, *args: Any) -> Any:
        """Reproduce the old unbounded contract response wait."""
        while True:
            yield from self.sleep(0.2)

    def async_act_wrapper(self) -> Generator:
        """Run the scheduler without an application round."""
        yield from self.async_act()

    def async_act(self) -> Generator:
        """Run real market materialisation and save its fallback result."""
        self.result = yield from self.chain_markets(CREATED)
        self.completed = True


def test_hung_pool_read_starts_rpc_fallback_on_scheduler_ticks(tmp_path: Path) -> None:
    """Healthy event and receipt reads must not hide a hung pool RPC."""
    seconds = [0.0]
    epoch = datetime.datetime(2026, 1, 1)

    class Clock(datetime.datetime):
        """Advance RPC and scheduler deadlines together."""

        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            """Return the current scheduler time."""
            return cls.fromtimestamp(epoch.timestamp() + seconds[0], tz=tz)

    behaviour = ScheduledChainPool(tmp_path)
    with (
        patch(
            "packages.valory.skills.abstract_round_abci.behaviour_utils.datetime.datetime",
            Clock,
        ),
        patch("time.monotonic", side_effect=lambda: seconds[0]),
    ):
        behaviour.act()
        for tick in range(1, 32):
            seconds[0] = tick * 0.2
            behaviour.act()
            if behaviour.completed:
                break
    assert behaviour.completed, "The pool request must expire without a response"
    assert behaviour.result == []
    state = json.loads((tmp_path / "lightning_chain.json").read_text())
    assert state["rpc_error_since"] == CREATED
    assert state["last_block"] < behaviour.head
    assert FreshQueue(tmp_path / "fresh_watch.json").pending == []


def test_retryable_chain_pool_error_keeps_market_pending(tmp_path: Path) -> None:
    """An RPC error must not consume a chain market's only request chance."""
    behaviour = setup_watch(tmp_path)
    market = {**raw_market(), "block_number": 100, "block_hash": "0xabc"}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [market]
    queue.save()
    behaviour._get_pool_balances = lambda *args: done(None)
    behaviour._chain_pool_balances = lambda *args: done(None)
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        assert run(behaviour._select(False, True)) == {"lane": "wait"}
    saved = FreshQueue(queue.path)
    assert saved.pending == [market]
    assert saved.asked == {}


def test_confirmed_missing_chain_pool_is_dropped(tmp_path: Path) -> None:
    """A no-code result must release the pending queue for its next market."""
    behaviour = setup_watch(tmp_path)
    market = {**raw_market(), "block_number": 100, "block_hash": "0xabc"}
    newer = {**raw_market(), "id": "newer", "creationTimestamp": 1001}
    queue = FreshQueue(tmp_path / "fresh_watch.json")
    queue.pending = [market, newer]
    queue.save()
    behaviour._get_pool_balances = lambda *args: done([])
    behaviour._chain_pool_balances = lambda *args: done([])
    with patch(
        "packages.valory.skills.fresh_watch_abci.behaviours.time.time",
        return_value=1050,
    ):
        assert run(behaviour._select(False, True)) == {"lane": "wait"}
    assert FreshQueue(queue.path).pending == [newer]
