#!/bin/sh
# Build on a development machine with Rust, then transfer only the binaries.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/xiaojingyu-dump-build.XXXXXXXX")
mkdir -p "$build_dir/output"
git clone https://github.com/dmaivel/ntoseye.git "$build_dir/ntoseye"
git -C "$build_dir/ntoseye" checkout --detach d30a43c069231678b508a66ade94dabcedc2bbaa
git -C "$build_dir/ntoseye" apply "$repo_dir/integrations/dump-backend/ntoseye-hospital.patch"
(cd "$build_dir/ntoseye" && cargo build --release --locked --no-default-features --features cli -j 2)
cp "$build_dir/ntoseye/target/release/ntoseye" "$build_dir/output/ntoseye-hospital"
git clone https://github.com/vmi-rs/ephemera.git "$build_dir/ephemera"
git -C "$build_dir/ephemera" checkout --detach 066c860f1e6f19c0bb87435019d8a7542bf7cc07
(cd "$build_dir/ephemera" && cargo build --release --locked -j 2)
cp "$build_dir/ephemera/target/release/ephemera" "$build_dir/output/ephemera"
printf 'Binaries: %s\n' "$build_dir/output"
sha256sum "$build_dir/output/ntoseye-hospital" "$build_dir/output/ephemera"
