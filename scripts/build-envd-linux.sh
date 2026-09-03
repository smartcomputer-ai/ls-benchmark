#!/usr/bin/env bash
# Build lightspeed-envd for a Linux sandbox architecture from the sibling
# checkout, inside the digest-pinned Rust image the Lightspeed release uses.
# The release pipeline publishes x86_64 only, as a static musl binary; this
# produces that same musl binary from a local commit, or the aarch64 (glibc)
# binary an arm64 Docker daemon (Apple silicon) needs for local development.
#
# Usage: scripts/build-envd-linux.sh [arm64|amd64]   (default: the host arch)
# Output: .local/envd/<target>/lightspeed-envd and its .sha256
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

ARCH="${1:-$(uname -m)}"
case "$ARCH" in
  arm64|aarch64) ARCH=arm64; TARGET=aarch64-unknown-linux-gnu ;;
  amd64|x86_64) ARCH=amd64; TARGET=x86_64-unknown-linux-musl ;;
  *) echo "unknown architecture: $ARCH" >&2; exit 2 ;;
esac

LS="${LIGHTSPEED_CHECKOUT:-../lightspeed}"
[ -f "$LS/Cargo.toml" ] || { echo "sibling checkout not found at $LS" >&2; exit 1; }
LS="$(cd "$LS" && pwd)"
IMAGE="$(sed -n 's/^LIGHTSPEED_RELEASE_BUILD_BASE_IMAGE=//p' "$LS/release/metadata.env")"
[ -n "$IMAGE" ] || { echo "LIGHTSPEED_RELEASE_BUILD_BASE_IMAGE missing from $LS/release/metadata.env" >&2; exit 1; }

OUT=".local/envd/$TARGET"
mkdir -p "$OUT"
GIT_SHA="$(git -C "$LS" rev-parse HEAD)"

echo "building lightspeed-envd for $TARGET from $LS@${GIT_SHA:0:12} in $IMAGE" >&2
docker run --rm --platform "linux/$ARCH" \
  -e CARGO_TARGET_DIR=/target \
  -e LIGHTSPEED_GIT_SHA="$GIT_SHA" \
  -e TARGET="$TARGET" \
  -v "ls-benchmark-cargo-registry-$ARCH:/usr/local/cargo/registry" \
  -v "ls-benchmark-envd-target-$ARCH:/target" \
  -v "$LS:/workspace" \
  -v "$(pwd)/$OUT:/out" \
  -w /workspace \
  "$IMAGE" \
  bash -c '
    set -euo pipefail
    apt-get update >/dev/null
    apt-get install -y --no-install-recommends \
      clang cmake git libprotobuf-dev libssl-dev musl-tools pkg-config protobuf-compiler >/dev/null
    git config --global --add safe.directory /workspace
    if [[ "$TARGET" == *-linux-musl ]]; then
      # Same static target as the release: aws-lc-rs compiles with musl-gcc.
      rustup target add "$TARGET"
      cargo build --release --locked --target "$TARGET" -p environment-daemon
      install -m 0755 "/target/$TARGET/release/lightspeed-envd" /out/lightspeed-envd
    else
      cargo build --release --locked -p environment-daemon
      install -m 0755 /target/release/lightspeed-envd /out/lightspeed-envd
    fi
  '
(cd "$OUT" && shasum -a 256 lightspeed-envd | tee lightspeed-envd.sha256 >&2)
if [ -n "$(git -C "$LS" status --porcelain -- crates)" ]; then
  echo "$GIT_SHA-dirty" > "$OUT/lightspeed-envd.gitsha"
else
  echo "$GIT_SHA" > "$OUT/lightspeed-envd.gitsha"
fi
echo "export LIGHTSPEED_HARBOR_ENVD_PATH=$(pwd)/$OUT/lightspeed-envd"
