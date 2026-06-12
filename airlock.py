"""
Airlock — TOTP two-phase remote approval (the mediated-channel counterpart of console `land`).

At the console, the console itself is the out-of-band channel, so admin `land` stays a single
approval. When the practitioner approves *through an instance conversation* (phone, relay), the
channel is the thing to defend against: the airlock binds presence-proofs (TOTP codes) to the
existing prepare/land two-phase gate so no single code — harvested, relayed, or replayed — can
both stage and land, and nothing can land unreviewed.

    open --code 1--> staged --(review: floor..ceiling)--code 2--> landed
      ^                |
      +---- revert ----+    (abort: FREE, no code; TTL expiry: lazy auto-revert)

Why the 120s floor: a TOTP code lives at most ~90s (30s step, +/-1 window acceptance), so any code
visible at staging time is arithmetically dead by the earliest legal landing moment. Strict window
ordering (code 2 strictly later than code 1) and consume-once (a window number is never accepted
twice for one purpose) close the same-window and replay paths as defense in depth. Content-binding
(landing names the staged oid) means what lands is exactly what was staged — a swap fails closed.

Abort is free by design: charging presence-proof to *decline* would incentivize landing.

Honest residual: the practitioner's view of what was staged flows through the relaying instance.
Content-binding makes swap-after-stage impossible and the audit trail makes deception detectable
after the fact; it does NOT make the relay's display trustworthy in the moment. The console remains
the stronger channel.

State (open | staged | landed, with revert folding back to open) is derived from the audit log —
staging is operational, not content; nothing here touches the storage spine. The clock is
injectable for tests; server time is authoritative for every gate.
"""
import base64
import hashlib
import hmac
import os
import struct
import time

from local_capstore import MergePreparation, MergeSummary, Approval, PROP_PREFIX as PROP

STEP = 30                      # RFC 6238 time step (seconds)
DIGITS = 6


class AirlockError(Exception):
    """A gate refused. The message names the failed gate and both values where applicable."""


# ---------------------------------------------------------------- TOTP (RFC 6238, stdlib only)
def generate_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode()


def otpauth_uri(secret: str, label: str = "Stasima:practitioner", issuer: str = "Stasima") -> str:
    return (f"otpauth://totp/{label}?secret={secret}&issuer={issuer}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def totp_at(secret: str, window: int) -> str:
    """The code for an absolute window number (window = unix_time // STEP)."""
    key = base64.b32decode(secret.strip(), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", window), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return f"{code:0{DIGITS}d}"


def verify_code(secret: str, code: str, now: float):
    """Accept the current window +/-1 (clock skew). Returns the MATCHED window number, else None."""
    w = int(now // STEP)
    for cand in (w, w - 1, w + 1):
        if hmac.compare_digest(totp_at(secret, cand), str(code).strip()):
            return cand
    return None


# ---------------------------------------------------------------- the gate
class Airlock:
    def __init__(self, store, audit, *, secret_path, land_fn, validate_fn, approver,
                 floor_s: int = 120, ceiling_s: int = 7200, clock=time.time, prop_prefix: str = PROP):
        # floor must exceed worst-case code lifetime (STEP + one window of skew acceptance ~= 90s)
        # so that no code obtained at staging survives to the landing moment.
        self.store = store
        self.audit = audit
        self.secret_path = secret_path
        self.land_fn = land_fn          # (prepared, approval) -> land_and_record result
        self.validate_fn = validate_fn  # (prepared) -> log-entry seq (early feedback; land re-validates)
        self.approver = approver
        self.floor_s = floor_s
        self.ceiling_s = ceiling_s
        self.clock = clock
        self.prop_prefix = prop_prefix

    # ---- secret + code consumption ----
    def _secret(self) -> str:
        if not os.path.exists(self.secret_path):
            raise AirlockError("airlock not provisioned — run: admin totp-provision")
        with open(self.secret_path, encoding="utf-8") as f:
            return f.read().strip()

    def _consume(self, code, purpose, obj, min_window=None) -> int:
        """Verify a code and burn its window for this purpose. Codes are never logged — windows are."""
        w = verify_code(self._secret(), code, self.clock())
        if w is None:
            self.audit.append("system", "totp_reject", detail={"purpose": purpose, "object": obj,
                                                               "reason": "invalid"})
            raise AirlockError("invalid code")
        if min_window is not None and w <= min_window:
            self.audit.append("system", "totp_reject", detail={"purpose": purpose, "object": obj,
                                                               "window": w, "min_window": min_window,
                                                               "reason": "not strictly later"})
            raise AirlockError(f"code is from window {w}, which is not strictly later than the staging "
                               f"window {min_window} — wait for a fresh code")
        for e in self.audit.events(op="totp_accept"):
            if e["detail"].get("window") == w and e["detail"].get("purpose") == purpose:
                self.audit.append("system", "totp_reject", detail={"purpose": purpose, "object": obj,
                                                                   "window": w, "reason": "replay"})
                raise AirlockError(f"a code from window {w} was already used for {purpose} (consume-once)")
        self.audit.append("practitioner", "totp_accept", detail={"window": w, "purpose": purpose, "object": obj})
        return w

    # ---- state machine (derived from the audit log; TTL is lazy) ----
    def _fold(self, proposal_id):
        cur, ev = "open", None
        full_ref = self.prop_prefix + proposal_id
        for e in self.audit.events():
            d = e["detail"]
            if e["op"] == "airlock_stage" and d.get("proposal") == proposal_id:
                cur, ev = "staged", e
            elif e["op"] == "airlock_revert" and d.get("proposal") == proposal_id:
                cur, ev = "open", e
            elif e["op"] == "land_merge" and d.get("proposal") == full_ref:
                cur, ev = "landed", e
        return cur, ev

    def state(self, proposal_id) -> dict:
        st, e = self._fold(proposal_id)
        if st == "staged":
            d = e["detail"]
            if self.clock() - d["staged_at"] > self.ceiling_s:   # lazy TTL: observed -> reverted
                self.audit.append("system", "airlock_revert", target_ref=self.prop_prefix + proposal_id,
                                  detail={"proposal": proposal_id, "reason": "ttl",
                                          "staged_oid": d["staged_oid"]})
                return {"state": "open", "reverted": "ttl"}
            return {"state": "staged", "staged_oid": d["staged_oid"], "staged_at": d["staged_at"],
                    "window": d["window"], "changed_paths": d.get("changed_paths", []),
                    "lands_after": d["staged_at"] + self.floor_s,
                    "expires_at": d["staged_at"] + self.ceiling_s}
        return {"state": st}

    def staged(self) -> list:
        """Currently staged proposals (cockpit view)."""
        return [{"proposal_id": pid, "staged_oid": st["staged_oid"],
                 "lands_after": st["lands_after"], "expires_at": st["expires_at"]}
                for pid, st in self._staged_proposals()]

    def _staged_proposals(self):
        seen, out = set(), []
        for e in self.audit.events(op="airlock_stage"):
            pid = e["detail"]["proposal"]
            if pid in seen:
                continue
            seen.add(pid)
            st = self.state(pid)
            if st["state"] == "staged":
                out.append((pid, st))
        return out

    # ---- the three ops ----
    def stage(self, proposal_id, code) -> dict:
        """Code 1: freeze the proposal, prepare the merge, start the review clock."""
        if self.state(proposal_id)["state"] == "staged":
            raise AirlockError(f"{proposal_id} is already staged")
        # prove the proposal stageable BEFORE consuming the code — failures must not burn windows
        prepared = self.store.prepare_merge(self.prop_prefix + proposal_id, self.store.canon_ref)
        seq = self.validate_fn(prepared)
        w = self._consume(code, "stage", proposal_id)
        staged_at = self.clock()
        self.audit.append("practitioner", "airlock_stage", target_ref=self.prop_prefix + proposal_id,
                          result_oid=prepared.candidate_oid,
                          detail={"proposal": proposal_id, "staged_oid": prepared.candidate_oid,
                                  "window": w, "staged_at": staged_at, "seq": seq,
                                  "changed_paths": prepared.summary.changed_paths})
        return {"proposal_id": proposal_id, "staged_oid": prepared.candidate_oid,
                "staged_at": staged_at, "lands_after": staged_at + self.floor_s,
                "expires_at": staged_at + self.ceiling_s,
                "changed_paths": prepared.summary.changed_paths, "log_seq": seq}

    def land(self, staged_oid_prefix, code) -> dict:
        """Code 2: after the review floor, strictly later window, bound to the staged content."""
        if len(str(staged_oid_prefix)) < 8:
            raise AirlockError("staged oid prefix too short — give at least 8 hex characters")
        matches = [(pid, st) for pid, st in self._staged_proposals()
                   if st["staged_oid"].startswith(staged_oid_prefix)]
        if not matches:
            raise AirlockError(f"no staged proposal matches oid prefix {staged_oid_prefix!r} (content-binding)")
        if len(matches) > 1:
            raise AirlockError(f"oid prefix {staged_oid_prefix!r} is ambiguous across staged proposals")
        pid, st = matches[0]
        elapsed = self.clock() - st["staged_at"]
        if elapsed < self.floor_s:   # gates before code verification — a floor miss must not burn a window
            raise AirlockError(f"review floor not met: {elapsed:.0f}s since staging, floor is "
                               f"{self.floor_s}s — review, then retry with a fresh code")
        w2 = self._consume(code, "land", pid, min_window=st["window"])
        prepared = MergePreparation(candidate_oid=st["staged_oid"], into=self.store.canon_ref,
                                    proposal_ref=self.prop_prefix + pid,
                                    summary=MergeSummary(st.get("changed_paths", []), [], []))
        return self.land_fn(prepared, Approval(st["staged_oid"], self.approver, f"airlock-totp-w{w2}"))

    def revert(self, proposal_id) -> dict:
        """Abort a staged review. FREE — never requires a code (charging for decline would
        incentivize landing). Returns the proposal to open with its entries intact."""
        if self.state(proposal_id)["state"] != "staged":
            raise AirlockError(f"{proposal_id} is not staged")
        self.audit.append("system", "airlock_revert", target_ref=self.prop_prefix + proposal_id,
                          detail={"proposal": proposal_id, "reason": "manual"})
        return {"proposal_id": proposal_id, "state": "open"}
