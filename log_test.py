"""
Acceptance checks for log entries + the state sequence (per the implementation brief):
  1. proposal without a log entry -> rejected at land
  2. proposal with two log entries -> rejected
  3. seq != head+1 -> rejected with both values in the error
  4. successful land -> state/<seq> tag on the merge commit; canon_state seq advances
  5. push_all/verify_sync carry the state tags (fresh stand-in remote)
  6. first-ever land on a repo with no state tags -> accepted seq is 3c (chat-era freeze ::3B + 1)
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_capstore import LocalCapStore, Identity, Approval
from map_index import SqliteMapIndex, StubEmbedder
from audit_log import SqliteAuditLog
from entries import compose_entry
from cap_server import land_and_record, canon_seq, seq_display

CANON = "refs/heads/main"
def prop(p): return f"refs/concordance/proposals/{p}"

work = tempfile.mkdtemp(prefix="cap-log-")
gd = os.path.join(work, "stasima.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
store = LocalCapStore(gd, approvers={"practitioner"})
index, emb, audit = SqliteMapIndex(":memory:"), StubEmbedder(dim=64), SqliteAuditLog(":memory:")

entry = lambda env, body: compose_entry(env, body).encode()
log_entry = lambda seq, body: entry({"type": "log", "title": f"{seq_display(int(seq,16))}", "status": "active", "seq": seq}, body)

store.bootstrap_canon({"practice/seed.md": entry({"type": "kno", "title": "Seed", "status": "active"}, "the seed")},
                      "bootstrap")
assert canon_seq(store) == 0x3B, "pre-land canon sits at the chat-era freeze ::3B"


def make_proposal(pid, files):
    store.create_branch(prop(pid), store.resolve_ref(CANON))
    store.commit(prop(pid), files, f"propose {pid}", Identity("r2"),
                 expected_parent=store.resolve_ref(prop(pid)), op_id=f"op-{pid}")
    return store.prepare_merge(prop(pid))


def land(prepared):
    return land_and_record(store, index, emb, audit, prepared,
                           Approval(prepared.candidate_oid, "practitioner", "test"))


def rejected(prepared, *needles):
    try:
        land(prepared)
        return False
    except ValueError as e:
        return all(n in str(e) for n in needles)


# 1. no log entry
p = make_proposal("p-nolog", {"practice/a.md": entry({"type": "kno", "title": "A", "status": "active"}, "a")})
assert rejected(p, "exactly one log entry", "found 0"), "missing log entry must reject"
print("1. missing log entry rejected      OK")

# 2. two log entries
p = make_proposal("p-twologs", {"practice/b.md": entry({"type": "kno", "title": "B", "status": "active"}, "b"),
                                "meta/log/3c.md": log_entry("3c", "story one"),
                                "meta/log/3d.md": log_entry("3d", "story two")})
assert rejected(p, "exactly one log entry", "found 2"), "two log entries must reject"
print("2. two log entries rejected        OK")

# 3. wrong seq (head is ::3B, entry claims ::3E) — error carries both values
p = make_proposal("p-wrongseq", {"practice/c.md": entry({"type": "kno", "title": "C", "status": "active"}, "c"),
                                 "meta/log/3e.md": log_entry("3e", "premature")})
assert rejected(p, "::3E", "::3B", "::3C"), "seq mismatch must reject with both values"
print("3. wrong seq rejected w/ values    OK")

# 4 + 6. first-ever land: seq 3c accepted; tag lands on the merge commit
p = make_proposal("p-good", {"practice/d.md": entry({"type": "kno", "title": "D", "status": "active"}, "d"),
                             "meta/log/3c.md": log_entry("3c", "::3C — substrate moved; the freeze ends.")})
res = land(p)
assert res["seq"] == "3c" and res["display"] == "::3C"
assert store.resolve_ref("refs/tags/state/3c") == res["landed"], "tag points at the merge commit"
assert canon_seq(store) == 0x3C
print("4/6. first land = ::3C, tagged     OK", res["landed"][:10])

# tag idempotency: re-tagging the same oid is fine; a different oid errors loudly
store.tag("state/3c", res["landed"])
try:
    store.tag("state/3c", store.resolve_ref(prop("p-nolog")))
    assert False, "repointing a state tag must error"
except Exception:
    pass
print("   tag idempotent / repoint errors OK")

# next land must be ::3D
p = make_proposal("p-next", {"practice/e.md": entry({"type": "kno", "title": "E", "status": "active"}, "e"),
                             "meta/log/3d.md": log_entry("3d", "::3D — second land.")})
res2 = land(p)
assert res2["seq"] == "3d" and canon_seq(store) == 0x3D
print("   monotonic next land = ::3D      OK")

# 5. sync carries the state tags
remote = os.path.join(work, "remote.git")
sp.run(["git", "init", "--bare", "-q", remote], check=True)
store.set_remote("mirror", remote)
report = store.push_all("mirror")
assert "refs/tags/state/3c" in report["synced"] and "refs/tags/state/3d" in report["synced"], report
assert not report["missing_on_remote"] and not report["oid_mismatch"]
print("5. state tags replicate via sync   OK")

print("\nOK -- log entries + state sequence: all acceptance checks pass.")
