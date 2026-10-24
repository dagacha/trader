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

"""Consensus state that resets the trade cap at the start of a new staking epoch."""

from typing import Any

from packages.valory.skills.abstract_round_abci.base import (
    CollectSameUntilThresholdRound,
    get_name,
)
from packages.valory.skills.decision_maker_abci.payloads import EpochResetPayload
from packages.valory.skills.decision_maker_abci.states.base import (
    Event,
    SynchronizedData,
)


class EpochResetRound(CollectSameUntilThresholdRound):
    """Reset the trade counter and Mech-only queue when a new staking epoch begins.

    This round runs at the start of every decision-maker cycle, immediately
    after randomness and *before* ``TradeCapRound`` evaluates the cap, so a
    counter from an older on-chain checkpoint period resets before the cap
    check. The durable counter reader preserves counts for the same period
    and binds legacy counters to the current period. The staking skill's
    ``is_checkpoint_reached`` flag clears the ``mech_only_queue``. The flag
    alone does not reset the counter, as a stale checkpoint file can also
    set it after a restart.
    """

    payload_class = EpochResetPayload
    synchronized_data_class = SynchronizedData
    done_event = Event.DONE
    none_event = Event.NONE
    no_majority_event = Event.NO_MAJORITY
    selection_key: Any = (
        get_name(SynchronizedData.successful_trade_count),
        get_name(SynchronizedData.mech_only_queue),
    )
    collection_key = get_name(SynchronizedData.participant_to_selection)
