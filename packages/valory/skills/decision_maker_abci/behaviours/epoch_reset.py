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

"""Behaviour that resets the trade cap when a new staking epoch starts."""

import json
from typing import Generator

from packages.valory.skills.decision_maker_abci.behaviours.base import (
    DecisionMakerBaseBehaviour,
)
from packages.valory.skills.decision_maker_abci.payloads import EpochResetPayload
from packages.valory.skills.decision_maker_abci.states.epoch_reset import (
    EpochResetRound,
)


class EpochResetBehaviour(DecisionMakerBaseBehaviour):
    """Reset the trade counter and queue at a new staking epoch boundary."""

    matching_round = EpochResetRound

    def async_act(self) -> Generator:
        """Reset the trade counter and clear the Mech-only queue at the epoch boundary."""
        with self.context.benchmark_tool.measure(self.behaviour_id).local():
            if self.synchronized_data.is_checkpoint_reached:
                # New staking epoch: reset the cap and clear the Mech-only queue.
                # Reset the durable file too, otherwise the pre-epoch count would
                # be reloaded on the next restart and keep the cap armed.
                self.store_trade_count_state(0, set())
                self.context.logger.info(
                    "New staking epoch detected; resetting the trade cap counter."
                )
                payload = EpochResetPayload(
                    self.context.agent_address,
                    successful_trade_count=0,
                    mech_only_queue=json.dumps([]),
                )
            else:
                # No epoch change: re-write existing values (no-op). Use the
                # durable counter so synchronized data is re-hydrated from the
                # file after a restart wiped the ABCI database.
                payload = EpochResetPayload(
                    self.context.agent_address,
                    successful_trade_count=self.durable_trade_count(),
                    mech_only_queue=json.dumps(self.synchronized_data.mech_only_queue),
                )
        yield from self.finish_behaviour(payload)
