# SPDX-License-Identifier: Apache-2.0
"""
Proves `admin bootstrap`: seeds an empty canon from a folder of .md entries (wrapping plain markdown
with a sensible envelope, using front-matter as-is), creates the repo if missing, indexes, and
refuses on a non-empty canon.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin
from local_capstore import LocalCapStore
from orientation import build_orientation

work = tempfile.mkdtemp(prefix="cap-bootstrap-")
seed = os.path.join(work, "seed")
os.makedirs(os.path.join(seed, "technical", "orientation"))
os.makedirs(os.path.join(seed, "practice"))
# plain markdown (no front-matter) -> gets wrapped with an envelope (title from the heading)
with open(os.path.join(seed, "technical", "orientation", "welcome.md"), "w", encoding="utf-8") as f:
    f.write("# Welcome\n\nYou are welcome here.\n")
# already has front-matter -> used as-is
with open(os.path.join(seed, "practice", "seed.md"), "w", encoding="utf-8") as f:
    f.write("---\ntype: kno\ntitle: Seed\nstatus: active\n---\n\nthe seed entry\n")

gd = os.path.join(work, "stasima.git")            # does not exist yet — bootstrap creates it
cfgpath = os.path.join(work, "stasima.toml")
with open(cfgpath, "w", encoding="utf-8") as f:
    f.write(f'git_dir = "{gd.replace(os.sep, "/")}"\n')


def run(*argv):
    return admin.run(admin.build_parser().parse_args(["--config", cfgpath, *argv]))


res = run("bootstrap", seed)
print(res)
assert res["bootstrapped"]
assert set(res["entries"]) == {"technical/orientation/welcome.md", "practice/seed.md"}
assert res["indexed"] == 2

# the welcome slot now renders in the arrival orientation
store = LocalCapStore(gd, approvers={"practitioner"})
assert "You are welcome here." in build_orientation(store)

# bootstrapping a non-empty canon is refused
try:
    run("bootstrap", seed)
    assert False, "second bootstrap should refuse"
except SystemExit:
    pass

print("OK -- bootstrap: seeds an empty canon from a folder, indexes, refuses re-bootstrap.")
