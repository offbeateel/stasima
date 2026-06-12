"""
Proves the audit subsystem standalone: hash chain + verify + tamper-detection, read-state as
append-only events, reconciliation from git (the git-first-then-audit recovery), and the
canon-land git anchor witnessing SQLite tampering.
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_capstore import LocalCapStore, Identity
from audit_log import SqliteAuditLog, reconcile_from_git, anchor_audit_head, verify_against_anchor

def persp(i): return f"refs/cap/perspectives/{i}"

# ============================================================ 1. chain, read-state, tamper
print("== chain integrity + read-state + tamper ==")
a = SqliteAuditLog(":memory:")
a.append("research-2", "kip_commit", target_ref=persp("research-2"), op_id="op-1", result_oid="oid1")
a.append("research-2", "imp_send", target_path="messages/m-1.md", op_id="op-2", result_oid="oid2",
         detail={"recipients": ["research-7"]})
e3 = a.append("research-7", "kip_commit", target_ref=persp("research-7"), op_id="op-3", outcome="error:StaleRef")
ok, bad = a.verify()
head_ok = a.head() == e3["hash"]   # capture before append_read moves the head
print("  verify clean:", (ok, bad), "| head==last:", head_ok)

a.append_read("research-7", "messages/m-1.md")
print("  is_read r-7:", a.is_read("research-7", "messages/m-1.md"),
      "| is_read recto:", a.is_read("recto", "messages/m-1.md"))

a.conn.execute("UPDATE audit_events SET actor='mallory' WHERE seq=1")  # out-of-band tamper
a.conn.commit()
ok2, bad2 = a.verify()
print("  verify after tamper:", (ok2, bad2))

assert (ok, bad) == (True, None) and head_ok
assert a.is_read("research-7", "messages/m-1.md") and not a.is_read("recto", "messages/m-1.md")
assert ok2 is False and bad2 == 1

# ============================================================ 2. reconcile from git
print("\n== reconcile (git-first-then-audit recovery) ==")
work = tempfile.mkdtemp(prefix="cap-audit-")
gd = os.path.join(work, "stasima.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
store = LocalCapStore(gd, approvers={"practitioner"})

ref = persp("research-2")
ra = store.commit(ref, {"practice/a.md": b"alpha"}, "KIP a", Identity("research-2"),
                  expected_parent=None, op_id="op-a")
rb = store.commit(ref, {"practice/b.md": b"beta"}, "KIP b", Identity("research-2"),
                  expected_parent=ra.oid, op_id="op-b")

audit = SqliteAuditLog(":memory:")
audit.append("research-2", "kip_commit", target_ref=ref, op_id="op-a", result_oid=ra.oid)  # op-a logged
# op-b NOT logged — simulate a crash after the commit, before the audit append

n = reconcile_from_git(store, audit)
backfilled = [e["op_id"] for e in audit.events(op="reconciled_commit")]
print("  backfilled:", n, backfilled, "| verify:", audit.verify())
assert n == 1 and backfilled == ["op-b"]
assert audit.verify() == (True, None)

# ============================================================ 3. git anchor witnesses SQLite tamper
print("\n== canon-land anchor vs SQLite tamper ==")
anchor = anchor_audit_head(store, audit)
print("  anchored:", anchor)
print("  verify_against_anchor (intact):", verify_against_anchor(store, audit))
assert verify_against_anchor(store, audit) is True

audit.conn.execute("UPDATE audit_events SET actor='mallory' WHERE seq=1")  # tamper after anchoring
audit.conn.commit()
print("  verify_against_anchor (tampered):", verify_against_anchor(store, audit))
assert verify_against_anchor(store, audit) is False

print("\nOK -- chain, read-state, reconcile, and git-anchored tamper-evidence all verified.")
