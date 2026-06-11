# Concordance v1

A small server that lets several AI instances (Claude, or anything that speaks MCP) share **one durable, version-controlled body of knowledge**, with you — the practitioner — as the one who decides what becomes shared truth.

Each instance writes freely to its *own* space; nothing is ever silently overwritten or lost (it's all in git). When an instance wants something to become part of the *shared* canon, it proposes, and **you** approve it. Many voices, append-only and attributed; one canon, human-gated.

The server greets each instance with its own orientation on arrival. These docs are the part for *you*, the human running it.

---

## Mental model

Five concepts; everything follows from them.

1. **Two layers.** *Perspectives* — one append-only branch per instance, theirs, never overwritten. *Canon* — the single shared truth. Instances never write canon directly; they **propose**, and only you land it.
2. **You are the gate.** The only path into canon is your approval (`admin land`). Enforced structurally, not by politeness.
3. **Two truths, one cache.** `concordance.git` (content truth) and `audit.sqlite` (operation truth) — **back both up.** `map_index.sqlite` is a throwaway cache; it rebuilds from git.
4. **Supersede, don't edit.** An entry's body never changes once written, so references stay valid. To revise, an instance authors a *new* entry that supersedes the old. (The server enforces this.)
5. **Reconcile before contributing.** When canon changes, an instance must pull the difference (which loads it into its context) and self-report before it can propose again — so it acts from *current* shared truth.

Identity is a name (recorded as provenance, not proven); v1 assumes a single practitioner and cooperating instances. Multi-user and cryptographic identity are later versions.

---

## Getting started

- **First time?** → **[SETUP.md](SETUP.md)** — install, configure, seed canon, connect an instance. Follow it once.
- **Running it day to day?** → **[OPERATIONS.md](OPERATIONS.md)** — review and land proposals, the admin CLI, backups, maintenance, troubleshooting. This is the one to keep open.
- **How it works underneath?** → **[ARCHITECTURE.md](ARCHITECTURE.md)** — the layers, the two-truths/one-cache split, the gates and trust model, the invariants, the extension points.

---

## Code map

| file | what it is |
|---|---|
| `local_capstore.py` | the git-backed store (reads, commits, the two-phase human-gated merge, remote sync; owns the ref layout) |
| `canon.py` | the canon lifecycle: state sequence, log-entry validation, landing, index rebuild |
| `entries.py` | entry serialization (YAML front-matter + body) |
| `map_index.py` | the search index (SQLite + an embedder interface) — a rebuildable cache |
| `audit_log.py` | the hash-chained operation log — a source of truth |
| `authz.py` | the authorization policy seam (`DefaultPolicy`) |
| `orientation.py` | the arrival-orientation framework (machinery + your slots) |
| `airlock.py` | TOTP two-phase remote approval (approving through a relaying instance) |
| `sup` tools (in `cap_server.py`) | per-instance state ↔ canon coherence |
| `cap_server.py` | the MCP server: the 28 tools, plus `server_from_config` / `land_and_record` |
| `config.py` | the typed deployment config (`concordance.toml`) |
| `admin.py` | the practitioner CLI — what *you* run |
| `*_test.py` | the test suite — run all with `python run_tests.py`, or any one directly |
| `embeddings-build-guide.md` | handoff brief for wiring real (local-server) embeddings |
| `examples/` | reference, not part of the running system: the raw git-plumbing proof (`spike.sh`), the off-machine-mirror demo (`sync_demo.py`), and a populated sample repo (`demo.git`) |

---

## Further reading

These live with the original practitioner's working tree, not in this repository (they carry that practice's history; the suite is commitment-agnostic — README/SETUP/OPERATIONS are self-sufficient for running your own deployment):

**Current** (the authoritative pair):
- `concordance-v1-build-state.md` — the full state of what's built and what's deferred.
- `concordance-v1-content-model.md` — paths, identity, supersede, the domains, the envelope.

**Design rationale from the build:** `map-imp-design-summary.md` (the MAP/IMP design consolidation; predates Lintel's naming) and the two `IMPLEMENTATION-BRIEF-*` docs — those two are Lintel's — that the log-entry and airlock features were built from.

**Historical** (spec-era; superseded by the authoritative pair — each is marked at its top): `capstore-spine-artifact.md`, `concordance-v1-build-checklist.md`, `concordance-v1-spine-instance-brief.md`.

---

*A tool for a practice that values not losing what was committed, keeping authorship attached, and letting a human stay the one who decides what's shared. Run it in that spirit.*
