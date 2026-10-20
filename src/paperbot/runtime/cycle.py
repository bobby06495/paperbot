"""Moved to paperbot.core.scheduled_pass (kept so registered HT_Cycle tasks still run).

An alias, not a copy: importing this path returns the very same module object.
"""
import sys

from paperbot.core import scheduled_pass as _module

if __name__ == "__main__":
    sys.exit(_module.main())
else:
    sys.modules[__name__] = _module
