# Omenstrat fast

This single-agent Omen service uses agent id **25**. It uses the existing Safe,
Mech HTTP client, transaction settlement, redemption, staking, and UI code.
It has its own composition and strategy. The normal Omenstrat and Polystrat
compositions are unchanged.

## Latency design

At startup, registration loads the manifest and endpoint of the configured
priority mech. This is a single discovery step. There is no version detection.
The two permitted tools are checked in order against that manifest. The plain
`superforcaster` is used only if the calibrated tool is absent. An unanswered
request is not retried with another tool: each market can be asked at most once.

The trading lane is:

`FreshWatch -> DecisionRequest -> MechRequest -> MechResponse -> FastDecision -> BetPlacement -> settlement -> PostBetUpdate -> FreshWatch`

FreshWatch uses a timestamp-bounded, paginated query. It polls only the selected
creators and binary markets. It includes `creationTimestamp >= last_seen -
OVERLAP_S`, bounded below by the oldest fresh timestamp. The default overlap is
120 seconds. It queues fresh markets in creation order and deduplicates IDs
against both the queue and the persisted asked set.
A cooperative poller keeps discovery on the 15-second cadence while the FSM
waits for a Mech response or transaction settlement. It only collects markets;
requests and bets stay serial. It reloads the queue after each HTTP reply, so
an in-flight poll cannot restore an entry that the foreground already marked
asked. The queue, cursor, asked IDs, and last housekeeping entry are in
`STORE_PATH/fresh_watch.json`. The file is replaced atomically. A market is marked
asked before a paid request. A crash in that gap can lose an opportunity, but
cannot ask that market twice. Do not share this file between agent instances.
Keep the store volume on restart. A corrupt file stops selection instead of
silently discarding the asked set.

Asked IDs store their market creation timestamps. Each successful poll removes
only entries older than `FRESH_MAX_AGE_S + max(0, OVERLAP_S)` seconds. With the
defaults, this retains 720 seconds of creation history. An ID stays at the
cutoff itself. A removed market is already outside both the query and the
fresh-age filter, so it cannot be queued again. This bounds the asked set by the markets in that time interval,
plus old queued markets processed since the last poll. Old saved asked lists
use the saved watermark as a conservative upper bound on their creation times;
they become eligible for pruning only after that watermark expires.

Queued work takes priority, including after the active window ends. When the
queue is empty, housekeeping can start once every 600 seconds inside the window.
Outside it, housekeeping can run on each cycle. The lane runs redemption,
staking checkpoint, performance summary, and ChatUI load. Existing funds-manager
and HTTP-handler funding checks stay active. Bet placement also checks Safe
balances and wraps xDAI if needed. Housekeeping is not preempted if a market
appears while it runs; the poller queues it for the next watch. There is no full market refresh,
ResetAndPause, benchmarking, staking KPI stop, or trade-cap check in this app.

`fresh_watch_abci` owns the two new rounds and the strategy. This keeps the
normal `decision_maker_abci` cycle unchanged. The new composition reuses the
existing DecisionRequest, BetPlacement, Redeem, and PostBetUpdate rounds.

The pre-settlement round clears the previous transaction history and retry state.
It keeps the new prepared transaction and does not reset Tendermint.

The request records live pool prices. FastDecision reads the pool again, blends
the YES probability with the title prior, then selects the side with the largest
edge. Numeric phrases take priority over still-hold phrases. The NO probability
is `1 - p_hat(YES)`. All bounds must pass. The stake is
`min(CAP, KELLY_BANKROLL * (p_side - price) / (1 - price))` in xDAI.
The final execution quote also checks the blended edge, maximum price, and slip
bound. `[FAST_DECISION]` records one terminal preparation decision per market,
including age at request, answer, and bet preparation. A prepared bet is not proof
that its transaction settled; use the existing settlement logs for that result.

## Parameters

| Environment variable | Default |
| --- | --- |
| `CREATORS` | `0x89c5cc945dd550bcffb72fe42bff002429f46fec`, `0xffc8029154ecd55abed15bd428ba596e7d23f557` |
| `FRESH_MAX_AGE_S` | `600` |
| `OVERLAP_S` | `120` |
| `POLL_S` | `15` |
| `ACTIVE_WINDOW_UTC` | `00:00-05:30` (start included, end excluded) |
| `HOUSEKEEPING_INTERVAL_S` | `600` |
| `TOOLS` | `superforcaster_calibrated_full_search`, then `superforcaster` |
| `PRIOR_WEIGHT` | `0.5` (tool weight; prior weight is its complement) |
| `PRIORS` | `still_hold: 0.85`, `numeric_threshold: 0.60`, `other_event: 0.18` |
| `MIN_EDGE` | `0.03` |
| `MAX_PRICE` | `0.90` |
| `NO_MIN_P` | `0.70` |
| `CAP` | `4.0` xDAI |
| `KELLY_BANKROLL` | `10.0` xDAI |
| `MAX_SLIP` | `0.10` absolute price rise |
| `USE_OFFCHAIN` | `true` |
| `MECH_MARKETPLACE_CONFIG` | V2 marketplace `0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB`; priority mech `0x552cEA7Bc33CbBEb9f1D90c1D11D2C6daefFd053`, service `975`; poll interval `1.5` s; response timeout `300` s; no mech failover |
| `FINALIZE_TIMEOUT` | `15.0` s, down from `60.0` |
| `TM_CHECK_SLEEP_DELAY` | `1` s, down from `3` |
| `RETRY_TIMEOUT` | `1` s, down from `3` |
| `STOP_TRADING_IF_STAKING_KPI_MET` | `false` |
| `STRATEGIES_KWARGS.absolute_max_bet_size` | `4000000000000000000` wei (4 xDAI); the fast formula uses `CAP` directly |

The active window uses UTC regardless of the host timezone. `MAX_TRADES` is not
read by this composition. The new parameters are declared in both new skills,
the new agent, and the new service. The two-tool allowlist is fixed; the parameter
must retain those two names in priority order.

## Pilot through quickstart

These are operator steps for a later authorized pilot. This change does not mint,
publish, deploy, or change a server.

1. Review and build the branch from `dagacha/trader`. Check `getAgentIds` on the
   intended service before any mint. Confirm that it expects agent id **25**.
2. Supply a reviewed release artifact for this service before quickstart use.
   The template records the local service CID. It is not available on IPFS until
   a separate publication is authorized. Do not use the normal trader release
   artifact for this agent. Quickstart also needs the matching agent release.
3. Copy `configs/config_omenstrat_fast.json` from this repository next to
   `configs/config_predict_trader.json` in the quickstart checkout. Use a separate
   quickstart instance and store volume for this pilot. Set the release version
   and CID to the approved artifact. Keep `configurations.gnosis.agent_id = 25`.
4. Set the RPC, Safe, service, staking, and funding inputs in that instance.
   Retain the V2 marketplace and priority mech 975. Confirm the runtime-computed
   marketplace config still has the 1.5-second response poll interval.
5. In that quickstart instance, run
   `./run_service.sh configs/config_omenstrat_fast.json` only after deployment is
   authorized. The trader migration skips fast templates and caches cleanly.
6. Inspect `[FAST_DECISION]` and settlement logs. Check that the queue survives
   a local restart before leaving the pilot unattended.

## Open items and limits

Mech 975 off-chain latency is reported as 75–85 seconds in the task brief; this
change reduces agent-side delay and does not remove that tool runtime. Measure
it during the pilot. No live paid request or trade was made for validation.

Housekeeping can delay a queued market until the current housekeeping pass ends.
Discovery continues during that pass. The overlap recovers late-indexed markets
at or up to `OVERLAP_S` seconds below the saved watermark, while they are still
fresh. Markets indexed after that overlap or the fresh-age limit can be missed.
