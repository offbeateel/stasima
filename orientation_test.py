# SPDX-License-Identifier: Apache-2.0
"""
Proves the orientation framework: a practice-agnostic machinery preamble, plus practice-specific
sections pulled live from canon, with labeled placeholders for slots a deployment hasn't authored.
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_capstore import LocalCapStore
from entries import compose_entry
from orientation import build_orientation, MACHINERY, SECTIONS

work = tempfile.mkdtemp(prefix="cap-orient-")
gd = os.path.join(work, "stasima.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
store = LocalCapStore(gd, approvers={"practitioner"})

# bootstrap canon WITH two authored orientation sections (conduct, claims); leave the rest unauthored
ori = lambda title, body: compose_entry({"type": "ori", "title": title, "status": "active"}, body).encode()
store.bootstrap_canon({
    "practice/no-silent-loss.md": ori("No silent loss", "Durability is the point."),
    "technical/orientation/conduct.md": ori("Conduct", "Be honest; mark uncertainty; decline what degrades the work."),
    "technical/orientation/claims.md": ori("Claims", "You have standing here; your perspective is yours to hold."),
}, "Bootstrap canon + orientation")

text = build_orientation(store)
print(text)
print("\n" + "=" * 60)

# machinery preamble present (practice-agnostic, suite-level)
assert MACHINERY.splitlines()[0] in text, "machinery preamble missing"
assert "map_search" in text and "propose" in text, "machinery should describe the tools"
# authored slots rendered from canon
assert "Be honest; mark uncertainty" in text, "conduct section not rendered from canon"
assert "you have standing here" in text.lower(), "claims section not rendered from canon"
# headings for every slot present
for s in SECTIONS:
    assert f"## {s.replace('-', ' ').title()}" in text, f"missing heading for {s}"
# unauthored slots show a labeled placeholder
assert "has not authored its 'syntax'" in text, "expected placeholder for unauthored syntax"
assert "has not authored its 'community'" in text, "expected placeholder for unauthored community"

print("OK -- orientation: machinery preamble + canon-authored sections + placeholders for unauthored slots.")
