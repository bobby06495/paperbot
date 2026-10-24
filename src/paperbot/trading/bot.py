"""Moved to paperbot.core.engine (kept so `python -m paperbot.trading.bot` still works).

An alias, not a copy: importing this path returns the very same module object.
"""
import sys

from paperbot.core import engine as _module

if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
