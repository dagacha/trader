# Omenstrat fast

This single-agent Omen service template uses agent id **25**. Existing Omen
quickstarts keep their configured agent ID when released. It uses the existing
Safe, Mech HTTP client, transaction settlement, redemption, staking, and UI code.
It has its own composition and strategy. The normal Omenstrat and Polystrat
compositions are unchanged.

## Latency design

At startup, registration loads the manifests and endpoints of the allowed Mechs.
This is a single discovery step. There is no version detection. Dynamic selection
is on by default. It uses the shared tool filter and Mech ranking. The priority
Mech is used only when `FAST_DYNAMIC_MECH=false`.
The two permitted tools are checked in order against the discovered tools.
The plain `superforcaster` is used only if the calibrated tool is absent.
A request is sent only to a Mech that lists the tool in `relevant_tools`.
An unanswered request is not retried with another tool: each market can be asked
at most once. A required deposit returns to the same selected Mech and reuses
the signed request before response polling starts.

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
including age at request, answer, and bet preparation, plus `mech` and `latency_s`. A prepared bet is not proof
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
| `FAST_DYNAMIC_MECH` | `true`; controls `use_dynamic_mech_selection` |
| `VALID_MECHS` | Normal Omen allowlist plus Mech 975 |
| `MECH_MARKETPLACE_CONFIG` | V2 marketplace `0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB`; dynamic selection; static priority `0x552cEA7Bc33CbBEb9f1D90c1D11D2C6daefFd053`, service `975`; poll interval `1.5` s; response timeout `300` s; no mech failover |
| `FINALIZE_TIMEOUT` | `15.0` s, down from `60.0` |
| `TM_CHECK_SLEEP_DELAY` | `1` s, down from `3` |
| `RETRY_TIMEOUT` | `1` s, down from `3` |
| `STOP_TRADING_IF_STAKING_KPI_MET` | `false` |
| `STRATEGIES_KWARGS.absolute_max_bet_size` | `4000000000000000000` wei (4 xDAI); the fast formula uses `CAP` directly |

The active window uses UTC regardless of the host timezone. `MAX_TRADES`
defaults to `3` per staking period; `0` disables the cap. The durable counter
survives restarts. After the cap, only off-chain Mech requests run until the
staking activity target is met. Then the agent waits and performs periodic
housekeeping. An activity target met before the cap does not stop trading.
Fast parameters are declared in both skills, the agent, and the service. The two-tool allowlist is fixed; the parameter
must retain those two names in priority order.

## Release through quickstart

Follow the [three-step release guide](../../../../README.md#release-an-omen-trader):
publish, run `release_service.sh`, then optionally run the config migration.
Use `--quickstart /path/to/canary/quickstart --dry-run` first, then release and
observe that one trader before the fleet. Existing configs keep their name, description,
agent ID, staking settings, and user values. The fast JSON template is only a
defaults source. Do not copy it over an existing trader config.

Release builds `valory/oar-omenstrat_fast:<agent-hash>`. To roll back, use
`--service trader_pearl`; release builds `valory/oar-trader:<agent-hash>`.
In both directions, release checks the cached Omen service package's public ID.
If it differs from the selected service, migration sets the top-level
`allow_different_service_public_id` flag to `true` in
`configs/config_predict_trader.json`. This also repairs a config whose hash
already points to the selected service. A fresh quickstart or a cache with the
same public ID needs no new flag. Migration keeps an existing flag.
To preview a repair, run `python3 scripts/migrate_trader_config.py
/path/to/quickstart --service omenstrat_fast --dry-run`. For rollback, select
`--service trader_pearl`. Remove `--dry-run` to apply the repair. These changes
keep the trader's own name and description, service ID, Safe, and staking settings.
Neither path mints a service. The olas-scripts
`enable_offchain_controls_all.sh` helper is not needed for fast traders.

## Open items and limits

The task brief reports that trader 48 selected Mech
`0xdb78159e9246ec738f51c2c9cb1169b5c0e45fee` on 24 October. Its off-chain
`superforcaster_calibrated_full_search` requests took 22–62 seconds, with a median
of about 31 seconds. Mech 975 took 75–85 seconds. These are prior observations,
not a latency guarantee. No live paid request or trade was made for validation.

Housekeeping can delay a queued market until the current housekeeping pass ends.
Discovery continues during that pass. The overlap recovers late-indexed markets
at or up to `OVERLAP_S` seconds below the saved watermark, while they are still
fresh. Markets indexed after that overlap or the fresh-age limit can be missed.

## Mech latency logs

Both Omen compositions log each answer in the shared request and response path.
Mech-only answers use the same path. The line format is:

```text
[MECH_LATENCY] tool=<tool> mech=<address> offchain=<bool> sent_ts=<unix> answered_ts=<unix> latency_s=<float> market=<id>
```

`sent_ts` is the actual off-chain POST time, including a re-POST after a deposit,
or the time the agent observes on-chain request settlement. `answered_ts` is the
response read time. A timeout does not count as an answer. `market=-` means that
the request has no market context. Timing records are local to the agent process.

Find median latency per tool in an agent log:

```sh
grep '\[MECH_LATENCY\]' agent.log | awk '{for(i=1;i<=NF;i++){split($i,a,"=");if(a[1]=="tool")t=a[2];if(a[1]=="latency_s")l=a[2]}print t,l}' | sort -k1,1 -k2,2n | awk 'function emit(){if(n) print tool,(v[int((n+1)/2)]+v[int((n+2)/2)])/2} $1!=tool{emit();tool=$1;n=0} {v[++n]=$2} END{emit()}'
```
