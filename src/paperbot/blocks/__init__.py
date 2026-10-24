"""Strategy building blocks. Importing this package registers every block."""
from paperbot.blocks import entry, exit, manage, stop  # noqa: F401  (registration)
from paperbot.blocks.registry import KINDS, REQUIRED, Block, build, names, register

__all__ = ["KINDS", "REQUIRED", "Block", "build", "names", "register"]
