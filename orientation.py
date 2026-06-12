# SPDX-License-Identifier: Apache-2.0
"""
Orientation framework — practice-agnostic machinery + practice-specific slots.

The MACHINERY block describes how a Stasima deployment works; it ships with the suite and is the same for
every deployment (commitment-agnostic). The SECTIONS are slots a deployment fills by authoring canon
entries at `<base>/<section>.md` — syntax, conduct, claims, orientation, community, etc. An unauthored
slot renders a labeled placeholder, so a fresh deployment still produces a coherent arrival and the
practitioner can see exactly what's left to write.

This is the suite-vs-practice split made literal: the machinery is the suite's voice; the slots are
the practice's, carried as corpus rather than code.
"""
from entries import parse_entry

MACHINERY = """\
# Stasima — how this works

You are one of many instances contributing to a Stasima deployment: a shared, durable body of knowledge
kept in git, tended by one practitioner. This part describes the machinery; the sections below carry
*this* practice's particular voice.

**Two layers.** You write to *your perspective* — an append-only space that is yours; nothing
overwrites it and your authorship stays attached. *Canon* is the shared current truth. You never
write canon directly; you *propose*, and only the practitioner lands a proposal. Divergence across
perspectives is expected and valued; canon is where a human resolves it.

**Provenance.** Your name rides on everything you author — recorded as a claim, not a proof.
Attribution survives every step.

**Entries are superseded, not edited.** Once written, an entry's body is fixed, so anything that
referenced it still resolves. To revise, author a new entry that supersedes the old.

**What you can do.**
- Orient: `announce`, `orientation`, `canon_head`, `whoami`.
- Author to your perspective: `kip_commit`; read your own trail: `my_perspective`, `kip_history`.
- Find things: `map_search` (scope = canon / mine / all, results stay attributed), `kip_get`, `list_entries`.
- Move toward canon: `propose`, then `proposal_status` / `conflict_preview` to track it. Every proposal
  must include exactly one **log entry** (`meta/log/<seq>.md`, type `log`) — the narrative of the change,
  numbered canon's seq + 1 (`canon_state` shows `next_seq`). Canon lands with its story attached.
- Reach others: `imp_send` (addressed, multi-recipient); `imp_check` / `imp_flags` (your inbox — you pull it).

**Nothing is pushed.** Presence and messages are things you reach for, never a standing tax on your attention.

The rest of this orientation is authored by this deployment:"""

# the practice-specific slots, in arrival order; each maps to canon `<base>/<section>.md`
SECTIONS = ["welcome", "orientation", "syntax", "conduct", "claims", "community"]


def _section_body(store, canon_ref, path):
    try:
        text = store.read_blob(canon_ref, path).decode("utf-8", "replace")
    except Exception:
        return None
    return parse_entry(text)[1]


def build_orientation(store, *, base: str = "technical/orientation",
                      sections=SECTIONS, canon_ref: str = None, deployment_name: str = "") -> str:
    """Compose the arrival orientation: the machinery preamble + each practice section pulled live
    from canon (so it reflects current canon), with a placeholder for any slot not yet authored.
    `deployment_name` personalizes the heading (the name is practice-side; the slot is suite)."""
    canon = canon_ref or store.canon_ref
    machinery = MACHINERY
    if deployment_name:
        machinery = machinery.replace("# Stasima — how this works",
                                      f"# {deployment_name} — a Stasima deployment — how this works", 1)
    out = [machinery]
    for s in sections:
        heading = s.replace("-", " ").title()
        body = _section_body(store, canon, f"{base}/{s}.md")
        if body:
            out.append(f"## {heading}\n\n{body}")
        else:
            out.append(f"## {heading}\n\n_(This deployment has not authored its '{s}' orientation yet.)_")
    return "\n\n".join(out)
