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
#
# Usage:
#   ./build_image_offline.sh                                        # default option-2 agent
#   ./build_image_offline.sh /path/to/trader [valory/trader:0.1.0:<hash>]
#
# Prereqs on a fresh box: Docker, and uv (https://docs.astral.sh/uv/).

set -euo pipefail

REPO="${1:-$(cd "$(dirname "$0")" && pwd)}"
AGENT="${2:-valory/trader:0.1.0:bafybeiasw73g6en52r2645el5qkxockkm736ca45mdqaulmqg3yzmjjqwa}"
BASE_IMAGE="valory/open-autonomy:0.21.26"
AGENT_HASH="${AGENT##*:}"
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
#     --update-hashes is the opposite flag and would drift the agent hash.)
echo ">> autonomy packages sync --update-packages ..."
"$AUTONOMY" packages sync --update-packages

# 2. integrity gate: local fingerprints must match packages.json, else the
#    offline build would NOT reproduce the expected agent hash.
echo ">> autonomy packages lock --check ..."
"$AUTONOMY" packages lock --check

# 3. offline fetch of the agent + all transitive deps from the LOCAL packages
#    registry. --registry-path is mandatory: aea only searches cwd + one parent.
echo ">> aea fetch --local (offline, no IPFS) ..."
( cd "$BUILD_DIR" && "$AEA" --registry-path "$REPO/packages" fetch --local "$AGENT" --alias agent )
rm -rf "$BUILD_DIR/agent/.build"   # let the image rebuild it cleanly

# 4. custom Dockerfile: COPY the pre-fetched agent in (no `aea fetch`, no IPFS).
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

# 6. verify the stuck-agent fix landed (300s tolerance, commit 04be2510)
echo ">> verify BLOCKS_STALL_TOLERANCE == 300 ..."
docker run --rm --entrypoint bash "$IMAGE_TAG" -c \
  'grep -R "BLOCKS_STALL_TOLERANCE" /home/agent/vendor/valory/skills/abstract_round_abci/base.py' || true

echo
echo "## DONE. Image: $IMAGE_TAG"
echo "## Next (deploy): point your quickstart config \"hash\" at the matching"
echo "##   service CID for this agent: bafybeiguxxx4me5wahgpjhghevihaopiak5feg634zgrus3fxwocfz2sea"
echo "##   then:  bash run_service_cron.sh   (or ./run_service.sh configs/config_predict_trader.json)"
