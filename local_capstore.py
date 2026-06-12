"""
LocalCapStore — a thin subprocess wrapper around the `git` plumbing commands
proven in spike.sh. No libgit2; every primitive shells out to the git binary
against a BARE repo with no working tree.

This is the artifact's CapStore ABC, local backend, with the typed error model.
Illustrative reference implementation — readable over clever. Requires git >= 2.38.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Mapping, Optional

Oid = str
ZERO = "0" * 40  # sha1 "ref must not exist" sentinel

# the ref layout (single source — everything else imports these)
PERSP_PREFIX = "refs/concordance/perspectives/"
PROP_PREFIX = "refs/concordance/proposals/"


# --- supporting types (from the artifact) -----------------------------------
@dataclass(frozen=True)
class Identity:
    instance_id: str  # the authoring instance's name, as recorded — a claim, not a proof


@dataclass(frozen=True)
class RefInfo:
    name: str
    oid: Oid


@dataclass(frozen=True)
class TreeEntry:
    type: str  # "blob" | "tree"
    oid: Oid
    name: str


@dataclass(frozen=True)
class CommitResult:
    oid: Oid
    ref: str
    parents: list[Oid]
    op_id: str
    author: Identity


@dataclass(frozen=True)
class MergeSummary:
    changed_paths: list[str]
    conflicts: list[str]
    authoring_instances: list[str]


@dataclass(frozen=True)
class MergePreparation:
    candidate_oid: Oid
    into: str
    proposal_ref: str
    summary: MergeSummary


@dataclass(frozen=True)
class Approval:
    candidate_oid: Oid
    approved_by: str
    evidence: str


# --- error taxonomy (from the artifact) -------------------------------------
class CapStoreError(Exception):
    retryable: bool = False


class RefNotFound(CapStoreError): ...
class PathNotFound(CapStoreError): ...
class StaleRef(CapStoreError): retryable = True            # CAS miss
class NonFastForward(CapStoreError): ...                   # append-only violation
class ProtectedRef(CapStoreError): ...                     # write attempt to main
class MergeConflict(CapStoreError): ...
class MergeNotApproved(CapStoreError): ...                 # land_merge gate failed
class BackendUnavailable(CapStoreError): retryable = True  # git/disk failure


# --- the backend ------------------------------------------------------------
class LocalCapStore:
    def __init__(
        self,
        git_dir: str,
        *,
        approvers: set[str],
        canon_ref: str = "refs/heads/main",
        committer: tuple[str, str] = ("capstore", "capstore@stasima.local"),
        author_domain: str = "stasima.local",
        git_bin: str = "git",
    ):
        self.git_dir = git_dir
        self.approvers = approvers
        self.canon_ref = canon_ref
        self.committer = committer
        self.author_domain = author_domain
        self.git_bin = git_bin

    # ---- low-level git invocation ----
    def _run(self, *args: str, input: Optional[bytes] = None, extra_env: Optional[dict] = None):
        env = dict(os.environ)
        env["GIT_DIR"] = self.git_dir
        if extra_env:
            env.update(extra_env)
        p = subprocess.run(
            [self.git_bin, *args],
            input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")

    def _git(self, *args: str, input: Optional[bytes] = None, extra_env: Optional[dict] = None) -> bytes:
        rc, out, err = self._run(*args, input=input, extra_env=extra_env)
        if rc != 0:
            raise BackendUnavailable(f"git {' '.join(args)} -> {rc}: {err.strip()}")
        return out

    def _author_env(self, instance_id: str) -> dict:
        return {
            "GIT_AUTHOR_NAME": instance_id,
            "GIT_AUTHOR_EMAIL": f"{instance_id}@{self.author_domain}",
            "GIT_COMMITTER_NAME": self.committer[0],
            "GIT_COMMITTER_EMAIL": self.committer[1],
        }

    # ---- reads ----
    def resolve_ref(self, ref: str) -> Optional[Oid]:
        rc, out, _ = self._run("rev-parse", "--verify", "--quiet", ref)
        oid = out.decode().strip()
        return oid if rc == 0 and oid else None

    def read_blob_at(self, commit: Oid, path: str) -> bytes:
        rc, out, err = self._run("cat-file", "blob", f"{commit}:{path}")
        if rc != 0:
            raise PathNotFound(f"{path} @ {commit}: {err.strip()}")
        return out

    def read_blob(self, ref: str, path: str) -> bytes:
        if self.resolve_ref(ref) is None:
            raise RefNotFound(ref)
        return self.read_blob_at(ref, path)

    def blob_oid(self, ref: str, path: str) -> Oid:
        """The git object id of the file at (ref, path) — the derived version pin MAP records."""
        rc, out, err = self._run("rev-parse", f"{ref}:{path}")
        if rc != 0:
            raise PathNotFound(f"{path} @ {ref}: {err.strip()}")
        return out.decode().strip()

    def list_refs(self, prefix: str = "") -> list[RefInfo]:
        args = ["for-each-ref", "--format=%(refname)%09%(objectname)"]
        if prefix:
            args.append(prefix)
        out = self._git(*args).decode()
        return [RefInfo(*line.split("\t")) for line in out.splitlines() if line]

    def list_tree(self, ref: str, path: str = "") -> list[TreeEntry]:
        treeish = ref if path == "" else f"{ref}:{path}"
        rc, out, err = self._run("ls-tree", "--format=%(objecttype)\t%(objectname)\t%(path)", treeish)
        if rc != 0:
            raise PathNotFound(f"{treeish}: {err.strip()}")
        return [TreeEntry(*line.split("\t")) for line in out.decode().splitlines() if line]

    def list_paths(self, ref: str, path: str = "") -> list[str]:
        """Recursive list of file paths under a tree (relative to `path`)."""
        treeish = ref if path == "" else f"{ref}:{path}"
        rc, out, err = self._run("ls-tree", "-r", "--name-only", treeish)
        if rc != 0:
            raise PathNotFound(f"{treeish}: {err.strip()}")
        return out.decode().splitlines()

    def history(self, ref: str, path: str) -> list[dict]:
        """Commit trail touching `path`, newest first: oid, author (instance), subject."""
        rc, out, err = self._run("log", "--format=%H%x09%an%x09%s", ref, "--", path)
        if rc != 0:
            raise RefNotFound(f"{ref}: {err.strip()}")
        rows = []
        for line in out.decode().splitlines():
            h, an, s = line.split("\t", 2)
            rows.append({"oid": h, "author": an, "subject": s})
        return rows

    def is_ancestor(self, maybe_ancestor: Oid, descendant: Oid) -> bool:
        rc, _, _ = self._run("merge-base", "--is-ancestor", maybe_ancestor, descendant)
        return rc == 0

    def changed_paths(self, a: Oid, b: Oid) -> list[str]:
        """Paths that differ between two commits — the canon diff an instance reconciles with."""
        rc, out, _ = self._run("diff", "--name-only", a, b)
        return out.decode().split() if rc == 0 else []

    def commit_ops(self, ref: str) -> list[dict]:
        """Every commit reachable from `ref`, with its self-describing op_id + author.
        The raw material for audit reconciliation (the artifact's git-first-then-audit recovery)."""
        rc, out, _ = self._run("rev-list", ref)
        if rc != 0:
            return []
        rows = []
        for oid in out.decode().split():
            author = self._trailer(oid, "instance-id") or self._git("show", "-s", "--format=%an", oid).decode().strip()
            rows.append({"oid": oid, "op_id": self._trailer(oid, "op-id"), "author": author})
        return rows

    def preview_merge(self, proposal_ref: str, into: str = "refs/heads/main") -> MergeSummary:
        """Read-only: what WOULD merging do? Computes the merged tree but creates no
        candidate commit (no dangling objects). `conflicts` is non-empty iff it wouldn't merge clean."""
        into_tip = self.resolve_ref(into)
        if into_tip is None:
            raise RefNotFound(into)
        prop_tip = self.resolve_ref(proposal_ref)
        if prop_tip is None:
            raise RefNotFound(proposal_ref)
        rc, out, err = self._run("merge-tree", "--write-tree", into, proposal_ref)
        text = out.decode()
        if rc not in (0, 1):
            raise BackendUnavailable(f"merge-tree -> {rc}: {err.strip()}")
        lines = text.splitlines()
        conflicts = [l for l in lines[1:] if l.strip()] if rc == 1 else []
        changed = []
        if rc == 0 and lines:
            changed = self._git("diff", "--name-only", into_tip, lines[0].strip()).decode().split()
        authors = sorted(set(self._git("log", "--format=%an", f"{into_tip}..{prop_tip}").decode().split()) - {""})
        return MergeSummary(changed_paths=changed, conflicts=conflicts, authoring_instances=authors)

    # ---- trailer / result helpers ----
    def _trailer(self, commit: Oid, key: str) -> Optional[str]:
        body = self._git("show", "-s", "--format=%b", commit).decode()
        for line in body.splitlines():
            if line.lower().startswith(key.lower() + ":"):
                return line.split(":", 1)[1].strip()
        return None

    def _commit_result(self, oid: Oid, ref: str) -> CommitResult:
        parents = self._git("rev-list", "--parents", "-n", "1", oid).decode().split()[1:]
        inst = self._trailer(oid, "instance-id") or self._git("show", "-s", "--format=%an", oid).decode().strip()
        return CommitResult(oid=oid, ref=ref, parents=parents,
                            op_id=self._trailer(oid, "op-id") or "", author=Identity(inst))

    # ---- tree building (temp index, no working tree) ----
    def _build_tree(self, base: Optional[Oid], changes: Mapping[str, Optional[bytes]]) -> Oid:
        fd, idx = tempfile.mkstemp(prefix="capstore-idx-")
        os.close(fd); os.unlink(idx)                  # git creates the index itself
        env = {"GIT_INDEX_FILE": idx}
        try:
            if base:
                self._git("read-tree", base, extra_env=env)
            else:
                self._git("read-tree", "--empty", extra_env=env)
            for path, content in changes.items():
                if content is None:
                    self._git("update-index", "--force-remove", path, extra_env=env)
                else:
                    blob = self._git("hash-object", "-w", "--stdin", input=content, extra_env=env).decode().strip()
                    self._git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", extra_env=env)
            return self._git("write-tree", extra_env=env).decode().strip()
        finally:
            if os.path.exists(idx):
                os.unlink(idx)

    def _cas_update(self, ref: str, new: Oid, old: Oid) -> None:
        rc, _, err = self._run("update-ref", ref, new, old)
        if rc != 0:
            raise StaleRef(f"{ref}: CAS failed (expected {old}, found {self.resolve_ref(ref)}): {err.strip()}")

    # ---- provisioning (bootstrap corpus; bypasses ProtectedRef only on first creation) ----
    def bootstrap_canon(self, changes: Mapping[str, Optional[bytes]], message: str) -> CommitResult:
        if self.resolve_ref(self.canon_ref) is not None:
            raise ProtectedRef(f"{self.canon_ref} already exists; advance it via land_merge")
        tree = self._build_tree(None, changes)
        oid = self._git("commit-tree", tree, input=(message + "\n\nop-id: bootstrap").encode(),
                        extra_env=self._author_env("practitioner")).decode().strip()
        self._cas_update(self.canon_ref, oid, ZERO)
        return self._commit_result(oid, self.canon_ref)

    # ---- writes (perspectives + proposals only; never canon) ----
    def commit(self, ref: str, changes: Mapping[str, Optional[bytes]], message: str,
               author: Identity, expected_parent: Optional[Oid], op_id: str,
               append_only: bool = True) -> CommitResult:
        if ref == self.canon_ref:
            raise ProtectedRef(f"{ref} advances only via land_merge")

        tip = self.resolve_ref(ref)

        # tip-local idempotency: the op that produced the current tip is being retried (OQ4)
        if tip is not None and self._trailer(tip, "op-id") == op_id:
            return self._commit_result(tip, ref)

        # compare-and-swap precondition
        if expected_parent is None:
            if tip is not None:
                raise StaleRef(f"{ref} exists at {tip}, expected create-only")
        elif tip != expected_parent:
            raise StaleRef(f"{ref} at {tip}, expected {expected_parent}")

        tree = self._build_tree(expected_parent, changes)
        msg = message.rstrip() + f"\n\nop-id: {op_id}\ninstance-id: {author.instance_id}"
        args = ["commit-tree", tree] + (["-p", expected_parent] if expected_parent else [])
        new = self._git(*args, input=msg.encode(), extra_env=self._author_env(author.instance_id)).decode().strip()

        # append-only is structural: the new commit's only parent is the prior tip, so
        # history is never rewritten. The CAS below rejects a concurrent advance (StaleRef).
        self._cas_update(ref, new, expected_parent or ZERO)
        return self._commit_result(new, ref)

    def create_branch(self, ref: str, at: Oid) -> RefInfo:
        rc, _, err = self._run("update-ref", ref, at, ZERO)
        if rc != 0:
            raise StaleRef(f"{ref} already exists or update failed: {err.strip()}")
        return RefInfo(ref, at)

    def tag(self, name: str, at: Oid) -> RefInfo:
        """Lightweight tag (a ref under refs/tags). Create-only; idempotent if it already points at
        `at`; the same name at a DIFFERENT oid is an integrity violation and errors loudly."""
        ref = f"refs/tags/{name}"
        cur = self.resolve_ref(ref)
        if cur == at:
            return RefInfo(ref, at)
        if cur is not None:
            raise CapStoreError(f"{ref} already exists at {cur}; refusing to repoint to {at}")
        self._cas_update(ref, at, ZERO)
        return RefInfo(ref, at)

    # ---- merge to canon (two-phase; the human gate is land_merge) ----
    def prepare_merge(self, proposal_ref: str, into: str = "refs/heads/main") -> MergePreparation:
        into_tip = self.resolve_ref(into)
        if into_tip is None:
            raise RefNotFound(into)
        prop_tip = self.resolve_ref(proposal_ref)
        if prop_tip is None:
            raise RefNotFound(proposal_ref)

        rc, out, err = self._run("merge-tree", "--write-tree", into, proposal_ref)
        text = out.decode()
        if rc == 1:  # conflict: line 0 is the (conflicted) tree, the rest names conflicts
            conflicts = [l for l in text.splitlines()[1:] if l.strip()]
            raise MergeConflict(f"{proposal_ref} into {into}: {conflicts}")
        if rc != 0:
            raise BackendUnavailable(f"merge-tree -> {rc}: {err.strip()}")

        merged_tree = text.splitlines()[0].strip()
        changed = self._git("diff", "--name-only", into_tip, merged_tree).decode().split()
        authors = sorted(set(self._git("log", "--format=%an", f"{into_tip}..{prop_tip}").decode().split()) - {""})
        cand = self._git(
            "commit-tree", merged_tree, "-p", into_tip, "-p", prop_tip,
            input=f"Merge {proposal_ref} into {into}\n\napproved-by: <pending>".encode(),
            extra_env=self._author_env(self.committer[0]),
        ).decode().strip()
        # NOTE: `cand` is durable but unreferenced — nothing points at it until land_merge.
        return MergePreparation(cand, into, proposal_ref,
                                MergeSummary(changed, [], authors))

    def land_merge(self, prepared: MergePreparation, approval: Approval) -> CommitResult:
        if approval.candidate_oid != prepared.candidate_oid:
            raise MergeNotApproved("approval does not bind to the prepared candidate")
        if approval.approved_by not in self.approvers:
            raise MergeNotApproved(f"{approval.approved_by!r} is not a configured approver")
        # CAS on the into tip recorded at prepare time (candidate's first parent).
        base = self._git("rev-parse", f"{prepared.candidate_oid}^1").decode().strip()
        self._cas_update(prepared.into, prepared.candidate_oid, base)  # StaleRef if canon moved since prepare
        return self._commit_result(prepared.candidate_oid, prepared.into)

    # ---- remote sync (off-machine mirror; preserves the custom ref namespaces) ----
    # Default git refspecs only move refs/heads/* + tags, which would SILENTLY drop
    # perspectives and proposals. These refspecs carry the whole store, including the
    # state-sequence tags (state/<seq>) that number canon.
    SYNC_REFSPECS = ["refs/heads/*:refs/heads/*", "refs/concordance/*:refs/concordance/*",
                     "refs/tags/state/*:refs/tags/state/*"]

    def set_remote(self, name: str, url: str) -> None:
        rc, _, _ = self._run("remote", "get-url", name)
        self._git("remote", "set-url" if rc == 0 else "add", name, url)

    def push_all(self, remote: str = "origin", *, prune: bool = False) -> dict:
        """Push every head + stasima ref to the remote, then verify nothing was dropped.
        Non-fast-forward pushes fail (append-only is preserved). `prune` mirrors deletions
        within these namespaces only — off by default for an append-only store."""
        args = ["push"] + (["--prune"] if prune else []) + [remote] + self.SYNC_REFSPECS
        self._git(*args)
        return self.verify_sync(remote)

    def fetch_all(self, remote: str = "origin") -> None:
        self._git("fetch", remote, *self.SYNC_REFSPECS)

    def verify_sync(self, remote: str = "origin") -> dict:
        """Compare local refs against the remote's; surface anything missing or mismatched.
        This is the anti-silent-loss check — a push without it is just a hope."""
        local = {}
        for pfx in ("refs/heads/", "refs/concordance/", "refs/tags/state/"):
            for r in self.list_refs(pfx):
                local[r.name] = r.oid
        remote_refs = {}
        for line in self._git("ls-remote", remote).decode().splitlines():
            if "\t" in line:
                oid, name = line.split("\t")
                remote_refs[name] = oid
        missing = sorted(n for n in local if n not in remote_refs)
        mismatch = sorted(n for n in local if n in remote_refs and remote_refs[n] != local[n])
        return {
            "synced": sorted(n for n in local if remote_refs.get(n) == local[n]),
            "missing_on_remote": missing,
            "oid_mismatch": mismatch,
        }


# --- runnable demo (mirrors spike.sh through the typed API) ------------------
if __name__ == "__main__":
    import subprocess as sp

    work = tempfile.mkdtemp(prefix="capstore-demo-")
    gd = os.path.join(work, "stasima.git")
    sp.run(["git", "init", "--bare", "-q", gd], check=True)
    store = LocalCapStore(gd, approvers={"practitioner"})

    store.bootstrap_canon({"canon/orientation.md": b"Welcome to the Stasima.\n"}, "Bootstrap canon")
    print("canon bootstrapped:", store.resolve_ref("refs/heads/main")[:10])

    persp = "refs/concordance/perspectives/research-2"
    r1 = store.commit(persp, {"instances/research-2/entries/0001.md": b"Entry 1.\n"},
                      "KIP 0001", Identity("research-2"), expected_parent=None, op_id="op-aaa")
    r2 = store.commit(persp, {"instances/research-2/entries/0002.md": b"Entry 2.\n"},
                      "KIP 0002", Identity("research-2"), expected_parent=r1.oid, op_id="op-bbb")
    print("perspective tip:", r2.oid[:10], "author:", r2.author.instance_id, "op_id:", r2.op_id)

    # idempotent retry of the op that produced the tip → returns the existing commit
    again = store.commit(persp, {"instances/research-2/entries/0002.md": b"Entry 2.\n"},
                         "KIP 0002", Identity("research-2"), expected_parent=r1.oid, op_id="op-bbb")
    print("idempotent retry same oid:", again.oid == r2.oid)

    # stale CAS → StaleRef
    try:
        store.commit(persp, {"x.md": b"z"}, "x", Identity("research-2"), expected_parent=r1.oid, op_id="op-zzz")
    except StaleRef:
        print("StaleRef raised on stale expected_parent: True")

    # proposal → prepare → land (human gate)
    main_tip = store.resolve_ref("refs/heads/main")
    store.create_branch("refs/concordance/proposals/p-001", main_tip)
    store.commit("refs/concordance/proposals/p-001",
                 {"canon/entries/principle-1.md": b"Never silently lose work.\n"},
                 "Propose principle-1", Identity("research-2"), expected_parent=main_tip, op_id="op-ccc")
    prep = store.prepare_merge("refs/concordance/proposals/p-001")
    print("prepared candidate:", prep.candidate_oid[:10], "changes:", prep.summary.changed_paths,
          "authors:", prep.summary.authoring_instances)

    landed = store.land_merge(prep, Approval(prep.candidate_oid, "practitioner", "local-confirm"))
    print("landed canon:", landed.oid[:10],
          "canon entries:", [e.name for e in store.list_tree("refs/heads/main", "canon/entries")])

    # writing canon directly is refused
    try:
        store.commit("refs/heads/main", {"canon/x.md": b"y"}, "x", Identity("research-2"), main_tip, "op-x")
    except ProtectedRef:
        print("ProtectedRef raised on direct canon write: True")

    # an unapproved land is refused
    try:
        store.land_merge(prep, Approval(prep.candidate_oid, "research-2", "self"))
    except MergeNotApproved:
        print("MergeNotApproved raised for non-approver: True")

    print("demo repo at:", gd)
