"""Historical backtesting for paperbot's strategies (simulation only, never trades).

  config       backtesting/config/backtest.json: every execution and cost assumption
  data         providers (IBKR read-only, Yahoo, synthetic for tests), cache, validation
  engine       bar-by-bar simulator driving the live decision functions (trading.signals)
               and the live risk guard (trading.risk.RiskGuard)
  metrics      performance/risk metrics and validation periods from the run ledgers
  analysis     plain-English summaries, research questions, limitations
  experiments  rule variants kept apart from config/rules.json, and their comparison
  runs         run folders, metadata, lock, dataset usage, retention
  report       HTML reports and the dashboard's BACKTESTING data
  runner, cli  paperbot-backtest

Importing this package never imports paperbot.trading.bot or .strategy (the live
order code) and never connects to IBKR.
"""
