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

"""Discover proposed questions and retain exact-title preview answers."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional

from eth_abi import decode
from eth_abi.exceptions import DecodingError

from packages.valory.skills.agent_performance_summary_abci.models import (
    write_json_atomically,
)
from packages.valory.skills.fresh_watch_abci.chain_discovery import (
    ChainRPCError,
    DELIVERY_TOPIC,
    PROPOSER,
    REQUEST_TOPIC,
    event_scan,
)

PREVIEW_TTL = 15 * 60


def preview_questions(payload: Any) -> List[str]:
    """Read unique titles without changing their text.

    :param payload: delivery JSON.
    :return: exact question titles.
    """
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return []
    if not isinstance(result, dict):
        return []
    questions = result.get("questions")
    if isinstance(questions, dict):
        questions = list(questions.values())
    if not isinstance(questions, list):
        return []
    titles: List[str] = []
    for question in questions:
        title = question.get("question") if isinstance(question, dict) else None
        if isinstance(title, str) and title.strip() and title not in titles:
            titles.append(title)
    return titles


def preview_night(now: float, active_window: str) -> str:
    """Get the UTC date on which the active window starts.

    :param now: Unix timestamp.
    :param active_window: UTC start and end in HH:MM-HH:MM format.
    :return: date used for the per-night request cap.
    """
    start, end = active_window.split("-")
    date = datetime.fromtimestamp(now, timezone.utc)
    if start > end and date.strftime("%H:%M") < end:
        date -= timedelta(days=1)
    return date.strftime("%Y-%m-%d")


class PreviewStore:
    """Keep paid preview requests on disk before they can be sent."""

    def __init__(self, path: Path) -> None:
        """Read local state.

        :param path: JSON store path.
        """
        self.path = path
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            raw = {}
        self.entries: Dict[str, Dict[str, Any]] = raw.get("entries", {})
        self.counts: Dict[str, int] = raw.get("counts", {})
        self.requests: Dict[str, Dict[str, Any]] = raw.get("requests", {})
        self.last_block: int = raw.get("last_block", 0)

    def save(self) -> None:
        """Write local state atomically."""
        write_json_atomically(
            self.path,
            dict(
                entries=self.entries,
                counts=self.counts,
                requests=self.requests,
                last_block=self.last_block,
            ),
        )

    @staticmethod
    def _expired(entry: Dict[str, Any], now: float) -> bool:
        """Check the preview time limit."""
        return now - (entry.get("requested_at") or entry["delivered_at"]) >= PREVIEW_TTL

    def ingest(self, payload: Any, delivered_at: float, now: float) -> List[str]:
        """Add unseen titles from a delivered proposal.

        :param payload: proposal delivery JSON.
        :param delivered_at: proposal delivery block time.
        :param now: detection time.
        :return: titles added to this store.
        """
        added: List[str] = []
        if now - delivered_at >= PREVIEW_TTL:
            return added
        for title in preview_questions(payload):
            if title in self.entries or re.search(r"\bstill hold\b", title, re.I):
                continue
            self.entries[title] = dict(
                title=title,
                delivered_at=delivered_at,
                requested_at=None,
                answered_at=None,
                market_seen_at=None,
                p_yes=None,
            )
            added.append(title)
        if added:
            self.save()
        return added

    def next_title(self, now: float) -> Optional[str]:
        """Find the next unrequested title.

        :param now: Unix timestamp.
        :return: exact title, or None.
        """
        return next(
            (
                title
                for title, entry in self.entries.items()
                if entry["requested_at"] is None and not self._expired(entry, now)
            ),
            None,
        )

    def reserve(
        self, title: str, now: float, night: str, limit: int
    ) -> Optional[Dict[str, Any]]:
        """Persist a request and consume its cap before the paid call.

        :param title: exact proposed title.
        :param now: request time.
        :param night: active window's start date.
        :param limit: maximum paid preview requests in this night.
        :return: reserved entry, or None when not eligible.
        """
        entry = self.entries.get(title)
        if (
            entry is None
            or entry["requested_at"] is not None
            or self._expired(entry, now)
            or self.counts.get(night, 0) >= limit
        ):
            return None
        entry["requested_at"] = now
        self.counts = {night: self.counts.get(night, 0) + 1}
        self.save()
        return entry.copy()

    def answer(self, title: str, p_yes: float, now: float) -> None:
        """Retain a prediction for an exact reserved title.

        :param title: exact requested title.
        :param p_yes: tool probability for YES.
        :param now: answer time.
        """
        entry = self.entries.get(title)
        if entry is None or entry["requested_at"] is None or self._expired(entry, now):
            return
        if not 0 <= p_yes <= 1:
            return
        entry.update(p_yes=p_yes, answered_at=now)
        self.save()

    def answer_failed(self, title: str, now: float) -> None:
        """Allow a market to use its normal request after a preview failure.

        :param title: exact requested title.
        :param now: failure time.
        """
        entry = self.entries.get(title)
        if entry is not None and entry["requested_at"] is not None:
            entry.update(failed_at=now)
            self.save()

    def match(self, title: str, now: float) -> Optional[Dict[str, Any]]:
        """Attach a market only to an exact nonexpired reserved title.

        :param title: exact market title.
        :param now: market detection time.
        :return: cached answer or pending request; None for no exact match.
        """
        entry = self.entries.get(title)
        if (
            entry is None
            or entry["requested_at"] is None
            or entry.get("failed_at") is not None
            or self._expired(entry, now)
        ):
            return None
        if entry["market_seen_at"] is None:
            entry["market_seen_at"] = now
            self.save()
        return entry.copy()

    def consume(self, title: str, now: float) -> Optional[Dict[str, Any]]:
        """Take an answered preview once for the selected market.

        :param title: exact market title.
        :param now: selection time.
        :return: completed preview, or None while its answer is pending.
        """
        entry = self.match(title, now)
        if entry is None or entry["answered_at"] is None:
            return None
        del self.entries[title]
        self.save()
        return entry

    def expire(self, now: float) -> List[Dict[str, Any]]:
        """Remove expired titles and report orphan paid requests once.

        :param now: Unix timestamp.
        :return: expired paid requests whose market never appeared.
        """
        expired = [
            title for title, entry in self.entries.items() if self._expired(entry, now)
        ]
        orphans = [
            self.entries[title].copy()
            for title in expired
            if self.entries[title]["requested_at"] is not None
            and self.entries[title]["market_seen_at"] is None
        ]
        for title in expired:
            del self.entries[title]
        if expired:
            self.save()
        return orphans


class PreviewMixin:
    """Read proposal chain events through the cooperative ledger reader."""

    params: Any
    context: Any
    _chain_rpc: Callable[..., Generator[None, None, Any]]
    get_http_response: Callable[..., Generator[None, None, Any]]

    def preview_store(self) -> PreviewStore:
        """Read the durable local preview cache.

        :return: latest preview state.
        """
        return PreviewStore(self.params.store_path / "lightning_preview.json")

    def preview_log(self, entry: Dict[str, Any], reason: str = "") -> None:
        """Log lifecycle times without the full question text.

        :param entry: cached preview record.
        :param reason: optional lifecycle reason.
        """
        self.context.logger.info(
            "[LIGHTNING_PREVIEW] title_hash=%s delivered_at=%s requested_at=%s "
            "answered_at=%s market_seen_at=%s reason=%s",
            hashlib.sha256(entry["title"].encode()).hexdigest(),
            entry.get("delivered_at"),
            entry.get("requested_at"),
            entry.get("answered_at"),
            entry.get("market_seen_at"),
            reason,
        )

    def _preview_json(
        self, digest: str, request_id: Optional[str] = None
    ) -> Generator[None, None, Any]:
        """Read a proposal file from the configured IPFS gateway.

        :param digest: bytes32 digest as hex.
        :param request_id: optional response filename.
        :yield: cooperative HTTP I/O.
        :return: decoded JSON, or None on failure.
        """
        url = self.params.ipfs_address + "f01701220" + digest.removeprefix("0x")
        if request_id is None:
            url += "/metadata.json"
        else:
            url += "/" + str(int(request_id, 16))
        response = yield from self.get_http_response(method="GET", url=url)
        if response is None or response.status_code != 200:
            return None
        try:
            return json.loads(response.body)
        except (ValueError, TypeError):
            return None

    def preview_poll(self, now: float) -> Generator[None, None, None]:
        """Read proposal events through the caller's RPC error handling.

        :param now: detection time.
        :yield: cooperative ledger and gateway I/O.
        :raises ChainRPCError: when RPC data or event bytes are malformed.
        """
        try:
            yield from self._preview_poll(now)
        except (ValueError, TypeError, KeyError, DecodingError) as exc:
            raise ChainRPCError("Invalid preview chain data: " + str(exc)) from exc

    def _preview_poll(self, now: float) -> Generator[None, None, None]:
        """Discover proposer deliveries and queue non-still-hold questions.

        :param now: detection time.
        :yield: cooperative ledger and gateway I/O.
        """
        cfg = self.params.lightning
        if not cfg.lightning_preview or not self.params.fast.in_window(now):
            return
        store = self.preview_store()
        for orphan in store.expire(now):
            self.preview_log(orphan, "orphan_fee_loss")
        block, logs = yield from event_scan(self, now)
        height = _integer(block["number"])
        if height <= store.last_block:
            return
        creators = [
            "0x" + address.lower().removeprefix("0x").zfill(64)
            for address in cfg.lightning_creators
        ]
        request_logs = []
        delivery_logs = []
        for log in logs:
            if log.get("removed"):
                continue
            address = log["address"].lower()
            topics = [_hex(value) for value in log["topics"]]
            if (
                address
                == self.params.mech_marketplace_config.mech_marketplace_address.lower()
                and topics
                and topics[0] == REQUEST_TOPIC
            ):
                request_logs.append(log)
            elif address == PROPOSER and topics and topics[0] == DELIVERY_TOPIC:
                delivery_logs.append(log)
        # Reload after I/O so foreground reservations are not overwritten.
        store = self.preview_store()
        for log in request_logs:
            topics = [_hex(value).lower() for value in log["topics"]]
            if (
                len(topics) != 3
                or topics[1] != "0x" + PROPOSER[2:].zfill(64)
                or topics[2] not in creators
            ):
                continue
            _, ids, data = decode(
                ["uint256", "bytes32[]", "bytes[]"], _bytes(log["data"])
            )
            for request_id, digest in zip(ids, data):
                if len(digest) == 32:
                    store.requests.setdefault(
                        _hex(request_id),
                        {
                            "digest": _hex(digest),
                            "created_at": now,
                            "tool": None,
                        },
                    )
        for log in delivery_logs:
            request_id, _, data = decode(
                ["bytes32", "uint256", "bytes"], _bytes(log["data"])
            )
            request = store.requests.get(_hex(request_id))
            if request is not None and len(data) == 32:
                request.update(
                    delivery_digest=_hex(data),
                    delivery_block=_integer(log["blockNumber"]),
                )
        store.last_block = height
        store.save()
        for request_id, request in list(store.requests.items()):
            if now - request["created_at"] >= PREVIEW_TTL:
                latest = self.preview_store()
                latest.requests.pop(request_id, None)
                latest.save()
                continue
            if request.get("tool") is None:
                metadata = yield from self._preview_json(request["digest"])
                if not isinstance(metadata, dict):
                    continue
                request["tool"] = metadata.get("tool", "")
                latest = self.preview_store()
                latest.requests[request_id] = request
                latest.save()
            if (
                request["tool"] != "propose-question"
                or not request.get("delivery_digest")
                or request.get("processed")
            ):
                continue
            payload = yield from self._preview_json(
                request["delivery_digest"], request_id
            )
            if payload is None:
                continue
            delivered = yield from self._chain_rpc(
                "get_block", block_identifier=request["delivery_block"]
            )
            latest = self.preview_store()
            added = latest.ingest(payload, _integer(delivered["timestamp"]), now)
            request["processed"] = True
            latest.requests[request_id] = request
            latest.save()
            for title in added:
                self.preview_log(latest.entries[title])


def _integer(value: Any) -> int:
    """Read a normalized or hex RPC integer."""
    return (
        int(value, 16)
        if isinstance(value, str) and value.startswith("0x")
        else int(value)
    )


def _bytes(value: Any) -> bytes:
    """Read bytes from a normalized or hex RPC field."""
    return (
        bytes.fromhex(value.removeprefix("0x"))
        if isinstance(value, str)
        else bytes(value)
    )


def _hex(value: Any) -> str:
    """Read one RPC bytes field as lower-case hex."""
    return "0x" + _bytes(value).hex()
