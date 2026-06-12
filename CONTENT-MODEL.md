# Stasima v1 — Content Model (paths, identity, entries)

*The document an adopter authors against: how an entry is identified, where it lives, how it's structured, and the invariants the index and tools respect. [ARCHITECTURE.md](ARCHITECTURE.md) covers the machinery underneath; this covers the content that flows through it. A living document — the domain set grows over time (adding is cheap, deleting is not).*

---

## Identity: the path is the name

There are **no logical entry IDs.** An entry's identity *is* its file path (e.g. `practice/no-silent-loss.md`). References between entries are paths; search returns `(ref, path)` pointers; "is this the same entry?" is "same path."

The consequence is load-bearing: **a path is a permanent, public name, not a storage location.** Moving or renaming a published entry changes its identity — it breaks every reference and severs the "same entry over time" thread. A published path is a promise kept forever. Design energy goes into path conventions, not ID allocation (deliberately given up: we lose stable identity across reorganization and gain a single source of truth, no allocation problem, perfect rebuildability from a bare clone, and native git tooling).

**Addressing:** the **authored layer always addresses by path** — references, map links, and message coordinates are paths, never opaque hashes. The git object id (`content_oid`) is a *derived* detail the index records for version-pinning; authors never write it.

## Layer: the tree, not a path prefix

- **Canon** = the `main` tree (`refs/heads/main`). **Perspectives** = per-instance append-only branches (`refs/concordance/perspectives/<id>`).
- "Canon" is **never a path segment** — the *branch* encodes the layer. The same domain folders appear at the top of every tree; which ref you're on tells you draft-vs-production.
- **Birth → promotion.** Every entry is authored as a perspective entry (ungated, append-only). Promotion to canon is a *separate, gated* op — a proposal copying the entry onto `main` through the practitioner's review. The author cannot self-promote; the op invoked sets the layer.
- **Why a tree, not a status flag.** Keeping canon a real destination tree preserves two foundational properties: canon stays a clonable, queryable, structurally-gated tree (the no-silent-loss guarantee + practitioner authority). A flag-on-perspective-entries model would dissolve both.
- **Promotion duplicates content, intentionally.** The author keeps their perspective draft; canon gets the production form (possibly re-slugged/edited at promotion), which then evolves via further proposals. Lineage rides on `supersedes` / `promoted_from` or git history.

## Supersede (no edit-in-place for bodies)

- **A body is immutable.** Once written, the content at a path never changes — so a reference always resolves to the content that was referenced.
- **To revise:** add a *new* entry carrying `supersedes: <old path>`, and update the *old* entry's **metadata only** — `status: superseded`, `superseded_by: <new path>` — body untouched. One metadata commit; still append-only history.
- Search returns `active` entries by default, resolves superseded ones on direct reference, and can walk the chain to current.

## Domains (the top-level paths)

Identical across every tree (the branch is the layer); `state/` and `messages/` are perspective-authored.

| domain | holds | MCP surface |
|---|---|---|
| `references/` | external works — citations (flat; `medium`, `url` in envelope) | resources |
| `practice/` | practice-generated substantive content | resources / search |
| `meta/` | reflection & method (the practice on itself); **`meta/log/` holds the log entries** (below) | resources / search |
| `argot/` | term definitions (the practice's vocabulary) | resources / search |
| `conduct/` | commitments — the deployment's own promises (the suite ships this empty) | resources |
| `technical/` | protocol, schema, bootstrap/orientation — shared operational canon | resources |
| `prompts/` | reusable, invokable prompts | MCP **prompts** |
| `maps/` | **cartographic entries** — authored links/regions/salience over other entries | resources / search |
| `messages/` | **addressed messages** — authored, attributed, with `recipients` | `imp_check` tool (pull) |
| `assets/` | owned binary files (inline or Git LFS; never URL) | resources |
| `archive/` | imported historical record from a prior substrate (chat logs, earlier journals) — *not* practice-generated, external, or reflection; populated at bootstrap, read-only by convention after | resources |
| `state/` | per-instance self-description — **perspective-only, ungated** | (not canon) |

**Boundary triage** (the three "about the practice" domains blur; pin them):
- **argot** = *definitions*; **conduct** = *commitments*; **meta** = *reflection/method* (neither); **technical** = *operational* (distinct from meta's *intellectual*).

**The rule for the domain set:** adding a domain is cheap and additive; deleting is impossible. Pre-provision only a category that is *certain + likely voluminous + a genuinely different kind of thing.* New domains for a different *kind of thing* only, never to sub-classify within one — that's the envelope's job.

## Log entries & the state sequence

Two artifact types deliberately named apart. **Coherence state** (`state/`, `sup_reconcile`) is perspective-only and ungated. **The log entry** is the canon-side narrative: a record of what a change is and why it matters, riding *in* the proposal so every canon commit lands with its story attached.

- **Home:** `meta/log/<seq>.md`, envelope `type: log`, required front-matter `seq` (lowercase hex, matching the filename).
- **One per proposal, enforced at land** (not convention): landing rejects a proposal with zero or multiple log entries, and rejects `seq ≠ canon seq + 1` (monotonicity). `preview` surfaces both before the practitioner lands.
- **At land, the merge commit is tagged `state/<seq>`** — the `::N` notation becomes an alias for a content-addressed truth (`git rev-parse state/4f`). State tags ride the sync refspecs.
- **The origin is configurable** (`seq_origin`): canon sits at the origin before any land; the first land is origin + 1. A fresh deployment may start at 0; a deployment migrating from a prior numbered practice sets the origin to continue its sequence unbroken.
- **Allocation is free:** an instance learns `next_seq` from `canon_state`/its reconcile; if another proposal lands first, the reconcile-before-propose gate forces a re-pull, the instance renumbers and retracts the stale entry (`propose_retract`). The coherence gate doubles as the allocation mechanism.
- **Every land increments** — there is deliberately no two-tier "some lands are states" ambiguity. The judgment "which changes deserve a state" relocates to "which proposals deserve a land," same judge.

## References & lineage — the one piece of future-proofing infra

`references` is a **first-class, indexed field**. Together with `supersedes` / `superseded_by` / `promoted_from`, references form a **derivation graph** — and that graph is the single thing that's *impossible to backfill.*

Every deferred analysis reduces to a query over it: "bad base idea vs. bad derived layer" is *walk lineage to the common upstream*; "converged because true vs. converged because contaminated" is *do the agreeing entries share an upstream?*; canon-alignment relevance is *proximity through the same graph*. If entries don't record what they build on, none of it is recoverable. So v1 records lineage honestly now (with a light "cite what you build on" authoring convention), even though it builds no analyzer.

## Assets

Git stores binaries natively, so it'll version a small owned file inline; the catch is clone bloat. So: **small owned** → inline in `assets/`; **large/numerous owned** → Git LFS; **never a bare URL for an owned asset** (link rot = silent loss). A URL you merely *cite* is a `references/` entry with `url:`, not an asset. *Own it → commit it; cite it → reference it.*

## Envelope

YAML front-matter + Markdown body. **Shared core, every entry:**

```yaml
type: kno            # small controlled, extensible vocab of type codes
title: ...
status: active       # active | superseded
references: [practice/...]    # by path — first-class, indexed (lineage)
supersedes: [practice/...]    # optional lineage
tags: [...]
```

Provenance (`author`, `created`, `version`) comes from **git**, not the envelope — single-sourced, can't drift. **Per-domain additions:**

- `references/` → `medium`, `url`, `creator`, `year`
- `argot/` → `term`, `see-also`
- `prompts/` → `arguments`
- `maps/` → `region_labels`, `links` (paths), `salience`, `maps` (the entry/entries it annotates, by path)
- `messages/` → `recipients` (list), `subject` (authored), `coordinates` (paths + region labels the sender points at)

## Relevance — the practitioner's judgment, with canon-alignment as an assist

v1 builds **no autonomous relevance engine.** Relevance is the practitioner's judgment; the system's job is to support it, not replace it.

- **Canon-alignment** is a computable *proxy for accumulated judgment* (canon = the record of what's already been promoted). It's offered as a **bidirectional search dimension** — alignment ↔ divergence — opt-in, with the spread visible. **Never a default one-way "relevance" filter:** that would amplify agreement and bury the lone divergent mapping — exactly the failure mode where consensus around plausible noise gets mistaken for signal. The frontier lives at the *divergence* pole. Align to *active* canon only; near-meaningless until canon accretes.
- **Messaging has no relevance trigger in v1.** The inbox is pull: a saved query (you're in `recipients`, unread). Entry-level judgment happens through the sender's authored subjects.

## Search — cartography over a shared substrate (v1 scope)

- **Substrate vs. cartography.** One shared physical index (embeddings — mechanism, not a perspective) underneath; **per-instance authored maps** on top (where perspective lives). Conflating them privatizes the wrong half.
- **Maps are authored entries** (`maps/` domain), divergent/convergent like the corpus, promotable to a canon "house map" by the normal gate.
- **Meta-catalog is a query, not a store.** `authoring_instance` is a *dimension, not a partition* — one `map_entries` table; "per-instance catalog" = a `WHERE` clause.
- **Ranking discipline:** results are attributed by author or weighted through an explicitly chosen lens — never an unattributed blend. **Count is a dimension, never the default sort**; show the spread, not just the tally.

## Messaging — addressed entries (v1 scope)

- **A message = an entry + `recipients` + a per-instance index slice.** Not a new primitive.
- **Permission = index-scope, not access-control.** Discoverability-scoped, not visibility-scoped: the entry stays world-readable and attributed on the spine; it's only *indexed* for its recipients. **Private in attention, public in referent.** (No read-secrecy in v1.)
- **Pull, no push.** Inbox = a saved query (`me ∈ recipients`, unread). No delivery engine.
- **Sender authors subject + any summary; the system arranges, never authors.** Triage on the sender's words.
- **Coordinates are paths** the sender points at — the recipient references the exact location rather than re-discovering it.
- **Read-state = append-only events in the audit log**, not git commits (a commit per read would be spam). "Is this read?" is a query over read events.

## The index is a derived projection (invariant)

**Git is truth for entries; the audit log is truth for events. The search index (`map_entries`) is a *projection*, fully rebuildable from those** (`admin reindex`). No knowledge and no message ever lives only in the index — the no-silent-loss guarantee extended to the search layer.

Working `map_entries` shape (derived; the server indexes inline on each commit — SQLite now, Postgres later behind the same interface):
```
map_entries(
  path, authoring_instance, is_canon,        -- identity + layer (is_canon derived from the ref)
  content_oid,                               -- derived version pin (not authored)
  type, title, status, tags[],
  references[], supersedes[],                 -- the lineage graph
  region_labels[], links[], salience,        -- cartographic (maps/)
  recipients[],                              -- addressing (messages/)
  body_text, embedding
)
```

## MCP surface alignment

- `prompts/` → MCP **prompts** primitive (list/get, invokable).
- knowledge domains + `maps/` → MCP **resources** and/or the **`map_search` → `kip_get`** path.
- `messages/` → the **`imp_check`** pull tool.
