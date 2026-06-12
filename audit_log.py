"""
Audit log — the operation-layer TRUTH, complementary to git's content-layer truth.

Records what git can't: ops that produce no commit (read-receipts, denials), the order/timing
of operations, and outcomes (ok/error). Append-only, hash-chained. SQLite is its source of truth
(git stays source of truth for the information itself).

Scope: writes (state changes) and failures (what's breaking). Successful reads are observability,
not logged; read-state IS logged (a read-receipt is a write-like, forensic event).

The hash chain is tamper-EVIDENCE at this threat model (cooperative, single practitioner, no crypto):
it detects accidental corruption, deletion, and reordering, and yields one head hash summarizing the
whole history. It is not forgery-proof (no signature to forge) — signing the head is the additive
upgrade. Per-canon-land, the head is anchored into git (replicated, durable), so the git substrate
can witness tampering of the SQLite log.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from local_capstore import Identity, PERSP_PREFIX, PROP_PREFIX

GENESIS = "0" * 64
ANCHOR_REF = "refs/cap/audit-anchor"

_HASHED = ["seq", "ts", "actor", "op", "target_ref", "target_path",
           "op_id", "result_oid", "outcome", "detail", "prev_hash"]


def _canonical(ev: dict) -> str:
    return json.dumps({k: ev.get(k) for k in _HASHED}, sort_keys=True, default=str, separators=(",", ":"))


def _hash(ev: dict) -> str:
    return hashlib.sha256(_canonical(ev).encode()).hexdigest()


class AuditLog(ABC):
    @abstractmethod
    def append(self, actor: str, op: str, *, target_ref=None, target_path=None,
               op_id=None, result_oid=None, outcome="ok", detail=None) -> dict: ...
    @abstractmethod
    def head(self) -> str: ...
    @abstractmethod
    def count(self) -> int: ...
    @abstractmethod
    def verify(self) -> tuple[bool, Optional[int]]: ...
    @abstractmethod
    def head_at(self, seq: int) -> str: ...
    @abstractmethod
    def events(self, *, op=None, actor=None, op_id=None) -> list[dict]: ...
    @abstractmethod
    def append_read(self, instance_id: str, message_path: str) -> dict: ...
    @abstractmethod
    def is_read(self, instance_id: str, message_path: str) -> bool: ...


class SqliteAuditLog(AuditLog):
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
                 seq INTEGER PRIMARY KEY, ts TEXT, actor TEXT, op TEXT,
                 target_ref TEXT, target_path TEXT, op_id TEXT, result_oid TEXT,
                 outcome TEXT, detail TEXT, prev_hash TEXT, hash TEXT)""")
        self.conn.commit()

    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
        return d

    def append(self, actor, op, *, target_ref=None, target_path=None,
               op_id=None, result_oid=None, outcome="ok", detail=None) -> dict:
        ev = {"seq": self.count() + 1,
              "ts": datetime.now(timezone.utc).isoformat(),
              "actor": actor, "op": op, "target_ref": target_ref, "target_path": target_path,
              "op_id": op_id, "result_oid": result_oid, "outcome": outcome,
              "detail": detail or {}, "prev_hash": self.head()}
        ev["hash"] = _hash(ev)
        self.conn.execute(
            "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["seq"], ev["ts"], ev["actor"], ev["op"], ev["target_ref"], ev["target_path"],
             ev["op_id"], ev["result_oid"], ev["outcome"], json.dumps(ev["detail"]), ev["prev_hash"], ev["hash"]))
        self.conn.commit()
        return ev

    def head(self) -> str:
        r = self.conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
        return r["hash"] if r else GENESIS

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]

    def verify(self) -> tuple[bool, Optional[int]]:
        prev = GENESIS
        for r in self.conn.execute("SELECT * FROM audit_events ORDER BY seq"):
            d = self._row(r)
            if d["prev_hash"] != prev:
                return False, d["seq"]
            if _hash(d) != d["hash"]:
                return False, d["seq"]
            prev = d["hash"]
        return True, None

    def head_at(self, seq: int) -> str:
        """Recompute the chain hash up to `seq` from stored fields, chaining the RECOMPUTED prev
        (not the stored prev_hash) so an upstream tamper propagates forward and is detectable."""
        prev = GENESIS
        for r in self.conn.execute("SELECT * FROM audit_events WHERE seq<=? ORDER BY seq", (seq,)):
            d = self._row(r)
            d["prev_hash"] = prev
            prev = _hash(d)
        return prev

    def events(self, *, op=None, actor=None, op_id=None) -> list[dict]:
        where, params = [], []
        for col, val in (("op", op), ("actor", actor), ("op_id", op_id)):
            if val is not None:
                where.append(f"{col}=?"); params.append(val)
        sql = "SELECT * FROM audit_events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY seq"
        return [self._row(r) for r in self.conn.execute(sql, params)]

    def append_read(self, instance_id, message_path) -> dict:
        return self.append(instance_id, "read_receipt", target_path=message_path)

    def is_read(self, instance_id, message_path) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM audit_events WHERE op='read_receipt' AND actor=? AND target_path=? LIMIT 1",
            (instance_id, message_path)).fetchone()
        return r is not None


# ---------------------------------------------------------------- git integration
def reconcile_from_git(store, audit: AuditLog) -> int:
    """git-first-then-audit recovery (CAPstore OQ5): for any committed op_id with no audit event,
    backfill one from the self-describing commit. Closes the tolerated failure (handler died after
    the commit, before the audit append)."""
    canon = store.canon_ref
    refs = ([canon] if store.resolve_ref(canon) else [])
    refs += [r.name for r in store.list_refs(PERSP_PREFIX)]
    refs += [r.name for r in store.list_refs(PROP_PREFIX)]
    known = {e["op_id"] for e in audit.events() if e["op_id"]}
    backfilled = 0
    for ref in refs:
        for c in store.commit_ops(ref):
            if c["op_id"] and c["op_id"] not in known:
                audit.append(c["author"], "reconciled_commit", target_ref=ref,
                             op_id=c["op_id"], result_oid=c["oid"], detail={"reconciled": True})
                known.add(c["op_id"])
                backfilled += 1
    return backfilled


def anchor_audit_head(store, audit: AuditLog, anchor_ref: str = ANCHOR_REF) -> dict:
    """Write the current chain head into git — call on each canon land. Rides the refs/cap/*
    sync refspec, so the anchor replicates to any mirror. git then witnesses the SQLite log's integrity."""
    payload = {"seq": audit.count(), "head": audit.head()}
    store.commit(anchor_ref, {"audit-head.json": json.dumps(payload, separators=(",", ":")).encode()},
                 f"audit anchor @ seq {payload['seq']}", Identity("system"),
                 expected_parent=store.resolve_ref(anchor_ref), op_id=f"anchor-{payload['head'][:12]}")
    return payload


def verify_against_anchor(store, audit: AuditLog, anchor_ref: str = ANCHOR_REF) -> Optional[bool]:
    """True/False if the SQLite log still matches the git-anchored checkpoint; None if no anchor yet.
    Recomputes the chain up to the anchored seq and compares to the git-stored head — so tampering of
    the SQLite truth is caught by the replicated git substrate."""
    if store.resolve_ref(anchor_ref) is None:
        return None
    anchor = json.loads(store.read_blob(anchor_ref, "audit-head.json").decode())
    return audit.head_at(anchor["seq"]) == anchor["head"]
