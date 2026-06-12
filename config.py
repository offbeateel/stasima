"""
Configuration — one typed, validated config per deployment, replacing scattered env vars.

Loads from a TOML file (flat keys) with env-var overrides; sensible defaults fill the rest. Pure
stdlib (`tomllib`), no component imports — the assembly from a Config lives in cap_server
(`server_from_config`), the single place wiring happens. GitHub creds + notification endpoints
arrive with 1.1 (multi-user / sync); they're intentionally absent here.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields


class ConfigError(Exception):
    pass


# env var -> field name (env overrides the file; both override defaults)
_ENV = {
    "STASIMA_GIT_DIR": "git_dir",
    "STASIMA_APPROVERS": "approvers",
    "STASIMA_CANON_REF": "canon_ref",
    "STASIMA_MAP_DB": "map_db",
    "STASIMA_AUDIT_DB": "audit_db",
    "STASIMA_EMBED_URL": "embed_url",
    "STASIMA_EMBED_MODEL": "embed_model",
    "STASIMA_EMBED_DIM": "embed_dim",
}


@dataclass
class Config:
    git_dir: str = ""
    deployment_name: str = ""   # this deployment's own name (practice-side); blank = generic "Stasima"
    approvers: list = field(default_factory=lambda: ["practitioner"])
    canon_ref: str = "refs/heads/main"
    map_db: str = ""        # blank -> derived beside git_dir (throwaway cache)
    audit_db: str = ""      # blank -> derived beside git_dir (TRUTH — back it up)
    committer_name: str = "capstore"
    committer_email: str = "capstore@stasima.local"
    embed_backend: str = "stub"            # "stub" | "local-server"
    embed_url: str = ""
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    # task prefixes — defaults match the default model (nomic is prefix-conditioned and degrades
    # badly without them). CLEAR these if you switch to a model that doesn't use prefixes.
    embed_doc_prefix: str = "search_document: "
    embed_query_prefix: str = "search_query: "
    orientation_base: str = "technical/orientation"
    # state-sequence origin: canon's seq before any land (first land = origin + 1). The suite
    # default is the original practice's chat-era freeze (::3B); a fresh deployment may set 0
    # (TOML accepts hex: seq_origin = 0x3b).
    seq_origin: int = 0x3B
    # airlock (TOTP two-phase remote approval). Floor must exceed worst-case code lifetime
    # (30s step + ±1 window ≈ 90s) so no code obtained at staging survives to landing.
    airlock_secret_path: str = ""   # blank -> derived beside git_dir; NOT in git
    airlock_floor_s: int = 120
    airlock_ceiling_s: int = 7200

    @classmethod
    def load(cls, path: str | None = None, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        data: dict = {}
        if path:
            if not os.path.exists(path):
                raise ConfigError(f"config file not found: {path}")
            with open(path, "rb") as f:
                data.update(tomllib.load(f))
        for ev, name in _ENV.items():
            if env.get(ev):
                data[name] = env[ev]
        if env.get("STASIMA_EMBED_URL") and "embed_backend" not in data:
            data["embed_backend"] = "local-server"
        if isinstance(data.get("approvers"), str):
            data["approvers"] = [a.strip() for a in data["approvers"].split(",") if a.strip()]
        for intf in ("embed_dim", "airlock_floor_s", "airlock_ceiling_s", "seq_origin"):
            if intf in data:
                try:
                    data[intf] = int(data[intf])
                except (TypeError, ValueError):
                    raise ConfigError(f"{intf} must be an integer, got {data[intf]!r}")
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)} (config is flat TOML; check spelling)")
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.git_dir:
            raise ConfigError("git_dir is required (set it in the config file or STASIMA_GIT_DIR)")
        if self.embed_backend not in ("stub", "local-server"):
            raise ConfigError(f"embed_backend must be 'stub' or 'local-server', got {self.embed_backend!r}")
        if self.embed_backend == "local-server" and not self.embed_url:
            raise ConfigError("embed_backend 'local-server' requires embed_url")
        if int(self.embed_dim) <= 0:
            raise ConfigError("embed_dim must be a positive integer")
        if not self.approvers:
            raise ConfigError("at least one approver is required")
        if self.airlock_floor_s <= 0 or self.airlock_ceiling_s <= self.airlock_floor_s:
            raise ConfigError("airlock gates must satisfy 0 < airlock_floor_s < airlock_ceiling_s")

    def resolved_map_db(self) -> str:
        return self.map_db or os.path.join(os.path.dirname(self.git_dir), "map_index.sqlite")

    def resolved_audit_db(self) -> str:
        return self.audit_db or os.path.join(os.path.dirname(self.git_dir), "audit.sqlite")

    def resolved_airlock_secret(self) -> str:
        return self.airlock_secret_path or os.path.join(os.path.dirname(self.git_dir), "totp.secret")
