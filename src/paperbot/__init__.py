"""paperbot: an Interactive Brokers paper-trading bot (Trend Join Long).

Subpackages:
  trading    the engine: bot (orders, risk guard wiring), strategy, risk
  runtime    process control: runstate (STOP/pid/state files), cycle (scheduled
             one-pass runs), stopbot, kill_switch
  market     market data helpers: S&P 500 constituents, morning gap prefilter
  reporting  notifications (Telegram/ntfy) and the performance summary/dashboard
  ops        operations: Task Scheduler setup/cleanup, log rotation, setup check
paths.py is the single source of every file location.
"""
