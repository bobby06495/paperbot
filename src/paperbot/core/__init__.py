"""The paper bot runner: connection, order handling, risk and sizing.

Nothing in core knows which strategy is running; a strategy is a profile loaded
from rules.json (core/profile.py) and run by strategies/rule_strategy.py.
"""
