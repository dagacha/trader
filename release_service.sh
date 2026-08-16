#!/usr/bin/env bash
# release_service.sh
#
# Atomically release the fork's trader service: build the agent image,
# publish the service to IPFS, and ONLY THEN repoint the quickstart
# config(s) at the new service. This couples the three artifacts that
# must stay in lockstep, preventing the drift failure where the
# quickstart config references a service whose agent image was never
# built (docker compose then dies with
#   'manifest for valory/oar-trader:<hash> not found: manifest unknown'
# because fork images are not published to Docker Hub).
#
#   artifact 1: agent image   valory/oar-trader:<agent-hash>  (built locally)
#   artifact 2: service CID   packages.json service/valory/trader_pearl/0.1.0
#                            (published via `autonomy push-all --remote`)
#   artifact 3: quickstart    configs/config_predict_trader.json "hash"
#
# Usage:
#   ./release_service.sh                                    # release to ALL fork-based traders (see below)
#   ./release_service.sh --quickstart 28                    # ~/28-trader/quickstart
#   ./release_service.sh --quickstart 21,38,34              # comma-separated trader IDs
#   ./release_service.sh --quickstart ~/28-trader/quickstart
#   ./release_service.sh --quickstart 28,~/31-trader/quickstart   # IDs and paths mix
#
# Default (no --quickstart): auto-discover every ~/N-trader/quickstart whose
# config tracks THIS fork (agent_release.repository.owner == dagacha) and
# update those. Traders on the official valory-xyz release are skipped with a
# notice — repointing them also requires flipping their agent_release, which
# this script deliberately does not do.
# Options:
#   --quickstart SPEC  quickstart clone(s) whose config "hash" gets updated.
#                      SPEC is a comma-separated list where each item is either
#                      a trader ID N (expands to ~/N-trader/quickstart) or a
#                      path. Repeatable. Each clone's runtime cron script has a
#                      preflight image gate that relies on this alignment.
#   --force-build      rebuild the image even if it exists locally
#   --skip-publish     skip `autonomy push-all --remote` (service assumed published)
#   --dry-run          report current state and what would be done; no changes
#
# Idempotent: re-running with no repo changes is a no-op — image exists,
# push re-pushes identical (content-addressed) CIDs, config already matches.
#
# Prereqs: Docker, uv (https://docs.astral.sh/uv/), network to PyPI +
# the Autonolas IPFS registry.

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SERVICE_YAML="$REPO/packages/valory/services/trader_pearl/service.yaml"
PACKAGES_JSON="$REPO/packages/packages.json"
GATEWAY="https://gateway.autonolas.tech/ipfs"

QUICKSTARTS=()
FORCE_BUILD=0
SKIP_PUBLISH=0
DRY_RUN=0

# Expand a --quickstart SPEC item into $HOME/N-trader/quickstart.
# Items are comma-separated; each item is edge-trimmed only (paths may
# contain inner spaces), a leading ~ is expanded, pure digits N become
# $HOME/N-trader/quickstart. Errors out on garbage or on zero usable items.
add_quickstart() {
  local item added=0
  while IFS= read -r item; do
    # trim leading/trailing whitespace only (keep inner spaces in paths)
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [ -n "$item" ] || continue
    item="${item/#\~/$HOME}"   # expand unquoted leading ~ (shell can't inside a list)
    if [[ "$item" =~ ^[0-9]+$ ]]; then
      item="$HOME/${item}-trader/quickstart"
    elif [[ "$item" != */* ]]; then
      echo "ERROR: --quickstart item '$item' is neither a trader ID (e.g. 28) nor a path" >&2
      exit 1
    fi
    if [ ! -f "$item/configs/config_predict_trader.json" ]; then
      echo "ERROR: no configs/config_predict_trader.json under '$item'" >&2
      exit 1
    fi
    QUICKSTARTS+=("$item")
    added=1
  done < <(tr ',' '\n' <<<"$1")
  if [ "$added" -ne 1 ]; then
    echo "ERROR: --quickstart '$1' contains no usable items" >&2
    exit 1
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --quickstart)
      [ $# -ge 2 ] || { echo "ERROR: --quickstart requires a value (trader ID, path, or comma list)" >&2; exit 1; }
      add_quickstart "$2"; shift 2;;
    --force-build)   FORCE_BUILD=1; shift;;
    --skip-publish)  SKIP_PUBLISH=1; shift;;
    --dry-run)       DRY_RUN=1; shift;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 1;;
  esac
done

# ------------------------------------------------------------------
# Default target: every trader on this machine that tracks THIS fork.
# A trader dir is ~/N-trader/quickstart with a config whose
# agent_release.repository.owner matches FORK_OWNER. Traders on the
# official release are listed and skipped (repointing them to the fork
# also requires flipping agent_release — a deliberate manual change).
# ------------------------------------------------------------------
FORK_OWNER="dagacha"
if [ "${#QUICKSTARTS[@]}" -eq 0 ]; then
  echo ">> no --quickstart given; discovering traders tracking owner '$FORK_OWNER' in $HOME ..."
  for _d in "$HOME"/*-trader/quickstart; do
    _cfg="$_d/configs/config_predict_trader.json"
    [ -f "$_cfg" ] || continue
    _owner="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("agent_release",{}).get("repository",{}).get("owner",""))' "$_cfg" 2>/dev/null || true)"
    if [ "$_owner" = "$FORK_OWNER" ]; then
      QUICKSTARTS+=("$_d")
    else
      echo "   skip ${_d#"$HOME"/} (agent_release.owner='${_owner:-none}' != '$FORK_OWNER')"
    fi
  done
  if [ "${#QUICKSTARTS[@]}" -eq 0 ]; then
    echo ">> no fork-based traders found; building + publishing only (configs untouched)"
  else
    echo ">> will update: ${QUICKSTARTS[*]}"
  fi
fi

# ------------------------------------------------------------------
# 0. validate every selected quickstart config BEFORE build/publish.
#    A config that is invalid JSON or lacks a non-empty top-level "hash"
#    means a real run would leave that deployment pointing nowhere/new, so a
#    real run fails fast and nonzero instead of skipping it silently.
#    Consistent with the gateway check, --dry-run only reports the defect and
#    continues (a dry-run must still show the plan for the healthy targets).
# ------------------------------------------------------------------
BROKEN_CONFIGS=()
# ${arr[@]+"${arr[@]}"} is safe under `set -u` on bash 3.2 (macOS) and 5+;
# plain "${arr[@]}" on an empty array crashes 3.2.
for qs in "${QUICKSTARTS[@]+"${QUICKSTARTS[@]}"}"; do
  cfg="$qs/configs/config_predict_trader.json"
  if ! python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)          # not valid JSON
if not isinstance(d, dict) or "hash" not in d or not isinstance(d["hash"], str) or not d["hash"]:
    sys.exit(1)          # no usable top-level "hash"
# explicit conditionals, not asserts — asserts are stripped by PYTHONOPTIMIZE=1
' "$cfg" >/dev/null 2>&1; then
    if [ "$DRY_RUN" -eq 1 ]; then
      echo ">> (dry-run) $cfg is not valid JSON or lacks a non-empty top-level \"hash\"; a real run would FAIL here (rc=1) and skip building/publishing"
      echo ">> (dry-run) excluding $cfg from this preview"
      BROKEN_CONFIGS+=("$cfg")
    else
      echo "ERROR: $cfg is not valid JSON with a non-empty top-level \"hash\"; refusing to leave this target unreleased." >&2
      echo "       Fix the config, or drop it from --quickstart / make it not track the fork." >&2
      exit 1
    fi
  fi
done

# ------------------------------------------------------------------
# 1. repo venv with aea + autonomy (one-time on a fresh clone)
# ------------------------------------------------------------------
if [ ! -x "$REPO/.venv/bin/autonomy" ] || [ ! -x "$REPO/.venv/bin/aea" ]; then
  echo ">> uv sync (one-time venv setup)..."
  if [ "$DRY_RUN" -eq 1 ]; then echo "   (dry-run: skipped)"; else (cd "$REPO" && uv sync); fi
fi
AUTONOMY="$REPO/.venv/bin/autonomy"

# ------------------------------------------------------------------
# 2. integrity: local packages must match packages.json pinned hashes.
#    Without this the agent hash we read below would NOT be the one the
#    deployment expects. (build_image_offline.sh is invoked with BIO_SKIP_SYNC=1
#    so it trusts this single verification instead of re-running it.)
# ------------------------------------------------------------------
echo ">> autonomy packages sync --update-packages (up to 3 attempts) ..."
if [ "$DRY_RUN" -eq 1 ]; then
  echo "   (dry-run: skipped)"
else
  for _attempt in 1 2 3; do
    if (cd "$REPO" && "$AUTONOMY" packages sync --update-packages); then break; fi
    if [ "$_attempt" -eq 3 ]; then
      echo "ERROR: packages sync failed after 3 attempts." >&2
      exit 1
    fi
    echo "   attempt $_attempt failed (often a transient IPFS/RPC timeout); retrying..."
    sleep $((_attempt * 5))
  done
fi

echo ">> autonomy packages lock --check ..."
if [ "$DRY_RUN" -eq 1 ]; then
  echo "   (dry-run: skipped)"
else
  (cd "$REPO" && "$AUTONOMY" packages lock --check)
fi

# ------------------------------------------------------------------
# 3. resolve agent hash + service CID from the (verified) tree
# ------------------------------------------------------------------
AGENT_HASH="$(sed -n 's/^agent: valory\/trader:[0-9.]*:\(bafybei[a-z0-9]*\)[[:space:]]*$/\1/p' "$SERVICE_YAML")"
if [ -z "$AGENT_HASH" ]; then
  echo "ERROR: could not read agent hash from $SERVICE_YAML" >&2
  exit 1
fi
SVC_CID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["dev"].get("service/valory/trader_pearl/0.1.0",""))' "$PACKAGES_JSON")"
if [ -z "$SVC_CID" ]; then
  echo "ERROR: could not read service CID from $PACKAGES_JSON" >&2
  exit 1
fi
IMAGE="valory/oar-trader:$AGENT_HASH"

echo
echo "## Repo:         $REPO"
echo "## Agent hash:   $AGENT_HASH"
echo "## Image:        $IMAGE"
echo "## Service CID:  $SVC_CID"
echo

# ------------------------------------------------------------------
# 4. build the agent image (skip when already present unless --force-build)
# ------------------------------------------------------------------
if docker image inspect "$IMAGE" >/dev/null 2>&1 && [ "$FORCE_BUILD" -eq 0 ]; then
  echo ">> image $IMAGE already present locally; skipping build"
else
  echo ">> building $IMAGE via build_image_offline.sh ..."
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "   (dry-run: skipped)"
  else
    BIO_SKIP_SYNC=1 "$REPO/build_image_offline.sh" "$REPO" "valory/trader:0.1.0:$AGENT_HASH"
    docker image inspect "$IMAGE" >/dev/null 2>&1 \
      || { echo "ERROR: build did not produce $IMAGE" >&2; exit 1; }
  fi
fi

# ------------------------------------------------------------------
# 5. publish all packages (incl. the service) to the remote IPFS registry.
#    Content-addressed: re-publishing unchanged packages is a no-op that
#    yields the same CIDs.
# ------------------------------------------------------------------
if [ "$SKIP_PUBLISH" -eq 1 ]; then
  echo ">> --skip-publish: skipping autonomy push-all --remote"
elif [ "$DRY_RUN" -eq 1 ]; then
  echo ">> (dry-run) would run: autonomy push-all --remote"
else
  echo ">> autonomy push-all --remote ..."
  (cd "$REPO" && "$AUTONOMY" push-all --remote --retries 3)
fi

# ------------------------------------------------------------------
# 5b. `push-all` only pushes packages.json["dev"]. Third-party packages are
#     never pushed by it; fork-modified third-party skills get CIDs upstream
#     never serves (e.g. valory/transaction_settlement_abci:bafybeia77fs…), so a
#     fresh clone stalls mid-`packages sync` with "The read operation timed out".
#     After the push-all, content-address every third-party package whose CID is
#     not yet resolvable on the gateway, by directory path (content-addressed →
#     idempotent). `push` (unlike `push-all`) takes no --retries. Skipped with
#     step 5 under --skip-publish; honored by --dry-run.
# ------------------------------------------------------------------
if [ "$SKIP_PUBLISH" -eq 1 ]; then
  echo ">> --skip-publish: skipping third-party path-push (with step 5)"
else
  echo ">> pushing unresolvable third-party packages ..."
  _cand="$(mktemp)"
  trap '[ -n "${_cand:-}" ] && rm -f "$_cand"' EXIT
  python3 - "$PACKAGES_JSON" "$GATEWAY" <<'PY' > "$_cand"
import json, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
pkgs, gateway = sys.argv[1], sys.argv[2]
plural = {"skill": "skills", "protocol": "protocols", "contract": "contracts",
          "connection": "connections", "custom": "customs"}
items = []
for key, cid in json.load(open(pkgs))["third_party"].items():
    t, a, n, _ = key.split("/")
    items.append((t, f"packages/{a}/{plural[t]}/{n}", cid))
def resolvable(cid):
    try:
        urllib.request.urlopen(urllib.request.Request(f"{gateway}/{cid}", method="GET"),
                               timeout=20).close()
        return True
    except Exception:
        return False
with ThreadPoolExecutor(max_workers=8) as ex:
    results = list(ex.map(resolvable, [c for _, _, c in items]))
for (kind, path, cid), ok in zip(items, results):
    if not ok:
        print(f"{kind}\t{path}\t{cid}")
PY
  had=0; skipped=0
  while IFS=$'\t' read -r _k _p _c; do
    [ -n "$_k" ] || continue
    if [ ! -d "$REPO/$_p" ]; then
      echo "   (unresolvable, no local copy of $_p; not pushable from here)"
      skipped=$((skipped + 1)); continue
    fi
    had=$((had + 1))
    echo "   missing provider: $_p ($_c)"
    if [ "$DRY_RUN" -eq 1 ]; then
      echo "   (dry-run) would push: autonomy push $_k $_p --remote"
    else
      (cd "$REPO" && "$AUTONOMY" push "$_k" "$_p" --remote) \
        || { echo "ERROR: could not push $_p" >&2; exit 1; }
    fi
  done < "$_cand"
  rm -f "$_cand"
  if [ "$had" -gt 0 ] && [ "$skipped" -gt 0 ]; then
    echo "   re-pushed $had missing provider(s); $skipped skipped (no local copy here)"
  elif [ "$had" -gt 0 ]; then
    echo "   done: re-pushed all missing providers"
  elif [ "$skipped" -gt 0 ]; then
    echo "   all missing providers are unpushable here (no local copy); re-push from a machine with the packages"
  else
    echo "   all third-party CIDs resolvable; none to push"
  fi
fi

# ------------------------------------------------------------------
# 6. verify the service CID is resolvable BEFORE touching any config.
#    This is the coupling guarantee: configs are only repointed at a
#    service that is actually published and whose image is built.
# ------------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  # report-only: a dry-run must still show what would change even when the
  # service is not published yet (first-time dry-runs on an unreleased fork)
  echo ">> (dry-run) gateway check for $SVC_CID:"
  if curl -fsSL --max-time 60 -o /dev/null "$GATEWAY/$SVC_CID"; then
    echo "   resolvable"
  else
    echo "   NOT resolvable yet — a real run would stop here and require publishing first"
  fi
else
  echo ">> verifying $SVC_CID is resolvable on $GATEWAY ..."
  if ! curl -fsSL --max-time 60 -o /dev/null "$GATEWAY/$SVC_CID"; then
    echo "ERROR: service $SVC_CID not resolvable on the gateway." >&2
    echo "       Publish first (autonomy push-all --remote) before updating configs." >&2
    exit 1
  fi
  echo "   OK: resolvable"
fi

# ------------------------------------------------------------------
# 7. repoint quickstart config(s) at the released service CID.
#    Occurrence-validated text edit: only a substitution that makes the
#    parsed top-level "hash" equal the new CID is accepted, so nested
#    "hash" fields can never be edited by mistake; everything else in the
#    file — including local manual edits — is preserved byte-for-byte.
# ------------------------------------------------------------------
for qs in "${QUICKSTARTS[@]+"${QUICKSTARTS[@]}"}"; do
  cfg="$qs/configs/config_predict_trader.json"
  # Step 0 already guarantees existence/validity/hash, and already aborted
  # (real run) or excluded (dry-run) any broken config, so no re-guards here.
  for _b in "${BROKEN_CONFIGS[@]+"${BROKEN_CONFIGS[@]}"}"; do
    if [ "$_b" = "$cfg" ]; then
      echo ">> (dry-run) skipping flagged-broken config $cfg"
      continue 2
    fi
  done
  old_hash="$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["hash"])' "$cfg")"
  if [ "$old_hash" = "$SVC_CID" ]; then
    echo ">> $cfg already at $SVC_CID (no change)"
    continue
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo ">> (dry-run) would update $cfg: $old_hash -> $SVC_CID"
    continue
  fi
  python3 - "$cfg" "$SVC_CID" <<'PY'
import json, re, sys
path, new_hash = sys.argv[1], sys.argv[2]
text = open(path).read()
# Replace each "hash" occurrence in turn; accept only the edit that makes the
# parsed top-level "hash" equal new_hash. Guarantees we never silently edit a
# nested "hash" field (e.g. inside env_variables) instead of the real one.
for m in re.finditer(r'("hash"\s*:\s*")[^"]*(")', text):
    cand = text[:m.start()] + m.group(1) + new_hash + m.group(2) + text[m.end():]
    try:
        if json.loads(cand).get("hash") == new_hash:
            open(path, "w").write(cand)
            break
    except json.JSONDecodeError:
        continue
else:
    raise SystemExit("ERROR: could not update the top-level \"hash\" in " + path)
PY
  echo ">> updated $cfg: $old_hash -> $SVC_CID"
done

# ------------------------------------------------------------------
# 8. summary
# ------------------------------------------------------------------
echo
echo "## DONE."
echo "## Image:       $IMAGE"
echo "## Service CID: $SVC_CID"
# exclude any config excluded earlier (dry-run broken targets are not updated)
VALID_QS=()
_br=""
for qs in "${QUICKSTARTS[@]+"${QUICKSTARTS[@]}"}"; do
  _br=""
  for _b in "${BROKEN_CONFIGS[@]+"${BROKEN_CONFIGS[@]}"}"; do [ "$_b" = "$qs/configs/config_predict_trader.json" ] && _br=1; done
  [ -z "$_br" ] && VALID_QS+=("$qs")
done
if [ "${#VALID_QS[@]}" -gt 0 ]; then
  echo "## Configs updated (targeted): ${VALID_QS[*]}"
  [ "${#BROKEN_CONFIGS[@]}" -gt 0 ] && echo "## NOTE: excluded (broken) in dry-run: ${BROKEN_CONFIGS[*]}"
  echo "## Commit the config change(s) in each quickstart repo, e.g.:"
  echo "##   git add configs/config_predict_trader.json"
  echo "##   git commit -m \"fix(traderNN): point at service $SVC_CID (agent ${AGENT_HASH:0:16}...)\""
else
  echo "## No valid (non-broken) configs to update."
fi
