# SPDX-License-Identifier: Apache-2.0
"""
Airlock acceptance checks (per the implementation brief), with an injectable clock:
  1. two codes from the same window -> land rejected (strict ordering; tested with floor=0 to
     isolate ordering, since the default floor makes same-window landing unreachable)
  2. land at +40s with a fresh next-window code -> rejected (floor), both values in the error
  3. land at >=+2m with a fresh code -> succeeds; full land_and_record chain (state tag, seq)
  4. mutation against a staged proposal -> rejected (frozen)
  5. correct code, wrong oid prefix -> rejected (content-binding)
  6. replayed window for the same purpose -> rejected; acceptances + rejection visible in audit
  7. staged past the ceiling -> lazily auto-reverted; proposal writable again
  8. abort requires no code; proposal returns to open with entries intact
  9. console land byte-identical -> admin_test (unchanged) is the regression evidence
Plus: RFC 6238 test vector, and the practitioner_attention field.
"""
import base64
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
from airlock import Airlock, AirlockError, totp_at, verify_code, generate_secret
from cap_server import build_server, compose_entry, land_and_record, _validate_log_entry, canon_seq
from mcp.shared.memory import create_connected_server_and_client_session as connect

CANON = "refs/heads/main"

# ---- RFC 6238 vector: secret "12345678901234567890", T=59 -> step 1 -> 94287082 (6-digit: 287082)
rfc = base64.b32encode(b"12345678901234567890").decode()
assert totp_at(rfc, 59 // 30) == "287082", "RFC 6238 vector failed"
assert verify_code(rfc, "287082", 59) == 1 and verify_code(rfc, "000000", 59) is None
print("rfc-6238 vector     OK")


class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


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


def errtext(res):
    return "".join(getattr(c, "text", "") for c in res.content)


async def main():
    work = tempfile.mkdtemp(prefix="cap-airlock-")
    gd = os.path.join(work, "stasima.git")
    sp.run(["git", "init", "--bare", "-q", gd], check=True)
    store = LocalCapStore(gd, approvers={"practitioner"})
    index, emb, audit = SqliteMapIndex(":memory:"), StubEmbedder(dim=64), SqliteAuditLog(":memory:")
    env = {"type": "kno", "title": "Seed", "status": "active"}
    store.bootstrap_canon({"practice/seed.md": compose_entry(env, "the seed").encode()}, "bootstrap")
    index_entry(index, emb, ref=CANON, path="practice/seed.md", is_canon=True, authoring_instance="practitioner",
                content_oid=store.resolve_ref(CANON), envelope=env, body="the seed")

    secret_path = os.path.join(work, "totp.secret")
    with open(secret_path, "w") as f:
        f.write(generate_secret())
    secret = open(secret_path).read().strip()
    clock = Clock()
    code = lambda: totp_at(secret, int(clock.t // 30))      # the code the practitioner's app shows "now"

    land_fn = lambda prepared, approval: land_and_record(store, index, emb, audit, prepared, approval)
    validate_fn = lambda prepared: _validate_log_entry(store, prepared)
    mk = lambda floor: Airlock(store, audit, secret_path=secret_path, land_fn=land_fn,
                               validate_fn=validate_fn, approver="practitioner",
                               floor_s=floor, ceiling_s=7200, clock=clock)
    airlock = mk(120)

    mcp = build_server(store, index, emb, audit, DefaultPolicy(), airlock)
    async with connect(mcp) as client:
        async def call(name, **kw):
            return await client.call_tool(name, kw)

        # arrive, reconcile, author two proposals (each with log entry ::3C — both valid pre-land)
        await call("canon_diff", instance_id="r2")
        await call("sup_reconcile", instance_id="r2", body="read canon")
        for pid, slug in (("p-1", "alpha"), ("p-2", "beta")):
            assert not err(await call("propose", instance_id="r2", proposal_id=pid, domain="practice",
                                      slug=slug, body=f"{slug} entry", op_id=f"{pid}-1"))
            assert not err(await call("propose", instance_id="r2", proposal_id=pid, domain="meta/log",
                                      slug="3c", body="::3C — first land.", op_id=f"{pid}-log",
                                      type="log", seq="3c"))

        # stage p-1 (code 1 at window W0)
        st = payload(await call("stage_approve", proposal_id="p-1", code=code()))
        assert len(st["staged_oid"]) == 40 and st["log_seq"] == "3c"
        print("stage p-1           OK", st["staged_oid"][:10])

        # (4) frozen: mutation against staged p-1 rejected
        r = await call("propose", instance_id="r2", proposal_id="p-1", domain="practice",
                       slug="late", body="late", op_id="late-1")
        assert err(r) and "frozen" in errtext(r)
        r = await call("propose_retract", instance_id="r2", proposal_id="p-1",
                       path="practice/alpha.md", op_id="late-2")
        assert err(r) and "frozen" in errtext(r)
        print("freeze (staged)     OK")

        # (2) floor: +40s, fresh next-window code -> rejected with both values
        clock.advance(40)
        r = await call("land_approve", staged_oid_prefix=st["staged_oid"][:12], code=code())
        assert err(r) and "40" in errtext(r) and "120" in errtext(r), errtext(r)
        print("floor reject @+40s  OK")

        # (5) content-binding: valid code, wrong oid -> no match, fails closed (and burns no code)
        r = await call("land_approve", staged_oid_prefix="deadbeef00", code=code())
        assert err(r) and "content-binding" in errtext(r)
        print("content-binding     OK")

        # (1) strict ordering, isolated with floor=0: stage p-2 and try to land in the SAME window
        free = mk(0)
        st2 = free.stage("p-2", code())                     # code (window W) consumed for 'stage'
        try:
            free.land(st2["staged_oid"][:12], code())       # same window W for 'land' -> not strictly later
            assert False, "same-window land must be rejected"
        except AirlockError as e:
            assert "strictly later" in str(e)
        print("strict ordering     OK")

        # (8) abort: no code, back to open, entries intact
        out = payload(await call("stage_revert", proposal_id="p-2"))
        assert out["state"] == "open"
        assert "practice/beta.md" in store.list_paths("refs/cap/proposals/p-2")
        assert not err(await call("propose", instance_id="r2", proposal_id="p-2", domain="practice",
                                  slug="gamma", body="writable again", op_id="p-2-3"))
        print("abort (free)        OK")

        # (6) replay: re-stage p-2 with the SAME window's code -> consume-once rejects
        try:
            free.stage("p-2", code())
            assert False, "replayed stage window must be rejected"
        except AirlockError as e:
            assert "consume-once" in str(e)
        accepts = [e for e in audit.events(op="totp_accept")]
        rejects = [e for e in audit.events(op="totp_reject")]
        assert accepts and rejects, "acceptances and rejections both visible in audit"
        clock.advance(30)                                    # fresh window -> re-stage works
        st2b = free.stage("p-2", code())
        print("replay reject       OK (re-stage with fresh code OK)")

        # (3) land p-1 past the floor with a fresh code -> full chain
        clock.advance(60)                                    # now +130s since p-1 staged
        out = payload(await call("land_approve", staged_oid_prefix=st["staged_oid"][:12], code=code()))
        assert out["seq"] == "3c" and out["display"] == "::3C"
        assert store.resolve_ref("refs/tags/state/3c") == out["landed"]
        assert canon_seq(store) == 0x3C
        print("land @+130s         OK ->", out["display"])

        # (7) ceiling: p-2 (staged at +70s) blows past the ceiling -> lazy auto-revert, writable
        clock.advance(7300)
        s = airlock.state("p-2")
        assert s["state"] == "open" and s.get("reverted") == "ttl"
        ttl_events = [e for e in audit.events(op="airlock_revert") if e["detail"].get("reason") == "ttl"]
        assert ttl_events, "ttl revert recorded in audit"
        print("ceiling auto-revert OK")

        # practitioner_attention rides canon_state + announce
        await call("imp_send", sender="r2", recipients=["practitioner"], subject="look here",
                   body="attn", op_id="m-1")
        cs = payload(await call("canon_state"))
        assert cs["practitioner_attention"] == 1, cs
        an = payload(await call("announce", instance_id="r2"))
        assert an["practitioner_attention"] == 1
        await call("imp_mark_read", instance_id="practitioner", message_path="messages/m-1.md")
        assert payload(await call("canon_state"))["practitioner_attention"] == 0
        print("attention flag      OK")

        ok, bad = audit.verify()
        assert ok, (ok, bad)
        print("\nOK -- airlock: ordering, floor, content-binding, freeze, replay, ceiling, abort, attention.")


anyio.run(main)
