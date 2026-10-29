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

"""Test consensus updates and fast-lane routing."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from packages.valory.skills.abstract_round_abci.base import AbciAppDB
from packages.valory.skills.decision_maker_abci.states.base import SynchronizedData
from packages.valory.skills.fresh_watch_abci.rounds import (
    Event,
    FastDecisionRound,
    FreshWatchRound,
    SelectionPayload,
)


@pytest.mark.parametrize(
    "lane,event",
    [
        ("market", Event.MARKET),
        ("lightning_decision", Event.LIGHTNING),
        ("mech_only", Event.MECH_ONLY),
        ("housekeeping", Event.HOUSEKEEPING),
        ("wait", Event.WAIT),
    ],
)
def test_watch(lane: Any, event: Any) -> None:
    """Persist selection data and clear stale transaction state."""
    data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
                tx_submitter=["bet_placement_round"],
                lightning_probability=[0.99],
                lightning_preview_probability=[0.99],
                lightning_preview_request=[True],
            )
        )
    )
    round_ = FreshWatchRound(data, MagicMock())
    assert round_.end_block() is None
    round_.process_payload(
        SelectionPayload("agent", json.dumps(dict(lane=lane, sampled_bet_index=0)))
    )
    outcome = round_.end_block()
    assert outcome is not None
    result, actual = outcome
    assert actual == event
    assert result.db.get("sampled_bet_index") == 0
    assert result.db.get("tx_submitter") is None
    assert result.db.get("lightning_probability") is None
    assert result.db.get("lightning_preview_probability") is None
    assert result.db.get("lightning_preview_request") is False
    assert result.db.get("mech_responses") == "[]"
    assert result.db.get("is_marketplace_v2") is True


@pytest.mark.parametrize("stake,event", [(1, Event.BET), (0, Event.SKIP)])
def test_fast_decision(stake: Any, event: Any) -> None:
    """Set the existing bet-placement inputs from a fast decision."""
    data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
            )
        )
    )
    round_ = FastDecisionRound(data, MagicMock())
    assert round_.end_block() is None
    round_.process_payload(
        SelectionPayload("agent", json.dumps(dict(stake=stake, side=1)))
    )
    outcome = round_.end_block()
    assert outcome is not None
    result, actual = outcome
    assert actual == event
    assert result.db.get("bet_amount") == stake * 10**18
    assert result.db.get("vote") == 1


@pytest.mark.parametrize("cls", [FreshWatchRound, FastDecisionRound])
def test_no_majority(cls: Any) -> None:
    """Conflicting sovereign setup votes do not apply an arbitrary result."""
    data = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("a", "b")],
                participants=[("a", "b")],
                consensus_threshold=[2],
            )
        )
    )
    round_ = cls(data, MagicMock())
    round_.process_payload(SelectionPayload("a", "one"))
    round_.process_payload(SelectionPayload("b", "two"))
    assert round_.end_block()[1] == Event.NO_MAJORITY


def test_question_template_does_not_leak_between_watch_selections() -> None:
    """Keep the current question template and clear it on a later idle lane."""
    data: Any = SynchronizedData(
        AbciAppDB(
            setup_data=dict(
                all_participants=[("agent",)],
                participants=[("agent",)],
                consensus_threshold=[1],
                question_template_id=[2],
            )
        )
    )
    for selection, expected in [
        (
            dict(
                lane="lightning_decision",
                lightning_probability=0.95,
                question_template_id=2,
            ),
            2,
        ),
        (dict(lane="market", question_template_id=0), 0),
        (dict(lane="mech_only"), None),
    ]:
        round_ = FreshWatchRound(data, MagicMock())
        round_.process_payload(SelectionPayload("agent", json.dumps(selection)))
        outcome = round_.end_block()
        assert outcome is not None
        data, _ = outcome
        assert data.db.get("question_template_id") == expected
