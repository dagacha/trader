# MAX_TRADES implementation handoff

## Branch and goal

- **Working branch:** `max-trades-v0.33.2`
- **Base:** Trader `v0.33.2`, commit `5409a6e2f8ac0458461590048eadc8f852ff8f6b`.
- **Goal:** after `MAX_TRADES` successfully settled Omen placements, prevent every further position opening while retaining a bounded Mech-only analysis flow for open markets.

Implement against this v0.33.2 package tree, not `main`. Quickstart currently pins Trader `v0.33.2` and service `service/valory/trader_pearl/0.1.0` CID `bafybeiawwee4k5sga223bwh7wsu5dnzubam2dtked6lioe7roegkxbouzm`; local package edits do not alter what Quickstart deploys until a new release is built, published, and pinned.

## Architecture decision (2026-08-04)

Two candidate architectures were assessed:

- **Option A (rejected):** keep routing the capped path through the normal `SamplingRound → ToolSelectionRound → DecisionRequestRound` flow and discard the Mech answer in `DecisionReceive`. Cheap, but it is the late-guard approach this document forbids: normal trading logic and bet-queue bookkeeping keep running while capped, only one sampled market is analyzed per cycle, and error paths still blacklist markets.
- **Option B (chosen):** a dedicated Mech-only flow, isolated from trading logic, that deterministically walks all open markets in bounded batches with persisted queue state and routes to `CallCheckpointRound` when done.

**Option B is the committed direction.** The Option A shortcut partially present in the working tree (see below) must be removed as part of the work.

## Environment and current verification

The previously absent `aea` dependency is resolved. From the Trader checkout:

```bash
poetry env use python3.10
poetry install
poetry run autonomy init --reset --author valory --remote --ipfs \
  --ipfs-node /dns/registry.autonolas.tech/tcp/443/https
poetry run autonomy packages sync --update-packages
poetry run pytest packages/valory/skills/decision_maker_abci/tests/test_rounds.py -q
```

The last command passed: **11 tests passed**. `uv run pytest` was inappropriate here and failed with `ModuleNotFoundError: aea`.

## Current working-tree state

### Successful placement counter (keep)

New, currently untracked files:

```text
packages/valory/skills/decision_maker_abci/states/trade_count.py
packages/valory/skills/decision_maker_abci/behaviours/trade_count.py
```

Changed files and intent:

```text
packages/valory/skills/decision_maker_abci/payloads.py
  TradeCountPayload(successful_trade_count)

packages/valory/skills/decision_maker_abci/states/base.py
  SynchronizedData.successful_trade_count

packages/valory/skills/decision_maker_abci/rounds.py
  imports/registers TradeCountRound
  TradeCountRound transition: DONE -> RedeemRouterRound
  TradeCountRound in initial_states and db_pre_conditions
  successful_trade_count in cross_period_persisted_keys

packages/valory/skills/decision_maker_abci/behaviours/round_behaviour.py
  registers TradeCountBehaviour

packages/valory/skills/trader_abci/composition.py
  FinishedBetPlacementTxRound -> TradeCountRound
```

The composed route is currently:

```text
FinishedBetPlacementTxRound -> TradeCountRound -> RedeemRouterRound
```

`FinishedSellOutcomeTokensTxRound` remains routed to `RedeemRouterRound`; do not change it. The counter behaviour increments the synchronized count. This is safe only while `TradeCountRound` is entered exclusively from successful `BetPlacementRound` settlement. Never enter it for sell, failed/replayed transaction, sampling, or Mech paths.

### Cap consensus gate (keep, rewire)

`TradeCapRound` (`states/trade_cap.py`) and `TradeCapBehaviour` (`behaviours/trade_cap.py`) exist and are wired:

```text
RandomnessRound            DONE -> TradeCapRound
BenchmarkingRandomnessRound DONE -> TradeCapRound
TradeCapRound DONE      -> SamplingRound
TradeCapRound MECH_ONLY -> SamplingRound   # WRONG under Option B — must retarget
```

The behaviour computes `mech_only = max_trades > 0 and successful_trade_count >= max_trades` and persists `SynchronizedData.mech_only_mode` via the round's selection key. Timeout/no-majority self-loops and `db_pre_conditions[TradeCapRound] = set()` are in place.

### Option A shortcut in DecisionReceive (remove)

These changes implement the rejected late-guard and must be reverted:

```text
packages/valory/skills/decision_maker_abci/behaviours/decision_receive.py
  vote/sell computation gated on `mech_only_mode is not True`

packages/valory/skills/decision_maker_abci/states/decision_receive.py
  end_block short-circuits DONE -> DONE_NO_SELL when mech_only_mode is True
```

The `DONE_NO_SELL` reuse is also a live bug: `DONE_NO_SELL -> FinishedDecisionMakerRound -> PreTxSettlementRound`, which assumes a prepared bet transaction. In mech-only mode no transaction exists, so the capped path would enter settlement with a missing or stale tx hash. Under Option B the normal decision flow is never entered while capped, so both edits are removed rather than fixed.

### Parameter plumbing (keep)

`max_trades` defaults to `0` (disabled), is validated as non-negative by `DecisionMakerParams`, and is wired through:

```text
packages/valory/agents/trader/aea-config.yaml
packages/valory/skills/decision_maker_abci/models.py
packages/valory/skills/decision_maker_abci/skill.yaml
packages/valory/skills/trader_abci/skill.yaml
packages/valory/services/trader_pearl/service.yaml
```

## Revised workplan (Option B)

### Phase 0 — Remove the Option A shortcut

- Revert the `mech_only_mode` gate in `behaviours/decision_receive.py`.
- Revert the `DONE_NO_SELL` short-circuit in `states/decision_receive.py`.
- Keep `SynchronizedData.mech_only_mode` and `Event.MECH_ONLY`; they are needed by the gate.
- Verify `DONE_NO_SELL` semantics return to their v0.33.2 baseline (`git diff 5409a6e2` over both files should show no changes).

### Phase 1 — Complete the counter vertically

- Test `TradeCountRound` consensus and `TradeCountBehaviour` increment.
- Test only `FinishedBetPlacementTxRound` reaches the count round.
- Test sells do not increment.
- Test persistence across reset periods.
- Retain `TradeCountRound: set()` in `DecisionMakerAbciApp.db_pre_conditions`; it is required because this state is an entry point in the composed FSM.

### Phase 2 — Test the cap gate

The gate itself exists; add the missing tests:

- Cap disabled (`max_trades = 0`): always `DONE`, `mech_only_mode` false.
- Below cap: `DONE`.
- At/above cap: `MECH_ONLY`, `mech_only_mode` persisted true.
- Timeout and no-majority self-loops.

### Phase 3 — Build the Mech-only request flow and establish the no-bet invariant

Retarget `TradeCapRound MECH_ONLY` away from `SamplingRound` into a new, dedicated flow:

```text
TradeCapRound MECH_ONLY -> MechOnlySelectionRound (new)
MechOnlySelectionRound DONE       -> Mech request path (mech_interact_abci)
MechOnlySelectionRound NO_MARKETS -> CallCheckpointRound (via a new Finished round)
Mech response (capped)            -> MechOnlyReceiveRound (new, Trader-owned router)
MechOnlyReceiveRound DONE (queue remaining) -> MechOnlySelectionRound
MechOnlyReceiveRound DONE (queue empty)     -> CallCheckpointRound (via Finished round)
```

Components:

- `states/mech_only.py` + `behaviours/mech_only.py`: a selection round/behaviour that builds a valid Mech request (metadata equivalent to what `DecisionRequestBehaviour` derives from `sampled_bet`) **without** going through `SamplingRound`. A direct `TradeCapRound -> ToolSelectionRound` is insufficient: `DecisionRequestBehaviour` needs `sampled_bet`, normally provided by `SamplingRound`. Do not claim a Mech-only flow until this dedicated request path supplies valid metadata.
- A Trader-owned response router (`MechOnlyReceiveRound`/behaviour) that consumes the Mech delivery on the capped path instead of `DecisionReceiveRound`.
- Composition wiring in `trader_abci/composition.py` for the new Finished rounds; new entries in `initial_states`/`db_pre_conditions` as required; FSM spec regeneration (`make fix-abci-app-specs`).

Hard invariants — the capped path must not:

- reach `BetPlacementRound`, `PolymarketBetPlacementRound`, or `SellOutcomeTokensRound`;
- call profitability calculation;
- set a vote or bet amount;
- sell or blacklist a market based on an unprofitable analysis or Mech error;
- mutate market/bet-queue state as if a trade occurred (in particular, do not run `SamplingBehaviour` bookkeeping).

The Mech-only response handler may track valid delivery/tool health.

Tests for this phase must include an FSM reachability check that no path from `TradeCapRound[MECH_ONLY]` reaches any bet/sell placement round, plus behaviour tests that the router never sets vote/bet fields.

### Phase 4 — Bounded open-market batching

Only after the no-bet invariant is tested:

- Add a positive `MAX_MECH_REQUESTS_PER_CYCLE` parameter with a conservative default, plumbed like `max_trades` (models, skill.yamls, aea-config, service.yaml).
- Define an explicit `is_open_for_mech` predicate over fetched, unresolved, safely queryable binary Omen markets. Do not reuse `SamplingBehaviour.processable_bet()` as the definition of open; it includes trading-specific conditions.
- Select markets deterministically (e.g. sorted by market id), cap each batch, and persist a queue/cursor in synchronized state (`cross_period_persisted_keys` if the queue spans periods); do not silently discard excess work.
- Account for `mech_interact_abci` limiting work by `multisend_batch_size` (default observed as `1`). Multiple requested markets need explicit continuation semantics through the `MechOnlyReceiveRound -> MechOnlySelectionRound` loop.
- Route a completed queue to `CallCheckpointRound` rather than normal decision processing.
- Tests: queue construction determinism, batch capping, cursor persistence, empty-queue routing, and continuation across multiple Mech round-trips.

### Phase 5 — Release / Quickstart

1. Run focused new tests and the relevant Trader suite; run `make generators`, `make fix-abci-app-specs`, `autonomy packages lock`, and CI check targets.
2. Regenerate package/service hashes and release metadata using Trader's release tooling.
3. Publish the changed package/service release to IPFS.
4. Update Quickstart `configs/config_predict_trader.json` with the new Trader version/release CID and expose `MAX_TRADES` (and `MAX_MECH_REQUESTS_PER_CYCLE`) with the intended provisioning policy.
5. Run Quickstart configuration/service tests.

## Relevant v0.33.2 locations

```text
packages/valory/skills/trader_abci/composition.py
packages/valory/skills/decision_maker_abci/rounds.py
packages/valory/skills/decision_maker_abci/states/base.py
packages/valory/skills/decision_maker_abci/behaviours/redeem.py
packages/valory/skills/decision_maker_abci/behaviours/sampling.py
packages/valory/skills/decision_maker_abci/behaviours/decision_request.py
packages/valory/skills/decision_maker_abci/behaviours/decision_receive.py
packages/valory/skills/mech_interact_abci/behaviours/request.py
```

`RedeemBehaviour.async_act()` currently recognizes a settled placement and performs its bookkeeping on the route through `RedeemRouterRound`. The dedicated `TradeCountRound` was inserted before that route so `RedeemPayload`/`RedeemRound` semantics are not overloaded.

## Open decisions

- Confirm whether `MAX_TRADES` counts only successful Omen placements (current scaffold assumes yes) or also Polymarket placements. Current scope and routing count Omen only.
- Finalize the exact `is_open_for_mech` eligibility predicate for post-cap Mech calls.
- Choose whether remaining markets are processed over subsequent cycles or all bounded batches within one period; synchronized queue state is required either way.
- Decide whether capped Mech analysis should affect tool-health/performance telemetry. It must not affect betting policy or market queue state.
