"""QQ platform plugin speaking OneBot v11 against SnowLuma / NapCat / Lagrange / go-cqhttp.

Distinct from the built-in ``qqbot`` platform, which speaks Tencent's
official QQ Bot API v2.  See ``adapter.py`` and ``README.md``.
"""

def register(ctx) -> None:
    from .adapter import register as register_platform

    register_platform(ctx)

    from . import addressee, proactive_judge

    register_task = getattr(ctx, "register_auxiliary_task", None)
    if callable(register_task):
        for task in (proactive_judge, addressee):
            register_task(
                task.TASK_KEY,
                display_name=task.TASK_DISPLAY_NAME,
                description=task.TASK_DESCRIPTION,
                defaults=dict(task.TASK_DEFAULTS),
            )

__all__ = ["register"]
