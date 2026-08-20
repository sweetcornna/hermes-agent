#!/usr/bin/env python3
"""Out-of-band evolution driver for the Grantley persona (A3 G4).

Runs as a hermes cron job with ``no_agent: true``. The decay tick and daily
life beat are arithmetic/seeded-draw operations with zero model cost;
``illustrate`` is the explicit exception and calls the Cornna image provider
to write a local asset, never to publish or deliver it.

Subcommands::

    grantley_job.py decay            # recover fatigue, age topics
    grantley_job.py advance          # draw + apply today's life beat (idempotent per calendar day)
    grantley_job.py illustrate       # render today's local Grantley illustration (no publish)
    grantley_job.py show             # print current state (operator aid)
    grantley_job.py dedupe-events    # report/delete pre-fix duplicate auto_beat rows
    grantley_job.py install-profile  # write SOUL.md into a profile dir

Every subcommand prints one JSON object to stdout, so a cron notepad or a log
scraper can consume the result directly.

Storage resolution deserves a note. corlinman's equivalent job ran 1260 times
in production and failed all 1260 with ``data_dir_unavailable``, because it
resolved its database from an application-state attribute that was never
populated in that deployment — a silent, permanent no-op. Here resolution is
explicit and layered (``--db`` → ``$HERMES_HOME/plugin-data/grantley/data.db``
→ ``plugins.plugin_storage.plugin_db`` for in-repo development), and an unresolvable database
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

# Also make the repo root importable *before* the package import below, so
# ``grantley.life.now_dt()`` can reach ``hermes_time`` (the repo's shared
# configured-timezone clock) on its very first call. This matters here
# specifically: this script is the entry point hermes cron runs as a
# subprocess (``persona.decay`` / ``persona.life_advance``), and that
# subprocess environment deliberately strips hermes-owned PYTHONPATH
# entries (see ``tools/environments/local.py::_strip_hermes_owned_pythonpath``
# and the ``repo_root`` param ``corlinman_jobs`` bakes into its other
# generated entry scripts for the same reason) — so the repo root is not on
# ``sys.path`` here for free the way it is inside the main hermes process.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from grantley import jobs, life, persona, store as store_mod  # noqa: E402


def _resolve_db(explicit: str | None) -> Path:
    """Resolve the plugin database path. Raises with a usable message."""
    if explicit:
        return Path(explicit).expanduser()

    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        root = Path(hermes_home).expanduser() / "plugin-data" / "grantley"
        root.mkdir(parents=True, exist_ok=True)
        return root / "data.db"

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        from plugins.plugin_storage import plugin_data_dir  # noqa: PLC0415

        return plugin_data_dir("grantley") / "data.db"
    except Exception:
        pass

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
    return _emit(jobs.run_life_advance(st, args.persona, data_dir=db.parent))


def cmd_illustrate(args: argparse.Namespace) -> int:
    """Generate today's local Grantley illustration; never publish it."""
    db = _resolve_db(args.db)
    st = _open_store(db)
    return _emit(jobs.run_life_illustration(st, args.persona, data_dir=db.parent))


def cmd_dedupe_events(args: argparse.Namespace) -> int:
    """Report, or with ``--apply`` delete, duplicate ``auto_beat`` rows.

    Cleanup for rows a *pre-fix* duplicate ``advance`` run already wrote —
    ``jobs.run_life_advance`` itself is idempotent per ``(persona_id,
    calendar day)`` going forward and never calls this. Within each
    ``(persona_id, calendar day)`` group of more than one row, the earliest
    row is kept and every later one in that group is deleted; nothing
    outside a duplicate group is touched.

    Dry run by default — prints the plan (rows scanned, duplicate groups,
    rows that *would* be deleted and their ids) without deleting anything.
    Pass ``--apply`` to actually delete. Scoped to ``--persona`` unless
    ``--all-personas`` is given. The calendar day always uses Hermes's
    *configured* timezone (:func:`life.now_dt`'s tzinfo), matching exactly
    what ``run_life_advance`` uses to decide "same day" — never the host's
    local zone.
    """
    st = _open_store(_resolve_db(args.db))
    target_persona = None if args.all_personas else args.persona
    result = st.dedupe_daily_events(
        persona_id=target_persona,
        kind=args.kind,
        tz=life.now_dt().tzinfo,
        dry_run=not args.apply,
    )
    return _emit(result)


def cmd_show(args: argparse.Namespace) -> int:
    db = _resolve_db(args.db)
    st = _open_store(db)
    state = st.load_state(args.persona)
    doc = persona.load_persona_document()
    events = st.retrieve(args.persona)
    return _emit({
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
    })


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
        return _emit({
            "ok": False,
            "error": "exists",
            "message": f"{target} already exists; pass --force to overwrite",
        })
    target.write_text(doc.stable, encoding="utf-8")
    return _emit({
        "ok": True,
        "wrote": str(target),
        "chars": len(doc.stable),
        "volatile_delivered_via": "MemoryProvider.prefetch -> user-message sidecar",
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="explicit sqlite path")
    parser.add_argument(
        "--persona", default=persona.PERSONA_ID, help="persona slug (default: grantley)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("decay", help="apply time decay to persona state")
    sub.add_parser("advance", help="draw and apply today's life beat (no LLM)")
    sub.add_parser("illustrate", help="write today's local reference-anchored image")
    sub.add_parser("show", help="print current persona state")

    dedupe = sub.add_parser(
        "dedupe-events",
        help="report/delete duplicate auto_beat rows written before the idempotency fix",
    )
    dedupe.add_argument(
        "--kind", default="auto_beat", help="event kind to dedupe (default: auto_beat)"
    )
    dedupe.add_argument(
        "--all-personas",
        action="store_true",
        help="scope to every persona instead of just --persona",
    )
    dedupe.add_argument(
        "--apply",
        action="store_true",
        help="actually delete duplicates (default: dry run, report only)",
    )

    install = sub.add_parser("install-profile", help="write SOUL.md into a profile")
    install.add_argument("profile_dir", help="target profile directory")
    install.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    handlers = {
        "decay": cmd_decay,
        "advance": cmd_advance,
        "illustrate": cmd_illustrate,
        "show": cmd_show,
        "dedupe-events": cmd_dedupe_events,
        "install-profile": cmd_install_profile,
    }
    try:
        return handlers[args.command](args)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return _emit({"ok": False, "error": "job_failed", "message": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
