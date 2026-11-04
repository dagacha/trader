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

"""Check mempool timing, nonce use, and receipt attribution."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from eth_abi import encode
from eth_utils import keccak  # type: ignore[import-not-found]

from packages.valory.skills.fresh_watch_abci.lane_behaviour import LaneRPCError
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import (
    done,
    raw_market,
    run,
)
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MARKET,
    MODULE,
    SAFE,
    TOKEN,
    lane,
    prepared,
    reply_on_send,
    settled,
)


def fill_log(market: str = MARKET, buyer: str = SAFE) -> dict:
    """Return a real binary market buy event."""
    return dict(
        address=market,
        topics=[
            "0x"
            + keccak(text="FPMMBuy(address,uint256,uint256,uint256,uint256)").hex(),
            "0x" + buyer[2:].rjust(64, "0"),
            "0x" + "00" * 32,
        ],
        data="0x" + encode(["uint256"] * 3, [4 * 10**18, 0, 6 * 10**18]).hex(),
    )


def pending(seen: float = 1002) -> Any:
    """Use a complete pending market row."""
    row = raw_market()
    row.update(id=MARKET, collateralToken=TOKEN, creationTimestamp=int(seen))
    return SimpleNamespace(
        market=row,
        creation_tx="0xcreation",
        seen_at=seen,
        priority_fee=10**9,
        max_fee=2000000007,
        initial_funds=7 * 10**18,
    )


@pytest.mark.parametrize(
    "logs", [[], [fill_log(buyer=TOKEN)], [fill_log(market=TOKEN)]]
)
def test_status_one_without_our_buy_is_no_fill(tmp_path: Any, logs: list) -> None:
    """A successful empty-code call must not count as a placed bet."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    with patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd:
        sd.return_value.safe_contract_address = SAFE
        sd.return_value.previous_checkpoint = None
        b._complete(info, {"status": "0x1", "logs": logs}, {"timestamp": hex(1005)})
    assert b.durable_trade_count() == 0
    assert b.read_counted_placement_keys() == set()
    assert not (tmp_path / "multi_bets.json").exists()
    assert (
        json.loads((tmp_path / "fresh_watch.json").read_text())["pending"][0]["id"]
        == MARKET
    )
    assert "status=no_fill" in str(b.context.logger.method_calls)
    assert "lightning_lane_inflight" not in b.context.shared_state
    assert "lightning_lane_eoa_nonce" not in b.context.shared_state


@pytest.mark.parametrize("signing_delay", [0, 0.12, 0.18])
def test_sign_first_and_send_at_due_time(tmp_path: Any, signing_delay: float) -> None:
    """Measure delay from first sight and use the cached nonce and funds."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage

    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    prepared(b)
    b._mempool = MagicMock()
    b._mempool.take_head.return_value = None
    b._rpc = MagicMock(side_effect=AssertionError("send path uses cached state"))
    clock = [1002.0]
    b.sleep = MagicMock(side_effect=lambda delay: advance(delay))

    def advance(delay: float) -> Any:
        assert 0 <= delay <= 0.05
        clock[0] += delay
        return done(None)

    def response() -> Any:
        clock[0] += signing_delay
        return SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction(
                "ethereum", {"raw_transaction": "0xabcdef"}
            ),
        )

    reply_on_send(b, response, signing=True)
    sent = []
    b._broadcast = MagicMock(side_effect=lambda *_: sent.append(clock[0]))
    candidate = pending()
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", side_effect=lambda: clock[0]),
    ):
        sd.return_value.safe_contract_address = SAFE
        sd.return_value.previous_checkpoint = None
        assert run(
            b._send(
                candidate.market,
                SimpleNamespace(stake=4.0, price=0.5, p_hat=0.95),
                (0.9, 4444444444444444445),
                {},
                1002,
                pending=candidate,
            )
        )
    assert sent == [pytest.approx(1002 + max(0.15, signing_delay))]
    tx = b.context.signing_dialogues.create.call_args.kwargs["raw_transaction"].body
    assert tx["maxPriorityFeePerGas"] == 10**9
    assert tx["maxFeePerGas"] == 2000000007
    assert tx["nonce"] == 7
    assert MARKET in b.context.shared_state["lightning_mempool_sent_markets"]
    assert "sent_ms=" in str(b.context.logger.info.call_args)
    b._rpc.assert_not_called()


@pytest.mark.parametrize("when", ["signing", "delay"])
def test_head_before_send_cancels_without_blocking_fallback(
    tmp_path: Any, when: str
) -> None:
    """Cancel a signed pending shot when its next block has arrived."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage

    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    prepared(b)
    b._mempool = MagicMock()
    head = {"timestamp": hex(1005), "number": "0x3"}
    b._mempool.take_head.side_effect = [head] if when == "signing" else [None, head]
    reply_on_send(
        b,
        SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction(
                "ethereum", {"raw_transaction": "0xabcdef"}
            ),
        ),
        signing=True,
    )
    b._broadcast = MagicMock()
    with (
        patch.object(type(b), "synchronized_data", new_callable=PropertyMock) as sd,
        patch(MODULE + ".time.time", return_value=1002),
    ):
        sd.return_value.safe_contract_address = SAFE
        sd.return_value.previous_checkpoint = None
        assert not run(
            b._send(
                pending().market,
                SimpleNamespace(stake=4.0, price=0.5, p_hat=0.95),
                (0.9, 4444444444444444445),
                {},
                1002,
                pending=pending(),
            )
        )
    b._broadcast.assert_not_called()
    assert not b.context.shared_state.get("lightning_mempool_sent_markets")
    assert not b.context.shared_state.get("lightning_lane_inflight")
    assert b._shots == 0


@pytest.mark.parametrize("flag,url", [(False, "wss://rpc.test/key"), (True, "")])
def test_mempool_off_never_starts_source(tmp_path: Any, flag: bool, url: str) -> None:
    """Keep the ordinary lane unchanged without both mempool controls."""
    b = lane(tmp_path, lightning_mempool=flag, lightning_wss_url=url)
    with patch(MODULE + ".MempoolSource") as source:
        run(b._drain_mempool(1002))
        run(b._drain_mempool(1002))
    source.assert_not_called()
    assert b._mempool is None
    assert b.context.logger.info.call_count == int(flag)


def test_mempool_source_window_margin_and_teardown(tmp_path: Any) -> None:
    """Start inside the five-minute margin and stop outside it or on teardown."""
    from dataclasses import replace

    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    b.context.params.fast = replace(b.params.fast, active_window_utc="01:00-02:00")
    with patch(MODULE + ".MempoolSource") as cls:
        source = cls.return_value
        source.drain.return_value = []
        source.take_head.return_value = None
        run(b._drain_mempool(3299))
        cls.assert_not_called()
        run(b._drain_mempool(3300))
        source.start.assert_called_once()
        run(b._drain_mempool(7501))
        source.stop.assert_called_once()
        assert b._mempool is None
        run(b._drain_mempool(3600))
        b.stop()
        assert source.stop.call_count == 2


def test_pending_replacements_dedupe_by_market_before_signing(tmp_path: Any) -> None:
    """Ignore replacement hashes for the same predicted market."""
    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    prepared(b)
    b._checked = b._safe_ok = True
    b._mempool = MagicMock()
    b._mempool.take_head.return_value = None
    first, replacement = pending(), pending()
    replacement.creation_tx = "0xreplacement"
    b._mempool.drain.return_value = [first, replacement]
    b._send = MagicMock(return_value=done(False))
    decision = (SimpleNamespace(stake=4.0, p_hat=0.95), (0.9, 5 * 10**18), "")
    with patch(MODULE + ".lane_decision", return_value=decision):
        run(b._drain_mempool(1002))
    assert b._send.call_count == 1


@pytest.mark.parametrize("missing", ["nonce", "balances", "settled"])
def test_pending_missing_cached_state_skips_without_rpc(
    tmp_path: Any, missing: str
) -> None:
    """Refuse a pending shot until the idle nonce and balances are ready."""
    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    prepared(b)
    if missing == "nonce":
        b.context.shared_state.pop("lightning_lane_eoa_nonce")
    elif missing == "balances":
        b._balances.clear()
    else:
        b.context.shared_state["lightning_lane_last_fsm_settlement"] = 1001
    b._rpc = MagicMock(side_effect=AssertionError("send must not read RPC"))
    with patch(MODULE + ".time.time", return_value=1002):
        assert not run(
            b._send(
                pending().market,
                SimpleNamespace(stake=4.0, p_hat=0.95),
                (0.9, 5 * 10**18),
                {},
                1002,
                pending=pending(),
            )
        )
    b._rpc.assert_not_called()
    assert "cached_" in str(b.context.logger.info.call_args)


def test_pending_arrival_interrupts_polling_rpc_wait(tmp_path: Any) -> None:
    """Drain a pending creation while a polling RPC response is still absent."""
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller

    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    prepared(b)
    b._checked = b._safe_ok = True
    b._mempool = MagicMock()
    b._mempool.take_head.return_value = None
    b._mempool.drain.return_value = []
    clock = [1002.0]
    event = pending()
    observed = []

    def parent_sleep(seconds: float) -> Any:
        assert seconds <= 0.05
        clock[0] += seconds
        b._mempool.drain.return_value = [event]
        yield

    b.sleep = lambda seconds: type(b).sleep(b, seconds)

    def send(*args: Any, **kwargs: Any) -> Any:
        observed.append(clock[0])
        return done(False)

    b._send = MagicMock(side_effect=send)
    b._build_http_request_message = MagicMock(return_value=(MagicMock(), MagicMock()))
    response = (SimpleNamespace(stake=4.0, p_hat=0.95), (0.9, 5 * 10**18), "")
    with (
        patch.object(FreshMarketPoller, "sleep", side_effect=parent_sleep),
        patch(MODULE + ".time.time", side_effect=lambda: clock[0]),
        patch(MODULE + ".lane_decision", return_value=response),
    ):
        waiting = b._rpc("eth_blockNumber", [])
        next(waiting)
        next(waiting)
        waiting.close()
    assert observed == [pytest.approx(1002.05)]
    assert b._send.call_count == 1


@pytest.mark.parametrize(
    "reason", ["head", "cap", "priority", "sent", "strategy", "reset"]
)
def test_pending_queue_guards_and_night_reset(tmp_path: Any, reason: str) -> None:
    """Apply pending eligibility before signing and keep one nightly budget."""
    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    prepared(b)
    b._checked = b._safe_ok = True
    b._mempool = MagicMock()
    b._mempool.take_head.return_value = None
    candidate = pending()
    b._mempool.drain.return_value = [candidate]
    b._send = MagicMock(return_value=done(False))
    result: Any = (SimpleNamespace(stake=4.0, p_hat=0.95), (0.9, 5 * 10**18), "")
    if reason == "head":
        b._last_head = {"timestamp": hex(1005), "_seen_at": 1002.01}
    elif reason == "cap":
        b._shots = 3
    elif reason == "priority":
        candidate.priority_fee = 2000000001
    elif reason == "sent":
        b.context.shared_state["lightning_mempool_sent_markets"] = {MARKET}
    elif reason == "strategy":
        result = (None, None, "base_rate")
    else:
        b._night = "yesterday"
        b._shots = 3
        b._pending_seen.add(MARKET)
        b._creation_seen[MARKET] = {"number": "0x1"}
        b.context.shared_state["lightning_mempool_sent_markets"] = {MARKET}
    with (
        patch(MODULE + ".time.time", return_value=1002),
        patch(MODULE + ".lane_decision", return_value=result),
    ):
        run(b._drain_mempool(1002))
    assert b._send.call_count == int(reason == "reset")
    if reason == "reset":
        assert b._shots == 0
        assert b._night == "1970-01-01"
        assert not b._creation_seen
        assert not b.context.shared_state["lightning_mempool_sent_markets"]
        saved = json.loads((tmp_path / "lightning_lane_state.json").read_text())
        assert saved["mempool_sent"] == []


@pytest.mark.parametrize(
    "active,inflight", [(False, False), (True, False), (True, True)]
)
def test_sleep_keeps_requested_wait_and_drains_candidates(
    tmp_path: Any, active: bool, inflight: bool
) -> None:
    """Keep the full wait and drain candidates between short source waits."""
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller

    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    prepared(b)
    b._checked = b._safe_ok = True
    clock = [1002.0]
    waits = []
    sent = []
    if active:
        b._mempool = MagicMock()
        b._mempool.take_head.return_value = None
        b._mempool.drain.side_effect = lambda: [pending()]
    if inflight:
        b.context.shared_state["lightning_lane_inflight"] = {"tx": "0xshot"}

    def sleep(seconds: float) -> Any:
        waits.append(seconds)
        clock[0] += seconds
        return done(None)

    def send(*args: Any, **kwargs: Any) -> Any:
        sent.append(clock[0])
        return done(False)

    b._send = send
    decision = (SimpleNamespace(stake=4.0, p_hat=0.95), (0.9, 5 * 10**18), "")
    with (
        patch.object(FreshMarketPoller, "sleep", side_effect=sleep),
        patch(MODULE + ".time.time", side_effect=lambda: clock[0]),
        patch(MODULE + ".lane_decision", return_value=decision),
    ):
        run(type(b).sleep(b, 0.5))
    assert clock[0] - 1002 == pytest.approx(0.5)
    assert sum(waits) == pytest.approx(0.5)
    if active:
        assert max(waits) <= 0.05
        assert sent == ([] if inflight else [pytest.approx(1002.05)])
    else:
        assert waits == [0.5]
    b.clean_up()
    b.clean_up()
    b._take_head()


@pytest.mark.parametrize("now", [1002, 1062])
def test_restart_rebroadcasts_identical_signed_bytes(tmp_path: Any, now: float) -> None:
    """Recover a broadcast without a new signature or fee increase."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    info["signed_transaction"] = {"raw_transaction": "0xabcdef"}
    b.context.shared_state["lightning_lane_inflight"] = info
    b.context.shared_state["lightning_mempool_sent_markets"] = {MARKET}
    b._persist()
    restarted = lane(tmp_path)
    restarted._rpc = MagicMock(side_effect=[done(None), done("0x7")])
    restarted._broadcast = MagicMock()
    with patch(MODULE + ".time.time", return_value=now):
        run(restarted._watch_receipt({}))
    args = restarted._broadcast.call_args.args
    assert args[1] == "0xabcdef"
    assert args[0].signed_transaction.body == info["signed_transaction"]
    assert restarted.context.shared_state["lightning_mempool_sent_markets"] == {MARKET}
    restarted.context.signing_dialogues.create.assert_not_called()
    assert restarted._rebroadcast_tx is None


@pytest.mark.parametrize("creation_block", [None, "0x2", "0x1", "missing", "replaced"])
def test_pending_completion_uses_mined_creation_data_when_available(
    tmp_path: Any, creation_block: Any
) -> None:
    """Complete a real decoded pending row with mined time and index metadata."""
    from packages.valory.skills.fresh_watch_abci.mempool import decode_pending
    from packages.valory.skills.fresh_watch_abci.tests.test_mempool import (
        MULTISEND,
        TRANSACTIONS,
    )

    b = lane(tmp_path)
    candidate = decode_pending(TRANSACTIONS[0], b.params.lightning, MULTISEND, 1002.125)
    assert candidate is not None
    info = dict(
        tx="0xshot",
        market=candidate.market["id"],
        row=candidate.market,
        source="mempool",
        creation_tx=candidate.creation_tx,
        amount=4 * 10**18,
        probability=0.95,
        sent_at=1002,
        eoa_nonce=7,
    )
    b.context.shared_state["lightning_lane_inflight"] = info
    receipt = dict(
        status="0x1",
        blockNumber="0x2",
        transactionIndex="0x5",
        logs=fill_log_list(info["market"]),
    )
    block = {"timestamp": hex(1005), "number": "0x2"}

    def rpc(method: str, params: Any) -> Any:
        if params == ["0xshot"]:
            return done(receipt)
        if params == [candidate.creation_tx]:
            if creation_block == "replaced":
                b.context.shared_state["lightning_lane_inflight"] = {"tx": "0xother"}
                return done(None)
            return done(
                None
                if creation_block is None
                else {
                    "blockNumber": (
                        "0x1" if creation_block == "missing" else creation_block
                    ),
                    "transactionIndex": "0x4",
                }
            )
        return done(
            None
            if creation_block == "missing"
            else {"timestamp": hex(1000), "number": "0x1"}
        )

    b._rpc = MagicMock(side_effect=rpc)
    run(b._watch_receipt(block))
    if creation_block == "replaced":
        assert b.context.shared_state["lightning_lane_inflight"] == {"tx": "0xother"}
        return
    b.read_bets()
    assert b.bets[0].id == candidate.market["id"]
    assert b.bets[0].condition_id == candidate.market["condition"]["id"]
    assert b.bets[0].creationTimestamp == (
        1005 if creation_block == "0x2" else 1000 if creation_block == "0x1" else 1002
    )
    assert b.durable_trade_count() == 1
    if creation_block in {"0x1", "0x2"}:
        assert info["block_delta"] == (0 if creation_block == "0x2" else 1)
    if creation_block == "0x2":
        assert info["creation_index"] == 1


def fill_log_list(market: str) -> list:
    """Wrap the real buy event in a receipt list."""
    return [fill_log(market=market)]


def test_sent_mempool_market_cannot_fire_fallback_even_after_revert(
    tmp_path: Any,
) -> None:
    """Keep one lane shot per market after a consumed mempool transaction."""
    from pathlib import Path

    from packages.valory.skills.fresh_watch_abci.chain_discovery import creation_details

    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "chain_creation.json").read_text()
    )
    log = fixture["logs"][-1]
    market = creation_details(log)
    b = lane(tmp_path)
    b.context.shared_state["lightning_mempool_sent_markets"] = {market["id"]}
    b._rpc = MagicMock(side_effect=AssertionError("market already had its lane shot"))
    run(b._market(log, {"timestamp": hex(1000)}, 1002))
    b._rpc.assert_not_called()
    assert market["id"] in b._creation_seen


def test_pending_persist_failure_returns_market_to_fallback(tmp_path: Any) -> None:
    """Do not mark a pending market sent when its signed journal cannot save."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage

    b = lane(tmp_path, lightning_mempool=True)
    prepared(b)
    reply_on_send(
        b,
        SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction(
                "ethereum", {"raw_transaction": "0xabcdef"}
            ),
        ),
        signing=True,
    )
    b._persist = MagicMock(side_effect=[None, OSError("full disk")])
    candidate = pending(1001)
    with patch(MODULE + ".time.time", return_value=1002):
        with pytest.raises(OSError):
            run(
                b._send(
                    candidate.market,
                    SimpleNamespace(stake=4.0, p_hat=0.95),
                    (0.9, 5 * 10**18),
                    {},
                    1001,
                    pending=candidate,
                )
            )
    assert b._shots == 0
    assert not b.context.shared_state.get("lightning_mempool_sent_markets")


def test_corrupt_journal_can_recover_on_a_later_tick(tmp_path: Any) -> None:
    """Read repaired recovery data before normal lane work resumes."""
    path = tmp_path / "lightning_lane_state.json"
    path.write_text("broken")
    b = lane(tmp_path, lightning_lane=False)
    path.write_text(json.dumps(dict(night="today", shots=1, inflight=None)))
    run(b.async_act())
    assert b._journal_ok
    assert b._shots == 1


def test_queue_failure_stays_inside_lane_tick(tmp_path: Any) -> None:
    """Keep a failed pending tick from stopping the ordinary lane."""
    b = lane(tmp_path, lightning_lane=False)
    b._drain_mempool = MagicMock(side_effect=ValueError("unusable candidate"))
    run(b.async_act())
    assert "[LIGHTNING_MEMPOOL] tick_failed=true" in str(
        b.context.logger.warning.call_args
    )


@pytest.mark.parametrize(
    "error", [LaneRPCError, OSError, ValueError, KeyError, TypeError]
)
def test_sleep_drain_failure_stays_inside_async_act(tmp_path: Any, error: Any) -> None:
    """Log a failed sleep drain and keep the lane tick running."""
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller

    b = lane(tmp_path, lightning_lane=False)
    b._mempool = MagicMock()
    b.sleep = lambda seconds: type(b).sleep(b, seconds)
    clock = [1002.0]

    def sleep(seconds: float) -> Any:
        clock[0] += seconds
        return done(None)

    b._persist = MagicMock(side_effect=error("full disk"))
    calls = [0]

    def drain(now: float) -> Any:
        calls[0] += 1
        if calls[0] > 1:
            b._persist()
        return done(None)

    b._drain_mempool = drain
    with (
        patch.object(FreshMarketPoller, "sleep", side_effect=sleep),
        patch(MODULE + ".time.time", side_effect=lambda: clock[0]),
    ):
        run(b.async_act())
    assert clock[0] == pytest.approx(1003.0)
    b._persist.assert_called()
    b.context.logger.warning.assert_called_with("[LIGHTNING_MEMPOOL] tick_failed=true")
    assert not b._draining_mempool


@pytest.mark.parametrize("read", ["nonce", "eoa", "native", "wrapped"])
@pytest.mark.parametrize("transition", ["inflight", "complete", "settlement"])
def test_idle_refresh_discards_state_after_interleaved_shot(
    tmp_path: Any, read: str, transition: str
) -> None:
    """Do not restore stale caches after pending work changes the nonce lock."""
    b = lane(tmp_path)
    prepared(b)
    b._was_fresh = True
    b._balance_at = 1
    steps = {"nonce": 0, "eoa": 1, "native": 2, "wrapped": 3}
    calls = []

    def rpc(method: str, params: Any) -> Any:
        calls.append(method)
        if len(calls) == steps[read] + 1:
            yield
        return "0x9"

    b._rpc = rpc
    clock = patch(MODULE + ".time.time", return_value=1040)
    with clock:
        refresh = b._refresh_idle(1040)
        next(refresh)
        if transition == "inflight":
            b.context.shared_state["lightning_lane_inflight"] = {"tx": "0xshot"}
        elif transition == "complete":
            info = settled(b, tmp_path)
            b.context.shared_state["lightning_lane_inflight"] = info
            b._complete(info, {"status": "0x0"}, {"timestamp": hex(1040)})
        else:
            b.context.shared_state["lightning_lane_last_fsm_settlement"] = 1040
            b.context.shared_state.pop("lightning_lane_eoa_nonce", None)
        run(refresh)
    assert len(calls) == steps[read] + 1
    assert b._balance_at == (0 if transition == "complete" else 1)
    if transition != "inflight":
        assert "lightning_lane_eoa_nonce" not in b.context.shared_state


def test_real_signing_wait_and_scheduler_tick_meet_send_delay(tmp_path: Any) -> None:
    """Resume an asynchronous signer in short ticks before the due broadcast."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage
    from packages.valory.skills.fresh_watch_abci.behaviours import FreshMarketPoller

    b = lane(tmp_path, lightning_mempool=True, lightning_wss_url="wss://rpc.test/key")
    prepared(b)
    b._mempool = MagicMock()
    b._mempool.take_head.return_value = None
    b._mempool.drain.return_value = []
    b.sleep = lambda seconds: type(b).sleep(b, seconds)
    clock = [1002.0]
    dialogue = MagicMock()
    dialogue.dialogue_label.dialogue_reference = ("reply", "")
    b.context.requests.request_id_to_callback = {}
    b.context.signing_dialogues.create.return_value = (MagicMock(), dialogue)
    response = SimpleNamespace(
        performative=SigningMessage.Performative.SIGNED_TRANSACTION,
        signed_transaction=SignedTransaction(
            "ethereum", {"raw_transaction": "0xabcdef"}
        ),
    )
    waits = []
    delivered: list[float] = []

    def sleep(seconds: float) -> Any:
        waits.append(seconds)
        clock[0] += seconds
        if not delivered:
            b.context.requests.request_id_to_callback["reply"](response, b)
            delivered.append(clock[0])
        yield

    sent = []

    def broadcast(*args: Any) -> None:
        sent.append(clock[0])

    b._broadcast = broadcast
    candidate = pending()
    assert b.tick_interval <= 0.05
    with (
        patch.object(FreshMarketPoller, "sleep", side_effect=sleep),
        patch(MODULE + ".time.time", side_effect=lambda: clock[0]),
    ):
        assert run(
            b._send(
                candidate.market,
                SimpleNamespace(stake=4.0, p_hat=0.95),
                (0.9, 5 * 10**18),
                {},
                1002,
                pending=candidate,
            )
        )
    assert delivered == [pytest.approx(1002.05)]
    assert sent == [pytest.approx(1002.15)]
    assert max(waits) <= 0.05


@pytest.mark.parametrize("started", [1002.0, 1002.2])
def test_sent_ms_includes_final_journal_write_time(
    tmp_path: Any, started: float
) -> None:
    """Log the actual broadcast time after the signed journal reaches disk."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage

    b = lane(tmp_path, lightning_mempool=True)
    prepared(b)
    reply_on_send(
        b,
        SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction(
                "ethereum", {"raw_transaction": "0xabcdef"}
            ),
        ),
        signing=True,
    )
    clock = [started]
    sent = []

    def persist() -> None:
        clock[0] += 0.02

    def broadcast(*args: Any) -> None:
        sent.append(clock[0])

    def advance(seconds: float) -> Any:
        clock[0] += seconds
        return done(None)

    b.sleep = advance
    b._persist = persist
    b._broadcast = broadcast
    with patch(MODULE + ".time.time", side_effect=lambda: clock[0]):
        run(
            b._send(
                pending().market,
                SimpleNamespace(stake=4.0, p_hat=0.95),
                (0.9, 5 * 10**18),
                {},
                1002,
                pending=pending(),
            )
        )
    assert sent == [pytest.approx(max(1002.15, started + 0.04))]
    assert b.context.logger.info.call_args.args[4] == pytest.approx(
        (sent[0] - 1002) * 1000
    )


def test_head_during_final_journal_write_cancels_before_broadcast(
    tmp_path: Any,
) -> None:
    """Recheck the source after disk I/O before sending the signed bytes."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage

    b = lane(tmp_path, lightning_mempool=True)
    prepared(b)
    b._mempool = MagicMock()
    b._mempool.take_head.return_value = None
    persist = b._persist
    writes = []

    def save() -> None:
        persist()
        writes.append(True)
        if len(writes) == 2:
            b._mempool.take_head.return_value = {
                "timestamp": hex(1005),
                "number": "0x3",
                "_seen_at": 1002.2,
            }

    b._persist = save
    reply_on_send(
        b,
        SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction(
                "ethereum", {"raw_transaction": "0xabcdef"}
            ),
        ),
        signing=True,
    )
    b._broadcast = MagicMock()
    with patch(MODULE + ".time.time", return_value=1002.2):
        assert not run(
            b._send(
                pending().market,
                SimpleNamespace(stake=4.0, p_hat=0.95),
                (0.9, 5 * 10**18),
                {},
                1002,
                pending=pending(),
            )
        )
    b._broadcast.assert_not_called()
    assert not b.context.shared_state.get("lightning_mempool_sent_markets")
    assert not b.context.shared_state.get("lightning_lane_inflight")
    assert b._shots == 0


def test_startup_recovery_keeps_original_pending_send_deadline(tmp_path: Any) -> None:
    """Do not rebroadcast a saved signed intent before its original delay."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    info.update(
        source="mempool",
        first_seen=1002.0,
        creation_tx="0xcreation",
        signed_transaction={"raw_transaction": "0xabcdef"},
    )
    b.context.shared_state["lightning_lane_inflight"] = info
    b._persist()
    restarted = lane(tmp_path)
    restarted._broadcast = MagicMock()
    restarted._rpc = MagicMock(side_effect=[done(None), done("0x7")])
    with patch(MODULE + ".time.time", return_value=1002.05):
        run(restarted._watch_receipt({}))
    restarted._broadcast.assert_not_called()
    restarted._rpc.assert_not_called()
    assert restarted._rebroadcast_tx == info["tx"]
    with patch(MODULE + ".time.time", return_value=1002.15):
        run(restarted._watch_receipt({}))
    assert restarted._broadcast.call_args.args[1] == "0xabcdef"


def test_pending_deadline_during_delay_rolls_back_saved_intent(tmp_path: Any) -> None:
    """Release a signed pending intent if the question closes before send."""
    from aea.helpers.transaction.base import SignedTransaction

    from packages.open_aea.protocols.signing import SigningMessage

    b = lane(tmp_path, lightning_mempool=True)
    prepared(b)
    candidate = pending(1001.9)
    candidate.market["openingTimestamp"] = 1002
    reply_on_send(
        b,
        SimpleNamespace(
            performative=SigningMessage.Performative.SIGNED_TRANSACTION,
            signed_transaction=SignedTransaction(
                "ethereum", {"raw_transaction": "0xabcdef"}
            ),
        ),
        signing=True,
    )
    clock = [1001.9]

    def sleep(seconds: float) -> Any:
        clock[0] += seconds
        return done(None)

    b.sleep = sleep
    b._broadcast = MagicMock()
    with patch(MODULE + ".time.time", side_effect=lambda: clock[0]):
        assert not run(
            b._send(
                candidate.market,
                SimpleNamespace(stake=4.0, p_hat=0.95),
                (0.9, 5 * 10**18),
                {},
                candidate.seen_at,
                pending=candidate,
            )
        )
    b._broadcast.assert_not_called()
    assert b._shots == 0
    assert not b.context.shared_state.get("lightning_lane_inflight")
    assert not b.context.shared_state.get("lightning_mempool_sent_markets")
    assert "pending_expired" in str(b.context.logger.info.call_args)


def recovered_intent(tmp_path: Any, loaded_at: float = 1002.05) -> Any:
    """Load a saved signed intent with its original send deadline."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    info.update(
        source="mempool",
        first_seen=1002.0,
        send_after=1002.15,
        creation_tx="0xcreation",
        signed_transaction={"raw_transaction": "0xabcdef"},
    )
    b._shots = 1
    b.context.shared_state["lightning_lane_inflight"] = info
    b.context.shared_state["lightning_mempool_sent_markets"] = {MARKET}
    b._persist()
    with patch(MODULE + ".time.time", return_value=loaded_at):
        restarted = lane(tmp_path)
    restarted._mempool = MagicMock()
    restarted._mempool.take_head.return_value = None
    restarted._broadcast = MagicMock()
    restarted._rpc = MagicMock(side_effect=[done(None), done("0x7")])
    return restarted


@pytest.mark.parametrize("reason", ["head", "creation", "expiry"])
def test_known_unsent_recovery_cancels_before_first_broadcast(
    tmp_path: Any, reason: str
) -> None:
    """Cancel an intent restored before its delay when the block arrives."""
    b = recovered_intent(tmp_path)
    now = 1002.15
    if reason == "head":
        b._mempool.take_head.return_value = {"timestamp": hex(1005), "_seen_at": 1002.1}
    elif reason == "creation":
        b._creation_seen[MARKET] = {"number": "0x3"}
    else:
        now = 1010
    with patch(MODULE + ".time.time", return_value=now):
        run(b._watch_receipt({}))
    b._broadcast.assert_not_called()
    b._rpc.assert_not_called()
    assert not b.context.shared_state.get("lightning_lane_inflight")
    assert not b.context.shared_state.get("lightning_mempool_sent_markets")
    assert b._shots == 0
    saved = json.loads((tmp_path / "lightning_lane_state.json").read_text())
    assert saved["inflight"] is None
    assert saved["mempool_sent"] == []


def test_known_unsent_recovery_rechecks_head_after_nonce_read(tmp_path: Any) -> None:
    """A head received during recovery RPC must cancel the first broadcast."""
    b = recovered_intent(tmp_path)

    def rpc(method: str, params: Any) -> Any:
        if method == "eth_getTransactionReceipt":
            return done(None)
        b._mempool.take_head.return_value = {"timestamp": hex(1005), "_seen_at": 1002.1}
        return done("0x7")

    b._rpc = rpc
    with patch(MODULE + ".time.time", return_value=1002.15):
        run(b._watch_receipt({}))
    b._broadcast.assert_not_called()
    assert b._shots == 0
    assert not b.context.shared_state.get("lightning_lane_inflight")


@pytest.mark.parametrize("loaded_at", [1002.05, 1002.2])
def test_recovery_keeps_identical_bytes_after_due_without_cancellation(
    tmp_path: Any, loaded_at: float
) -> None:
    """Keep normal recovery for an eligible intent or an uncertain prior send."""
    b = recovered_intent(tmp_path, loaded_at)
    if loaded_at > 1002.15:
        b._mempool.take_head.return_value = {"timestamp": hex(1005), "_seen_at": 1002.1}
    with patch(MODULE + ".time.time", return_value=1002.2):
        run(b._watch_receipt({}))
    assert b._broadcast.call_args.args[1] == "0xabcdef"
    assert b._shots == 1
    assert b.context.shared_state["lightning_mempool_sent_markets"] == {MARKET}
