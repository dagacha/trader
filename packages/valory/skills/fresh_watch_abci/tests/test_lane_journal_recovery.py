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

"""Keep Safe preparation blocked until a failed journal read is recovered."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from packages.valory.skills.fresh_watch_abci.tests.test_lane_behaviour import (
    MODULE,
    done,
    lane,
    run,
    settled,
)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("saved_receipt", [False, True])
def test_journal_read_failure_blocks_guard_until_reconciled(
    tmp_path: Any, enabled: bool, saved_receipt: bool
) -> None:
    """Retry journal reads and release Safe preparation after bookkeeping."""
    b = lane(tmp_path)
    info = settled(b, tmp_path)
    receipt = {"status": "0x1", "blockNumber": "0x2", "logs": []}
    block = {"timestamp": hex(1005), "number": "0x2"}
    if saved_receipt:
        info.update(receipt=receipt, receipt_block=block)
    b.context.shared_state["lightning_lane_inflight"] = info
    b._night, b._shots = "1970-01-01", 3
    b._persist()
    journal = tmp_path / "lightning_lane_state.json"
    original = journal.read_bytes()
    read_text = Path.read_text
    failed: list[bool] = []

    def fail_journal(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == journal:
            failed.append(True)
            raise PermissionError("journal read unavailable")
        return read_text(path, *args, **kwargs)

    with patch.object(Path, "read_text", new=fail_journal):
        restarted = lane(tmp_path, lightning_lane=enabled)
        restarted.sleep = lambda _delay: iter([None])
        restarted._rpc = MagicMock(side_effect=AssertionError("unloaded journal"))
        waiting = restarted.wait_for_lightning_lane(reserve=True)
        assert next(waiting) is None
        assert "lightning_lane_fsm_preparing" not in restarted.context.shared_state
        run(restarted.async_act())
        run(restarted.async_act())
        assert failed == [True, True, True]
        assert next(waiting) is None
        assert journal.read_bytes() == original
        assert restarted.durable_trade_count() == 0
        restarted._rpc.assert_not_called()

    with patch(MODULE + ".time.time", return_value=1005):
        if not saved_receipt:
            restarted._rpc = MagicMock(return_value=done(None))
            run(restarted.async_act())
            assert restarted.context.shared_state["lightning_lane_inflight"] == info
            assert next(waiting) is None
            assert journal.read_bytes() == original
        restarted._rpc = MagicMock(side_effect=[done(receipt), done(block)])
        run(restarted.async_act())
        assert run(waiting) is None

    assert "lightning_lane_inflight" not in restarted.context.shared_state
    assert restarted.context.shared_state.get("lightning_lane_fsm_preparing") is (
        True if enabled else None
    )
    assert restarted._shots == 3
    assert restarted.durable_trade_count() == 1
    assert restarted.read_counted_placement_keys() == {"omen:0xfeed"}
    restarted.read_bets()
    assert restarted.bets[0].invested_amount == info["amount"]
    assert restarted._rpc.call_count == (0 if saved_receipt else 2)
