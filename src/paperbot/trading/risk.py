"""Moved to paperbot.core.risk.

An alias, not a copy: importing this path returns the very same module object,
so old-path and new-path code can never see different settings or state.
"""
import sys

from paperbot.core import risk as _module

sys.modules[__name__] = _module
