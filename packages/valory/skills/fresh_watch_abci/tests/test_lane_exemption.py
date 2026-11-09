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

"""Check the optional lane exemption from the durable trade cap."""

# pylint: disable=protected-access

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from eth_abi import encode

from packages.valory.skills.agent_performance_summary_abci.activity_goal import (
    activity_goal_progress,
    count_trades_since,
    read_trades,
    record_trade,
)
from packages.valory.skills.agent_performance_summary_abci.models import (
    write_json_atomically,
)
from packages.valory.skills.fresh_watch_abci.lightning import LightningConfig
from packages.valory.skills.fresh_watch_abci.tests.test_behaviours import done, run
from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MARKET,
    MODULE,
    SAFE,
    buy_logs,
    lane,
    prepared,
    settled,
)
from packages.valory.skills.fresh_watch_abci.tests.test_lane_mempool import pending


def test_lane_trade_cap_exemption_defaults_off() -> None:
    """Require an explicit opt-in before changing cap accounting."""
    assert LightningConfig().lightning_lane_exempt_max_trades is False
    assert (
        LightningConfig(
            lightning_lane_exempt_max_trades=True
        ).lightning_lane_exempt_max_trades
        is True
    )


@pytest.mark.parametrize("exempt", [False, True])
@pytest.mark.parametrize("source", ["mempool", "block"])
def test_lane_shot_at_trade_cap(tmp_path: Any, exempt: bool, source: str) -> None:
    """Apply the global cap to both shot paths only without the exemption."""
    b = lane(
        tmp_path,
        lightning_lane_exempt_max_trades=exempt,
        lightning_still_hold=True,
        lightning_still_hold_min_entry_price=0,
        lightning_mempool=True,
        lightning_wss_url="wss://rpc.test/key",
    )
    b.context.params.fast = replace(b.params.fast, still_hold_min_price=0)
    prepared(b)
    assert b.store_trade_count_state(b.params.max_trades, {"omen:previous"})
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "chain_creation.json").read_text()
    )
    now = 1002 if source == "mempool" else fixture["blockTimestamp"] + 0.4
    (tmp_path / "lightning_base_rate.json").write_text(
        json.dumps(
            dict(
                n=100,
                rate=0.95,
                template_id=2,
                creators=sorted(b.params.lightning.lightning_creators),
                window_days=21,
                refreshed_at=int(now),
            )
        )
    )
    b._send = MagicMock(side_effect=lambda *args, **kwargs: done(True))
    with patch(MODULE + ".time.time", return_value=now):
        if source == "mempool":
            b._checked = b._safe_ok = True
            b._mempool = MagicMock()
            b._mempool.take_head.return_value = None
            b._mempool.drain.return_value = [pending()]
            run(b._drain_mempool(now))
        else:
            b._rpc = MagicMock(
                side_effect=[
                    done({"logs": fixture["logs"]}),
                    done("0x" + encode(["uint256[]"], [[7 * 10**18] * 2]).hex()),
                ]
            )
            run(
                b._market(
                    fixture["logs"][-1],
                    dict(timestamp=hex(int(now)), number="0x2", hash="0xblock"),
                    now,
                )
            )
    assert b._send.call_count == int(exempt)
    if exempt:
        assert b._send.call_args.args[1].stake > 0
    else:
        assert "trade_cap" in str(b.context.logger.info.call_args)


@patch(MODULE + ".LightningLaneBehaviour.synchronized_data", new_callable=PropertyMock)
def test_exempt_fill_counts_activity_once_without_advancing_cap(
    synchronized: Any, tmp_path: Any
) -> None:
    """Use the ledger for activity and repeated completion of an exempt fill."""
    b = lane(tmp_path, lightning_lane_exempt_max_trades=True)
    period_start = 100
    synchronized.return_value.previous_checkpoint = period_start
    synchronized.return_value.safe_contract_address = SAFE
    record_trade(tmp_path, 1000, "previous-market", "omen:previous")
    assert b.record_successful_placement("omen:previous") == 1
    progress = count_trades_since(tmp_path, period_start, b.context.logger)
    assert activity_goal_progress(tmp_path, period_start, progress) == 1
    before = (tmp_path / "successful_trade_count.txt").read_bytes()
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    receipt = {"status": "0x1", "logs": buy_logs()}
    block = {"timestamp": hex(1005)}
    b._complete(info, receipt, block)
    assert (tmp_path / "successful_trade_count.txt").read_bytes() == before
    assert b.read_counted_placement_keys() == {"omen:previous"}
    assert [trade["placement_key"] for trade in read_trades(tmp_path)] == [
        "omen:previous",
        "omen:0xfeed",
    ]
    progress = count_trades_since(tmp_path, period_start, b.context.logger)
    assert activity_goal_progress(tmp_path, period_start, progress) == 2
    bets_before = (tmp_path / "multi_bets.json").read_bytes()
    with patch(MODULE + ".write_json_atomically", wraps=write_json_atomically) as write:
        b._complete(info, receipt, block)
    assert not any(
        call.args[0] == tmp_path / "multi_bets.json" for call in write.call_args_list
    )
    assert (tmp_path / "multi_bets.json").read_bytes() == bets_before
    assert (tmp_path / "successful_trade_count.txt").read_bytes() == before
    assert len(read_trades(tmp_path)) == 2
    b.read_bets()
    assert b.bets[0].invested_amount == info["amount"]
    assert json.loads((tmp_path / "fresh_watch.json").read_text())["pending"] == []
    assert MARKET in b.context.shared_state["lightning_lane_bet_markets"]
    assert "lightning_lane_inflight" not in b.context.shared_state


@pytest.mark.parametrize("failure", ["corrupt", "unreadable"])
def test_exempt_ledger_read_failure_keeps_completion_pending(
    tmp_path: Any, failure: str
) -> None:
    """Retry a saved fill after an unusable ledger becomes available."""
    b = lane(tmp_path, lightning_lane_exempt_max_trades=True)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    ledger = tmp_path / "activity_goal_trades.json"
    if failure == "corrupt":
        ledger.write_text("{}")
    else:
        ledger.mkdir()
    receipt = {"status": "0x1", "logs": buy_logs()}
    block = {"timestamp": hex(1005)}
    b._complete(info, receipt, block)
    b.context.logger.warning.assert_called_once_with(
        "[LIGHTNING_LANE] completion_pending=true reason=trade_ledger_write"
    )
    assert b.context.shared_state["lightning_lane_inflight"] is info
    assert info["receipt"] == receipt
    assert not (tmp_path / "multi_bets.json").exists()
    assert b.durable_trade_count() == 0
    if failure == "corrupt":
        ledger.unlink()
    else:
        ledger.rmdir()
    b._complete(info, receipt, block)
    assert read_trades(tmp_path)[0]["placement_key"] == "omen:0xfeed"
    assert "lightning_lane_inflight" not in b.context.shared_state


@pytest.mark.parametrize("failure", ["raise", "missing"])
def test_exempt_ledger_write_failure_keeps_completion_pending(
    tmp_path: Any, failure: str
) -> None:
    """Keep the saved receipt and retry until the ledger contains its key."""
    b = lane(tmp_path, lightning_lane_exempt_max_trades=True)
    info = settled(b, tmp_path)
    b.context.shared_state["lightning_lane_inflight"] = info
    receipt = {"status": "0x1", "logs": buy_logs()}
    block = {"timestamp": hex(1005)}
    with patch(
        MODULE + ".record_trade",
        side_effect=OSError("disk unavailable") if failure == "raise" else None,
    ):
        b._complete(info, receipt, block)
    b.context.logger.warning.assert_called_once_with(
        "[LIGHTNING_LANE] completion_pending=true reason=trade_%s_write", "ledger"
    )
    assert b.context.shared_state["lightning_lane_inflight"] is info
    assert info["receipt"] == receipt
    assert b.durable_trade_count() == 0
    assert b.read_counted_placement_keys() == set()
    b._complete(info, receipt, block)
    assert len(read_trades(tmp_path)) == 1
    assert b.durable_trade_count() == 0
    assert "lightning_lane_inflight" not in b.context.shared_state


@pytest.mark.parametrize("exempt", [False, True])
def test_startup_reports_trade_cap_exemption(tmp_path: Any, exempt: bool) -> None:
    """Show the active cap policy on the existing startup line."""
    b = lane(tmp_path, lightning_lane_exempt_max_trades=exempt)
    b._rpc = MagicMock(
        side_effect=[done(hex(1)), done("0x" + encode(["bool"], [True]).hex())]
    )
    assert run(b._startup()) is True
    b.context.logger.info.assert_called_once_with(
        "[LIGHTNING_LANE] startup=%s rpc_host=%s exempt_max_trades=%s",
        "ready",
        "rpc.test",
        str(exempt).lower(),
    )
