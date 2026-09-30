#!/bin/sh
# Build rosetta3 for native execution only, without Microlift.
#
#   rosetta3-native/build.sh ROSETTA3_CHECKOUT [OUTPUT_DIR]
#
# rosetta3 depends on Microlift, its x86-64 recompiler, which is a private Git
# repository. Native execution (a Mach-O slice for this machine's CPU, such as
# the x86_64 Roblox build on an x86-64 PC) does not use it, so this script
# builds rosetta3 against microlift-stub/, a stand-in with the same API.
#
# The checkout is not changed: its committed tree (HEAD) is exported to
# OUTPUT_DIR/tree, where a Cargo [patch] swaps Microlift for the stand-in. The
# binary is copied to OUTPUT_DIR/rosetta3. OUTPUT_DIR defaults to
# ${XDG_CACHE_HOME:-~/.cache}/rostrap/rosetta3-native.
set -eu

usage() {
    echo "usage: $0 ROSETTA3_CHECKOUT [OUTPUT_DIR]" >&2
    exit 2
}

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
    usage
fi
checkout=$(cd "$1" && pwd)
here=$(cd "$(dirname "$0")" && pwd)
out=${2:-"${XDG_CACHE_HOME:-$HOME/.cache}/rostrap/rosetta3-native"}

if [ ! -f "$checkout/crates/rosetta3/Cargo.toml" ]; then
    echo "$0: $checkout is not a rosetta3 checkout" >&2
    exit 1
fi
if ! git -C "$checkout" rev-parse --verify --quiet HEAD >/dev/null; then
    echo "$0: $checkout has no commit to build" >&2
    exit 1
fi

# rosetta3's README asks for Rust 1.96 or newer.
version=$(rustc --version | awk '{print $2}')
major=${version%%.*}
minor=${version#*.}
minor=${minor%%.*}
if [ "$major" -lt 1 ] || { [ "$major" -eq 1 ] && [ "$minor" -lt 96 ]; }; then
    echo "$0: rosetta3 needs Rust 1.96 or newer; this is $version (try: rustup update stable)" >&2
    exit 1
fi

mkdir -p "$out"
out=$(cd "$out" && pwd)
tree="$out/tree"
marker="$tree/.rostrap-rosetta3-tree"
# The tree is replaced on every build, and only ever one this script made.
if [ -e "$tree" ]; then
    if [ ! -f "$marker" ]; then
        echo "$0: $tree exists and was not made by this script; move it away first" >&2
        exit 1
    fi
    rm -rf "$tree"
fi
mkdir "$tree"
touch "$marker"
git -C "$checkout" archive --format=tar HEAD | tar -x -C "$tree"

stub="$here/microlift-stub"
case "$stub" in
    *\"* | *\\*)
        echo "$0: the path $stub cannot be written into Cargo's configuration" >&2
        exit 1
        ;;
esac
mkdir -p "$tree/.cargo"
cat > "$tree/.cargo/config.toml" <<EOF
[patch."https://github.com/microlift/microlift"]
microlift = { path = "$stub" }
EOF

echo "building rosetta3 $(git -C "$checkout" rev-parse --short HEAD) from $checkout (native execution only)"
# Cargo reads .cargo/config.toml from the directory it runs in.
(cd "$tree" && CARGO_TARGET_DIR="$out/target" cargo build --release --bin rosetta3)
cp "$out/target/release/rosetta3" "$out/rosetta3"
echo
echo "built $out/rosetta3"
echo "Choose it in rostrap's settings (rosetta3 page, rosetta3 program)."
