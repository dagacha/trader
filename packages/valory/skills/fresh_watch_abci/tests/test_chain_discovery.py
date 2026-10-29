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
from eth_abi import decode, encode

from packages.valory.protocols.ledger_api import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.behaviour_utils import TimeoutException
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
CONDITION = FIXTURE["logs"][1]
QUESTION = FIXTURE["logs"][0]


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
                lightning_preview=False,
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


@pytest.mark.parametrize("topics", [[], ["0x" + "00" * 32, CREATION["topics"][1]]])
def test_creation_rejects_other_event_topics(topics: list[str]) -> None:
    """Do not decode logs with missing or unrelated event topics."""
    with pytest.raises(ValueError, match="Unexpected factory event"):
        creation_details(dict(CREATION, topics=topics))


@pytest.mark.parametrize("count", [0, 2])
def test_creation_requires_one_condition(count: int) -> None:
    """Reject market creations without exactly one condition."""
    types = ["address", "address", "address", "bytes32[]", "uint256"]
    values = list(decode(types, bytes.fromhex(CREATION["data"][2:])))
    values[3] = values[3] * count
    with pytest.raises(ValueError, match="Require one market condition"):
        creation_details(dict(CREATION, data=encode(types, values)))


def test_receipt_rejects_nonbinary_condition() -> None:
    """Do not use a title when its condition has three outcomes."""
    condition = dict(CONDITION, data=encode(["uint256"], [3]))
    assert receipt_question([QUESTION, condition], CONDITION["topics"][1]) is None


@pytest.mark.parametrize(
    "template,title,outcomes",
    [
        (0, "Will it happen?", ["Yes", "No"]),
        (2, 'Will it happen?\x1f"Yes","No"', ["Yes", "No"]),
        (2, 'Will it happen?␟"No","Yes"', None),
        (2, "Will it happen?", None),
        (1, "Will it happen?", None),
    ],
)
def test_receipt_accepts_only_binary_question_templates(
    template: int, title: str, outcomes: Any
) -> None:
    """Read supported templates and preserve the order of binary outcomes."""
    types = ["uint256", "string", "address", "uint32", "uint32", "uint256", "uint256"]
    values = list(decode(types, bytes.fromhex(QUESTION["data"][2:])))
    values[:2] = [template, title]
    question = dict(QUESTION, data=encode(types, values))
    unrelated = dict(QUESTION, topics=[QUESTION["topics"][0], "0x" + "00" * 32, "", ""])
    logs = [CREATION, CONDITION, unrelated, question]
    result = receipt_question(logs, CONDITION["topics"][1])
    if outcomes is None:
        assert result is None
    else:
        assert result == {
            "title": "Will it happen?",
            "openingTimestamp": 1793664000,
            "outcomes": outcomes,
            "outcomeSlotCount": 2,
        }


def test_receipt_without_matching_question_waits_for_subgraph() -> None:
    """Return no title when the condition exists but its question log is absent."""
    assert receipt_question([CONDITION, CREATION], CONDITION["topics"][1]) is None


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
    assert [address.lower() for address in filters["address"]] == [FACTORY.lower()]
    assert filters["topics"] == [[CREATION_TOPIC]]
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


@pytest.fixture
def ledger_harness(tmp_path: Path) -> Any:
    """Prepare the ledger dialogue and request callback without a live connection."""
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
    return harness


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"get_block_result": {"number": BLOCK}}, {"number": BLOCK}),
        ({"number": BLOCK}, {"number": BLOCK}),
    ],
)
def test_rpc_helper_uses_own_ledger_and_bounded_wait(
    ledger_harness: Any, body: dict[str, Any], expected: Any
) -> None:
    """Use the configured ledger chain and accept wrapped or raw results."""
    harness = ledger_harness
    harness.wait_for_message = MagicMock(
        side_effect=lambda timeout: done(
            SimpleNamespace(
                performative=LedgerApiMessage.Performative.STATE,
                state=SimpleNamespace(body=body),
            )
        )
    )
    rows = run(
        ChainDiscoveryMixin._chain_rpc(harness, "get_block", block_identifier="latest")
    )
    assert rows == expected
    inputs = harness.context.ledger_api_dialogues.create.call_args.kwargs
    assert inputs["ledger_id"] == "ethereum"
    assert inputs["callable"] == "get_block"
    assert inputs["kwargs"].body == {
        "block_identifier": "latest",
        "chain_id": "gnosis",
    }
    harness.wait_for_message.assert_called_once_with(timeout=5.0)
    assert harness.context.requests.request_id_to_callback == {"nonce": "callback"}


@pytest.mark.parametrize("timeout", [False, True])
def test_rpc_helper_reports_ledger_errors(ledger_harness: Any, timeout: bool) -> None:
    """Expose ledger errors and bounded wait timeouts to the fallback interval."""
    if timeout:
        ledger_harness.wait_for_message = MagicMock(side_effect=TimeoutException())
    else:
        ledger_harness.wait_for_message = lambda timeout: done(
            SimpleNamespace(
                performative=LedgerApiMessage.Performative.ERROR,
                message="RPC rejected the request",
            )
        )
    message = "Ledger response timed out" if timeout else "RPC rejected the request"
    with pytest.raises(ChainRPCError, match=message):
        run(ChainDiscoveryMixin._chain_rpc(ledger_harness, "get_block"))


@pytest.mark.parametrize("rows", [[{"id": "market", "title": "Exact title"}], [], {}])
def test_subgraph_lookup_uses_exact_market_id(tmp_path: Path, rows: Any) -> None:
    """Return the exact market row only when the indexer has a usable list."""
    harness: Any = Harness(tmp_path)
    response = object()
    harness.get_http_response = MagicMock(return_value=done(response))
    harness.context.omen_subgraph = SimpleNamespace(
        get_spec=lambda: {"method": "POST", "url": "http://subgraph.invalid"},
        process_response=lambda reply: rows if reply is response else None,
    )
    market_id = creation_details(CREATION)["id"]
    result = run(ChainDiscoveryMixin._chain_subgraph_market(harness, market_id))
    assert result == (rows[0] if isinstance(rows, list) and rows else None)
    request = harness.get_http_response.call_args.kwargs
    query = json.loads(request["content"])["query"]
    assert 'where: {id: "' + market_id + '"}' in query
    assert "outcomeTokenMarginalPrices" in query
    assert request["method"] == "POST"
    assert request["url"] == "http://subgraph.invalid"


def test_subgraph_title_fallback_reports_source(tmp_path: Path) -> None:
    """Return the indexed market and identify its delayed title source."""
    harness: Any = Harness(tmp_path)
    harness.receipt = {"logs": []}
    market = {"id": creation_details(CREATION)["id"], "title": "Exact title"}
    harness.fallback = market
    assert (
        run(harness._chain_market(CREATION, {"timestamp": hex(CREATED)}, CREATED + 8))
        == market
    )
    harness.context.logger.info.assert_called_once_with(
        "[LIGHTNING_DETECT] market=%s delay_s=%.3f source=%s",
        market["id"],
        8,
        "subgraph",
    )


def test_empty_pool_has_zero_prices(tmp_path: Path) -> None:
    """Represent an empty pool without dividing by zero."""
    harness: Any = Harness(tmp_path)
    harness._get_pool_balances = lambda *args: done([0, 0])
    row = run(harness._chain_market(CREATION, {"timestamp": CREATED}, CREATED))
    assert row["outcomeTokenAmounts"] == [0, 0]
    assert row["outcomeTokenMarginalPrices"] == [0, 0]
    assert row["scaledLiquidityMeasure"] == 0


def test_stale_creation_advances_cursor_without_queueing(tmp_path: Path) -> None:
    """Do not read the receipt or retain a market past the freshness limit."""
    harness = Harness(tmp_path)
    assert (
        run(harness.chain_markets(CREATED + harness.params.fast.fresh_max_age_s + 1))
        == []
    )
    assert not FreshQueue(tmp_path / "fresh_watch.json").pending
    assert all(method != "get_transaction_receipt" for method, _ in harness.calls)
    state = json.loads((tmp_path / "lightning_chain.json").read_text())
    assert state == {"last_block": BLOCK, "rpc_error_since": None}


def test_invalid_get_logs_result_starts_fallback_interval(tmp_path: Path) -> None:
    """Treat a malformed log response as an RPC failure and keep the scan cursor."""
    harness: Any = Harness(tmp_path)
    harness.logs = {}
    assert run(harness.chain_markets(CREATED)) == []
    assert run(harness.chain_markets(CREATED + 61)) is None
    assert not (tmp_path / "fresh_watch.json").exists()
    state = json.loads((tmp_path / "lightning_chain.json").read_text())
    assert state["last_block"] is None
    assert state["rpc_error_since"] == CREATED


def test_chain_filters_removed_wrong_factory_and_wrong_creator(tmp_path: Path) -> None:
    """Queue only the allowed creator event emitted by the configured factory."""
    harness = Harness(tmp_path)
    harness.logs = [
        dict(CREATION, removed=True),
        dict(CREATION, address="0x" + "ab" * 20),
        dict(CREATION, topics=[CREATION_TOPIC, "0x" + "00" * 32]),
        CREATION,
    ]
    rows = run(harness.chain_markets(CREATED + 5))
    assert [row["id"] for row in rows] == [creation_details(CREATION)["id"]]
    assert len(FreshQueue(tmp_path / "fresh_watch.json").pending) == 1
    assert (
        len([name for name, _ in harness.calls if name == "get_transaction_receipt"])
        == 1
    )


def test_creation_before_head_uses_its_own_block(tmp_path: Path) -> None:
    """Fetch the creation header when a log is older than the current head."""
    harness = Harness(tmp_path)
    harness.head = BLOCK + 1
    rows = run(harness.chain_markets(CREATED + 5))
    assert [row["creationTimestamp"] for row in rows] == [CREATED]
    assert ("get_block", {"block_identifier": BLOCK}) in harness.calls
    state = json.loads((tmp_path / "lightning_chain.json").read_text())
    assert state["last_block"] == BLOCK + 1


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


def test_chain_and_preview_share_two_idle_rpc_calls(tmp_path: Path) -> None:
    """Use one head and one merged event scan for an idle discovery tick."""
    from packages.valory.skills.fresh_watch_abci.preview import (
        DELIVERY_TOPIC,
        PROPOSER,
        PreviewMixin,
        REQUEST_TOPIC,
    )

    class CombinedHarness(Harness, PreviewMixin):
        """Run both pollers with one saved chain."""

    harness: Any = CombinedHarness(tmp_path)
    harness.logs = []
    harness.params.lightning.lightning_preview = True
    marketplace = "0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB"
    harness.params.mech_marketplace_config = SimpleNamespace(
        mech_marketplace_address=marketplace
    )
    assert run(harness.chain_markets(CREATED + 8)) == []
    run(harness.preview_poll(CREATED + 8))
    assert [method for method, _ in harness.calls] == ["get_block", "get_logs"]
    filters = harness.calls[1][1]["filter_params"]
    assert {address.lower() for address in filters["address"]} == {
        FACTORY.lower(),
        marketplace.lower(),
        PROPOSER.lower(),
    }
    assert filters["topics"] == [[CREATION_TOPIC, REQUEST_TOPIC, DELIVERY_TOPIC]]
    assert PreviewMixin.preview_store(harness).last_block == BLOCK


def test_log_rpc_uses_contract_state_and_bounded_wait(ledger_harness: Any) -> None:
    """Dispatch nonempty getLogs through the JSON-safe event reader contract."""
    from packages.valory.protocols.contract_api import ContractApiMessage

    harness = ledger_harness
    harness.context.contract_api_dialogues = MagicMock()
    harness.context.contract_api_dialogues.create.return_value = (
        MagicMock(),
        MagicMock(),
    )
    harness.wait_for_message = lambda timeout: done(
        SimpleNamespace(
            performative=ContractApiMessage.Performative.STATE,
            state=SimpleNamespace(body={"logs": [CREATION]}),
        )
    )
    assert run(
        ChainDiscoveryMixin._chain_rpc(
            harness, "get_logs", filter_params={"fromBlock": BLOCK}
        )
    ) == [CREATION]
    harness.context.ledger_api_dialogues.create.assert_not_called()
    inputs = harness.context.contract_api_dialogues.create.call_args.kwargs
    assert inputs["performative"] == ContractApiMessage.Performative.GET_STATE
    assert inputs["contract_id"] == "valory/omen_event_reader:0.1.0"
    assert inputs["callable"] == "get_logs"
    assert inputs["kwargs"].body == {
        "filter_params": {"fromBlock": BLOCK},
        "chain_id": "gnosis",
    }


def test_shared_rpc_failure_is_not_retried_in_same_tick(tmp_path: Path) -> None:
    """Keep a failed head read within the per-tick RPC budget."""
    from packages.valory.skills.fresh_watch_abci.preview import PreviewMixin

    class CombinedHarness(Harness, PreviewMixin):
        """Share the real scan helper across both callers."""

    harness: Any = CombinedHarness(tmp_path)
    harness.params.lightning.lightning_preview = True
    harness.fail = True
    assert run(harness.chain_markets(CREATED)) == []
    with pytest.raises(ChainRPCError, match="ledger unavailable"):
        run(harness.preview_poll(CREATED))
    assert harness.calls == [("get_block", {"block_identifier": "latest"})]
    assert run(harness.chain_markets(CREATED + 61)) is None
    assert len(harness.calls) == 2
