- **[config/rules.json](config/rules.json)**: The risk envelope was widened materially without a matching proof that the strategy still satisfies the portfolio and broker guardrails.
  - **Context**: This branch increases `max_quantity_per_order` from `1` to `500`, `max_order_value` from `2500.0` to `30000.0`, `max_drawdown_usd` from `3000` to `6000`, and `max_daily_loss` from `1000.0` to `2000.0`. These are live risk controls, not cosmetic settings. A change of this size materially raises allowed exposure and can materially increase drawdown and loss under a bad move.
  - **Actionable Fix**: Recompute the values from the actual portfolio model and broker constraints, then add scenario-based regression tests showing the updated limits still satisfy value, risk, and drawdown constraints for representative account sizes before merge.
  - **Constraint**: Preserve the original trading strategy intent and risk logic while keeping exposure within the approved portfolio guardrails.

- **[tests/test_risk.py](tests/test_risk.py)**: The live-config regression check was weakened from risk enforcement to type validation.
  - **Context**: The updated test no longer asserts the actual approved risk values from the live config; it only checks that each field has the correct Python type. That means significant config drift can pass without failing the suite, which removes a key protection layer against silent risk-policy drift.
  - **Actionable Fix**: Restore exact-value or invariant-based checks for the approved live policy, or replace them with scenario tests that confirm the live limits remain within the safe operating envelope under realistic account conditions.
  - **Constraint**: Keep the config flexible where needed, but do not remove the checks that protect the strategy from silent risk drift.

- **[scripts/start_paperbot.ps1](scripts/start_paperbot.ps1)**: Startup remains ambiguous when multiple valid Python environments exist.
  - **Context**: The script picks the first valid interpreter from a local or parent `.venv`. In multi-environment workspaces, the wrong interpreter can be selected silently, which leads to a different dependency set and inconsistent runtime behavior.
  - **Actionable Fix**: Prefer a single verified environment or fail fast when more than one valid interpreter is present. The launcher should print the chosen interpreter and ensure it matches the intended project environment before it starts the bot.
  - **Constraint**: Preserve the Windows PowerShell convenience while guaranteeing the bot always runs against the verified runtime.

- **[src/paperbot/core/profile.py](src/paperbot/core/profile.py)**: The new profile system is structurally clean, but startup validation should be stricter to prevent stale or mismatched strategy config from silently reaching live execution.
  - **Context**: The profile loader validates `rules.json` for each strategy and resolves the selected profile, but the runtime still relies on a global active-profile selection model. A stale selection or wrong key can yield a materially different risk envelope without a strong runtime warning.
  - **Actionable Fix**: Emit a startup summary showing the resolved strategy key, loaded profile, and active limits, and fail closed if the config does not match the expected profile shape.
  - **Constraint**: Preserve the generic multi-profile architecture while ensuring startup remains deterministic and fail-closed.

- **[src/paperbot/strategies/rule_strategy.py](src/paperbot/strategies/rule_strategy.py)**: The entry-scan state is module-global and can go stale when the active profile changes.
  - **Context**: `_profile`, `_scan_bar`, `_scan_pos`, and `_scan_list` are reused across passes. If a profile reload or a strategy switch happens without resetting that state, the engine may continue scanning the wrong symbol list or wrong bar boundary.
  - **Actionable Fix**: Reset the scan state whenever the active profile changes and assert the active strategy key matches the selected profile before the scan continues. This keeps the generic strategy engine safe under a multi-profile runtime.
  - **Constraint**: Preserve the new generic strategy model while preventing stale scan state from affecting live entries.

- **[src/paperbot/blocks/context.py](src/paperbot/blocks/context.py)**: The evaluation context relies on a result object that is assumed to be fully initialized.
  - **Context**: Entry blocks mutate `result["price"]`, `result["gap"]`, and other fields while evaluation runs. If a block path creates a partial result or a new block omits one of those keys, the evaluation loop can fail mid-run with a `KeyError` rather than a clean “fail block” result.
  - **Actionable Fix**: Initialize all required result keys in one central place and guard block reads with safe defaults so a missing value becomes a clean validation failure instead of a runtime crash.
  - **Constraint**: Keep the block system modular and generic without breaking the evaluation contract that the strategy engine depends on.

- **[src/paperbot/core/engine.py](src/paperbot/core/engine.py)**: The long-running runner is safety-critical and needs stronger preflight validation before connection and order submission.
  - **Context**: The branch centralizes strategy loading and risk-gate validation, but the startup path still has a lot of moving parts. If the wrong profile or stale config reaches the runner, it can change risk and execution behavior before the bot exposes a visible warning.
  - **Actionable Fix**: Add an explicit startup summary and fail-fast preflight check that prints the selected strategy, the loaded profile, and the effective limits before connecting to IBKR.
  - **Constraint**: Preserve the orchestrated loop model while preventing a mismatched profile from reaching live trading.

- **[src/paperbot/runtime/cycle.py](src/paperbot/runtime/cycle.py)**: The loop sequencing is cleaner, but the ET boundary and force-close flows need stronger regression checks.
  - **Context**: The runtime logic now depends on session state, ET time-bound checks, and the post-close reconcile flow. A timing bug in the force-close and stop-reset order can leave a position unprotected or cancel the wrong order at the close.
  - **Actionable Fix**: Add targeted tests for ET-weekday boundaries, force-close transitions, and the post-close “await day orders done” flow to verify the stop protection and reconcile logic under those conditions.
  - **Constraint**: Keep the runtime loop modular and readable without weakening the protection logic that prevents unguarded positions.

- **[src/paperbot/trading/bot.py](src/paperbot/trading/bot.py)**: The compatibility wrapper is correct for backward compatibility, but it makes it easier to divide logic across modules without a clear ownership boundary.
  - **Context**: The file delegates to the core engine but keeps the older module path alive. That is useful for migration, but it also risks mixed execution paths when code still imports old modules while the runtime now prefers the core strategy engine.
  - **Actionable Fix**: Keep the compatibility wrapper as a thin shim only and ensure the runtime and tests use the new core path consistently so no old-path and new-path logic diverge.
  - **Constraint**: Preserve ease of migration while preventing duplicate or divergent execution paths in the same process.

- **[tests/test_strategy.py](tests/test_strategy.py)**: The strategy tests now exercise a profile-driven engine, but they still rely heavily on patched state and can hide wrong-profile bugs.
  - **Context**: The harness patches `_universe`, `ACTIVE`, and `_profile`, then validates sizing behaviors. This is deterministic, but it can mask a stale or wrong profile selection in the real runner.
  - **Actionable Fix**: Keep the deterministic mocks, but include at least one assertion against the real loaded profile and rules object before validating sizing decisions.
  - **Constraint**: Preserve the refactor to the generic strategy engine without allowing profile-selection bugs to hide behind the test harness.

- **[tests/test_risk_policy.py](tests/test_risk_policy.py)**: The policy checks are directionally correct but still need a tighter link to the approved live values.
  - **Context**: The tests now resolve the live limits by strategy key and compare them against a benchmark policy, which is a good step. However, without a strict invariant check, a drifting config can still be “approved” if it stays within a permissive benchmark.
  - **Actionable Fix**: Keep the benchmark and ensure the live values still satisfy the real guardrail invariants, not just approximate or type-correct values.
  - **Constraint**: Preserve the architecture migration while keeping the policy tests strict enough to catch silent risk drift.

- **[src/paperbot/core/risk.py](src/paperbot/core/risk.py)**: The fail-closed validation model is correct, but the current refactor makes configuration validation more important than ever.
  - **Context**: The module enforces a strict per-strategy `risk` object and rejects stale config keys. That is the right design, but it increases the consequence of a wrong profile or stale rules file because the risk envelope can change drastically without an obvious runtime warning.
  - **Actionable Fix**: Add explicit startup logging and config diagnostics that print the selected strategy key and the effective risk caps before the runner connects. This makes the migration safer and preserves fail-closed behavior.
  - **Constraint**: Preserve the new profile-based risk structure while keeping the bot impossible to start with a silently wrong risk configuration.

- **[src/paperbot/core/account.py](src/paperbot/core/account.py)**: The new account abstraction is sensible, but it should be validated against the actual IBKR account-value semantics before relying on it in production.
  - **Context**: Account-derived values such as net liquidation and buying power are safety-critical. If the new abstraction interprets account tags or missing values differently from the existing runner, it can change entry approvals and create a silent validation bug.
  - **Actionable Fix**: Add focused tests around real account-value parsing and fallback behavior for missing or unknown fields so the new path is proven before use in live execution.
  - **Constraint**: Preserve the modular account layer while ensuring the values used for order approval remain consistent with the broker’s actual account semantics.

- **[src/paperbot/reporting/telemetry.py](src/paperbot/reporting/telemetry.py)**: The telemetry/reporting changes appear aligned with the refactor, but they should remain separated from the trading safety path.
  - **Context**: Reporting signals are useful, but they must not create a hidden dependency loop or delay the trading loop. A reporting step that becomes slow or blocking inside the execution path can create latency and stale-state risk.
  - **Actionable Fix**: Keep telemetry and dashboard updates asynchronous and non-blocking relative to the execution path, and ensure they do not sit inside the entry-check or risk-validation hot path.
  - **Constraint**: Preserve observability and dashboard functionality while keeping latency and execution safety intact.