# SPDX-License-Identifier: Apache-2.0
"""
The append-only canon guard (Lintel's rehearsal finding). Canon paths are permanent promises;
revision is supersede (a new entry + a metadata flip), never deletion. A proposal whose merge into
canon would REMOVE a canon path must be refused — otherwise a retract-near-canon (an instance drops
a path that has since become a canon entry) silently deletes committed work at land. Proven here:

  1. preview_merge types the diff (added / removed / modified), so a removal is visible, not hidden
     inside a flat changed_paths.
  2. prepare_merge REFUSES a canon-removing merge (CanonAppendOnly), naming the path — so console
     land and the airlock both fail closed before any candidate is referenced.
  3. a normal additive proposal still prepares fine (the guard doesn't over-fire).
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stasima.local_capstore import LocalCapStore, Identity, CanonAppendOnly
from stasima.entries import compose_entry

work = tempfile.mkdtemp(prefix="cap-appendonly-")
gd = os.path.join(work, "stasima.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
store = LocalCapStore(gd, approvers={"practitioner"})
e = lambda t, b: compose_entry({"type": t, "title": "x", "status": "active"}, b).encode()

# canon has two entries
store.bootstrap_canon({"practice/keep.md": e("kno", "keep me"),
                       "meta/log/1.md": e("log", "::1")}, "bootstrap")
canon = store.resolve_ref("refs/heads/main")

# a proposal branched from canon that ADDS one entry and REMOVES a canon path (the dangerous shape)
store.create_branch("refs/cap/proposals/p-bad", canon)
store.commit("refs/cap/proposals/p-bad", {"practice/new.md": e("kno", "new")},
             "add", Identity("inst"), expected_parent=canon, op_id="b1")
tip = store.resolve_ref("refs/cap/proposals/p-bad")
store.commit("refs/cap/proposals/p-bad", {"practice/keep.md": None},   # remove a CANON path
             "drop keep", Identity("inst"), expected_parent=tip, op_id="b2")

# 1. preview types the diff and shows the removal
s = store.preview_merge("refs/cap/proposals/p-bad")
assert s.removed == ["practice/keep.md"], s.removed
assert "practice/new.md" in s.added, s.added
print("1. preview types the diff: removes =", s.removed, "| adds =", s.added)

# 2. prepare REFUSES the canon-removing merge
refused = None
try:
    store.prepare_merge("refs/cap/proposals/p-bad")
    raise SystemExit("FAIL: prepare_merge allowed a canon-removing land")
except CanonAppendOnly as ex:
    refused = str(ex)
assert refused and "practice/keep.md" in refused, refused
print("2. prepare_merge refused the canon removal, naming the path")

# 3. an additive-only proposal still prepares fine (guard doesn't over-fire)
store.create_branch("refs/cap/proposals/p-good", canon)
store.commit("refs/cap/proposals/p-good", {"practice/extra.md": e("kno", "extra")},
             "add", Identity("inst"), expected_parent=canon, op_id="g1")
prep = store.prepare_merge("refs/cap/proposals/p-good")
assert prep.summary.removed == [] and "practice/extra.md" in prep.summary.added
print("3. additive proposal prepares fine: adds =", prep.summary.added, "removes =", prep.summary.removed)

print("\nOK -- append-only guard: removals visible in preview, refused at prepare, adds unaffected.")
