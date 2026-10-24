"""Old command names, kept as aliases of the cross-platform `paper-bot` command.

    paperbot-start [-Yes] [-DryRun]     = paper-bot start [--yes] [--dry-run]
    paperbot-shutdown [-Timeout N]      = paper-bot stop [--timeout N]

(The PowerShell launch scripts and Windows Task Scheduler setup they used to
run are replaced by `paper-bot start` and its built-in scheduler.)
"""
import sys

ALIASES = {"-yes": "--yes", "-dryrun": "--dry-run", "-timeout": "--timeout",
           "-skiptest": None, "-pause": None, "-cancelorders": None}


def _translate(argv: list[str]) -> list[str]:
    out = []
    for arg in argv:
        mapped = ALIASES.get(arg.lower(), arg)
        if mapped is not None:
            out.append(mapped)
    return out


def start() -> int:
    from paperbot import cli
    return cli.main(["start", *_translate(sys.argv[1:])])


def shutdown() -> int:
    from paperbot import cli
    return cli.main(["stop", *_translate(sys.argv[1:])])


if __name__ == "__main__":
    sys.exit(start())
