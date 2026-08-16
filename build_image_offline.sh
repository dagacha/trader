#!/usr/bin/env bash
# build_image_offline.sh
#
# Build valory/oar-trader:<agent-hash> from this repo with NO Autonolas IPFS RPC
# dependency during the Docker build. Reproduces the exact image tag the quickstart
# expects (identical to a remote build), so run_service.sh / run_service_cron.sh
# pull it the same way. Works on any computer with Docker + uv.
#
# Why offline: `autonomy build-image` wraps `docker build`, whose inner `aea fetch`
# hits the Autonolas IPFS RPC — flaky for large cold third_party DAGs (504/hang).
# This script fetches the agent OFFLINE from the local packages registry, then a
# custom Dockerfile COPYs it in. Only PyPI is hit during the build (reliable).
#   NOTE: step 1 (`packages sync`) still reaches the Autonolas registry (needs network
#   on a fresh box); only the Docker build itself is IPFS-free — that is the real win.
#
# Usage:
#   ./build_image_offline.sh                                        # default option-2 agent
#   ./build_image_offline.sh /path/to/trader [valory/trader:0.1.0:<hash>]
#
# Prereqs on a fresh box: Docker, and uv (https://docs.astral.sh/uv/).

set -euo pipefail

REPO="${1:-$(cd "$(dirname "$0")" && pwd)}"
DEFAULT_AGENT="valory/trader:0.1.0:bafybeiberfq5biubzcatbh7il7jnafxpmniqoy7cgbscsutnaagy7rmw2m"
# service CID corresponding to DEFAULT_AGENT (valory/trader_pearl in packages.json)
DEFAULT_SERVICE_CID="bafybeif4xzmcmwrvwlkqewofhwg6lld7z332eqsfkph7doljidv7rygg4q"
AGENT="${2:-$DEFAULT_AGENT}"
BASE_IMAGE="valory/open-autonomy:0.21.26"
AGENT_HASH="${AGENT##*:}"
[[ "$AGENT_HASH" =~ ^bafy ]] || { echo "ERROR: '$AGENT' has no bafy... hash (got '$AGENT_HASH'); pass 'valory/trader:0.1.0:<hash>'" >&2; exit 1; }
IMAGE_TAG="valory/oar-trader:${AGENT_HASH}"
BUILD_DIR="$(mktemp -d /tmp/oar-build.XXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "## Repo:      $REPO"
echo "## Agent:     $AGENT"
echo "## Image:     $IMAGE_TAG"
echo "## Build dir: $BUILD_DIR"
echo

cd "$REPO"

# 0. repo venv with aea + autonomy (uv sync is one-time on a fresh clone)
if [ ! -x ".venv/bin/aea" ]; then
  echo ">> uv sync (one-time venv setup)..."
  command -v uv >/dev/null 2>&1 || { echo "ERROR: install uv first: https://docs.astral.sh/uv/"; exit 1; }
  uv sync
fi
AEA="$REPO/.venv/bin/aea"
AUTONOMY="$REPO/.venv/bin/autonomy"

# 1. materialize gitignored third_party packages to MATCH the pinned hashes.
#    (idempotent + hash-preserving: downloads so local == packages.json.
#     --update-hashes is the opposite flag and would drift the agent hash.
#     Retry to ride out transient Autonolas IPFS/RPC read timeouts — sync is
#     resumable, so re-running picks up where it left off.)
#     Set BIO_SKIP_SYNC=1 when the CALLER has already run packages sync +
#     lock --check against this tree (e.g. release_service.sh does both right
#     before invoking this script) — avoids paying the registry pass twice.
if [ "${BIO_SKIP_SYNC:-0}" = "1" ]; then
  echo ">> BIO_SKIP_SYNC=1: skipping packages sync + lock --check (caller verified the tree)"
else
echo ">> pruning stale ignored fixture dirs (migration for clean abstract_round_abci pin) ..."
rm -rf "$REPO/packages/valory/skills/abstract_round_abci/tests/data"
echo ">> autonomy packages sync --update-packages (up to 3 attempts) ..."
for _attempt in 1 2 3; do
  if "$AUTONOMY" packages sync --update-packages; then break; fi
  if [ "$_attempt" -eq 3 ]; then
    echo "ERROR: packages sync failed after 3 attempts." >&2
    exit 1
  fi
  echo "   attempt $_attempt failed (often a transient IPFS/RPC timeout); retrying..."
  sleep $((_attempt * 5))
done

# 2. integrity gate: local fingerprints must match packages.json, else the
#    offline build would NOT reproduce the expected agent hash.
echo ">> autonomy packages lock --check ..."
if ! "$AUTONOMY" packages lock --check; then
  echo "ERROR: packages lock --check failed." >&2
  echo "       Usually step 1 didn't fully materialize third-party packages" >&2
  echo "       (a 'Skill configuration not found' error). Re-run packages sync" >&2
  echo "       --update-packages until it completes, then retry." >&2
  exit 1
fi
fi

# 3. offline fetch of the agent + all transitive deps from the LOCAL packages
#    registry. --registry-path is mandatory: aea only searches cwd + one parent.
echo ">> aea fetch --local (offline, no IPFS) ..."
( cd "$BUILD_DIR" && "$AEA" --registry-path "$REPO/packages" fetch --local "$AGENT" --alias agent )
rm -rf "$BUILD_DIR/agent/.build"   # let the image rebuild it cleanly

# 4. custom Dockerfile: COPY the pre-fetched agent in (no `aea fetch`, no IPFS).
#    Mirrors open-autonomy 0.21.26's generated `build-image` template; only the
#    `aea fetch` step is replaced by the pre-vendored COPY. Re-sync if upstream changes.
cat > "$BUILD_DIR/Dockerfile" <<EOF
FROM ${BASE_IMAGE}
ARG AUTHOR=valory
RUN aea init --reset --local --author \${AUTHOR}
WORKDIR /home
COPY agent /home/agent
WORKDIR /home/agent
RUN pip install --upgrade pip
RUN aea build
RUN aea install --timeout 21600
RUN chmod -R a+x /root && chmod -R 777 /home
CMD ["/root/scripts/start.sh"]
HEALTHCHECK --interval=3s --timeout=600s --retries=600 CMD netstat -ltn | grep -c 26658 > /dev/null; if [ 0 != \$? ]; then exit 1; fi;
EOF

# 5. build (slow step is aea install's pip downloads, ~3-5 min; only PyPI is hit)
echo ">> docker build -t $IMAGE_TAG ..."
docker build -t "$IMAGE_TAG" "$BUILD_DIR"

# 6. verify the stuck-agent fix landed (300s tolerance, commit 04be2510); FAIL-CLOSED.
echo ">> verify BLOCKS_STALL_TOLERANCE == 300 in $IMAGE_TAG (fail-closed) ..."
docker run --rm --entrypoint bash "$IMAGE_TAG" -c \
  'grep -qE "^[[:space:]]*BLOCKS_STALL_TOLERANCE[[:space:]]*=[[:space:]]*300[[:space:]]*$" /home/agent/vendor/valory/skills/abstract_round_abci/base.py' \
  || { echo "ERROR: BLOCKS_STALL_TOLERANCE != 300 (or vendored base.py missing) in $IMAGE_TAG — refusing to succeed"; exit 1; }
echo "   OK: BLOCKS_STALL_TOLERANCE == 300"

echo
echo "## DONE. Image: $IMAGE_TAG"
echo "## Deploy happens in the SEPARATE quickstart repo (dagacha/quickstart), not this one:"
echo "##   set its configs/config_predict_trader.json \"hash\" to the matching service CID"
if [ "$AGENT" = "$DEFAULT_AGENT" ]; then
  echo "##   service CID: $DEFAULT_SERVICE_CID"
else
  echo "##   agent overridden — supply the SERVICE CID matching $AGENT yourself"
  echo "##   (it is NOT $DEFAULT_SERVICE_CID, which pairs with the default agent)"
fi
echo "##   then run that repo's run_service_cron.sh / run_service.sh (absent from THIS repo)."
