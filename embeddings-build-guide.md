# Build Guide — Wiring real embeddings (`LocalServerEmbedder`)

> **Status: COMPLETED 2026-06-11** (Ollama + nomic-embed-text, on the original machine — embedding
> models turned out to be light enough to not need the move; `ollama pull nomic-embed-text` is 274MB,
> CPU-fine). Verified by `embeddings_smoke.py`. **The lesson the original guide missed:** nomic-style
> retrieval models are *prefix-conditioned* — without `search_document: `/`search_query: ` task
> prefixes, ranking was *worse than the stub* (related scored below unrelated, live-verified).
> `LocalServerEmbedder` now takes `doc_prefix`/`query_prefix` (config: `embed_doc_prefix`/
> `embed_query_prefix`, defaulted for nomic), and the `Embedder` ABC gained `embed_query()` so the
> search side embeds differently from the indexing side. Kept below for re-running on another
> machine or swapping models — read with the prefix lesson in mind.

*A focused task brief for the instance picking this up. You are likely in a fresh session with no memory of the build; this is self-contained. Read it once, do the task, verify against the acceptance test, return.*

---

## The task in one sentence

Connect the already-written `LocalServerEmbedder` to a running local embedding model and confirm that semantic search works — i.e., `map_search` returns entries that are *related in meaning*, not just sharing words.

## Why this is the one piece left

Everything else in the Stasima is built and tested. Search runs today on `StubEmbedder` — a deterministic bag-of-hashed-tokens stand-in that's really *lexical* similarity. It's fine for exercising the plumbing but can't tell "durability" relates to "never losing committed work" unless the words overlap. Real embeddings fix that. The component to drive them (`LocalServerEmbedder`) exists and is wired through config; it has simply never been run against a live model server (that needed a capable machine — presumably the one you're on now).

## What already exists (don't rebuild it)

- **`LocalServerEmbedder`** in `map_index.py` — calls an OpenAI-compatible `POST /v1/embeddings` (LM Studio, Ollama, etc.), returns one vector per input, **normalized** (see "the normalization contract" below). Has `model_id` and `dim` attributes.
- **The `Embedder` ABC** — `embed(texts: list[str]) -> list[list[float]]`. The index keys ranking off `cosine()`, which is a **dot product**, so embedders must return **unit-length vectors**.
- **Config wiring** — `stasima.toml` has `embed_backend` (`"stub"` | `"local-server"`), `embed_url`, `embed_model`, `embed_dim`. `cap_server.components_from_config` already builds `LocalServerEmbedder` when `embed_backend = "local-server"`. You shouldn't need to touch the wiring — only the config and (maybe) the embedder's request/response handling if your server differs.

## Steps

1. **Stand up a server with an embedding model.**
   - *Ollama:* `ollama pull nomic-embed-text`, then it serves `http://localhost:11434/v1/embeddings` (OpenAI-compatible). Model name: `nomic-embed-text`, dim **768**.
   - *LM Studio:* download an embedding model, load it in the local-server tab, start the server (default `http://localhost:1234`). Note the model name and its output dim.
   - Whatever you use, write down three things: **base URL, model name, output dim.**

2. **Smoke-test the embedder directly**, before touching config. Something like:
   ```python
   from map_index import LocalServerEmbedder, cosine
   e = LocalServerEmbedder("http://localhost:11434", "nomic-embed-text", 768)
   v = e.embed(["durability and append-only history", "never losing committed work", "scaling throughput"])
   assert len(v) == 3 and len(v[0]) == 768
   print("related:",   round(cosine(v[0], v[1]), 3))   # expect HIGH (few shared words, related meaning)
   print("unrelated:", round(cosine(v[0], v[2]), 3))   # expect LOW
   assert cosine(v[0], v[1]) > cosine(v[0], v[2])       # THE test the stub can't pass
   ```
   If that last assertion holds, the model is producing real semantics and the wire format matches.

3. **Point the config at it** (`stasima.toml`):
   ```toml
   embed_backend = "local-server"
   embed_url     = "http://localhost:11434"
   embed_model   = "nomic-embed-text"
   embed_dim     = 768          # MUST equal the model's actual output dim
   ```

4. **Re-embed the corpus:** `python admin.py --config stasima.toml reindex`. This rebuilds the index from git using the real model.

5. **Acceptance:** run `map_search` with a query that's *semantically* (not lexically) related to an entry and confirm it ranks the entry highly. The clearest demonstration is a query that shares **no words** with the target entry but means the same thing — the stub ranks that near zero; a real model ranks it near the top.

## Gotchas (this is the real content)

1. **The normalization contract.** `cosine(a,b)` in `map_index.py` is `sum(x*y …)` — a dot product, correct *only* for unit vectors. `LocalServerEmbedder.embed()` now normalizes its outputs (a recent fix), so this is handled. Don't remove it. If you add a new embedder, it must return normalized vectors too. (If your search results look magnitude-skewed or weird, suspect normalization first.)
2. **Dim consistency is absolute.** `embed_dim` must equal the model's real output dim, and the **entire index must use one model** — `cosine` zips two vectors, so mixed dims silently truncate and produce garbage. Swapping models is always a **full `reindex`**. The per-row `model_id` lets you detect a mixed-model index; if you ever see inconsistent `model_id`s, reindex.
3. **Use the `/v1/embeddings` (OpenAI-compatible) path.** Both LM Studio and Ollama expose it, returning `{"data":[{"index":i,"embedding":[…]}, …]}` — which is exactly what the code parses. Ollama *also* has a native `/api/embeddings` with a different shape (single `prompt`, `{"embedding":…}`) — **don't** use that one; the current code expects the `/v1` shape.
4. **Cold start.** The model must be loaded/pulled before the first call. The first request may be slow while the server loads the model into memory; the 60s timeout covers it, but don't mistake a slow first call for a hang.
5. **Batch and token limits (matters only as the corpus grows).** `embed()` sends all `texts` in one request. For a large reindex, the server may cap array size or per-text tokens. If you hit limits, batch the inputs and/or truncate very long entry bodies to the model's max tokens. The current corpus is small enough that this won't bite yet — note it for later.
6. **Auth.** `api_key` defaults to `"not-needed"`; LM Studio/Ollama ignore it. A *hosted* OpenAI-compatible endpoint would need a real key — there's no config field for it yet, so add one (`embed_api_key`) if you go that route.

## Files you'll touch or read

- `map_index.py` — `Embedder`, `LocalServerEmbedder`, `cosine`, `_normalize`, `index_entry`.
- `config.py` — the `embed_*` fields.
- `cap_server.py` — `components_from_config` (the wiring; likely read-only for you).
- `OPERATIONS.md` (Embeddings section) — confirm it stays accurate after you're done.

## Done when

- The step-2 smoke test passes (semantic > lexical) against your live server.
- `map_search` on the real index ranks a meaning-related, word-disjoint query highly.
- The existing test suite still passes (`for t in *_test.py; do python $t; done`) — those use `StubEmbedder`, so they should be unaffected; if any break, you changed something you shouldn't have.
- `OPERATIONS.md`'s embeddings instructions match what you actually did.

---

*If you change the request/response handling for a server whose API differs, keep `StubEmbedder` and the `Embedder` contract intact — the whole test suite and the offline-dev path depend on them. The same brief format works for the other deferred components (GitHub backend, identity binding, the IMP social layer); this one is the smallest and the highest-leverage, since it turns search from lexical into semantic.*
