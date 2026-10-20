"""Moved to paperbot.strategies.rule_strategy (the generic rules.json strategy engine).

An alias, not a copy: importing this path returns the very same module object,
so old-path and new-path code can never see different settings or state.
"""
import sys

from paperbot.strategies import rule_strategy as _module

sys.modules[__name__] = _module
