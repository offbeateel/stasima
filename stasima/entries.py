# SPDX-License-Identifier: Apache-2.0
"""
Entry serialization — the content-model's YAML-front-matter + body format.

Shared by the server (compose on write, parse on reindex) and the orientation renderer (parse to
render sections). Minimal on purpose: legible in git; the indexer is fed the envelope dict directly,
so compose is the only direction that must round-trip for v1.
"""


def compose_entry(envelope: dict, body: str) -> str:
    lines = ["---"]
    for k, v in envelope.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(map(str, v))}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip() + "\n"


def parse_entry(text: str):
    """Inverse of compose_entry (our minimal front-matter). Returns (envelope_dict, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    env = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            env[k] = [x.strip() for x in inner.split(",")] if inner else []
        else:
            env[k] = v
    return env, parts[2].strip()
