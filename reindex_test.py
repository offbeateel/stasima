# SPDX-License-Identifier: Apache-2.0
"""
Proves reindex_from_git: build a corpus directly in git WITHOUT touching the index, then rebuild
the whole index from the repo. Covers the two gaps it closes — canon-indexing-after-landing and
index recovery — plus message reindex, provenance-through-promotion, and idempotency.
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stasima.local_capstore import LocalCapStore, Identity, Approval
from stasima.map_index import SqliteMapIndex, StubEmbedder
from stasima.cap_server import compose_entry, reindex_from_git

CANON = "refs/heads/main"
def persp(i): return f"refs/cap/perspectives/{i}"
def prop(p): return f"refs/cap/proposals/{p}"

work = tempfile.mkdtemp(prefix="cap-reindex-")
gd = os.path.join(work, "stasima.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
store = LocalCapStore(gd, approvers={"practitioner"})
index, emb = SqliteMapIndex(":memory:"), StubEmbedder(dim=64)


def author(ref, path, envelope, body, who, op_id):
    return store.commit(ref, {path: compose_entry(envelope, body).encode()}, f"KIP {path}",
                        Identity(who), expected_parent=store.resolve_ref(ref), op_id=op_id)


# --- build a corpus directly in git; the index stays empty ---
store.bootstrap_canon({"practice/no-silent-loss.md": compose_entry(
    {"type": "kno", "title": "No silent loss", "status": "active", "tags": ["durability"]},
    "Durability: never silently lose committed work or history.").encode()}, "Bootstrap canon")
author(persp("research-2"), "practice/durability-notes.md",
       {"type": "kno", "title": "Durability notes", "status": "active", "references": ["practice/no-silent-loss.md"]},
       "Notes on durability and never losing committed work; append-only git history.", "research-2", "op-1")
author(persp("research-7"), "practice/scaling-notes.md",
       {"type": "kno", "title": "Scaling notes", "status": "active"},
       "Scaling throughput and performance under concurrent load.", "research-7", "op-2")
author(persp("research-2"), "messages/m-1.md",
       {"type": "msg", "subject": "durability is load-bearing", "status": "active",
        "recipients": ["research-7"], "coordinates": ["practice/no-silent-loss.md"]},
       "Look here before scaling.", "research-2", "op-3")

# a real promotion to canon: proposal -> prepare -> land
store.create_branch(prop("p-1"), store.resolve_ref(CANON))
author(prop("p-1"), "practice/principle-durability.md",
       {"type": "kno", "title": "Durability principle", "status": "active"},
       "Durability is a stated principle of the practice.", "research-2", "op-4")
prep = store.prepare_merge(prop("p-1"))
store.land_merge(prep, Approval(prep.candidate_oid, "practitioner", "local-confirm"))

# index is empty until we rebuild
assert index.search(emb.embed(["durability"])[0], scope="all") == [], "index empty before reindex"

n1 = reindex_from_git(store, index, emb)
print("reindexed entries:", n1)

q = emb.embed(["avoid losing committed work durability"])[0]
allhits = index.search(q, scope="all", limit=20)
print("search all after reindex:")
for h in allhits:
    print(f"   {h.score:>6}  {h.authoring_instance:12} canon={h.is_canon!s:5} {h.path}")
paths = [h.path for h in allhits]
canon = [h.path for h in index.search(q, scope="canon", limit=20)]
inbox = index.inbox("research-7")
princ = [h for h in allhits if h.path == "practice/principle-durability.md"][0]
print("canon scope:", canon)
print("inbox research-7:", [(r.authoring_instance, r.subject) for r in inbox])
print("promoted entry author (provenance through promotion):", princ.authoring_instance)

n2 = reindex_from_git(store, index, emb)
allhits2 = index.search(q, scope="all", limit=20)
print("reindex again -> count:", n2, "| rows stable:", len(allhits2) == len(allhits))

# ---- assertions ----
assert n1 == 5, f"expected 5 indexed (2 canon + 2 r-2 + 1 r-7), got {n1}"
assert {"practice/no-silent-loss.md", "practice/durability-notes.md", "practice/principle-durability.md"} <= set(paths)
assert "messages/m-1.md" not in paths, "messages excluded from universal search"
assert "practice/principle-durability.md" in canon, "canon-after-landing is now indexed"
assert "practice/no-silent-loss.md" in canon
assert len(inbox) == 1 and inbox[0].authoring_instance == "research-2", "message recovered for recipient"
assert princ.is_canon and princ.authoring_instance == "research-2", "provenance survived promotion"
assert n2 == n1 and len(allhits2) == len(allhits), "reindex is idempotent (no duplicate rows)"
print("\nOK -- index rebuilt from git: canon (incl. landed), perspectives, and messages all recovered.")
