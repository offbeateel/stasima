#!/usr/bin/env bash
#
# CAPstore plumbing spike
# -----------------------
# Proves the `git` plumbing command sequence behind the LocalCapStore primitives,
# against a fresh BARE repo with NO working tree. Nothing here is CAPstore code;
# it's the end-to-end proof that the integration approach works before we build it.
#
# Primitives demonstrated:
#   commit(path -> bytes)        : hash-object -> temp-index -> write-tree -> commit-tree -> update-ref
#   compare-and-swap (StaleRef)  : update-ref <ref> <new> <old>   (atomic, native)
#   append-only (NonFastForward) : merge-base --is-ancestor       (the check CAPstore enforces)
#   prepare_merge / land_merge   : merge-tree --write-tree -> commit-tree(unreferenced) -> update-ref
#   MergeConflict                : merge-tree --write-tree exit code
#   provenance read-back         : ls-tree / show
#
# Requires Git >= 2.38 (for `merge-tree --write-tree`). Tested on 2.54.

set -euo pipefail

ZERO=0000000000000000000000000000000000000000     # sha1 "ref must not exist" sentinel
PASS=0
pass() { echo "    PASS  $*"; PASS=$((PASS+1)); }
fail() { echo "    FAIL  $*"; exit 1; }
banner() { echo; echo "=== $* ==="; }

# --- isolated bare repo, no checkout ----------------------------------------
ROOT="$(mktemp -d)"
export GIT_DIR="$ROOT/stasima.git"
git init --bare -q "$GIT_DIR"
echo "bare repo: $GIT_DIR"

# committer identity is the server ("capstore"); author is overridden per-op
export GIT_COMMITTER_NAME="capstore"   GIT_COMMITTER_EMAIL="capstore@stasima.local"
export GIT_AUTHOR_NAME="capstore"      GIT_AUTHOR_EMAIL="capstore@stasima.local"
export GIT_COMMITTER_DATE="2026-01-01T00:00:00 +0000"
export GIT_AUTHOR_DATE="2026-01-01T00:00:00 +0000"

# write a blob at PATH built on optional BASE tree-ish, echo the new tree oid
# usage: build_tree <base|""> <path> <content>
build_tree() {
  local base="$1" path="$2" content="$3" idx blob tree
  idx="$(mktemp)"; rm -f "$idx"             # git wants to create the index itself, not adopt a 0-byte file
  export GIT_INDEX_FILE="$idx"
  if [ -n "$base" ]; then git read-tree "$base"; else git read-tree --empty; fi
  blob="$(printf '%s' "$content" | git hash-object -w --stdin)"
  git update-index --add --cacheinfo "100644,$blob,$path"
  tree="$(git write-tree)"
  unset GIT_INDEX_FILE; rm -f "$idx"
  printf '%s' "$tree"
}

# ============================================================================
banner "STEP 0  empty repo has no refs"
[ -z "$(git for-each-ref)" ] && pass "no refs in a fresh bare repo" || fail "unexpected refs"

# ============================================================================
banner "STEP 1  bootstrap canon (main) — initial corpus commit"
t="$(build_tree "" canon/orientation.md $'Welcome to the Stasima.\n')"
c0="$(git commit-tree "$t" -m $'Bootstrap canon\n\nop-id: boot-0001')"
git update-ref refs/heads/main "$c0" "$ZERO"          # create-only (old = zero)
[ "$(git rev-parse refs/heads/main)" = "$c0" ] && pass "main created at $c0" || fail "main not set"

# ============================================================================
banner "STEP 2  perspective write — append-only branch per instance"
# first append: orphan commit (no parent), CAS create
t="$(build_tree "" instances/research-2/entries/0001.md $'Entry 1 by research-2.\n')"
p1="$(GIT_AUTHOR_NAME=research-2 GIT_AUTHOR_EMAIL=r2@stasima.local \
      git commit-tree "$t" -m $'KIP 0001\n\nop-id: op-aaa\ninstance-id: research-2')"
git update-ref refs/concordance/perspectives/research-2 "$p1" "$ZERO"
pass "perspective created at $p1"

# second append, based on the previous tip's tree, CAS expects old = p1
t="$(build_tree "$p1" instances/research-2/entries/0002.md $'Entry 2 by research-2.\n')"
p2="$(GIT_AUTHOR_NAME=research-2 GIT_AUTHOR_EMAIL=r2@stasima.local \
      git commit-tree "$t" -p "$p1" -m $'KIP 0002\n\nop-id: op-bbb\ninstance-id: research-2')"
git update-ref refs/concordance/perspectives/research-2 "$p2" "$p1"
pass "perspective advanced $p1 -> $p2 (CAS old=p1 matched)"

# ============================================================================
banner "STEP 3  compare-and-swap rejects a stale write (StaleRef)"
# tip is now p2; try to update with old=p1 (what a stale caller would send)
set +e
err="$(git update-ref refs/concordance/perspectives/research-2 "$p1" "$p1" 2>&1)"; rc=$?
set -e
[ $rc -ne 0 ] && pass "stale CAS rejected, exit=$rc  (\"${err##*: }\")" || fail "stale CAS should have failed"
[ "$(git rev-parse refs/concordance/perspectives/research-2)" = "$p2" ] && pass "ref unchanged after rejected CAS" || fail "ref moved on stale CAS"

# ============================================================================
banner "STEP 4  append-only check (NonFastForward) via merge-base"
git merge-base --is-ancestor "$p1" "$p2" && pass "p2 is fast-forward over p1 (append allowed)"
# a sideways commit (not a descendant of the tip) is what append_only must reject
side="$(GIT_AUTHOR_NAME=research-2 git commit-tree "$(git rev-parse "$p1^{tree}")" -p "$p1" -m 'sideways')"
if git merge-base --is-ancestor "$p2" "$side"; then fail "side should not be FF"; else
  pass "non-fast-forward update would be rejected (server enforces, git allows --force)"; fi

# ============================================================================
banner "STEP 5  proposal to canon + prepare_merge (durable, UNREFERENCED candidate)"
git update-ref refs/concordance/proposals/p-001 "$c0" "$ZERO"           # branch proposal off main
t="$(build_tree "$c0" canon/entries/principle-1.md $'Principle 1: never silently lose work.\n')"
pr="$(GIT_AUTHOR_NAME=research-2 GIT_AUTHOR_EMAIL=r2@stasima.local \
      git commit-tree "$t" -p "$c0" -m $'Propose principle-1\n\nop-id: op-ccc\ninstance-id: research-2')"
git update-ref refs/concordance/proposals/p-001 "$pr" "$c0"

mtree="$(git merge-tree --write-tree refs/heads/main refs/concordance/proposals/p-001)"   # clean -> tree oid
cand="$(git commit-tree "$mtree" -p "$c0" -p "$pr" -m $'Merge p-001 into canon\n\napproved-by: <pending>')"
[ "$(git cat-file -t "$cand")" = commit ] && pass "merge candidate $cand created and durable"
# crucial: NO ref points at it yet
if git for-each-ref --format='%(objectname)' | grep -q "$cand"; then
  fail "candidate is referenced — should be unreferenced before land_merge"; else
  pass "candidate is unreferenced (prepared, not landed)"; fi

# ============================================================================
banner "STEP 6  land_merge (human gate) — CAS-advance main to the candidate"
# in real CAPstore this runs ONLY after a verified practitioner Approval
git update-ref refs/heads/main "$cand" "$c0"                # CAS: main must still be at c0
[ "$(git rev-parse refs/heads/main)" = "$cand" ] && pass "main landed at merge candidate"
git ls-tree -r --name-only refs/heads/main | grep -q principle-1.md \
  && pass "proposed entry is present in canon after merge" || fail "merge content missing"

# ============================================================================
banner "STEP 7  MergeConflict detection (two edits to one path)"
tx="$(build_tree "$c0" canon/orientation.md $'X says hello.\n')"; cx="$(git commit-tree "$tx" -p "$c0" -m edit-x)"
ty="$(build_tree "$c0" canon/orientation.md $'Y says goodbye.\n')"; cy="$(git commit-tree "$ty" -p "$c0" -m edit-y)"
set +e
out="$(git merge-tree --write-tree "$cx" "$cy" 2>&1)"; rc=$?
set -e
[ $rc -ne 0 ] && pass "conflict detected, exit=$rc  (maps to MergeConflict)" || fail "expected a conflict"

# ============================================================================
banner "STEP 8  read-back: provenance + canon contents"
echo "  canon (main) tree:"
git ls-tree -r --name-only refs/heads/main | sed 's/^/      /'
echo "  perspective commit provenance (author = authoring instance):"
git show -s --format='      author=%an  subject=%s' "$p2"
echo "      trailers:"; git show -s --format='%b' "$p2" | sed 's/^/        /'
echo "  all refs:"
git for-each-ref --format='      %(refname)  ->  %(objectname:short)'

# ============================================================================
banner "RESULT"
echo "  $PASS checks passed."
echo "  repo left at: $GIT_DIR  (delete the parent temp dir to clean up)"
