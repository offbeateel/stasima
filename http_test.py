# SPDX-License-Identifier: Apache-2.0
"""
HTTP transport, end to end: boot the real server subprocess under STASIMA_CONFIG with
transport="http", connect with the MCP streamable-http client, initialize, list tools, announce.
Also: the bind-address guard (loopback/tailnet allowed; wider binds refused until 1.1 auth).
"""
import os
import socket
import subprocess as sp
import sys
import tempfile
import time

import anyio

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from config import Config, ConfigError
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# ---- bind-address guard (the structural "no outside exposure until 1.1") ----
def rejected(**kw):
    try:
        Config(git_dir="/x/r.git", transport="http", **kw).validate()
        return False
    except ConfigError:
        return True

Config(git_dir="/x/r.git", transport="http", http_host="127.0.0.1").validate()
Config(git_dir="/x/r.git", transport="http", http_host="localhost").validate()
Config(git_dir="/x/r.git", transport="http", http_host="100.101.1.5").validate()   # tailnet CGNAT
assert rejected(http_host="0.0.0.0"), "0.0.0.0 must be refused (no auth yet)"
assert rejected(http_host="192.168.1.50"), "LAN bind must be refused (no auth yet)"
assert rejected(http_host="example.com"), "hostnames other than localhost refused"
assert rejected(http_port=0), "port 0 refused"
print("bind guard          OK (loopback+tailnet allowed; LAN/0.0.0.0 refused until 1.1)")

# ---- live server over HTTP ----
work = tempfile.mkdtemp(prefix="stasima-http-")
gd = os.path.join(work, "stasima.git")
sp.run(["git", "init", "--bare", "-q", gd], check=True)
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
cfgpath = os.path.join(work, "stasima.toml")
with open(cfgpath, "w", encoding="utf-8") as f:
    f.write(f'git_dir = "{gd.replace(os.sep, "/")}"\ntransport = "http"\nhttp_port = {port}\n')

env = dict(os.environ, STASIMA_CONFIG=cfgpath)
proc = sp.Popen([sys.executable, os.path.join(HERE, "cap_server.py")], env=env,
                stdout=sp.DEVNULL, stderr=sp.DEVNULL)
try:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            if proc.poll() is not None:
                raise SystemExit(f"server exited early: {proc.returncode}")
            time.sleep(0.3)
    else:
        raise SystemExit("server never opened the port")
    print(f"server up           OK (127.0.0.1:{port})")

    async def main():
        async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = sorted(t.name for t in (await session.list_tools()).tools)
                assert "announce" in tools and "stage_approve" in tools, tools
                print(f"tools over http     OK ({len(tools)} tools)")
                res = await session.call_tool("announce", {"instance_id": "epode"})
                text = "".join(getattr(c, "text", "") for c in res.content)
                assert "Welcome to Stasima, epode." in text, text[:120]
                print("announce over http  OK ->", "Welcome to Stasima, epode.")

    anyio.run(main)
finally:
    proc.terminate()
    proc.wait(timeout=10)

print("\nOK -- http transport: bind guard + live server + real MCP client round-trip.")
