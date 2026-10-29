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

"""Check chain discovery against a real Omen creation receipt."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from packages.valory.skills.fresh_watch_abci.chain_discovery import (
    CREATION_TOPIC,
    ChainDiscoveryMixin,
    ChainRPCError,
    creation_details,
    receipt_question,
)
from packages.valory.skills.fresh_watch_abci.strategy import FastConfig, FreshQueue

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "chain_creation.json").read_text()
)
CREATION = FIXTURE["logs"][-1]
CREATED = FIXTURE["blockTimestamp"]
BLOCK = CREATION["blockNumber"]
FACTORY = CREATION["address"]


def done(value: Any) -> Any:
    """Return a cooperative result."""
    yield from ()
    return value


def run(generator: Any) -> Any:
    """Run a cooperative method to completion."""
    while True:
        try:
            next(generator)
        except StopIteration as result:
            return result.value


class Harness(ChainDiscoveryMixin):
    """Use saved chain results without a ledger connection."""

    def __init__(self, path: Path) -> None:
        """Set the normal params and record RPC requests."""
        self.params = SimpleNamespace(
            store_path=path,
            mech_chain_id="gnosis",
            fast=FastConfig(active_window_utc="00:00-23:59"),
            lightning=SimpleNamespace(
                lightning_chain_detect=True,
                lightning_creators=FastConfig().creators,
                omen_fpmm_factory=FACTORY,
                lightning_rpc_fallback_s=60,
            ),
        )
        self.context = SimpleNamespace(logger=MagicMock())
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.logs = [CREATION]
        self.head = BLOCK
        self.fail = False
        self.receipt = {"logs": FIXTURE["logs"]}
        self.fallback = None

    def _chain_rpc(self, method: str, **kwargs: Any) -> Any:
        """Return saved data or simulate an RPC failure."""
        self.calls.append((method, kwargs))
        if self.fail:
            raise ChainRPCError("ledger unavailable")
        results = {
            "get_block": {"number": self.head, "timestamp": CREATED},
            "get_logs": self.logs,
            "get_transaction_receipt": self.receipt,
        }
        return (yield from done(results[method]))

    def _get_pool_balances(self, *args: Any) -> Any:
        """Return a liquid live pool."""
        return (yield from done([7 * 10**18, 7 * 10**18]))

    def _chain_subgraph_market(self, market_id: str) -> Any:
        """Return a configured last-resort title lookup."""
        return (yield from done(self.fallback))


def test_decode_real_creation_and_exact_condition_question() -> None:
    """Match the condition to its question, not the first question in the tx."""
    market = creation_details(CREATION)
    assert market["id"] == "0xf46f0b6e9e96b466a3563d025577e65648c7daa7"
    assert market["creator"] == FastConfig().creators[0]
    question = receipt_question(FIXTURE["logs"], market["condition"]["id"])
    assert question is not None
    assert question["title"].startswith("Will Bruno Fernandes's status")
    assert question["openingTimestamp"] == 1793664000
    assert question["outcomes"] == ["Yes", "No"]
    assert receipt_question(FIXTURE["logs"], "0x" + "00" * 32) is None


def test_chain_filter_cursor_persistence_and_no_replayed_market(tmp_path: Path) -> None:
    """Request both indexed creators and resume after the saved creation block."""
    harness: Any = Harness(tmp_path)
    rows = run(harness.chain_markets(CREATED + 8))
    assert len(rows) == 1
    assert rows[0]["creationTimestamp"] == CREATED
    assert rows[0]["outcomeTokenMarginalPrices"] == [0.5, 0.5]
    filters = next(
        kwargs["filter_params"] for name, kwargs in harness.calls if name == "get_logs"
    )
    assert filters["address"].lower() == FACTORY.lower()
    assert filters["topics"] == [
        CREATION_TOPIC,
        ["0x" + creator[2:].rjust(64, "0") for creator in FastConfig().creators],
    ]
    assert filters["fromBlock"] <= BLOCK
    assert len(FreshQueue(tmp_path / "fresh_watch.json").pending) == 1
    harness.context.logger.info.assert_any_call(
        "[LIGHTNING_DETECT] market=%s delay_s=%.3f source=%s", rows[0]["id"], 8, "chain"
    )
    restart = Harness(tmp_path)
    restart.logs = []
    restart.head = BLOCK + 1
    assert run(restart.chain_markets(CREATED + 13)) == []
    assert restart.calls == [
        ("get_block", {"block_identifier": "latest"}),
        (
            "get_logs",
            {"filter_params": dict(filters, fromBlock=BLOCK - 2, toBlock=BLOCK + 1)},
        ),
    ]


def test_empty_scan_advances_cursor_and_duplicate_head_skips_logs(
    tmp_path: Path,
) -> None:
    """Persist the scanned head even when a block has no matching creation."""
    harness: Any = Harness(tmp_path)
    harness.logs = []
    assert run(harness.chain_markets(CREATED + 2)) == []
    assert (
        json.loads((tmp_path / "lightning_chain.json").read_text())["last_block"]
        == BLOCK
    )
    harness.calls = []
    assert run(harness.chain_markets(CREATED + 3)) == []
    assert harness.calls == [("get_block", {"block_identifier": "latest"})]


def test_rpc_fallback_requires_continuous_error_interval(tmp_path: Path) -> None:
    """Use subgraph only after the full RPC failure interval and reset on success."""
    harness: Any = Harness(tmp_path)
    harness.fail = True
    assert run(harness.chain_markets(CREATED)) == []
    assert run(harness.chain_markets(CREATED + 60)) == []
    assert run(harness.chain_markets(CREATED + 61)) is None
    harness.fail = False
    assert len(run(harness.chain_markets(CREATED + 62))) == 1
    harness.fail = True
    assert run(harness.chain_markets(CREATED + 63)) == []


def test_transient_materialization_does_not_drop_log(tmp_path: Path) -> None:
    """Retry an indexed market when its receipt or title is temporarily missing."""
    harness: Any = Harness(tmp_path)
    harness.receipt = {"logs": []}
    assert run(harness.chain_markets(CREATED + 5)) == []
    harness.receipt = {"logs": FIXTURE["logs"]}
    rows = run(harness.chain_markets(CREATED + 10))
    assert len(rows) == 1
    filters = [
        kwargs["filter_params"] for name, kwargs in harness.calls if name == "get_logs"
    ]
    assert filters[0]["fromBlock"] == filters[1]["fromBlock"]


@pytest.mark.parametrize(
    "enabled,window", [(False, "00:00-23:59"), (True, "12:00-13:00")]
)
def test_disabled_and_outside_window_do_not_call_rpc(
    tmp_path: Path, enabled: bool, window: str
) -> None:
    """Keep the standard lane unchanged and stop chain reads outside the window."""
    harness: Any = Harness(tmp_path)
    harness.params.lightning.lightning_chain_detect = enabled
    harness.params.fast = FastConfig(active_window_utc=window)
    assert run(harness.chain_markets(CREATED)) == ([] if enabled else None)
    assert harness.calls == []


def test_one_missing_title_does_not_block_other_markets(tmp_path: Path) -> None:
    """Persist a ready market while retaining a failed market's block for retry."""
    harness: Any = Harness(tmp_path)
    second = dict(
        CREATION,
        data="0x" + "00" * 12 + "ab" * 20 + CREATION["data"][66:],
        logIndex=CREATION["logIndex"] + 1,
        transactionHash="0x" + "12" * 32,
    )
    harness.logs = [CREATION, second]
    original_rpc = harness._chain_rpc

    def rpc(method: str, **kwargs: Any) -> Any:
        """Keep only the first market's title temporarily unavailable."""
        if (
            method == "get_transaction_receipt"
            and kwargs["transaction_hash"] == CREATION["transactionHash"]
        ):
            return (yield from done({"logs": []}))
        return (yield from original_rpc(method, **kwargs))

    harness._chain_rpc = rpc
    rows = run(harness.chain_markets(CREATED + 5))
    assert [row["id"] for row in rows] == ["0x" + "ab" * 20]
    assert len(FreshQueue(tmp_path / "fresh_watch.json").pending) == 1
    state = json.loads((tmp_path / "lightning_chain.json").read_text())
    assert state["last_block"] < BLOCK
    harness._chain_rpc = original_rpc
    assert len(run(harness.chain_markets(CREATED + 10))) == 2
    assert len(FreshQueue(tmp_path / "fresh_watch.json").pending) == 2


def test_pool_rpc_failures_eventually_allow_subgraph_fallback(tmp_path: Path) -> None:
    """Count failed pool reads toward the continuous RPC failure interval."""
    harness: Any = Harness(tmp_path)
    harness._get_pool_balances = lambda *args: done(None)
    assert run(harness.chain_markets(CREATED)) == []
    assert run(harness.chain_markets(CREATED + 61)) is None


def test_rpc_helper_uses_own_ledger_and_bounded_wait(tmp_path: Path) -> None:
    """Use the configured ledger chain and unwrap list results."""
    from packages.valory.protocols.ledger_api import LedgerApiMessage

    harness: Any = Harness(tmp_path)
    harness.context.ledger_api_dialogues = MagicMock()
    harness.context.ledger_api_dialogues.create.return_value = (
        MagicMock(),
        MagicMock(),
    )
    harness.context.default_ledger_id = "ethereum"
    harness.context.requests = SimpleNamespace(request_id_to_callback={})
    harness.context.outbox = MagicMock()
    harness._get_default_terms = lambda: None
    harness._get_request_nonce_from_dialogue = lambda dialogue: "nonce"
    harness.get_callback_request = lambda: "callback"
    harness.wait_for_message = MagicMock(
        side_effect=lambda timeout: done(
            SimpleNamespace(
                performative=LedgerApiMessage.Performative.STATE,
                state=SimpleNamespace(body={"get_logs_result": [CREATION]}),
            )
        )
    )
    rows = run(
        ChainDiscoveryMixin._chain_rpc(
            harness, "get_logs", filter_params={"fromBlock": BLOCK}
        )
    )
    assert rows == [CREATION]
    inputs = harness.context.ledger_api_dialogues.create.call_args.kwargs
    assert inputs["ledger_id"] == "ethereum"
    assert inputs["callable"] == "get_logs"
    assert inputs["kwargs"].body == {
        "filter_params": {"fromBlock": BLOCK},
        "chain_id": "gnosis",
    }
    harness.wait_for_message.assert_called_once_with(timeout=5.0)
    assert harness.context.requests.request_id_to_callback == {"nonce": "callback"}


def test_overnight_cursor_gap_scans_only_fresh_blocks(tmp_path: Path) -> None:
    """Bound an old durable cursor without dropping a fresh creation log."""
    harness = Harness(tmp_path)
    (tmp_path / "lightning_chain.json").write_text(
        json.dumps({"last_block": BLOCK - 15000, "rpc_error_since": None})
    )
    rows = run(harness.chain_markets(CREATED + 8))
    assert [row["id"] for row in rows] == [creation_details(CREATION)["id"]]
    filters = next(
        kwargs["filter_params"] for name, kwargs in harness.calls if name == "get_logs"
    )
    assert filters["fromBlock"] == BLOCK - harness.params.fast.fresh_max_age_s // 5 - 1
    assert filters["toBlock"] == BLOCK
    state = json.loads((tmp_path / "lightning_chain.json").read_text())
    assert state["last_block"] == BLOCK
