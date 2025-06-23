#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build_and_release.sh (self-contained)
# ---------------------------------------------------------------------------
# Builds (and optionally pushes) the `agentsystems/agent-control-plane` Docker
# image.  This is an in-repo copy of the shared release helper so the repo can
# be used standalone without cloning other codebases.
# ---------------------------------------------------------------------------
# Usage examples:
#   ./build_and_release.sh --version 0.3.0            # local build only
#   ./build_and_release.sh --version 0.3.0 --push     # build, push, tag
#   ./build_and_release.sh --help
# ---------------------------------------------------------------------------
set -euo pipefail

# Defaults
IMAGE="agentsystems/agent-control-plane"
VERSION="${VERSION:-}"
PUSH="false"
DOCKERFILE=""
CONTEXT="$(pwd)"
PLATFORM=""

function usage() {
  cat <<EOF
Usage: 
  $(basename "$0") [--version <ver>] [--push] [--image <name>] [--dockerfile <path>] [--context <dir>] [--platform <list>] [--help]

Options:
  --version      Tag to apply. Defaults to env VERSION or git describe.
  --push         Push image to registry *and* create matching Git tag.
  --image        Override default image name ($IMAGE).
  --dockerfile   Path to Dockerfile (default: Dockerfile in context dir).
  --context      Build context directory (default: current dir).
  --platform     Buildx platform list. Defaults to linux/amd64,linux/arm64 when pushing, host arch otherwise.
  --help         Show this help.
EOF
}

# -------- argument parsing ------------------------------------------------
while [[ $# -gt 0 ]]; do
  case $1 in
    --image) IMAGE="$2"; shift 2;;
    --version) VERSION="$2"; shift 2;;
    --push) PUSH="true"; shift;;
    --dockerfile) DOCKERFILE="$2"; shift 2;;
    --context) CONTEXT="$2"; shift 2;;
    --platform) PLATFORM="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

# -------- version helpers -------------------------------------------------
semver_regex='^v?[0-9]+\.[0-9]+\.[0-9]+([A-Za-z0-9.-]*)?$'

if [[ -z "$VERSION" ]]; then
  if [[ -n "${GITHUB_REF_NAME:-}" && "${GITHUB_REF:-}" == refs/tags/* ]]; then
    VERSION="$GITHUB_REF_NAME"
  elif git rev-parse --is-inside-work-tree &>/dev/null; then
    VERSION="$(git describe --tags --always)"
  else
    echo "Not a git repo and --version not supplied."; exit 1
  fi
fi

[[ "$VERSION" =~ $semver_regex ]] || { echo "Invalid version format: $VERSION"; exit 1; }

if [[ "$VERSION" == v* ]]; then
  GIT_TAG="$VERSION"
  DOCKER_VERSION="${VERSION#v}"
else
  GIT_TAG="v$VERSION"
  DOCKER_VERSION="$VERSION"
fi

CORE_VER="${DOCKER_VERSION%%-*}"

# Prevent duplicate or regressive versions
if git rev-parse "$GIT_TAG" &>/dev/null; then
  echo "Git tag $GIT_TAG already exists – aborting."; exit 1
fi
LATEST_CORE="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' | sed 's/^v//' | sort -V | tail -1 || true)"
if [[ -n "$LATEST_CORE" && "$DOCKER_VERSION" != *-* ]]; then
  if [[ "$(printf '%s\n%s' "$LATEST_CORE" "$CORE_VER" | sort -V | tail -1)" != "$CORE_VER" ]]; then
    echo "Version $CORE_VER must be greater than latest released $LATEST_CORE"; exit 1
  fi
fi

TAG_ARGS=( -t "$IMAGE:$DOCKER_VERSION" )
[[ "$DOCKER_VERSION" != *-* ]] && TAG_ARGS+=( -t "$IMAGE:latest" )

# Platforms
if [[ -z "$PLATFORM" ]]; then
  if [[ "$PUSH" == "true" ]]; then
    PLATFORM="linux/amd64,linux/arm64"
  else
    PLATFORM="$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"
  fi
fi

# Dockerfile default
[[ -z "$DOCKERFILE" ]] && DOCKERFILE="$CONTEXT/Dockerfile"
export DOCKER_BUILDKIT=${DOCKER_BUILDKIT:-1}

# -------- build -----------------------------------------------------------
BUILD_CMD=(docker buildx build "${TAG_ARGS[@]}" --platform "$PLATFORM" -f "$DOCKERFILE" "$CONTEXT")
[[ "$PUSH" == "true" ]] && BUILD_CMD+=(--push) || BUILD_CMD+=(--load)

echo "# ------------------------------------------------------------"
echo "# Building image : $IMAGE"
echo "# Git tag        : $GIT_TAG (created on push)"
echo "# Docker version : $DOCKER_VERSION"
echo "# Dockerfile     : $DOCKERFILE"
echo "# Context dir    : $CONTEXT"
echo "# Platforms      : $PLATFORM"
echo "# Push after build: $PUSH"
echo "# ------------------------------------------------------------"

"${BUILD_CMD[@]}"

echo "Image $IMAGE:$DOCKER_VERSION built successfully."
[[ "$PUSH" == "true" ]] && echo "Pushed to registry as $IMAGE:$DOCKER_VERSION"

# -------- git tagging -----------------------------------------------------
if [[ "$PUSH" == "true" ]]; then
  echo "Creating git tag $GIT_TAG and pushing to origin…"
  git tag -a "$GIT_TAG" -m "Release $GIT_TAG"
  git push origin "$GIT_TAG"
fi
