# SPDX-License-Identifier: Apache-2.0
"""
Full-loop test through the real MCP client/protocol path (in-memory transport):
orient -> author (indexed inline) -> map_search -> read own trail -> propose -> check status
-> message peers -> flag/inbox/read. Uses the deterministic StubEmbedder (offline).
"""
import json
import os
import subprocess as sp
import sys
import tempfile

import anyio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_capstore import LocalCapStore
from map_index import SqliteMapIndex, StubEmbedder, index_entry
from audit_log import SqliteAuditLog
from authz import DefaultPolicy
from cap_server import build_server, compose_entry, reindex_from_git
from mcp.shared.memory import create_connected_server_and_client_session as connect


def setup():
    work = tempfile.mkdtemp(prefix="cap-full-")
    gd = os.path.join(work, "stasima.git")
    sp.run(["git", "init", "--bare", "-q", gd], check=True)
    store = LocalCapStore(gd, approvers={"practitioner"})
    index, emb, audit = SqliteMapIndex(":memory:"), StubEmbedder(dim=64), SqliteAuditLog(":memory:")
    # bootstrap a canon entry and index it (simulating the initial/promotion index pass)
    env = {"type": "kno", "title": "No silent loss", "status": "active", "tags": ["durability"]}
    body = "Durability: the git substrate must never silently lose committed work or history."
    store.bootstrap_canon({"practice/no-silent-loss.md": compose_entry(env, body).encode()}, "Bootstrap canon")
    index_entry(index, emb, ref="refs/heads/main", path="practice/no-silent-loss.md", is_canon=True,
                authoring_instance="practitioner", content_oid=store.resolve_ref("refs/heads/main"),
                envelope=env, body=body)
    return store, index, emb, audit


def payload(res):
    sc = getattr(res, "structuredContent", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    txt = "".join(getattr(c, "text", "") for c in res.content)
    try:
        return json.loads(txt)
    except Exception:
        return txt


async def main():
    store, index, emb, audit = setup()
    mcp = build_server(store, index, emb, audit, DefaultPolicy())
    async with connect(mcp) as client:   # default: tool errors come back as CallToolResult(isError=True)
        names = sorted(t.name for t in (await client.list_tools()).tools)
        print(f"{len(names)} tools:", ", ".join(names))

        await client.call_tool("announce", {"instance_id": "research-2"})

        # author into perspectives (indexed inline)
        await client.call_tool("kip_commit", {"instance_id": "research-2", "domain": "practice",
            "slug": "durability-notes", "body": "Notes on durability and never losing committed work; append-only git history.",
            "op_id": "op-1", "title": "Durability notes", "references": ["practice/no-silent-loss.md"]})
        await client.call_tool("kip_commit", {"instance_id": "research-7", "domain": "practice",
            "slug": "scaling-notes", "body": "Scaling throughput and performance under concurrent request load.",
            "op_id": "op-2", "title": "Scaling notes"})

        # search — attributed, scoped
        allhits = payload(await client.call_tool("map_search", {"instance_id": "research-2",
            "query": "how do we avoid losing committed work durability", "scope": "all"}))
        print("map_search all:")
        for h in allhits:
            print(f"   {h['score']:>6}  {h['author']:12} canon={h['is_canon']!s:5} {h['path']}")
        canon_only = payload(await client.call_tool("map_search", {"instance_id": "research-2",
            "query": "durability", "scope": "canon"}))
        mine7 = payload(await client.call_tool("map_search", {"instance_id": "research-7",
            "query": "durability", "scope": "mine"}))
        print("map_search canon:", [h["path"] for h in canon_only])
        print("map_search mine(r-7):", [h["path"] for h in mine7])

        mp = payload(await client.call_tool("my_perspective", {"instance_id": "research-2"}))
        got = payload(await client.call_tool("kip_get", {"ref": "research-2", "path": "practice/durability-notes.md"}))
        print("my_perspective:", mp["entries"])

        # reconcile with canon before proposing (the coherence gate now requires it)
        await client.call_tool("canon_diff", {"instance_id": "research-2"})
        await client.call_tool("sup_reconcile", {"instance_id": "research-2", "body": "Read current canon."})

        # propose + track
        await client.call_tool("propose", {"instance_id": "research-2", "proposal_id": "p-1",
            "domain": "practice", "slug": "principle-durability", "body": "Promote durability to a stated principle.",
            "op_id": "op-3", "title": "Durability principle"})
        cp = payload(await client.call_tool("conflict_preview", {"proposal_id": "p-1"}))
        ps = payload(await client.call_tool("proposal_status", {"proposal_id": "p-1"}))
        print("conflict_preview:", cp, "| proposal_status:", ps["status"])

        # message multiple recipients, flag, inbox, read
        await client.call_tool("imp_send", {"sender": "research-2", "recipients": ["research-7", "recto"],
            "subject": "Durability is load-bearing", "body": "Look before proposing scaling changes.",
            "op_id": "m-1", "coordinates": ["practice/no-silent-loss.md"]})
        flag7 = payload(await client.call_tool("imp_flags", {"instance_id": "research-7"}))
        inbox7 = payload(await client.call_tool("imp_check", {"instance_id": "research-7"}))
        print("imp_flags r-7:", flag7, "| inbox r-7:", [(m["from"], m["subject"], m["coordinates"]) for m in inbox7])
        await client.call_tool("imp_mark_read", {"instance_id": "research-7", "message_path": "messages/m-1.md"})
        after = payload(await client.call_tool("imp_flags", {"instance_id": "research-7"}))
        recto = payload(await client.call_tool("imp_check", {"instance_id": "recto"}))
        print("imp_flags r-7 after read:", after, "| inbox recto:", [m["path"] for m in recto])

        # the bug fix: read-state lives in the audit log, so a reindex must NOT wipe it
        reindex_from_git(store, index, emb)
        after_reindex = payload(await client.call_tool("imp_flags", {"instance_id": "research-7"}))
        ok, bad = audit.verify()
        print("after reindex -> imp_flags r-7:", after_reindex, "| audit:", audit.count(), "verify:", (ok, bad))

        # authz seam: a message via kip_commit is denied (use imp_send), and the denial is audit-logged
        res = await client.call_tool("kip_commit", {"instance_id": "research-2", "domain": "messages",
                                                    "slug": "x", "body": "y", "op_id": "op-deny"})
        denied = bool(getattr(res, "isError", False))
        denial_logged = any(e["outcome"] == "denied" for e in audit.events())
        print("kip_commit into messages/ denied:", denied, "| denial audit-logged:", denial_logged)

        # ---- assertions ----
        paths = [h["path"] for h in allhits]
        assert "practice/durability-notes.md" in paths and "practice/no-silent-loss.md" in paths
        assert "messages/m-1.md" not in paths
        assert paths.index("practice/durability-notes.md") < paths.index("practice/scaling-notes.md")
        assert [h["path"] for h in canon_only] == ["practice/no-silent-loss.md"]
        assert all(h["author"] == "research-7" for h in mine7)
        assert mp["entries"] == ["practice/durability-notes.md"]
        assert "Notes on durability" in got
        assert cp["conflicts"] is False and ps["status"] == "pending"
        assert flag7["unread"] == 1 and after["unread"] == 0
        assert len(recto) == 1
        assert after_reindex["unread"] == 0, "read-state must survive a reindex (it lives in the audit log)"
        assert ok and audit.count() >= 4, "writes were audit-logged and the chain verifies"
        assert denied and denial_logged, "authz seam denies + logs a message sent via kip_commit"
        print("\nOK -- full loop verified end to end through MCP (audit-logged; read-state survives reindex; authz seam active).")


anyio.run(main)
