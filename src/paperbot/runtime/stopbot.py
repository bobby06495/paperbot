"""Gracefully stop a running paperbot. Never kills or signals any process.

    paperbot-stop [--timeout SECONDS]      (same as python -m paperbot.runtime.stopbot)

Creates the STOP file and waits for the bot to confirm it has exited (the
bot removes logs/bot.pid on the way out). On its next loop the bot cancels
its working entry/exit orders, keeps the protective stops at IBKR, and
disconnects. Delete the STOP file before starting the bot again.

If the bot does not exit within the timeout (for example it is waiting on a
slow IBKR data request), it is NOT force-killed: check logs/bot.log, or press
Ctrl+C in the bot's own window.
"""
import argparse
import sys
import time

from paperbot.runtime import runstate


def main() -> int:
    parser = argparse.ArgumentParser(description="Gracefully stop a running paperbot.")
    parser.add_argument("--timeout", type=int, default=120,
                        help="seconds to wait for the bot to exit (default 120)")
    args = parser.parse_args()

    pid = runstate.running_pid()
    if pid is None:
        print("paperbot is not running (no live logs/bot.pid). Nothing to stop.")
        return 0

    runstate.request_stop()
    print(f"STOP file created; waiting up to {args.timeout}s for the bot (pid {pid}) to exit...")
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if runstate.running_pid() is None:
            print("paperbot stopped. Protective stops remain at IBKR. "
                  "Delete the STOP file before restarting.")
            return 0
        time.sleep(1)
    print(f"paperbot (pid {pid}) has not exited after {args.timeout}s. It was NOT killed. "
          "Check logs/bot.log, or press Ctrl+C in the bot's window.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
