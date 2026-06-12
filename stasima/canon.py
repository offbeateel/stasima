# SPDX-License-Identifier: Apache-2.0
"""
Canon lifecycle — the layer between storage and the protocol surface.

Owns: the state sequence (seq tags, ::N display), log-entry validation (every proposal lands with
its story attached), the practitioner's landing routine, and the index rebuild. Used by the server
(cap_server), the cockpit (admin), and the airlock — none of which should have to reach through the
protocol surface to get at lifecycle machinery.
"""
from .entries import parse_entry
from .local_capstore import LocalCapStore, PERSP_PREFIX
from .map_index import index_entry
from .audit_log import anchor_audit_head

LOG_DIR = "meta/log/"
STATE_TAGS = "refs/tags/state/"
# Default sequence origin: this practice's chat-era freeze (::3B), kept as the suite default so the
# original deployment's continuity holds. A deployment sets `seq_origin` in config to start anywhere
# (0 -> first land is ::1). The first land is always origin + 1.
CHAT_ERA_FREEZE = 0x3B


def _instance_from_ref(ref: str):
    return ref[len(PERSP_PREFIX):] if ref.startswith(PERSP_PREFIX) else None


def reindex_from_git(store: LocalCapStore, index, embedder, *, clear: bool = True) -> int:
    """Rebuild the MAP index from git — the derived-projection invariant, in code. Walks canon +
    every perspective, reads each entry, re-embeds, upserts. Also the canon-indexing-after-landing
    path and the model-swap / recovery path. Proposals are staging and are not indexed."""
    if clear:
        index.clear()
    canon = store.canon_ref
    refs = ([canon] if store.resolve_ref(canon) else []) + [r.name for r in store.list_refs(PERSP_PREFIX)]
    n = 0
    for ref in refs:
        for path in store.list_paths(ref):
            if not path.endswith(".md"):
                continue
            envelope, body = parse_entry(store.read_blob(ref, path).decode("utf-8", "replace"))
            author = _instance_from_ref(ref)
            if author is None:                       # canon: originator of the path's history
                hist = store.history(ref, path)
                author = hist[-1]["author"] if hist else ""
            index_entry(index, embedder, ref=ref, path=path, is_canon=(ref == canon),
                        authoring_instance=author, content_oid=store.blob_oid(ref, path),
                        envelope=envelope, body=body)
            n += 1
    return n


# ---- log entries + the state sequence (the State Log's descendant) ----
def canon_seq(store, origin: int = CHAT_ERA_FREEZE) -> int:
    """Canon's current state number, read from the state/<seq> tags (hex). `origin` before any land."""
    vals = []
    for r in store.list_refs(STATE_TAGS):
        try:
            vals.append(int(r.name[len(STATE_TAGS):], 16))
        except ValueError:
            pass
    return max(vals, default=origin)


def seq_display(n: int) -> str:
    return f"::{format(n, 'X')}"


def validate_log_entry(store, prepared, origin: int = CHAT_ERA_FREEZE) -> str:
    """A proposal lands with its story attached: exactly one new log entry under meta/log/, whose
    seq (hex front-matter, matching the filename) is canon's seq + 1. Raises ValueError otherwise."""
    base = store.resolve_ref(prepared.into)
    tip = store.resolve_ref(prepared.proposal_ref)
    logs = [p for p in store.changed_paths(base, tip) if p.startswith(LOG_DIR)]
    if len(logs) != 1:
        raise ValueError(
            f"a proposal must contain exactly one log entry under {LOG_DIR} — found {len(logs)} "
            f"({logs or 'none'}); author it with propose(domain='meta/log', slug='<seq>', type='log', seq='<seq>')")
    path = logs[0]
    env, _ = parse_entry(store.read_blob(prepared.proposal_ref, path).decode("utf-8", "replace"))
    seq = str(env.get("seq", "")).lower()
    try:
        n = int(seq, 16)
    except ValueError:
        raise ValueError(f"log entry {path} needs front-matter `seq` as lowercase hex, got {env.get('seq')!r}")
    stem = path[len(LOG_DIR):-3]
    if stem != seq:
        raise ValueError(f"log entry filename {stem!r} must match its seq {seq!r}")
    current, expected = canon_seq(store, origin), canon_seq(store, origin) + 1
    if n != expected:
        raise ValueError(
            f"log entry is {seq_display(n)} but canon is at {seq_display(current)} — expected {seq_display(expected)}. "
            f"Re-pull (canon_diff), reconcile, renumber the log entry, and retract the stale one (propose_retract).")
    return seq


def land_and_record(store, index, embedder, audit, prepared, approval, *,
                    origin: int = CHAT_ERA_FREEZE) -> dict:
    """The practitioner's promotion routine — NOT a model-facing tool (landing is the human gate).
    Validates the proposal's log entry + seq monotonicity, lands the approved merge, audit-logs it,
    tags the merge commit state/<seq>, reindexes, and anchors the audit head into git."""
    seq = validate_log_entry(store, prepared, origin)
    r = store.land_merge(prepared, approval)
    audit.append(approval.approved_by, "land_merge", target_ref=prepared.into,
                 op_id=f"land-{r.oid[:12]}", result_oid=r.oid,
                 detail={"proposal": prepared.proposal_ref, "seq": seq})
    store.tag(f"state/{seq}", r.oid)
    reindex_from_git(store, index, embedder)
    anchor = anchor_audit_head(store, audit)
    return {"landed": r.oid, "seq": seq, "display": seq_display(int(seq, 16)), "anchor": anchor}
