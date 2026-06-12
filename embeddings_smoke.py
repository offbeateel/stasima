# SPDX-License-Identifier: Apache-2.0
"""
Live-embeddings acceptance (NOT part of run_tests.py — needs a running model server).

    python embeddings_smoke.py [base_url] [model] [dim]     defaults: http://localhost:11434 nomic-embed-text 768

Proves the two things the stub cannot:
  1. semantic > lexical — meaning-related texts outscore word-overlapping ones;
  2. a query sharing NO content words with its target still ranks that target first,
     side-by-side with the stub failing the same query.
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_capstore import LocalCapStore
from map_index import SqliteMapIndex, StubEmbedder, LocalServerEmbedder, cosine
from entries import compose_entry
from canon import reindex_from_git

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:11434"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "nomic-embed-text"
DIM = int(sys.argv[3]) if len(sys.argv) > 3 else 768

emb = LocalServerEmbedder(BASE, MODEL, DIM,
                          doc_prefix="search_document: ", query_prefix="search_query: ")

# ---- 1. raw semantics: related-in-meaning must beat word-overlap ----
v = emb.embed(["durability and append-only history",
               "never losing committed work",
               "scaling throughput under concurrent load"])
assert len(v) == 3 and len(v[0]) == DIM, f"expected {DIM}-dim vectors, got {len(v[0])}"
rel, unrel = cosine(v[0], v[1]), cosine(v[0], v[2])
print(f"semantic check      related={rel:.3f}  unrelated={unrel:.3f}  ->  {'OK' if rel > unrel else 'FAIL'}")
assert rel > unrel, "the model is not producing useful semantics"

# ---- 2. ranking over a corpus, with a word-disjoint query ----
work = tempfile.mkdtemp(prefix="embed-smoke-")
gd = os.path.join(work, "c.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
store = LocalCapStore(gd, approvers={"practitioner"})
e = lambda title, body: compose_entry({"type": "kno", "title": title, "status": "active"}, body).encode()
store.bootstrap_canon({
    "practice/no-silent-loss.md": e("No silent loss",
        "Durability principle: the substrate must never silently lose committed work. "
        "History is append-only; an entry, once written, is never overwritten."),
    "practice/scaling.md": e("Scaling notes",
        "Throughput and performance under concurrent request load; batching and parallelism."),
    "technical/airlock.md": e("Airlock approval",
        "Two TOTP codes gate remote landing: stage, review floor, then a fresh code to land."),
    "argot/bust.md": e("Bust-accumulation",
        "Fluent output that reads as authoritative but tracks nothing; agreement around noise mistaken for signal."),
}, "smoke corpus")

# the query shares essentially no content words with the durability entry
QUERY = "how do we keep saved material from vanishing after the fact"

results = {}
for name, em in (("stub", StubEmbedder(dim=64)), (MODEL, emb)):
    idx = SqliteMapIndex(":memory:")
    reindex_from_git(store, idx, em)
    hits = idx.search(em.embed_query([QUERY])[0], scope="all", limit=4)
    results[name] = hits
    print(f"\nranking via {name}:  query = {QUERY!r}")
    for h in hits:
        print(f"   {h.score:>7.4f}  {h.path}")

top = results[MODEL][0]
assert top.path == "practice/no-silent-loss.md", f"expected the durability entry first, got {top.path}"
stub_top = results["stub"][0].path if results["stub"] else None
print(f"\nword-disjoint query ->  {MODEL}: ranks durability FIRST  |  stub: top was {stub_top!r}")
print("OK -- live embeddings produce real semantic ranking; the index is one `admin reindex` away.")
