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

"""Pure scheduling, strategy, and encoding for the optional direct-send lane."""

import math
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address  # type: ignore[import-not-found]

from packages.valory.skills.fresh_watch_abci.chain_discovery import CREATION_TOPIC
from packages.valory.skills.fresh_watch_abci.lightning import (
    BaseRateCache,
    LightningConfig,
    natural_yes_index,
    still_hold_rule,
)
from packages.valory.skills.fresh_watch_abci.strategy import (
    Decision,
    FastConfig,
    buy_amount,
    decide,
)


def number(value: Any) -> int:
    """Read a JSON-RPC integer.

    :param value: hex or integer value.
    :return: integer value.
    """
    return (
        int(value, 16)
        if isinstance(value, str) and value.startswith("0x")
        else int(value)
    )


@dataclass
class SlotPoller:
    """Read one block per five-second slot, with bounded null-result retries."""

    offset: float = 0.3
    step: float = 0.25
    next_at: float = 0.0
    wanted: Optional[int] = None
    deadline: float = 0.0

    def request(self, now: float) -> Optional[Tuple[str, List[Any]]]:
        """Return the next read only when its slot or retry is due.

        :param now: local Unix timestamp.
        :return: raw JSON-RPC method and params, or None.
        """
        if now < self.next_at:
            return None
        if now - self.next_at > 10:
            self.wanted = None
        if self.wanted is None:
            return "eth_blockNumber", []
        return "eth_getBlockByNumber", [hex(self.wanted), False]

    def height(self, value: Any, now: float) -> None:
        """Read the current block after a height resync.

        :param value: head block height.
        :param now: local Unix timestamp.
        """
        self.wanted = number(value)
        self.next_at = now
        self.deadline = now + 6

    def block(
        self, value: Optional[Dict[str, Any]], now: float
    ) -> Optional[Dict[str, Any]]:
        """Advance to the next slot, or retry a missing block for six seconds.

        :param value: raw block or null.
        :param now: local Unix timestamp.
        :return: the block when present.
        """
        if value is None:
            if now >= self.deadline:
                self.wanted = None
                self.next_at = now
            else:
                self.next_at = now + self.step
            return None
        self.wanted = number(value["number"]) + 1
        self.next_at = number(value["timestamp"]) + 5 + self.offset
        self.deadline = self.next_at + 6
        return value


def creation_filter(
    block_hash: str, factory: str, creators: List[str]
) -> Dict[str, Any]:
    """Limit the log read to one block and the configured creators.

    :param block_hash: new block hash.
    :param factory: Omen factory address.
    :param creators: allowed creator addresses.
    :return: eth_getLogs filter.
    """
    return {
        "blockHash": block_hash,
        "address": factory,
        "topics": [
            CREATION_TOPIC,
            ["0x" + value[2:].lower().rjust(64, "0") for value in creators],
        ],
    }


@dataclass
class RPCStats:
    """Count RPC reads and block latency without retaining the keyed URL."""

    started_at: float = 0.0
    calls: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    paused_until: float = 0.0
    detections: List[float] = field(default_factory=list)

    def called(self, now: float, error: bool = False) -> None:
        """Count a call and pause for one minute after five consecutive errors.

        :param now: local Unix timestamp.
        :param error: whether the call failed.
        """
        self.calls += 1
        self.errors += int(error)
        self.consecutive_errors = self.consecutive_errors + 1 if error else 0
        if self.consecutive_errors >= 5:
            self.paused_until = now + 60
            self.consecutive_errors = 0

    def detected(self, timestamp: float, now: float) -> None:
        """Record local detection time minus block timestamp.

        :param timestamp: block timestamp.
        :param now: local Unix timestamp.
        """
        self.detections.append(max(0.0, now - timestamp))

    def ready(self, now: float) -> bool:
        """Check the RPC error pause.

        :param now: local Unix timestamp.
        :return: whether calls can resume.
        """
        return now >= self.paused_until

    def report(self, now: float) -> Optional[str]:
        """Report interval statistics once every ten minutes.

        :param now: local Unix timestamp.
        :return: safe log text, or None before the next interval.
        """
        if now - self.started_at < 600:
            return None
        samples = sorted(self.detections)
        p50 = samples[max(0, math.ceil(len(samples) * 0.5) - 1)] if samples else 0.0
        p90 = samples[max(0, math.ceil(len(samples) * 0.9) - 1)] if samples else 0.0
        result = f"[LIGHTNING_LANE_RPC] calls={self.calls} errors={self.errors} p50_detect_s={p50:.3f} p90_detect_s={p90:.3f}"
        self.started_at = now
        self.calls = self.errors = 0
        self.detections.clear()
        return result


def execution_ceiling(
    amount: int,
    quoted: int,
    p_side: float,
    requested: float,
    max_price: float,
    max_slip: float,
    min_edge: float,
) -> Optional[Tuple[float, int]]:
    """Apply the fast placement ceiling with exact integer rounding.

    :param amount: collateral investment in wei.
    :param quoted: locally quoted outcome tokens.
    :param p_side: calibrated probability of the selected outcome.
    :param requested: price at discovery or request time.
    :param max_price: maximum average execution price.
    :param max_slip: maximum price increase from the request price.
    :param min_edge: required execution edge.
    :return: ceiling and minimum tokens, or None when the quote has no margin.
    """
    ceiling = min(
        Fraction(str(max_price)),
        Fraction(str(requested)) + Fraction(str(max_slip)),
        Fraction(str(p_side)) - Fraction(str(min_edge)),
    )
    if quoted <= 0 or ceiling <= Fraction(amount, quoted):
        return None
    numerator = amount * ceiling.denominator
    min_out = (numerator + ceiling.numerator - 1) // ceiling.numerator
    return float(ceiling), min(min_out, quoted)


def lane_decision(
    market: Dict[str, Any],
    balances: List[int],
    now: float,
    fast: FastConfig,
    lightning: LightningConfig,
    cache: BaseRateCache,
    trade_count: int,
    max_trades: int,
    placement_min_edge: float,
) -> Tuple[Optional[Decision], Optional[Tuple[float, int]], str]:
    """Use the existing direct still-hold strategy for one eligible shot.

    :param market: receipt-derived binary market metadata.
    :param balances: current binary pool reserves.
    :param now: local Unix timestamp.
    :param fast: shared strategy controls.
    :param lightning: optional lane and still-hold controls.
    :param cache: durable resolved-market evidence.
    :param trade_count: durable settled trade count.
    :param max_trades: configured trade cap.
    :param placement_min_edge: edge enforced by placement.
    :return: strategy result, execution bounds, and explicit skip reason.
    """
    if not lightning.lightning_lane or not lightning.lightning_rpc_url:
        return None, None, "disabled"
    if lightning.lightning_lane_tip_gwei <= 0:
        return None, None, "zero_tip"
    if not lightning.lightning_still_hold:
        return None, None, "still_hold_off"
    if not fast.in_window(now):
        return None, None, "outside_window"
    if (
        not 0
        <= now - number(market["creationTimestamp"])
        <= lightning.lightning_lane_max_age_s
    ):
        return None, None, "market_age"
    if number(market.get("openingTimestamp", 0)) <= now:
        return None, None, "market_closed"
    if natural_yes_index(market) is None:
        return None, None, "unsupported_question"
    if (
        still_hold_rule(market.get("title", ""), market.get("creator", ""), lightning)
        is None
    ):
        return None, None, "not_still_hold"
    if max_trades > 0 and trade_count >= max_trades:
        return None, None, "trade_cap"
    rate = cache.usable(now, lightning)
    if rate is None:
        return None, None, "base_rate"
    if len(balances) != 2 or min(balances) <= 0:
        return None, None, "invalid_price"
    total = sum(balances)
    requested = [balances[1] / total, balances[0] / total]
    cfg = replace(
        fast, prior_weight=1.0, max_price=lightning.lightning_still_hold_max_price
    )
    decision = decide(
        market["title"],
        rate,
        balances,
        requested,
        cfg,
        number(market["fee"]),
        yes_only=True,
    )
    if decision.stake <= 0:
        return decision, None, decision.reason
    amount = int(decision.stake * 10**18)
    quoted = buy_amount(amount, balances, 0, number(market["fee"]))
    bounds = execution_ceiling(
        amount,
        quoted,
        decision.p_hat,
        requested[0],
        cfg.max_price,
        cfg.max_slip,
        placement_min_edge,
    )
    return decision, bounds, "" if bounds is not None else "ceiling"


def build_safe_calldata(
    owner: str,
    multisend: str,
    collateral: str,
    market: str,
    amount: int,
    min_out: int,
    wrapped_balance: int,
    native_balance: int,
) -> Tuple[bytes, int]:
    """Encode the owner's Safe batch without a contract API request.

    :param owner: agent EOA, which is a Safe owner.
    :param multisend: MultiSend delegate-call target.
    :param collateral: wrapped xDAI address.
    :param market: binary Omen FPMM address.
    :param amount: exact investment in wei.
    :param min_out: minimum YES tokens in wei.
    :param wrapped_balance: cached Safe wrapped xDAI balance.
    :param native_balance: cached Safe native xDAI balance.
    :return: Safe execTransaction calldata and its batch value.
    :raises ValueError: when the Safe cannot fund the stake.
    """
    shortfall = max(0, amount - wrapped_balance)
    if native_balance < shortfall:
        raise ValueError("Safe collateral does not cover the stake")
    calls = []
    if shortfall:
        calls.append((collateral, shortfall, bytes.fromhex("d0e30db0")))
    approve = bytes.fromhex("095ea7b3") + encode(
        ["address", "uint256"], [market, amount]
    )
    buy = bytes.fromhex("40993b26") + encode(
        ["uint256", "uint256", "uint256"], [amount, 0, min_out]
    )
    calls.extend([(collateral, 0, approve), (market, 0, buy)])
    packed = b"".join(
        b"\0"
        + bytes.fromhex(target[2:])
        + value.to_bytes(32, "big")
        + len(data).to_bytes(32, "big")
        + data
        for target, value, data in calls
    )
    multisend_data = bytes.fromhex("8d80ff0a") + encode(["bytes"], [packed])
    signature = bytes.fromhex(owner[2:]).rjust(32, b"\0") + bytes(32) + b"\1"
    zero = "0x" + "00" * 20
    data = bytes.fromhex("6a761202") + encode(
        [
            "address",
            "uint256",
            "bytes",
            "uint8",
            "uint256",
            "uint256",
            "uint256",
            "address",
            "address",
            "bytes",
        ],
        [multisend, shortfall, multisend_data, 1, 0, 0, 0, zero, zero, signature],
    )
    return data, shortfall


def fixed_transaction(
    owner: str,
    safe: str,
    data: bytes,
    nonce: int,
    base_fee: int,
    gas_limit: int,
    tip_gwei: float,
    eoa_balance: int,
    tip_cap_gwei: float = 1000,
) -> Dict[str, Any]:
    """Build one fixed-tip EIP-1559 shot, with no gas estimate or escalation.

    :param owner: agent EOA.
    :param safe: Safe target address.
    :param data: encoded Safe execTransaction.
    :param nonce: pending EOA nonce read after the last FSM settlement.
    :param base_fee: creation block base fee in wei.
    :param gas_limit: configured transaction gas limit.
    :param tip_gwei: configured fixed priority fee in gwei.
    :param eoa_balance: cached EOA native balance.
    :param tip_cap_gwei: configured maximum priority fee.
    :return: unsigned Ethereum transaction.
    :raises ValueError: for a zero tip, invalid cap, or inadequate gas balance.
    """
    if tip_gwei <= 0:
        raise ValueError("Lane tip must be positive")
    if tip_gwei > tip_cap_gwei or not 0 < tip_cap_gwei <= 2000:
        raise ValueError("Lane tip exceeds its cap")
    tip = int(Fraction(str(tip_gwei)) * 10**9)
    maximum = tip + 2 * base_fee
    if eoa_balance < 3 * gas_limit * maximum:
        raise ValueError("EOA gas balance is below three shots")
    return dict(
        type=2,
        chainId=100,
        nonce=nonce,
        gas=gas_limit,
        maxPriorityFeePerGas=tip,
        maxFeePerGas=maximum,
        to=to_checksum_address(safe),
        data="0x" + data.hex(),
        value=0,
        **{"from": to_checksum_address(owner)},
    )


def funding_balances(logs: List[Dict[str, Any]], market: str) -> Optional[List[int]]:
    """Read initial binary reserves from the creation receipt's funding logs.

    :param logs: creation transaction logs.
    :param market: FPMM address.
    :return: summed funding reserves, or None when no usable event exists.
    """
    topic = "0x" + keccak(text="FPMMFundingAdded(address,uint256[],uint256)").hex()
    balances = [0, 0]
    found = False
    for log in logs:
        if (
            log.get("address", "").lower() != market.lower()
            or not log.get("topics")
            or log["topics"][0].lower() != topic
        ):
            continue
        amounts, _ = decode(["uint256[]", "uint256"], bytes.fromhex(log["data"][2:]))
        if len(amounts) != 2:
            return None
        balances = [balances[index] + amounts[index] for index in range(2)]
        found = True
    return balances if found and min(balances) > 0 else None


def buy_result(logs: List[Dict[str, Any]], market: str) -> Optional[Dict[str, Any]]:
    """Decode this market's buy for durable attribution and fill-price logs.

    :param logs: lane transaction receipt logs.
    :param market: FPMM address.
    :return: buy amounts and indexed buyer/outcome, or None.
    """
    topic = "0x" + keccak(text="FPMMBuy(address,uint256,uint256,uint256,uint256)").hex()
    for log in logs:
        topics = log.get("topics", [])
        if (
            log.get("address", "").lower() != market.lower()
            or len(topics) != 3
            or topics[0].lower() != topic
        ):
            continue
        amount, fee, tokens = decode(
            ["uint256", "uint256", "uint256"], bytes.fromhex(log["data"][2:])
        )
        if tokens <= 0:
            return None
        return dict(
            buyer="0x" + topics[1][-40:].lower(),
            amount=amount,
            fee=fee,
            side=number(topics[2]),
            tokens=tokens,
            fill_price=amount / tokens,
        )
    return None


def pool_balances_call(
    market: str, conditional: str, collateral: str, condition_id: str
) -> Tuple[str, str]:
    """Read both reserves in one call using binary zero-parent position IDs.

    The collection derivation follows ConditionalTokens CTHelpers.getCollectionId.
    The lane supports one binary condition, so there is no parent point to add.

    :param market: FPMM address that owns the outcome tokens.
    :param conditional: ConditionalTokens contract address.
    :param collateral: market collateral token address.
    :param condition_id: single binary condition ID.
    :return: eth_call target and hex calldata for balanceOfBatch.
    """
    prime = (
        21888242871839275222246405745257275088696311157297823662689037894645226208583
    )
    positions = []
    for index_set in (1, 2):
        x = int.from_bytes(
            keccak(bytes.fromhex(condition_id[2:]) + index_set.to_bytes(32, "big")),
            "big",
        )
        odd = x >> 255
        while True:
            x = (x + 1) % prime
            squared = (pow(x, 3, prime) + 3) % prime
            y = pow(squared, (prime + 1) // 4, prime)
            if y * y % prime == squared:
                break
        collection = x | (odd << 254)
        position = keccak(
            bytes.fromhex(collateral[2:]) + collection.to_bytes(32, "big")
        )
        positions.append(int.from_bytes(position, "big"))
    data = keccak(text="balanceOfBatch(address[],uint256[])")[:4] + encode(
        ["address[]", "uint256[]"], [[market, market], positions]
    )
    return to_checksum_address(conditional), "0x" + data.hex()
