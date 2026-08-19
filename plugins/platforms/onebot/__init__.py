"""QQ platform plugin speaking OneBot v11 against NapCat / Lagrange / go-cqhttp.

Distinct from the built-in ``qqbot`` platform, which speaks Tencent's
official QQ Bot API v2.  See ``adapter.py`` and ``README.md``.
"""

from .adapter import register

__all__ = ["register"]
