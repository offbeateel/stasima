# SPDX-License-Identifier: Apache-2.0
"""
Proves SUP + canon coherence through the MCP client:
  - body-immutability guard on kip_commit (supersede, don't overwrite)
  - the reconcile-before-propose chain: propose blocked -> canon_diff (loads the diff) -> sup_reconcile
    (forced self-report, gated on having pulled) -> propose allowed
  - a canon land re-staleness: propose blocked again -> re-pull -> re-reconcile -> propose allowed
  - three-way agreement (audit canon_pull + reconcile_report, git state/ entry) on the same canon oid
  - sup_state / sup_who / canon_state symmetry
"""
import json
import os
import subprocess as sp
import sys
import tempfile

import anyio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_capstore import LocalCapStore, Identity, Approval
from map_index import SqliteMapIndex, StubEmbedder, index_entry
from audit_log import SqliteAuditLog
from authz import DefaultPolicy
from cap_server import build_server, compose_entry, land_and_record
from mcp.shared.memory import create_connected_server_and_client_session as connect

CANON = "refs/heads/main"
def persp(i): return f"refs/cap/perspectives/{i}"


def payload(res):
    sc = getattr(res, "structuredContent", None)
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and set(sc.keys()) == {"result"} else sc
    txt = "".join(getattr(c, "text", "") for c in res.content)
    try:
        return json.loads(txt)
    except Exception:
        return txt


def err(res):
    return bool(getattr(res, "isError", False))


def setup():
    work = tempfile.mkdtemp(prefix="cap-sup-")
    gd = os.path.join(work, "stasima.git")
    sp.run(["git", "init", "--bare", "-q", gd], check=True)
    store = LocalCapStore(gd, approvers={"practitioner"})
    index, emb, audit = SqliteMapIndex(":memory:"), StubEmbedder(dim=64), SqliteAuditLog(":memory:")
    env = {"type": "kno", "title": "Seed", "status": "active"}
    store.bootstrap_canon({"practice/seed.md": compose_entry(env, "the seed").encode()}, "bootstrap")
    index_entry(index, emb, ref=CANON, path="practice/seed.md", is_canon=True, authoring_instance="practitioner",
                content_oid=store.resolve_ref(CANON), envelope=env, body="the seed")
    return store, index, emb, audit


async def main():
    store, index, emb, audit = setup()
    mcp = build_server(store, index, emb, audit, DefaultPolicy())
    async with connect(mcp) as client:
        async def call(name, **kw):
            return await client.call_tool(name, kw)

        # --- body immutability ---
        assert not err(await call("kip_commit", instance_id="r2", domain="practice", slug="notes", body="Body A", op_id="k1"))
        assert err(await call("kip_commit", instance_id="r2", domain="practice", slug="notes", body="Body B", op_id="k2")), \
            "overwriting an existing body must be denied"
        assert not err(await call("kip_commit", instance_id="r2", domain="practice", slug="notes2", body="Body C", op_id="k3")), \
            "a new slug is fine"
        print("body-immutability   OK")

        # --- reconcile-before-propose ---
        assert err(await call("propose", instance_id="r2", proposal_id="p-1", domain="practice",
                              slug="principle", body="a principle", op_id="pr1")), "propose blocked before reconcile"
        # sup_reconcile without pulling is also blocked
        assert err(await call("sup_reconcile", instance_id="r2", body="skipping the read")), "reconcile needs a pull first"

        cd = payload(await call("canon_diff", instance_id="r2"))
        assert any(c["path"] == "practice/seed.md" for c in cd["changed"]), "first pull loads all canon"
        sr = payload(await call("sup_reconcile", instance_id="r2", body="I've read current canon."))
        old_tip = sr["canon_cursor"]
        assert not err(await call("propose", instance_id="r2", proposal_id="p-1", domain="practice",
                                  slug="principle", body="a principle", op_id="pr1")), "propose allowed after reconcile"
        # every proposal carries its log entry (canon sits at ::3B pre-land, so this one is ::3C)
        assert not err(await call("propose", instance_id="r2", proposal_id="p-1", domain="meta/log",
                                  slug="3c", body="::3C — first land in the new substrate.",
                                  op_id="pr1-log", type="log", seq="3c"))
        print("reconcile->propose  OK")

        # --- a canon land re-staleness (practitioner lands p-1 out of band) ---
        prepared = store.prepare_merge("refs/cap/proposals/p-1")
        land_and_record(store, index, emb, audit, prepared, Approval(prepared.candidate_oid, "practitioner", "cli"))
        new_tip = store.resolve_ref(CANON)
        assert new_tip != old_tip

        assert err(await call("propose", instance_id="r2", proposal_id="p-2", domain="practice",
                              slug="principle2", body="another", op_id="pr2")), "stale again after a land"
        cd2 = payload(await call("canon_diff", instance_id="r2"))
        assert any(c["path"] == "practice/principle.md" for c in cd2["changed"]), "pull loads the landed change"
        payload(await call("sup_reconcile", instance_id="r2", body="Read the new principle; adjusting."))
        assert not err(await call("propose", instance_id="r2", proposal_id="p-2", domain="practice",
                                  slug="principle2", body="another", op_id="pr2")), "propose allowed after re-reconcile"
        print("land->re-reconcile  OK")

        # --- three-way agreement on new_tip ---
        recon = store.read_blob(persp("r2"), f"state/reconciled-{new_tip[:12]}.md").decode()
        assert f"canon_cursor: {new_tip}" in recon, "git entry carries the canon cursor"
        evs = audit.events(actor="r2")
        assert any(e["op"] == "canon_pull" and e["result_oid"] == new_tip for e in evs), "audit pull at new_tip"
        assert any(e["op"] == "reconcile_report" and e["detail"].get("canon_cursor") == new_tip for e in evs), "audit report at new_tip"
        print("three-way agreement OK")

        # --- symmetry reads ---
        ss = payload(await call("sup_state", instance_id="r2"))
        assert ss["current_with_canon"] and any("reconciled-" in p for p in ss["state_entries"])
        sw = payload(await call("sup_who"))
        assert {"instance": "r2", "current_with_canon": True} in sw
        cs = payload(await call("canon_state"))
        assert cs["canon_tip"] == new_tip and len(cs["lands"]) >= 1
        print("sup_state/who/canon OK")

        ok, bad = audit.verify()
        assert ok, (ok, bad)
        print("\nOK -- SUP coherence: immutability, reconcile-gate, re-staleness, three-way agreement, symmetry.")


anyio.run(main)
