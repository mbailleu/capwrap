#!/usr/bin/env bash
# Install the AppArmor profile that lets a nix-installed bwrap create user
# namespaces on a host with kernel.apparmor_restrict_unprivileged_userns=1.
#
# Needs root.  Run once after installing bubblewrap via nix; the profile's
# attachment path is globbed, so nixpkgs updates do not require re-running it.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/capwrap-bwrap-nix.apparmor"
DEST=/etc/apparmor.d/capwrap-bwrap-nix

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root; re-running under sudo." >&2
    exec sudo -- "$0" "$@"
fi

install -m 0644 "$SRC" "$DEST"
echo "installed $DEST"

apparmor_parser --replace --write-cache "$DEST"
echo "profile loaded"

aa-status --json 2>/dev/null \
    | python3 -c 'import json,sys; p=json.load(sys.stdin)["profiles"]; print("\n".join(f"  {k}: {v}" for k,v in p.items() if "bwrap" in k))' \
    || aa-status | grep -i bwrap || true
