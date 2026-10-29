# Omenstrat lightning

This single-agent Omen service extends the [fast lane](../omenstrat_fast/README.md).
It uses the same Safe, tool order, Mech selection, trade cap, Kelly sizing,
settlement, and housekeeping. State belongs to one agent in its `STORE_PATH`.
The fast, Pearl, and Polymarket services keep their current defaults.

## Decisions and discovery

1. An allowed creator, the case-insensitive phrase `\bstill hold\b`, and Realitio
   template 2 with outcomes exactly `["Yes", "No"]` qualify for a direct YES
   decision. Other markets keep the plain fast strategy. The agent computes the
   YES rate from valid resolved markets with the same template and outcomes.
   It uses markets created in the last 21 days. The normal cache refresh runs once
   per day, outside `ACTIVE_WINDOW_UTC`. Missing or unusable evidence can refresh
   during the window, with retries at least 30 minutes apart. Samples remain
   usable for 48 hours. Fewer than 60 observations or a rate below 0.90 sends
   the market to the usual Mech path. This path uses the cached prior, or 0.95
   if no rate exists. The direct path never buys NO.
2. During `ACTIVE_WINDOW_UTC`, the agent scans factory creation events through
   its configured RPC through the contract API and checks each event's creator.
   One head read and one shared log read cover market and preview discovery.
   The creation
   receipt links the condition to its Realitio question and title. The subgraph
   supplies a title only if the receipt cannot supply it. Continuous RPC errors
   for more than 60 seconds enable the normal 15-second subgraph poll.
3. Question preview is experimental and off by default. When enabled, the agent
   watches the creators' `propose-question` requests and the proposer
   Mech's deliveries. It reads the delivery from the configured IPFS gateway.
   Each proposed title outside the still-hold template can start the usual
   prediction request before a market exists. Answers use exact titles. Case,
   punctuation, or whitespace changes do not match. Entries expire after
   15 minutes. A pending request can block its market only while it belongs to
   this process and is within the Mech timeout, capped at five minutes. Each
   night permits at most 15 preview requests. Expired previews
   whose market never appeared are logged as a paid request without a market.

All decisions use fresh pool balances before sizing and placement. At a base
rate of 0.95 and `MIN_EDGE=0.03`, the edge permits a ceiling of 0.92. Direct
still-hold bets use `LIGHTNING_STILL_HOLD_MAX_PRICE` in place of `MAX_PRICE`.
The usual slip bound can lower the ceiling.
`PRICE_MARGIN` leaves room when sizing. Placement reuses the existing GS013
minimum-output calculation and bounded pre-flight simulation. Direct bets count
against `MAX_TRADES` in the same way as Mech decisions.

## Parameters

The Boolean defaults are OFF in the shared skills and the fast agent/service.
The lightning agent/service and quickstart template enable still-hold decisions
and chain discovery. Experimental question preview remains off by default.
Other values below are the lightning defaults.

| Environment name | Lightning default | Purpose |
| --- | --- | --- |
| `LIGHTNING_STILL_HOLD` | `true` | Enable direct still-hold YES decisions. |
| `LIGHTNING_STILL_HOLD_MAX_PRICE` | `0.93` | Direct still-hold price ceiling. |
| `LIGHTNING_BASE_RATE_WINDOW_DAYS` | `21` | Creation-time sample window. |
| `LIGHTNING_BASE_RATE_MIN_N` | `60` | Minimum valid resolved sample. |
| `LIGHTNING_BASE_RATE_FLOOR` | `0.90` | Minimum rate for direct decisions. |
| `STILL_HOLD_PRIOR` | `0.95` | Mech prior when no cached rate exists; fast keeps `0.85`. |
| `LIGHTNING_CHAIN_DETECT` | `true` | Use factory events during the active window. |
| `OMEN_FPMM_FACTORY` | `0x9083A2B699c0a4AD06F63580BDE2635d26a3eeF0` | Factory to scan. |
| `LIGHTNING_RPC_FALLBACK_S` | `60` | Continuous RPC failure before subgraph fallback. |
| `LIGHTNING_PREVIEW` | `false` | Enable experimental predictions from proposed titles. |
| `LIGHTNING_PREVIEW_MAX_PER_NIGHT` | `15` | Maximum paid preview requests per night. |
| `LIGHTNING_CREATORS` | The two fast-lane creators | Creators allowed for lightning rules. |

The creators are `0x89c5cc945dd550bcffb72fe42bff002429f46fec` and
`0xffc8029154ecd55abed15bd428ba596e7d23f557`. The lightning template sets
`MAX_PRICE=0.90`, as fast does. All other fast controls apply.

Before release on operate 0.15.5 hosts, add **all 12 names in this table** to
olas-scripts `patch_agent_env_passthrough.py` `_NAMED_VARS`. A missing name
caused the reported 26 October outage. No host script is changed by this commit.

## Chain verification

The factory address and indexed creator were checked against creator
[transaction 0x1a1d…8c00](https://gnosisscan.io/tx/0x1a1d567d7d7c596895327e53568aacc24378cf45234b8a00fbe98d0c31608c00#eventlog),
block 48997577, 29 October 2026 at 04:19:45 UTC. It created market
`0xf46F0b6e9e96B466a3563d025577E65648C7daa7`. Its receipt contains
`FixedProductMarketMakerCreation`, `ConditionPreparation`, and `LogNewQuestion`.
The creation topic is
`0x92e0912d3d7f3192cad5c7ae3b47fb97f9c465c1dd12a5c24fd901ddb3905f43`.

The creator's proposer
[request transaction](https://gnosisscan.io/tx/0xc5832f9696e5779bbceb57f0ef4d259d99a4aa00fdb6e53f87c6a485f517232b#eventlog)
uses marketplace `0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB` and proposer
`0x601024e27f1c67b28209e24272ced8a31fc8151f`. Its
[delivery transaction](https://gnosisscan.io/tx/0xe6732def8da352e58980878ca75f1cf389d3f0eac0f516607b7951d1fb918c50#eventlog)
landed at block 48997570, 35 seconds before creation. The saved delivery format
has a JSON string in `result` and a mapping of questions keyed by ID.

## Logs

```text
[LIGHTNING_BASE_RATE] n=.. yes=.. rate=.. window=..
[LIGHTNING_DETECT] market=.. delay_s=.. source=chain|subgraph
[LIGHTNING_PREVIEW] title_hash=.. delivered_at=.. requested_at=.. answered_at=.. market_seen_at=..
```

Use the existing `[FAST_DECISION]`, `[MECH_LATENCY]`, and settlement logs to
check the resulting decision and transaction. These commands read local logs:

```sh
grep '\[LIGHTNING_BASE_RATE\]' agent.log
grep '\[LIGHTNING_DETECT\]' agent.log
grep '\[LIGHTNING_PREVIEW\]' agent.log
grep -E '\[LIGHTNING_|\[FAST_DECISION\]|\[MECH_LATENCY\]' agent.log
```

## Rollout and rollback

After review and merge, the proposed canary is two traders on different hosts
for three nights, compared with their fast siblings. Measure creation-to-bet
latency, execution price, Mech fees, skips, and settlement results. The target
is about 35–45 seconds from creation to a still-hold bet. Local tests do not
establish that timing.

Select this service with `release_service.sh --quickstart N --service
omenstrat_lightning`. Roll back one trader with:

```sh
./release_service.sh --quickstart N --service omenstrat_fast
```

Migration sets the feature flags for the selected service and records the
previous values. It adds other missing inputs and preserves an explicit creator
list. Rollback removes added inputs and restores recorded values. Operator edits
to other controls remain in place.
The release script already permits service public-ID changes. New quickstarts
can use `configs/config_omenstrat_lightning.json`; do not replace an existing
trader's identity or staking configuration with that template.

## Evidence supplied with the task

The project analysis reports 265 resolved still-hold markets created on
13–22 October, with 95.1% YES. Where superforcaster tools answered at least 0.9,
99–100% resolved YES. The safes' still-hold NO bets lost 29% over 277 bets.
Four traders' 28 October logs had about 20 YES decisions at prices of
0.75–0.91 skipped after blending with the old 0.85 prior.

Across 351 markets on 16–27 October, proposer delivery preceded creation by
25–65 seconds, with a median of 30 seconds. Exact title agreement was checked
for 12 of 12 deliveries. These analysis figures were supplied in the task;
the analysis folder was not available on this machine.

The supplied replay results for direct still-hold bets were +1.5 and +3.4 xDAI
per night on the safes' stakes for 26–27 October. After the next-block buyer's
trade, the stated opportunity was about 1.3 xDAI per market, or 33 xDAI per night
for all followers. These figures are context for the canary, not measured
results from this implementation.
