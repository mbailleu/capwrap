#!/usr/bin/env bash
# Build the playground that examples/agents/*.toml point at:
#
#   ~/capwrap-demo/repo   a small git repo, for mode="worktree"
#   ~/capwrap-demo/db     a shared "database", for mode="overlay"
#   ~/capwrap-demo/ref    reference material, for mode="ro"
#
# Safe to re-run; it recreates the tree from scratch.
set -euo pipefail

DEMO="${CAPWRAP_DEMO:-$HOME/capwrap-demo}"
rm -rf "$DEMO"
mkdir -p "$DEMO"/{repo,db,ref}

# --- a git repo for worktree mode -------------------------------------
cd "$DEMO/repo"
git init --quiet --initial-branch=main
git config user.email agent@capwrap.local
git config user.name "capwrap demo"
cat > README.md <<'EOF'
# demo project

Two agents work on this repo at once. Each gets its own branch and its own
checkout, so neither can see or clobber the other's work in progress.
EOF
mkdir -p src
cat > src/app.py <<'EOF'
def greet(name: str) -> str:
    return f"hello, {name}"
EOF
git add -A
git commit --quiet -m "initial commit"

# --- a shared mutable store for overlay mode --------------------------
cd "$DEMO/db"
cat > schema.sql <<'EOF'
CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);
EOF
echo "seed row" > rows.txt

# --- read-only reference material -------------------------------------
echo "House style: tabs are forbidden." > "$DEMO/ref/STYLE.md"

echo "demo playground ready at $DEMO"
echo
echo "  try:  capwrap run examples/agents/dev-a.toml --dry-run"
echo "        capwrap run examples/agents/dev-a.toml"
