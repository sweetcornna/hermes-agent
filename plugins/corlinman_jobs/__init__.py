"""The corlinman scheduler's jobs, migrated to hermes-native cron.

Twelve jobs ran on the corlinman production host. Nine are ported here; three
are deliberately dropped with their reasons recorded in code
(:data:`~plugins.corlinman_jobs.specs.DROPPED_JOBS`). See
``docs/migration-corlinman/D1-cron-port-notes.md`` for the full mapping
table, the trade-offs and the cutover procedure.

Layout::

    plugins/corlinman_jobs/
    ├── plugin.yaml   kind: standalone (opt-in; registers one CLI command)
    ├── __init__.py   register(ctx) — the CLI command, and nothing else
    ├── specs.py      the nine JobSpecs + three DroppedJobs — single source of truth
    ├── prompts.py    prompt bodies, each tagged VERBATIM or RECONSTRUCTED
    ├── preflight.py  the checks that gate an install, expressed as data
    ├── installer.py  dry-run planner, file writer, job creator, drift detector
    └── scripts/corlinman_jobs_lib.py
                      job-side logic, copied verbatim into $HERMES_HOME/scripts/

Why a plugin and not a script: the install has to be re-runnable, has to be
inspectable before it runs, and has to refuse to run when the profile is
misconfigured. That is a program with a CLI, and hermes already has a seam
for exactly that (``ctx.register_cli_command``). It registers **no model
tools and no hooks** — an agent has no business creating these jobs, and the
plugin costs zero tokens because it adds nothing to any schema.

Loading this plugin has no effect beyond adding ``hermes corlinman-jobs`` to
the CLI. It never creates a job, never writes a file, and never enables
anything on import; every side effect is behind an explicit subcommand.

Safety, stated once and meant: three of these jobs write to a real public
QQ空间 feed that already holds 19 real posts, and nothing on that side can be
deleted. Every job is created paused, the dry-run planner never executes a
job body, and there is no code path in this package that enables a job.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The ``hermes <name>`` subcommand this plugin adds.
CLI_COMMAND = "corlinman-jobs"

__all__ = ["CLI_COMMAND", "register"]


def register(ctx) -> None:
    """Register the operator CLI. Called once by the plugin loader.

    Imports the installer lazily: it pulls in ``cron.jobs`` and the qzone
    state module, and a plugin that is merely *discovered* should not drag
    those into every hermes process.
    """
    from .installer import corlinman_jobs_command, register_cli

    ctx.register_cli_command(
        name=CLI_COMMAND,
        help="Install the migrated corlinman scheduler jobs (created paused)",
        setup_fn=register_cli,
        handler_fn=corlinman_jobs_command,
        description=(
            "Plan, install and inspect the corlinman → hermes cron migration. "
            "`plan` is a pure dry run; `install` creates every job PAUSED and "
            "refuses to proceed on a preflight failure or to overwrite a "
            "hand-edited script. Nothing here enables a job."
        ),
    )
