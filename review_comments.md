- **[market_calendar.py:292–293, 352–354]**: **MEDIUM — Invalid IBKR hours can escape the supposedly non-raising market-hours check.**
  - **Context**: `parse_liquid_hours()` constructs `datetime.time` values from broker-provided strings without validating the format or catching invalid hours/minutes. Parsing occurs outside `cross_check_with_ibkr()`’s `try` block, so unexpected `liquidHours` data can raise during startup or the daily check and disrupt the bot.
  - **Actionable Fix**: Validate the date/time fields and catch parsing errors at the cross-check boundary. Log the malformed response and retain the calendar-only session rather than letting it abort the check.
  - **Constraint**: Keep valid IBKR closures and earlier closes authoritative; malformed data must not loosen the calendar’s safety gates or trigger orders.

- **[market_calendar.py:206–210, 316–318]**: **MEDIUM — An IBKR-reported later open is detected but not enforced.**
  - **Context**: `compare()` reports when IBKR’s open differs from the calendar, but `session()` only applies IBKR’s close and always retains the calendar’s open. I confirmed that applying IBKR hours of 11:00–16:00 still yields a 09:30 session. The strategy can therefore trade before the broker-reported open.
  - **Actionable Fix**: Apply the stricter open as well as the stricter close—for example, use the later of the calendar and IBKR opens—and ensure both scheduled gating and strategy session checks use it.
  - **Constraint**: Preserve normal and early-close behavior; a later broker-reported open must delay trading, not alter strategy rules or cause premature orders.

- **[test_index_switching.py:86, test_backtest_engine.py:28]**: **LOW — The full suite remains red after the configured strategy changes.**
  - **Context**: The full run reported 42 failures and 4 errors across 577 tests. Some assertions still expect the prior 3% gap and stop-price behavior, while `rules.json` now configures a 1.5% gap and a 0.75% stop.
  - **Actionable Fix**: Update affected expectations and fixtures to reflect the intended new configuration, then rerun the full suite to distinguish stale assertions from genuine regressions.
  - **Constraint**: Do not change the new strategy or risk settings merely to satisfy expectations that reflect the previous configuration.