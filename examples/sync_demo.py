"""
Proves the remote-sync helper: pushing to a stand-in remote (a second local bare
repo == same machinery as GitHub, no credentials) carries the WHOLE store, and
verify_sync confirms nothing was dropped. Contrasts with the naive 'push main'
footgun that silently leaves perspectives/proposals behind.
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # import modules from the parent
from local_capstore import LocalCapStore

KEPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.git")  # the archived demo repo
work = tempfile.mkdtemp(prefix="capstore-sync-")

# a full working copy of the kept repo (--mirror copies ALL refs, incl. refs/concordance/*)
source = os.path.join(work, "source.git")
sp.run(["git", "clone", "--mirror", "-q", KEPT, source], check=True)

# an empty bare repo standing in for GitHub
remote = os.path.join(work, "remote.git")
sp.run(["git", "init", "--bare", "-q", remote], check=True)


def remote_refs():
    out = sp.run(["git", f"--git-dir={remote}", "for-each-ref", "--format=%(refname)"],
                 capture_output=True, text=True).stdout.split()
    return sorted(out)


store = LocalCapStore(source, approvers={"practitioner"})
print("source refs:        ", sorted(r.name for r in store.list_refs()))

# --- the footgun: a naive 'push main' silently drops the stasima refs ---
sp.run(["git", f"--git-dir={source}", "push", "-q", remote, "refs/heads/main:refs/heads/main"], check=True)
print("after naive push:   ", remote_refs(), " <-- perspectives & proposals MISSING")

# --- the helper: explicit refspec + verification ---
store.set_remote("mirror", remote)
report = store.push_all("mirror")
print("after push_all:     ", remote_refs())
print("verify_sync:        ", report)

assert not report["missing_on_remote"], f"dropped refs: {report['missing_on_remote']}"
assert not report["oid_mismatch"], f"oid drift: {report['oid_mismatch']}"
print("OK: every local ref present on the remote at a matching oid.")
print("workdir:", work)
