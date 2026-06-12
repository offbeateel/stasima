# SPDX-License-Identifier: Apache-2.0
"""
Proves the admin CLI end to end against a real repo: reindex, reconcile, verify, status, preview,
and land (the human-gate promotion — audit + reindex + git anchor), driven through admin.run().
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stasima.local_capstore import LocalCapStore, Identity
from stasima.entries import compose_entry
from stasima import admin

entry = lambda title, body: compose_entry({"type": "kno", "title": title, "status": "active"}, body).encode()

work = tempfile.mkdtemp(prefix="cap-admin-")
gd = os.path.join(work, "stasima.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
store = LocalCapStore(gd, approvers={"practitioner"})

# a corpus with a perspective entry and a proposal (built directly in git; nothing indexed/logged yet)
store.bootstrap_canon({"practice/seed.md": entry("Seed", "the seed")}, "bootstrap")
store.commit("refs/cap/perspectives/research-2", {"practice/notes.md": entry("Notes", "durability notes")},
             "KIP notes", Identity("research-2"), expected_parent=None, op_id="op-1")
store.create_branch("refs/cap/proposals/p-1", store.resolve_ref("refs/heads/main"))
log = compose_entry({"type": "log", "title": "::3C", "status": "active", "seq": "3c"},
                    "::3C — substrate moved; first land.").encode()
store.commit("refs/cap/proposals/p-1", {"practice/principle.md": entry("Principle", "a principle"),
                                                "meta/log/3c.md": log},
             "propose", Identity("research-2"),
             expected_parent=store.resolve_ref("refs/cap/proposals/p-1"), op_id="op-2")

# a config pointing at the repo (forward slashes — valid + escape-free in TOML, fine for git/Python)
cfgpath = os.path.join(work, "stasima.toml")
with open(cfgpath, "w", encoding="utf-8") as f:
    f.write(f'git_dir = "{gd.replace(os.sep, "/")}"\n')


def run(*argv):
    return admin.run(admin.build_parser().parse_args(["--config", cfgpath, *argv]))


print("reindex  ", r := run("reindex"));     assert r["reindexed"] == 2   # canon seed + perspective notes (proposals not indexed)
print("reconcile", r := run("reconcile"));   assert r["backfilled"] >= 1
print("verify   ", r := run("verify"));      assert r["audit_verify_ok"] is True
print("status   ", r := run("status"))
assert "research-2" in r["perspectives"] and "p-1" in r["proposals"]
print("preview  ", r := run("preview", "p-1"))
assert r["conflicts"] == [] and "practice/principle.md" in r["changed_paths"]
assert r["log_entry_ok"] and r["log_entries"] == ["meta/log/3c.md"] and r["expected_seq"] == "3c"

ld = run("land", "p-1", "--by", "practitioner")
print("land     ", ld)
assert ld["landed"] and ld["anchor"]["head"] and ld["seq"] == "3c" and ld["display"] == "::3C"

s2 = run("status")
assert s2["audit_vs_anchor"] is True, "audit anchored into git on land"
assert s2["canon_seq"] == "::3C", "status reports the state number"
assert "practice/principle.md" in store.list_paths("refs/heads/main"), "promotion landed in canon"

# totp: provision -> a real current-window code verifies; a wrong code reports cleanly
import time
from stasima.airlock import totp_at, STEP
pv = run("totp-provision")
print("provision", {"secret_path": pv["secret_path"]})
secret = open(pv["secret_path"], encoding="utf-8").read().strip()
chk = run("totp-check", totp_at(secret, int(time.time() // STEP)))
assert chk["valid"] is True, chk
chk2 = run("totp-check", "000000")
assert chk2["valid"] is False, chk2

# backup: one command captures all truth; the mirror holds every ref incl. the state tag
dest = os.path.join(work, "backup")
bk = run("backup", dest)
assert bk["git_sync_ok"] and bk["audit_events"] >= 1, bk
mirror = LocalCapStore(os.path.join(dest, "stasima-mirror.git"), approvers={"x"})
assert mirror.resolve_ref("refs/heads/main") == store.resolve_ref("refs/heads/main")
assert mirror.resolve_ref("refs/tags/state/3c"), "state tag rode the backup"
assert os.path.exists(os.path.join(dest, "audit.sqlite"))
bk2 = run("backup", dest)   # repeatable: incremental push into the same mirror
assert bk2["git_sync_ok"]
print("backup   ", {"synced_refs": bk["synced_refs"], "copied": bk["copied"]})

print("\nOK -- admin CLI: reindex / reconcile / verify / status / preview / land all verified.")
