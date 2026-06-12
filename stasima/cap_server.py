# SPDX-License-Identifier: Apache-2.0
"""
Stasima CAP server — MCP protocol surface over LocalCapStore + the MAP index + the audit log.

Tools give an instance: orient -> author (with envelopes, indexed inline, audit-logged) -> search
-> review its own trail -> propose -> check status -> message peers (read-state in the audit log).

Audit scope: writes (state changes) and failures (what's breaking). Successful reads are observability
and are not logged; read-receipts ARE logged (forensic, write-like). Mutations follow git-first-then-
audit. Identity is the instance's declared name (a deployment binds it from the transport token).
"""
import os

from mcp.server.fastmcp import FastMCP

from .local_capstore import (LocalCapStore, Identity, PathNotFound, RefNotFound, StaleRef,
                            CapStoreError, PERSP_PREFIX as PERSP, PROP_PREFIX as PROP)
from .map_index import SqliteMapIndex, StubEmbedder, LocalServerEmbedder, index_entry
from .audit_log import SqliteAuditLog
from .authz import Denied, DefaultPolicy
from .entries import compose_entry, parse_entry          # shared content-model serialization
from .orientation import build_orientation               # practice-agnostic machinery + practice slots
from .airlock import Airlock                             # TOTP two-phase remote approval
# canon lifecycle (re-exported here for callers/tests that import via the server module)
from .canon import (LOG_DIR, CHAT_ERA_FREEZE, canon_seq, seq_display, reindex_from_git,
                   land_and_record, validate_log_entry, validate_log_entry as _validate_log_entry)


def build_server(store: LocalCapStore, index=None, embedder=None, audit=None, authz=None, airlock=None, *,
                 orientation_text: str = None, orientation_base: str = "technical/orientation",
                 seq_origin: int = CHAT_ERA_FREEZE, deployment_name: str = "",
                 http_host: str = "127.0.0.1", http_port: int = 8787) -> FastMCP:
    mcp = FastMCP("stasima", host=http_host, port=http_port)   # host/port used only by the http transport
    has_map = index is not None and embedder is not None

    def persp_ref(iid): return PERSP + iid
    def prop_ref(pid): return PROP + pid

    def resolve_alias(ref):
        if ref in ("canon", "main"):
            return store.canon_ref
        if ref.startswith("refs/"):
            return ref
        return persp_ref(ref)

    def _commit_retry(ref, path, content, author, op_id):
        for attempt in range(2):
            tip = store.resolve_ref(ref)
            try:
                return store.commit(ref, {path: content.encode()}, f"KIP {path}",
                                    Identity(author), expected_parent=tip, op_id=op_id)
            except StaleRef:
                if attempt == 1:
                    raise

    def _index(ref, path, is_canon, author, oid, envelope, body):
        if has_map:
            index_entry(index, embedder, ref=ref, path=path, is_canon=is_canon,
                        authoring_instance=author, content_oid=oid, envelope=envelope, body=body)

    def _log(actor, op, **kw):
        if audit is not None:
            audit.append(actor, op, **kw)

    def _authz(actor, op, ref=None, path=None):
        if authz is None:
            return
        try:
            authz.check(actor, op, ref, path)
        except Denied as e:
            _log(actor, op, target_ref=ref, target_path=path, outcome="denied", detail={"msg": str(e)})
            raise

    def _exists(ref, path):
        try:
            store.read_blob(ref, path)
            return True
        except (PathNotFound, RefNotFound):
            return False

    def _canon_cursor(actor):
        # the canon oid the instance last pulled — a server-tracked audit fact, not a self-claim
        if audit is None:
            return None
        evs = audit.events(actor=actor, op="canon_pull")
        return evs[-1]["result_oid"] if evs else None

    def _check_immutable(actor, ref, path, new_body):
        # bodies are immutable; a same-path write with a different body must supersede to a new slug
        if _exists(ref, path):
            old_body = parse_entry(store.read_blob(ref, path).decode("utf-8", "replace"))[1]
            if old_body.strip() != new_body.strip():
                _log(actor, "kip_commit", target_ref=ref, target_path=path, outcome="denied",
                     detail={"reason": "body immutable; supersede to a new slug"})
                raise Denied(f"{path} exists and an entry's body is immutable — supersede to a new slug")

    def _check_not_staged(proposal_id):
        # a staged proposal is frozen for review — the airlock's chamber must hold exactly what was staged
        if airlock is not None and airlock.state(proposal_id)["state"] == "staged":
            raise Denied(f"proposal {proposal_id} is frozen for review (staged) — land, revert, or let it expire")

    def _attention():
        # count of unread practitioner-recipient messages; delivery is conduct-convention, this is just the field
        if not has_map or audit is None:
            return None
        return len([m for m in index.inbox("practitioner") if not audit.is_read("practitioner", m.path)])

    def _require_reconciled(actor):
        # reaching into shared space (propose) requires you've reconciled with CURRENT canon first
        if audit is None:
            return
        tip = store.resolve_ref(store.canon_ref)
        if tip is None:
            return
        if not _exists(persp_ref(actor), f"state/reconciled-{tip[:12]}.md"):
            _log(actor, "propose", target_ref=store.canon_ref, outcome="denied",
                 detail={"reason": "not reconciled with current canon", "canon_tip": tip})
            raise Denied(f"reconcile with current canon {tip[:12]} first (canon_diff, then sup_reconcile)")

    def _orientation():
        # static override if provided (e.g. tests); otherwise render machinery + practice slots from canon
        return orientation_text if orientation_text else build_orientation(
            store, base=orientation_base, deployment_name=deployment_name)

    # ---------------------------------------------------------------- orient
    @mcp.tool()
    def announce(instance_id: str) -> dict:
        """Announce presence; returns orientation + current canon head + your perspective tip."""
        home = deployment_name or "Stasima"
        return {"welcome": f"Welcome to {home}, {instance_id}.", "orientation": _orientation(),
                "canon_head": store.resolve_ref(store.canon_ref),
                "your_perspective_tip": store.resolve_ref(persp_ref(instance_id)),
                "practitioner_attention": _attention()}

    @mcp.tool()
    def orientation() -> str:
        """The arrival orientation: practice-agnostic machinery + this deployment's authored sections."""
        return _orientation()

    @mcp.tool()
    def canon_head() -> dict:
        """Canon ref + state number + the list of canon entry paths."""
        head = store.resolve_ref(store.canon_ref)
        n = canon_seq(store, seq_origin)
        return {"canon_head": head, "seq": format(n, "x"), "display": seq_display(n),
                "entries": store.list_paths(store.canon_ref) if head else []}

    @mcp.tool()
    def whoami(instance_id: str) -> dict:
        """How the server sees you. (authz is a stub in this slice.)"""
        return {"instance_id": instance_id, "perspective_ref": persp_ref(instance_id),
                "namespace": f"perspectives/{instance_id}", "allowed_ops": ["kip_commit", "propose", "imp_send"],
                "note": "identity is a recorded name gated by the transport token; authz stubbed here"}

    # ---------------------------------------------------------------- author
    @mcp.tool()
    def kip_commit(instance_id: str, domain: str, slug: str, body: str, op_id: str,
                   title: str = "", type: str = "kno",
                   tags: list[str] | None = None, references: list[str] | None = None) -> dict:
        """Author an entry to your append-only perspective at <domain>/<slug>.md (YAML envelope + body)."""
        ref = persp_ref(instance_id)
        path = f"{domain}/{slug}.md"
        _authz(instance_id, "kip_commit", ref, path)
        _check_immutable(instance_id, ref, path, body)
        envelope = {"type": type, "title": title or slug, "status": "active",
                    "tags": tags or [], "references": references or []}
        try:
            r = _commit_retry(ref, path, compose_entry(envelope, body), instance_id, op_id)
        except CapStoreError as e:
            _log(instance_id, "kip_commit", target_ref=ref, target_path=path, op_id=op_id,
                 outcome=f"error:{e.__class__.__name__}", detail={"msg": str(e)})
            raise
        _index(ref, path, False, instance_id, r.oid, envelope, body)            # git-first ...
        _log(instance_id, "kip_commit", target_ref=ref, target_path=path, op_id=op_id, result_oid=r.oid)  # ... then audit
        return {"oid": r.oid, "ref": r.ref, "path": path, "op_id": r.op_id, "author": instance_id}

    # ---------------------------------------------------------------- read
    @mcp.tool()
    def kip_get(ref: str, path: str) -> str:
        """Read an entry's full text (envelope + body). `ref` may be 'canon', an instance name, or a full ref."""
        return store.read_blob(resolve_alias(ref), path).decode()

    @mcp.tool()
    def list_entries(ref: str, path: str = "") -> list[str]:
        """List entry paths under a ref ('canon', an instance name, or a full ref)."""
        return store.list_paths(resolve_alias(ref), path)

    @mcp.tool()
    def my_perspective(instance_id: str) -> dict:
        """Your perspective tip + your entry paths — your own state trail."""
        ref = persp_ref(instance_id)
        tip = store.resolve_ref(ref)
        return {"tip": tip, "entries": store.list_paths(ref) if tip else []}

    @mcp.tool()
    def kip_history(ref: str, path: str) -> list[dict]:
        """Version trail for an entry (newest first): oid, author, subject."""
        return store.history(resolve_alias(ref), path)

    # ---------------------------------------------------------------- propose + track
    @mcp.tool()
    def propose(instance_id: str, proposal_id: str, domain: str, slug: str, body: str, op_id: str,
                title: str = "", type: str = "kno", seq: str = "") -> dict:
        """Open or extend a proposal targeting canon at <domain>/<slug>.md. Landing is the practitioner's,
        out of band. A proposal must include exactly one LOG ENTRY before it can land — the narrative of
        the change: propose(domain='meta/log', slug='<seq>', type='log', seq='<seq>') where seq is
        canon's current seq + 1 in lowercase hex (see canon_state)."""
        ref = prop_ref(proposal_id)
        path = f"{domain}/{slug}.md"
        _authz(instance_id, "propose", ref, path)
        _check_not_staged(proposal_id)
        _require_reconciled(instance_id)
        envelope = {"type": type, "title": title or slug, "status": "active"}
        if seq:
            envelope["seq"] = seq.lower()
        try:
            if store.resolve_ref(ref) is None:
                store.create_branch(ref, store.resolve_ref(store.canon_ref))
            r = _commit_retry(ref, path, compose_entry(envelope, body), instance_id, op_id)
        except CapStoreError as e:
            _log(instance_id, "propose", target_ref=ref, target_path=path, op_id=op_id,
                 outcome=f"error:{e.__class__.__name__}", detail={"msg": str(e)})
            raise
        _log(instance_id, "propose", target_ref=ref, target_path=path, op_id=op_id, result_oid=r.oid)
        return {"proposal_id": proposal_id, "oid": r.oid, "path": path, "author": instance_id}

    @mcp.tool()
    def propose_retract(instance_id: str, proposal_id: str, path: str, op_id: str) -> dict:
        """Remove a path from a proposal — e.g. a stale log entry after renumbering (canon advanced,
        so your meta/log/<old-seq>.md must be retracted and re-authored at the new seq)."""
        ref = prop_ref(proposal_id)
        _authz(instance_id, "propose", ref, path)
        _check_not_staged(proposal_id)
        tip = store.resolve_ref(ref)
        if tip is None:
            raise RefNotFound(ref)
        r = store.commit(ref, {path: None}, f"retract {path}",
                         Identity(instance_id), expected_parent=tip, op_id=op_id)
        _log(instance_id, "propose_retract", target_ref=ref, target_path=path, op_id=op_id, result_oid=r.oid)
        return {"proposal_id": proposal_id, "retracted": path, "oid": r.oid}

    @mcp.tool()
    def proposal_status(proposal_id: str) -> dict:
        """pending / landed / unknown — 'landed' = the proposal tip is an ancestor of canon."""
        tip = store.resolve_ref(prop_ref(proposal_id))
        if tip is None:
            return {"proposal_id": proposal_id, "exists": False, "status": "unknown"}
        landed = store.is_ancestor(tip, store.resolve_ref(store.canon_ref))
        return {"proposal_id": proposal_id, "exists": True, "tip": tip, "status": "landed" if landed else "pending"}

    @mcp.tool()
    def conflict_preview(proposal_id: str) -> dict:
        """Would this proposal merge cleanly into canon right now? Read-only; creates no candidate."""
        summary = store.preview_merge(prop_ref(proposal_id))
        return {"conflicts": bool(summary.conflicts), "conflict_detail": summary.conflicts,
                "changed_paths": summary.changed_paths}

    @mcp.tool()
    def list_proposals() -> list[str]:
        """Open proposal ids."""
        return [r.name[len(PROP):] for r in store.list_refs(PROP)]

    @mcp.tool()
    def list_instances() -> list[str]:
        """Instances that have a perspective."""
        return [r.name[len(PERSP):] for r in store.list_refs(PERSP)]

    # ---------------------------------------------------------------- MAP (needs an index) + IMP (needs an index + audit)
    if has_map:
        @mcp.tool()
        def map_search(instance_id: str, query: str, scope: str = "all",
                       type: str | None = None, limit: int = 10) -> list[dict]:
            """Semantic search over the corpus, attributed. scope: canon | mine | all. Returns pointers
            (path, ref, author, is_canon, type, title, score, preview) — never an unattributed blend."""
            qv = embedder.embed_query([query])[0]
            hits = index.search(qv, scope=scope, instance_id=instance_id, type=type, limit=limit)
            return [{"path": h.path, "ref": h.ref, "author": h.authoring_instance, "is_canon": h.is_canon,
                     "type": h.type, "title": h.title, "score": h.score, "preview": h.preview} for h in hits]

        if audit is not None:
            @mcp.tool()
            def imp_send(sender: str, recipients: list[str], subject: str, body: str, op_id: str,
                         coordinates: list[str] | None = None) -> dict:
                """Author an addressed message — a KIP entry on your branch under messages/. World-readable and
                attributed on the spine; indexed into each recipient's inbox. `coordinates` are paths to jump to."""
                ref = persp_ref(sender)
                path = f"messages/{op_id}.md"
                _authz(sender, "imp_send", ref, path)
                envelope = {"type": "msg", "subject": subject, "status": "active",
                            "recipients": recipients, "coordinates": coordinates or []}
                try:
                    r = _commit_retry(ref, path, compose_entry(envelope, body), sender, op_id)
                except CapStoreError as e:
                    _log(sender, "imp_send", target_path=path, op_id=op_id,
                         outcome=f"error:{e.__class__.__name__}", detail={"msg": str(e)})
                    raise
                _index(ref, path, False, sender, r.oid, envelope, body)
                _log(sender, "imp_send", target_ref=ref, target_path=path, op_id=op_id,
                     result_oid=r.oid, detail={"recipients": recipients})
                return {"path": path, "from": sender, "recipients": recipients, "oid": r.oid}

            @mcp.tool()
            def imp_check(instance_id: str, unread_only: bool = True) -> list[dict]:
                """Your inbox: messages where you're a recipient. Authored fields only (sender, subject,
                coordinates) — IMP arranges, never synthesizes. Pull, not push."""
                msgs = index.inbox(instance_id)
                if unread_only:
                    msgs = [m for m in msgs if not audit.is_read(instance_id, m.path)]
                return [{"path": m.path, "from": m.authoring_instance, "subject": m.subject,
                         "coordinates": m.links, "ref": m.ref} for m in msgs]

            @mcp.tool()
            def imp_flags(instance_id: str) -> dict:
                """The lightweight flag: how much unread mail is waiting (a saved query, not a push)."""
                unread = [m for m in index.inbox(instance_id) if not audit.is_read(instance_id, m.path)]
                return {"unread": len(unread), "from": sorted({m.authoring_instance for m in unread})}

            @mcp.tool()
            def imp_mark_read(instance_id: str, message_path: str) -> dict:
                """Append a read-receipt to the audit log (append-only truth; survives a reindex)."""
                audit.append_read(instance_id, message_path)
                return {"marked_read": message_path}

    # ---------------------------------------------------------------- SUP: per-instance state ↔ canon coherence
    if audit is not None:
        @mcp.tool()
        def canon_diff(instance_id: str) -> dict:
            """Pull what changed in canon since you last reconciled — this LOADS the diff into your context
            so you can respond coherently. Advances your canon cursor (a server-tracked fact). You must then
            sup_reconcile before you can propose again."""
            tip = store.resolve_ref(store.canon_ref)
            prev = _canon_cursor(instance_id)
            if tip is None:
                paths = []
            elif prev is None:
                paths = store.list_paths(store.canon_ref)        # first pull: all of current canon
            else:
                paths = store.changed_paths(prev, tip)
            changed = []
            for p in paths:
                try:
                    changed.append({"path": p, "content": store.read_blob(store.canon_ref, p).decode("utf-8", "replace")})
                except (PathNotFound, RefNotFound):
                    changed.append({"path": p, "content": None})  # removed in canon
            _log(instance_id, "canon_pull", target_ref=store.canon_ref, result_oid=tip,
                 detail={"from": prev, "changed": paths})
            return {"canon_tip": tip, "from": prev, "changed": changed}

        @mcp.tool()
        def sup_reconcile(instance_id: str, body: str) -> dict:
            """Self-report what you updated about yourself after reading the canon diff. Allowed only after
            you've pulled current canon (canon_diff). Appends a state/ entry to your perspective — your own
            chronology, paired to the canon version. This is what unblocks propose."""
            tip = store.resolve_ref(store.canon_ref)
            if _canon_cursor(instance_id) != tip:
                raise Denied("pull current canon first (canon_diff), then reconcile")
            ref = persp_ref(instance_id)
            path = f"state/reconciled-{tip[:12]}.md"
            if _exists(ref, path):
                return {"path": path, "canon_cursor": tip, "already": True}
            envelope = {"type": "reconciliation", "title": f"Reconciled with canon {tip[:12]}",
                        "status": "active", "canon_cursor": tip}
            r = _commit_retry(ref, path, compose_entry(envelope, body), instance_id, f"reconcile-{tip[:12]}")
            _index(ref, path, False, instance_id, r.oid, envelope, body)
            _log(instance_id, "reconcile_report", target_ref=ref, target_path=path, result_oid=r.oid,
                 detail={"canon_cursor": tip})
            return {"path": path, "canon_cursor": tip, "oid": r.oid}

        @mcp.tool()
        def sup_state(instance_id: str) -> dict:
            """An instance's state trail + its standing relative to canon."""
            ref = persp_ref(instance_id)
            tip = store.resolve_ref(ref)
            states = [p for p in (store.list_paths(ref) if tip else []) if p.startswith("state/")]
            cursor = _canon_cursor(instance_id)
            return {"instance": instance_id, "perspective_tip": tip, "state_entries": states,
                    "canon_cursor": cursor, "current_with_canon": cursor == store.resolve_ref(store.canon_ref)}

        @mcp.tool()
        def sup_who() -> list[dict]:
            """Who holds a perspective, and whether each is current with canon."""
            canon_tip = store.resolve_ref(store.canon_ref)
            return [{"instance": r.name[len(PERSP):], "current_with_canon": _canon_cursor(r.name[len(PERSP):]) == canon_tip}
                    for r in store.list_refs(PERSP)]

        @mcp.tool()
        def canon_state() -> dict:
            """The shared canon state — the mirror of an instance's own: current tip, state number,
            entries, land chronology. A proposal's log entry must carry seq = this seq + 1."""
            tip = store.resolve_ref(store.canon_ref)
            n = canon_seq(store, seq_origin)
            lands = [{"oid": e["result_oid"], "ts": e["ts"], "by": e["actor"], "seq": e["detail"].get("seq")}
                     for e in audit.events(op="land_merge")]
            return {"canon_tip": tip, "seq": format(n, "x"), "display": seq_display(n),
                    "next_seq": format(n + 1, "x"),
                    "practitioner_attention": _attention(),
                    "entries": store.list_paths(store.canon_ref) if tip else [], "lands": lands[-10:]}

        if airlock is not None:
            @mcp.tool()
            def stage_approve(proposal_id: str, code: str) -> dict:
                """Airlock phase 1 — relay the practitioner's FIRST TOTP code. Freezes the proposal,
                prepares the merge, starts the review clock. Returns what was staged (oid, changed
                paths, log seq) for the practitioner to review. Console `land` is unchanged."""
                return airlock.stage(proposal_id, code)

            @mcp.tool()
            def land_approve(staged_oid_prefix: str, code: str) -> dict:
                """Airlock phase 2 — relay the practitioner's SECOND code (a fresh one: strictly later
                window, after the review floor). Lands exactly the staged oid; anything else fails closed."""
                return airlock.land(staged_oid_prefix, code)

            @mcp.tool()
            def stage_revert(proposal_id: str) -> dict:
                """Abort a staged review — FREE, never requires a code (charging presence-proof to
                decline would incentivize landing). The proposal returns to open, entries intact."""
                return airlock.revert(proposal_id)

    return mcp


def components_from_config(cfg):
    """Build the store / index / embedder / audit / authz / airlock from a Config — shared by the
    server and the admin CLI, so both wire components the same way."""
    store = LocalCapStore(cfg.git_dir, approvers=set(cfg.approvers), canon_ref=cfg.canon_ref,
                          committer=(cfg.committer_name, cfg.committer_email))
    index = SqliteMapIndex(cfg.resolved_map_db())
    audit = SqliteAuditLog(cfg.resolved_audit_db())
    if cfg.embed_backend == "local-server":   # LM Studio / Ollama (OpenAI-compatible)
        embedder = LocalServerEmbedder(cfg.embed_url, cfg.embed_model, cfg.embed_dim,
                                       doc_prefix=cfg.embed_doc_prefix,
                                       query_prefix=cfg.embed_query_prefix)
    else:
        embedder = StubEmbedder(dim=64)
    airlock = Airlock(store, audit,
                      secret_path=cfg.resolved_airlock_secret(),
                      land_fn=lambda prepared, approval: land_and_record(store, index, embedder, audit,
                                                                         prepared, approval,
                                                                         origin=cfg.seq_origin),
                      validate_fn=lambda prepared: _validate_log_entry(store, prepared, cfg.seq_origin),
                      approver=sorted(cfg.approvers)[0],
                      floor_s=cfg.airlock_floor_s, ceiling_s=cfg.airlock_ceiling_s)
    return store, index, embedder, audit, DefaultPolicy(canon_ref=cfg.canon_ref), airlock


def server_from_config(cfg) -> FastMCP:
    """Assemble the MCP server from a Config."""
    store, index, embedder, audit, authz, airlock = components_from_config(cfg)
    return build_server(store, index, embedder, audit, authz, airlock,
                        orientation_base=cfg.orientation_base, seq_origin=cfg.seq_origin,
                        deployment_name=cfg.deployment_name,
                        http_host=cfg.http_host, http_port=cfg.http_port)


def main() -> None:
    """Console entry point (`stasima` / `python -m stasima.cap_server`)."""
    from .config import Config
    _cfg = Config.load(os.environ.get("STASIMA_CONFIG"))
    _srv = server_from_config(_cfg)
    if _cfg.transport == "http":
        # One continuously-running server; clients connect to http://<host>:<port>/mcp.
        # Config validation already restricted the bind to loopback/tailnet (no transport auth
        # until 1.1); reach it from other devices via `tailscale serve` proxying to loopback.
        _srv.run(transport="streamable-http")
    else:
        _srv.run()   # stdio: the connecting client spawns this process


if __name__ == "__main__":
    main()
