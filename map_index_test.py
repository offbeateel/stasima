# SPDX-License-Identifier: Apache-2.0
"""
Exercises the MAP index + indexer + IMP queries with the deterministic StubEmbedder
(offline, reproducible). Proves the schema, scope filtering, attribution, the Q4 raw
material, and the append-only IMP inbox/read-state — no server, no model, no git.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_index import SqliteMapIndex, StubEmbedder, index_entry

CANON = "refs/heads/main"
def persp(i): return f"refs/cap/perspectives/{i}"

emb = StubEmbedder(dim=64)
idx = SqliteMapIndex(":memory:")

def add(ref, path, is_canon, author, envelope, body):
    index_entry(idx, emb, ref=ref, path=path, is_canon=is_canon,
                authoring_instance=author, content_oid="oid:" + path, envelope=envelope, body=body)

# --- knowledge entries (canon + two perspectives) ---
add(CANON, "practice/no-silent-loss.md", True, "practitioner",
    {"type": "kno", "title": "No silent loss", "tags": ["durability"]},
    "Durability principle: the git substrate must never silently lose committed work or history.")
add(persp("research-2"), "practice/durability-notes.md", False, "research-2",
    {"type": "kno", "title": "Durability notes", "references": ["practice/no-silent-loss.md"]},
    "Notes on durability and never losing committed work; append-only history kept in git.")
add(persp("research-7"), "practice/scaling-notes.md", False, "research-7",
    {"type": "kno", "title": "Scaling notes"},
    "Thoughts on scaling throughput and performance under heavy concurrent request load.")

# --- a cartographic map entry about the canon durability entry ---
add(persp("research-2"), "maps/durability-map.md", False, "research-2",
    {"type": "map", "title": "Foundations region", "links": ["practice/no-silent-loss.md"],
     "region_labels": ["foundations"], "salience": 0.9},
    "I place the durability principle at the center of the foundations region.")

# --- an addressed message to multiple recipients ---
add(persp("research-2"), "messages/look-here.md", False, "research-2",
    {"type": "msg", "subject": "Durability is load-bearing", "recipients": ["research-7", "recto"],
     "coordinates": ["practice/no-silent-loss.md"]},
    "Please both look at the durability principle before proposing scaling changes.")

q = emb.embed(["how do we avoid losing committed work durability"])[0]

print("== search all (attributed) ==")
for h in idx.search(q, scope="all", limit=10):
    print(f"  {h.score:>6}  {h.authoring_instance:12} canon={h.is_canon!s:5} {h.path}")

print("== search canon ==      ", [h.path for h in idx.search(q, scope="canon")])
print("== search mine(r-7) ==  ", [h.path for h in idx.search(q, scope="mine", instance_id="research-7")])
print("== cartography_of no-silent-loss ==",
      [(r.authoring_instance, r.region_labels) for r in idx.cartography_of("practice/no-silent-loss.md")])
print("== inbox research-7 ==  ", [(r.authoring_instance, r.subject, r.links) for r in idx.inbox("research-7")])
print("== inbox recto ==       ", [r.path for r in idx.inbox("recto")])
print("== inbox research-2 (sender, not recipient) ==", [r.path for r in idx.inbox("research-2")])

# ---- assertions ----
allhits = idx.search(q, scope="all", limit=10)
paths = [h.path for h in allhits]
assert "practice/no-silent-loss.md" in paths, "canon durability entry should match"
assert "practice/durability-notes.md" in paths, "perspective durability entry should match"
assert "messages/look-here.md" not in paths, "messages must be excluded from universal search"
assert paths.index("practice/durability-notes.md") < paths.index("practice/scaling-notes.md"), \
    "durability must outrank scaling for a durability query"
assert [h.path for h in idx.search(q, scope="canon")] == ["practice/no-silent-loss.md"], "canon scope"
assert all(h.authoring_instance == "research-7" for h in idx.search(q, scope="mine", instance_id="research-7")), "mine scope"
assert [r.authoring_instance for r in idx.cartography_of("practice/no-silent-loss.md")] == ["research-2"], "Q4 material"
assert idx.inbox("research-2") == [], "sender is not a recipient"
assert [r.path for r in idx.inbox("research-7")] == ["messages/look-here.md"], "recipient is addressed"
assert len(idx.inbox("recto")) == 1, "other recipient also addressed"
print("\nOK -- schema, scope, attribution, Q4 material, and inbox addressing verified (read-state in the audit log).")
