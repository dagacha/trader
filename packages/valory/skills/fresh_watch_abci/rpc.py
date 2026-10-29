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

"""Bound API reads even when the connection sends no response."""

import time
from typing import Any, Generator, List, cast

from aea.protocols.base import Message
from aea.protocols.dialogue.base import Dialogue

from packages.valory.skills.abstract_round_abci.behaviour_utils import TimeoutException
from packages.valory.skills.abstract_round_abci.models import Requests


def request_with_timeout(
    behaviour: Any, message: Message, dialogue: Dialogue, timeout: float
) -> Generator[None, None, Any]:
    """Poll on scheduler ticks and retain a discard callback after expiry.

    :param behaviour: calling behaviour.
    :param message: API read request.
    :param dialogue: request dialogue.
    :param timeout: maximum response wait in seconds.
    :yield: control during short local sleeps.
    :return: response received by the request callback.
    :raises TimeoutException: when the response deadline expires.
    """
    responses: List[Message] = []
    deadline = time.monotonic() + timeout

    def receive(response: Message, _current_behaviour: Any) -> None:
        """Capture the response while the calling behaviour sleeps."""
        if time.monotonic() < deadline:
            responses.append(response)

    dialogue.terms = behaviour._get_default_terms()
    nonce = behaviour._get_request_nonce_from_dialogue(dialogue)
    callbacks = cast(Requests, behaviour.context.requests).request_id_to_callback
    callbacks[nonce] = receive
    behaviour.context.outbox.put_message(message=message)
    while not responses:
        if time.monotonic() >= deadline:
            if nonce in callbacks:
                callbacks[nonce] = lambda _response, _behaviour: None
            raise TimeoutException()
        yield from behaviour.sleep(0.2)
    callbacks.pop(nonce, None)
    return responses[0]
