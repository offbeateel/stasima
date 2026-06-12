# SPDX-License-Identifier: Apache-2.0
"""
Proves Config: defaults, TOML load, env override, validation (missing/invalid/unknown), and that
server_from_config assembles a working server from a Config.
"""
import os
import subprocess as sp
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import Config, ConfigError


def raises(fn):
    try:
        fn()
        return False
    except ConfigError:
        return True


def write(text):
    p = os.path.join(tempfile.mkdtemp(), "c.toml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# defaults, with the required git_dir from env
cfg = Config.load(env={"STASIMA_GIT_DIR": os.path.join(tempfile.gettempdir(), "x", "stasima.git")})
assert cfg.embed_backend == "stub" and cfg.approvers == ["practitioner"]
assert cfg.resolved_map_db().endswith("map_index.sqlite")
assert cfg.resolved_audit_db().endswith("audit.sqlite")
print("defaults:        OK")

# flat TOML file
p = write('git_dir = "/srv/stasima.git"\n'
          'approvers = ["alice", "bob"]\n'
          'embed_backend = "local-server"\n'
          'embed_url = "http://localhost:1234"\n'
          'embed_dim = 1024\n')
cfg2 = Config.load(p, env={})
assert cfg2.approvers == ["alice", "bob"] and cfg2.embed_backend == "local-server" and cfg2.embed_dim == 1024
print("toml load:       OK")

# env overrides the file
cfg3 = Config.load(p, env={"STASIMA_EMBED_MODEL": "bge-m3", "STASIMA_APPROVERS": "carol,dave"})
assert cfg3.embed_model == "bge-m3" and cfg3.approvers == ["carol", "dave"]
print("env override:    OK")

# validation
assert raises(lambda: Config.load(env={})), "missing git_dir should fail"
assert raises(lambda: Config.load(write('git_dir="/x/r.git"\nembed_backend="local-server"\n'), env={})), "local-server needs url"
assert raises(lambda: Config.load(write('git_dir="/x/r.git"\nembed_dimm=768\n'), env={})), "unknown key should fail"
assert raises(lambda: Config.load("/no/such/file.toml", env={})), "missing file should fail"
print("validation:      OK")

# assembly: a real server from a config
work = tempfile.mkdtemp()
gd = os.path.join(work, "stasima.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
from cap_server import server_from_config
mcp = server_from_config(Config.load(env={"STASIMA_GIT_DIR": gd}))
assert mcp is not None
print("assembly:        OK")

print("\nOK -- config: defaults, TOML, env override, validation, and server assembly all verified.")
