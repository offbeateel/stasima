# SPDX-License-Identifier: Apache-2.0
"""
Authorization — the policy seam every mutating op crosses before its handler acts.

v1 is SEAM-ONLY (single-practitioner, cooperative): identity is the caller's self-asserted name,
so this does NOT prevent spoofing — that's identity-binding, deferred to 1.x (multi-user + GitHub
sync, where the trust boundary widens). What the seam does now:
  - makes the structural lanes explicit (defense in depth over what CAPstore already enforces),
  - adds a couple of guardrails reachable from the tool surface,
  - gives the middleware hook a table-driven policy + identity-binding slot into later,
  - feeds denials to the audit log.

CAPstore stays mechanism-not-policy: authz runs BEFORE the handler calls the store.
"""
from abc import ABC, abstractmethod
from typing import Optional

CANON_REF = "refs/heads/main"
PERSP_PREFIX = "refs/cap/perspectives/"
WRITE_OPS = {"kip_commit", "propose", "imp_send"}


class Denied(Exception):
    """Raised by a policy when an op is not permitted. Surfaced to the caller and audit-logged."""


class Authz(ABC):
    @abstractmethod
    def check(self, identity: str, op: str,
              target_ref: Optional[str] = None, target_path: Optional[str] = None) -> None:
        """Return None if allowed; raise Denied if not. Called before every mutating handler."""


class DefaultPolicy(Authz):
    """Cooperative single-practitioner defaults. A TablePolicy (per-instance namespaces/ops) and
    identity-binding replace/extend this in 1.x without changing the call sites."""

    def __init__(self, canon_ref: str = CANON_REF, persp_prefix: str = PERSP_PREFIX):
        self.canon_ref = canon_ref
        self.persp_prefix = persp_prefix

    def check(self, identity, op, target_ref=None, target_path=None):
        if op not in WRITE_OPS:
            return  # reads are open (you may read any layer)
        if target_ref == self.canon_ref:
            raise Denied("canon is human-gated — propose instead of writing it directly")
        if op == "kip_commit" and (target_path or "").startswith("messages/"):
            raise Denied("messages must be sent via imp_send (so they get recipients + inbox indexing)")
        if (target_ref and target_ref.startswith(self.persp_prefix)
                and target_ref != self.persp_prefix + identity):
            raise Denied(f"{identity} may write only its own perspective, not {target_ref}")
        return
