# SPDX-License-Identifier: Apache-2.0
"""
Admin CLI — practitioner-side maintenance + promotion. NOT model-facing: these are operator ops,
and `land` (promotion to canon) IS the human gate, performed here out of band.

    stasima-admin --config stasima.toml <command>

      bootstrap <dir>     seed an EMPTY canon from a folder of .md entries (one-time)
      totp-provision      generate the airlock TOTP secret (prints the otpauth:// URI)
      totp-check <code>   verify a code from your app (consumes nothing; diagnoses clock skew)
      inbox [--all] [--read PATH]   the practitioner's mail, from the cockpit (pull)
      backup <dest>       full LOCAL backup of everything that is truth: git mirror (all refs+tags),
                          consistent audit snapshot, config, TOTP secret. For a same-trust machine
                          move — carries the secret. Run it anywhere: synced folder, external drive.
      mirror <url>        off-machine backup to a git REMOTE (e.g. a private repo): content refs +
                          a consistent audit snapshot on refs/backup/audit, verified. The TOTP secret
                          is NEVER pushed. Run on a cadence for durable, off-box state.
      status              canon head, perspectives, proposals, audit health
      reindex             rebuild the MAP index from git
      reconcile           backfill audit events for committed ops missing one
      verify              check the audit chain (+ the git-anchored checkpoint)
      anchor              write the audit head into git now
      preview <id>        dry-run a proposal merge (conflicts / changed paths)
      land <id> [--by X]  approve + land a proposal to canon (audit + reindex + anchor)
"""
import argparse
import json
import os
import subprocess as sp
import sys
import time

from .config import Config
from .entries import compose_entry
from .cap_server import components_from_config
from .canon import reindex_from_git, land_and_record, canon_seq, seq_display, LOG_DIR
from .audit_log import reconcile_from_git, anchor_audit_head, verify_against_anchor
from .local_capstore import Approval, MergeConflict, CanonAppendOnly, PERSP_PREFIX as PERSP, PROP_PREFIX as PROP
from .airlock import generate_secret, otpauth_uri, verify_code, totp_at, STEP


def _first_heading(text: str):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _qr_ascii(data: str):
    """ASCII QR of `data`, or None if the optional qrcode package isn't installed."""
    try:
        import qrcode
    except ImportError:
        return None
    import io
    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)   # dark-terminal polarity; the URI below is the fallback
    return buf.getvalue()


def run(args) -> dict:
    cfg = Config.load(args.config)
    store, index, embedder, audit, authz, airlock = components_from_config(cfg)

    if args.cmd == "totp-provision":
        issuer = cfg.deployment_name or "Stasima"
        uri = lambda s: otpauth_uri(s, label=f"{issuer}:practitioner", issuer=issuer)
        path = cfg.resolved_airlock_secret()
        if os.path.exists(path) and not args.force:
            if args.qr:   # re-display the EXISTING secret's QR — no rotation
                with open(path, encoding="utf-8") as f:
                    secret = f.read().strip()
                qr = _qr_ascii(uri(secret))
                print(qr if qr else "(pip install qrcode for a scannable QR)")
                return {"secret_path": path, "otpauth_uri": uri(secret), "rotated": False,
                        "note": "existing secret re-displayed; scan the QR or enter the secret= value manually"}
            raise SystemExit(f"secret already exists at {path} — pass --force to rotate "
                             f"(rotating invalidates the practitioner's current authenticator entry), "
                             f"or --qr to re-display it")
        secret = generate_secret()
        with open(path, "w", encoding="utf-8") as f:
            f.write(secret + "\n")
        if args.qr:
            qr = _qr_ascii(uri(secret))
            print(qr if qr else "(pip install qrcode for a scannable QR)")
        return {"secret_path": path, "otpauth_uri": uri(secret),
                "note": "scan the QR (or enter the secret= value manually); the secret stays "
                        "server-side, never in git — if the QR won't scan, terminal polarity is the "
                        "usual culprit; the manual key always works"}

    if args.cmd == "totp-check":
        # verification only — consumes no windows, approves nothing; safe to run as often as you like
        spath = cfg.resolved_airlock_secret()
        if not os.path.exists(spath):
            raise SystemExit(f"no secret at {spath} — run totp-provision first")
        with open(spath, encoding="utf-8") as f:
            secret = f.read().strip()
        now = time.time()
        w = verify_code(secret, args.code, now)
        if w is not None:
            return {"valid": True, "matched_window": w, "current_window": int(now // STEP),
                    "note": "your authenticator and the server agree — the airlock will accept codes"}
        cur = int(now // STEP)
        for delta in range(-10, 11):                       # diagnose clock skew beyond the ±1 acceptance
            if totp_at(secret, cur + delta) == str(args.code).strip():
                return {"valid": False, "matched_window": cur + delta,
                        "skew": f"{delta:+d} windows (≈ {delta * STEP:+d}s)",
                        "note": "code is from the right secret but outside the ±1-window acceptance — "
                                "sync the server or phone clock"}
        return {"valid": False,
                "note": "no match within ±10 windows — mistyped code, or the app holds a different/old secret "
                        "(re-provision with --force and re-add to the app)"}

    if args.cmd == "bootstrap":
        if not os.path.isdir(os.path.join(cfg.git_dir, "objects")):
            sp.run(["git", "init", "--bare", "-q", cfg.git_dir], check=True)   # create the bare repo if missing
        if store.resolve_ref(cfg.canon_ref) is not None:
            raise SystemExit("canon already exists — add entries via propose + land, not bootstrap")
        changes = {}
        for root, _, files in os.walk(args.seed_dir):
            for fn in sorted(files):
                if not fn.endswith(".md"):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), args.seed_dir).replace(os.sep, "/")
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    text = f.read()
                if not text.lstrip().startswith("---"):    # plain markdown -> wrap with a sensible envelope
                    title = _first_heading(text) or os.path.splitext(fn)[0].replace("-", " ").title()
                    etype = "ori" if rel.startswith("technical/orientation/") else "kno"
                    text = compose_entry({"type": etype, "title": title, "status": "active"}, text)
                changes[rel] = text.encode()
        if not changes:
            raise SystemExit(f"no .md files found under {args.seed_dir!r}")
        r = store.bootstrap_canon(changes, "Bootstrap canon")
        return {"bootstrapped": r.oid, "entries": sorted(changes), "indexed": reindex_from_git(store, index, embedder)}

    if args.cmd == "status":
        ok, bad = audit.verify()
        unread = [m for m in index.inbox("practitioner") if not audit.is_read("practitioner", m.path)]
        return {"canon_head": store.resolve_ref(cfg.canon_ref),
                "canon_seq": seq_display(canon_seq(store, cfg.seq_origin)),
                "perspectives": [r.name[len(PERSP):] for r in store.list_refs(PERSP)],
                "proposals": [r.name[len(PROP):] for r in store.list_refs(PROP)],
                "staged": airlock.staged(),
                "practitioner_unread": len(unread),
                "audit_events": audit.count(), "audit_verify_ok": ok,
                "audit_vs_anchor": verify_against_anchor(store, audit)}

    if args.cmd == "inbox":
        if args.read:
            audit.append_read("practitioner", args.read)
            return {"marked_read": args.read}
        msgs = index.inbox("practitioner")
        if not args.all:
            msgs = [m for m in msgs if not audit.is_read("practitioner", m.path)]
        return {"unread" if not args.all else "all":
                [{"path": m.path, "from": m.authoring_instance, "subject": m.subject,
                  "coordinates": m.links} for m in msgs],
                "note": "read a message body with: kip_get equivalent -> git show <perspective>:<path>; "
                        "mark handled with: inbox --read <path>"}

    if args.cmd == "backup":
        # everything that is TRUTH, in one destination: full-ref git mirror (consistent by nature),
        # a consistent audit snapshot (sqlite backup API, safe against a live server), config + secret.
        # The map index is a derived cache and is deliberately not backed up.
        import shutil
        import sqlite3 as _sq
        os.makedirs(args.dest, exist_ok=True)
        mirror = os.path.join(args.dest, "stasima-mirror.git")
        if not os.path.isdir(os.path.join(mirror, "objects")):
            sp.run(["git", "init", "--bare", "-q", mirror], check=True)
        store.set_remote("backup", mirror)
        sync = store.push_all("backup")
        audit_copy = os.path.join(args.dest, "audit.sqlite")
        dst = _sq.connect(audit_copy)
        audit.conn.backup(dst)
        dst.close()
        copied = ["stasima-mirror.git", "audit.sqlite"]
        for src in (args.config, cfg.resolved_airlock_secret()):
            if src and os.path.exists(src):
                shutil.copy2(src, args.dest)
                copied.append(os.path.basename(src))
        ok = not sync["missing_on_remote"] and not sync["oid_mismatch"]
        return {"dest": args.dest, "git_sync_ok": ok, "synced_refs": len(sync["synced"]),
                "audit_events": audit.count(), "copied": copied}

    if args.cmd == "mirror":
        # off-machine backup to a GIT REMOTE (e.g. a PRIVATE repo) in one command: content refs +
        # a consistent audit snapshot on a dedicated ref (refs/backup/audit), both verified.
        # The TOTP secret is deliberately NEVER pushed — it is the airlock key and is re-provisionable;
        # keep it out of any remote, even a private one. (Use `backup <dest>` for a local bundle that
        # DOES carry the secret, for a same-trust machine move.)
        import sqlite3 as _sq
        import tempfile
        fd, snap = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            dst = _sq.connect(snap)          # consistent snapshot, safe against a live server
            audit.conn.backup(dst)
            dst.close()
            with open(snap, "rb") as f:
                store.commit_file("refs/backup/audit", "audit.sqlite", f.read(),
                                  f"audit snapshot ({audit.count()} events)")
        finally:
            os.remove(snap)
        sync = store.mirror_push("mirror", args.url,
                                 extra_refspecs=["refs/backup/audit:refs/backup/audit"])
        ok = not sync["missing_on_remote"] and not sync["oid_mismatch"]
        return {"remote": args.url, "git_sync_ok": ok, "synced_refs": len(sync["synced"]),
                "audit_events": audit.count(),
                "note": "content refs + audit snapshot pushed; TOTP secret NOT sent (re-provision on restore)"}

    if args.cmd == "reindex":
        return {"reindexed": reindex_from_git(store, index, embedder)}

    if args.cmd == "reconcile":
        return {"backfilled": reconcile_from_git(store, audit)}

    if args.cmd == "verify":
        ok, bad = audit.verify()
        return {"audit_verify_ok": ok, "first_bad_seq": bad,
                "audit_vs_anchor": verify_against_anchor(store, audit)}

    if args.cmd == "anchor":
        return {"anchored": anchor_audit_head(store, audit)}

    if args.cmd == "preview":
        s = store.preview_merge(PROP + args.proposal_id, cfg.canon_ref)
        logs = [p for p in (s.added + s.modified) if p.startswith(LOG_DIR)]
        return {"conflicts": s.conflicts, "authors": s.authoring_instances,
                "adds": s.added, "removes": s.removed, "modifies": s.modified,
                "would_remove_canon": s.removed,          # non-empty -> `land` will REFUSE (append-only)
                "log_entries": logs, "log_entry_ok": len(logs) == 1,
                "expected_seq": format(canon_seq(store, cfg.seq_origin) + 1, "x")}

    if args.cmd == "land":
        approver = args.by or sorted(cfg.approvers)[0]
        if approver not in cfg.approvers:
            raise SystemExit(f"{approver!r} is not a configured approver ({sorted(cfg.approvers)})")
        try:
            prepared = store.prepare_merge(PROP + args.proposal_id, cfg.canon_ref)
        except MergeConflict as e:
            raise SystemExit(f"conflict — not landing: {e}")
        except CanonAppendOnly as e:
            raise SystemExit(f"append-only — not landing: {e}")
        try:
            return land_and_record(store, index, embedder, audit, prepared,
                                   Approval(prepared.candidate_oid, approver, "cli-confirm"),
                                   origin=cfg.seq_origin)
        except ValueError as e:
            raise SystemExit(f"not landing: {e}")

    raise SystemExit(f"unknown command {args.cmd!r}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="stasima-admin", description="Stasima maintenance + promotion")
    ap.add_argument("--config", default=os.environ.get("STASIMA_CONFIG"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("status", "reindex", "reconcile", "verify", "anchor"):
        sub.add_parser(c)
    sub.add_parser("bootstrap").add_argument("seed_dir", help="folder of .md entries to seed an empty canon")
    tp = sub.add_parser("totp-provision")
    tp.add_argument("--force", action="store_true", help="rotate an existing secret")
    tp.add_argument("--qr", action="store_true", help="render a scannable ASCII QR (re-displays if the secret exists)")
    sub.add_parser("totp-check").add_argument("code", help="a code from your authenticator app")
    sub.add_parser("backup").add_argument("dest", help="destination folder for the full backup (carries the secret)")
    sub.add_parser("mirror").add_argument("url", help="git remote URL (e.g. a PRIVATE repo) — content + audit, no secret")
    ib = sub.add_parser("inbox")
    ib.add_argument("--all", action="store_true", help="include already-read messages")
    ib.add_argument("--read", default=None, metavar="PATH", help="mark a message path as read")
    sub.add_parser("preview").add_argument("proposal_id")
    land = sub.add_parser("land")
    land.add_argument("proposal_id")
    land.add_argument("--by", default=None, help="approver (defaults to the first configured)")
    return ap


def main(argv=None) -> dict:
    try:   # Windows consoles default to cp1252, which can't print the QR block chars (or em dashes)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    result = run(build_parser().parse_args(argv))
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == "__main__":
    main()
