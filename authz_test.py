# SPDX-License-Identifier: Apache-2.0
"""
Unit test for the authz seam (DefaultPolicy): reads open; own-lane writes allowed; canon,
messages-via-kip_commit, and cross-perspective writes denied. The server-level denial (that a
handler actually rejects + audit-logs) is exercised in server_test.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stasima.authz import DefaultPolicy, Denied

p = DefaultPolicy()
PE = "refs/cap/perspectives/"


def denied(fn):
    try:
        fn()
        return False
    except Denied:
        return True


# reads are open — even on canon
p.check("research-2", "map_search")
p.check("research-2", "kip_get", "refs/heads/main", "practice/x.md")

# allowed writes
p.check("research-2", "kip_commit", PE + "research-2", "practice/x.md")          # own perspective
p.check("research-2", "propose", "refs/cap/proposals/p-1", "practice/x.md")  # a proposal
p.check("research-2", "imp_send", PE + "research-2", "messages/op-1.md")          # own messages

# denied writes
assert denied(lambda: p.check("research-2", "kip_commit", "refs/heads/main", "practice/x.md")), "canon write"
assert denied(lambda: p.check("research-2", "kip_commit", PE + "research-2", "messages/x.md")), "message via kip_commit"
assert denied(lambda: p.check("research-2", "kip_commit", PE + "research-7", "practice/x.md")), "another's perspective"

print("OK -- DefaultPolicy: reads open; own-lane writes allowed; canon / messages-via-kip / cross-perspective denied.")
