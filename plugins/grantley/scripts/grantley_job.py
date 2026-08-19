#!/usr/bin/env python3
"""Out-of-band evolution driver for the Grantley persona (A3 G4).

Runs as a hermes cron job with ``no_agent: true`` — **zero LLM cost**, which
is the whole point: the decay tick and the daily life beat are arithmetic and
a seeded draw, not generation.

Subcommands::

    grantley_job.py decay            # recover fatigue, age topics
    grantley_job.py advance          # draw + apply today's life beat
    grantley_job.py show             # print current state (operator aid)
    grantley_job.py install-profile  # write SOUL.md into a profile dir

Every subcommand prints one JSON object to stdout, so a cron notepad or a log
scraper can consume the result directly.

Storage resolution deserves a note. corlinman's equivalent job ran 1260 times
in production and failed all 1260 with ``data_dir_unavailable``, because it
resolved its database from an application-state attribute that was never
populated in that deployment — a silent, permanent no-op. Here resolution is
explicit and layered (``--db`` → ``plugins.plugin_storage.plugin_db`` →
``$HERMES_HOME/plugin-data/grantley/data.db``), and an unresolvable database
is a non-zero exit with the reason on stdout rather than a swallowed error.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGIN_DIR.parent.parent

# Import the plugin package as a top-level ``grantley`` package. Works both
# in-repo and from an installed copy at $HERMES_HOME/plugins/grantley/,
# because every intra-package import is relative.
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

from grantley import jobs, life, persona, store as store_mod  # noqa: E402


def _resolve_db(explicit: str | None) -> Path:
    """Resolve the plugin database path. Raises with a usable message."""
    if explicit:
        return Path(explicit).expanduser()

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415

        return plugin_data_dir("grantley") / "data.db"
    except Exception:
        pass

    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        root = Path(hermes_home).expanduser() / "plugin-data" / "grantley"
        root.mkdir(parents=True, exist_ok=True)
        return root / "data.db"

    raise SystemExit(
        json.dumps(
            {
                "ok": False,
                "error": "db_unresolved",
                "message": (
                    "pass --db, or set HERMES_HOME, or run from a checkout "
                    "where plugins.plugin_storage is importable"
                ),
            },
            ensure_ascii=False,
        )
    )


def _open_store(db_path: Path) -> store_mod.GrantleyStore:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # Network filesystems (NFS/SMB/virtiofs) reject WAL; the default
        # rollback journal is correct there and the job still works.
        pass
    conn.execute("PRAGMA foreign_keys=ON")
    return store_mod.GrantleyStore(conn)


def _emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def cmd_decay(args: argparse.Namespace) -> int:
    st = _open_store(_resolve_db(args.db))
    personas = [args.persona] if args.persona else None
    return _emit(jobs.run_decay(st, persona_ids=personas))


def cmd_advance(args: argparse.Namespace) -> int:
    db = _resolve_db(args.db)
    st = _open_store(db)
    return _emit(
        jobs.run_life_advance(st, args.persona, data_dir=db.parent)
    )


def cmd_show(args: argparse.Namespace) -> int:
    db = _resolve_db(args.db)
    st = _open_store(db)
    state = st.load_state(args.persona)
    doc = persona.load_persona_document()
    events = st.retrieve(args.persona)
    return _emit(
        {
            "ok": True,
            "persona_id": state.persona_id,
            "mood": state.mood,
            "fatigue_bucket": persona.resolve_placeholder(state, "fatigue"),
            "recent_topics": state.recent_topics,
            "placeholders": {
                key: persona.resolve_placeholder(state, key)
                for key in doc.placeholder_keys()
            },
            "signals": life.compute_life_signals(
                state.state_json.get("life"), life.now_dt()
            ),
            "events_total": st.count_events(args.persona),
            "events_surfaced": [
                {"weight": round(e.weight, 4), "text": e.text} for e in events
            ],
        }
    )


def cmd_install_profile(args: argparse.Namespace) -> int:
    """Write the *stable* half of the persona document as ``SOUL.md``.

    This is the only place the identity prompt is materialised, and it runs
    at install time — never mid-conversation. Mutating ``SOUL.md`` while a
    conversation is live is the exact pattern ``AGENTS.md`` forbids.
    """
    profile_dir = Path(args.profile_dir).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    doc = persona.load_persona_document()
    target = profile_dir / "SOUL.md"
    if target.exists() and not args.force:
        return _emit(
            {
                "ok": False,
                "error": "exists",
                "message": f"{target} already exists; pass --force to overwrite",
            }
        )
    target.write_text(doc.stable, encoding="utf-8")
    return _emit(
        {
            "ok": True,
            "wrote": str(target),
            "chars": len(doc.stable),
            "volatile_delivered_via": "MemoryProvider.prefetch -> user-message sidecar",
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="explicit sqlite path")
    parser.add_argument(
        "--persona", default=persona.PERSONA_ID, help="persona slug (default: grantley)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("decay", help="apply time decay to persona state")
    sub.add_parser("advance", help="draw and apply today's life beat (no LLM)")
    sub.add_parser("show", help="print current persona state")

    install = sub.add_parser("install-profile", help="write SOUL.md into a profile")
    install.add_argument("profile_dir", help="target profile directory")
    install.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    handlers = {
        "decay": cmd_decay,
        "advance": cmd_advance,
        "show": cmd_show,
        "install-profile": cmd_install_profile,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
