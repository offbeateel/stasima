# SPDX-License-Identifier: Apache-2.0
"""
MAP index — the derived, rebuildable projection of the corpus that MAP/IMP query.

Design commitments this file honors:
  - git/audit are truth; this index is a projection, rebuildable from them.
  - one table, `authoring_instance` a DIMENSION not a partition (per-instance = a WHERE clause).
  - addressing by PATH (identity); `content_oid` recorded as a derived version pin.
  - results stay ATTRIBUTED (every Hit carries its author + layer) — never an unattributed blend.
  - IMP = entries with `recipients`; permission is index-scope (discoverability), not access-control.
    Messages live in the same table, excluded from universal search, surfaced via the recipient's inbox.
  - read-state is an append-only EVENT, never a mutable flag.

Storage and embeddings are both behind interfaces (SQLite now / Postgres later;
stub now / local-server model later) — both reversible because the index rebuilds from git.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Sequence


# ====================================================================== embeddings
class Embedder(ABC):
    model_id: str
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...   # documents (indexing side)

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        """Queries (search side). Retrieval models are often task-prefixed and embed queries
        differently from documents; the default is symmetric for embedders that don't care."""
        return self.embed(texts)


def _tokens(text: str) -> list[str]:
    return [w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if w]


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # inputs are normalized


class StubEmbedder(Embedder):
    """Deterministic, offline bag-of-hashed-tokens embedding. For dev/tests without a model server.
    It's essentially lexical similarity — enough to prove ranking/scope/index behavior reproducibly."""

    def __init__(self, dim: int = 64):
        self.dim = dim
        self.model_id = f"stub-{dim}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in _tokens(t):
                bucket = int(hashlib.md5(tok.encode()).hexdigest()[:8], 16) % self.dim
                v[bucket] += 1.0
            out.append(_normalize(v))
        return out


class LocalServerEmbedder(Embedder):
    """Calls an OpenAI-compatible /v1/embeddings endpoint — LM Studio, Ollama, etc.
    Local processing, self-contained, dodges native-wheel questions (the model runs outside Python).

    Task prefixes: many retrieval models (nomic-embed-text, mxbai, snowflake-arctic, ...) are
    prefix-conditioned — documents and queries each need an instruction prefix or quality degrades
    badly (verified live: nomic without prefixes ranks related BELOW unrelated). Configure
    `doc_prefix`/`query_prefix` per model; empty strings for models that don't use them."""

    def __init__(self, base_url: str, model: str, dim: int, api_key: str = "not-needed",
                 doc_prefix: str = "", query_prefix: str = ""):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_id = model
        self.dim = dim
        self.api_key = api_key
        self.doc_prefix = doc_prefix
        self.query_prefix = query_prefix

    def _post(self, texts: list[str]) -> list[list[float]]:
        import httpx

        r = httpx.post(
            f"{self.base_url}/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": texts},
            timeout=60,
        )
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        # normalize: the index's cosine() is a dot product, so vectors must be unit length
        # (idempotent if the model already returns normalized embeddings).
        return [_normalize(d["embedding"]) for d in data]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._post([self.doc_prefix + t for t in texts])

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return self._post([self.query_prefix + t for t in texts])


# ====================================================================== rows + hits
@dataclass
class MapRow:
    ref: str
    path: str
    is_canon: bool
    authoring_instance: str = ""
    content_oid: str = ""          # derived version pin (not authored)
    type: str = ""
    title: str = ""
    status: str = "active"
    tags: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)          # references / lineage graph
    region_labels: list[str] = field(default_factory=list)  # maps
    links: list[str] = field(default_factory=list)          # maps; or message coordinates
    salience: float = 0.0                                    # maps
    recipients: list[str] = field(default_factory=list)     # messages
    subject: str = ""                                        # messages
    body_text: str = ""
    embedding: list[float] = field(default_factory=list)
    model_id: str = ""


@dataclass
class Hit:
    path: str
    ref: str
    authoring_instance: str
    is_canon: bool
    type: str
    title: str
    score: float
    preview: str


# ====================================================================== index interface
class MapIndex(ABC):
    """The thin storage seam. SQLite now; a Postgres+pgvector backend implements the same ABC later."""

    @abstractmethod
    def upsert(self, row: MapRow) -> None: ...

    @abstractmethod
    def search(self, query_embedding: list[float], *, scope: str = "all",
               instance_id: Optional[str] = None, type: Optional[str] = None,
               status: str = "active", limit: int = 10) -> list[Hit]: ...

    @abstractmethod
    def cartography_of(self, target_path: str) -> list[MapRow]: ...   # Q4 raw material

    @abstractmethod
    def inbox(self, instance_id: str) -> list[MapRow]: ...   # all messages addressed to instance_id

    @abstractmethod
    def clear(self) -> None: ...   # for a full rebuild from git


# ====================================================================== sqlite backend
_COLS = ["ref", "path", "is_canon", "authoring_instance", "content_oid", "type", "title",
         "status", "tags", "refs", "region_labels", "links", "salience", "recipients",
         "subject", "body_text", "embedding", "model_id"]
_JSON_COLS = {"tags", "refs", "region_labels", "links", "recipients", "embedding"}


class SqliteMapIndex(MapIndex):
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS map_entries (
                ref TEXT NOT NULL, path TEXT NOT NULL, is_canon INTEGER NOT NULL,
                authoring_instance TEXT, content_oid TEXT, type TEXT, title TEXT, status TEXT,
                tags TEXT, refs TEXT, region_labels TEXT, links TEXT, salience REAL,
                recipients TEXT, subject TEXT, body_text TEXT, embedding TEXT, model_id TEXT,
                PRIMARY KEY (ref, path)
            );
            CREATE INDEX IF NOT EXISTS ix_author ON map_entries(authoring_instance);
            CREATE INDEX IF NOT EXISTS ix_canon  ON map_entries(is_canon);
            CREATE INDEX IF NOT EXISTS ix_type   ON map_entries(type);
            """
        )
        self.conn.commit()

    def upsert(self, row: MapRow) -> None:
        vals = []
        for c in _COLS:
            v = getattr(row, c)
            if c == "is_canon":
                v = 1 if v else 0
            elif c in _JSON_COLS:
                v = json.dumps(v)
            vals.append(v)
        ph = ",".join("?" * len(_COLS))
        self.conn.execute(f"INSERT OR REPLACE INTO map_entries ({','.join(_COLS)}) VALUES ({ph})", vals)
        self.conn.commit()

    def _row(self, r: sqlite3.Row) -> MapRow:
        d = {c: r[c] for c in _COLS}
        d["is_canon"] = bool(d["is_canon"])
        for c in _JSON_COLS:
            d[c] = json.loads(d[c]) if d[c] else ([] )
        return MapRow(**d)

    def search(self, query_embedding, *, scope="all", instance_id=None, type=None, status="active", limit=10):
        where = ["type != 'msg'"]                 # messages are not in universal search (index-scope)
        params: list = []
        if status:
            where.append("status = ?"); params.append(status)
        if type:
            where.append("type = ?"); params.append(type)
        if scope == "canon":
            where.append("is_canon = 1")
        elif scope == "mine":
            where.append("authoring_instance = ?"); params.append(instance_id or "")
        sql = "SELECT * FROM map_entries WHERE " + " AND ".join(where)
        scored = []
        for r in self.conn.execute(sql, params).fetchall():
            row = self._row(r)
            if not row.embedding:
                continue
            scored.append((cosine(query_embedding, row.embedding), row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Hit(path=row.path, ref=row.ref, authoring_instance=row.authoring_instance,
                is_canon=row.is_canon, type=row.type, title=row.title,
                score=round(s, 4), preview=row.body_text[:160])
            for s, row in scored[:limit]
        ]

    def cartography_of(self, target_path):
        rows = [self._row(r) for r in self.conn.execute("SELECT * FROM map_entries WHERE type='map'").fetchall()]
        return [r for r in rows if target_path in r.links]

    def inbox(self, instance_id):
        rows = [self._row(r) for r in self.conn.execute("SELECT * FROM map_entries WHERE type='msg'").fetchall()]
        return [r for r in rows if instance_id in r.recipients]   # read-state lives in the audit log

    def clear(self):
        self.conn.execute("DELETE FROM map_entries")
        self.conn.commit()


# ====================================================================== inline indexer
def index_entry(index: MapIndex, embedder: Embedder, *, ref: str, path: str, is_canon: bool,
                authoring_instance: str, content_oid: str, envelope: dict, body: str) -> MapRow:
    """The single-process server calls this inline on each commit. Truth stays in git;
    this writes the derived row. Cartographic prose / titles + body are what get embedded."""
    embed_text = " ".join(filter(None, [envelope.get("title", ""), body]))
    emb = embedder.embed([embed_text])[0]
    row = MapRow(
        ref=ref, path=path, is_canon=is_canon, authoring_instance=authoring_instance, content_oid=content_oid,
        type=envelope.get("type", ""), title=envelope.get("title", ""), status=envelope.get("status", "active"),
        tags=envelope.get("tags", []), refs=envelope.get("references", []),
        region_labels=envelope.get("region_labels", []),
        links=envelope.get("links", envelope.get("coordinates", [])),
        salience=float(envelope.get("salience", 0.0)),
        recipients=envelope.get("recipients", []), subject=envelope.get("subject", ""),
        body_text=body, embedding=emb, model_id=embedder.model_id,
    )
    index.upsert(row)
    return row
