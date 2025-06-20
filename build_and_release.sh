#!/usr/bin/env bash
# Wrapper script that delegates to the shared release_common.sh helper.
# Usage examples:
#   ./build_and_release.sh                       # local build (no push)
#   ./build_and_release.sh --version 0.2.0 --push  # build & push multi-arch image

DIR="$(cd "$(dirname "$0")" && pwd)"
# Path to the shared script (sibling repo)
"$DIR/../agentsystems-build-tools/release_common.sh" \
  --image agentsystems/agent-control-plane "$@"
