"""Rule experiments: variants of one rules.json strategy, kept in backtesting/experiments/.

An experiment file names its strategy, the settings that change (paths inside that
strategy's section, list positions as numbers: "entry.7.min", "stop.pct",
"manage.1.at_R", "time_filter.earliest_entry_et"), the hypothesis and why it is
worth testing. apply() returns a NEW section; validate() loads the variant with the
real profile loader (core/profile.py), so a variant the bot would refuse is refused
here too. Nothing in this module ever writes config/rules.json (tests check its bytes).

Selection discipline: variants are compared on the VALIDATION segment. The
out-of-sample segment is shown only as an untouched-data check and must not be
used to pick a winner; every evaluation is counted per dataset (runs.record_usage).
"""
import copy
import datetime as dt
import json
import math
import random
import re
import tempfile
from pathlib import Path

from paperbot import paths
from paperbot.core import profile as profiles
from paperbot.core.common import hhmm

ID_RE = re.compile(r"^EXP-[A-Za-z0-9_.-]{1,80}$")
COMPARE_KEYS = ("net_pnl", "return_pct", "trades", "win_rate_pct", "profit_factor", "expectancy",
                "max_drawdown_pct", "commission")


class ExperimentError(ValueError):
    pass


def _walk(node, path: str):
    """(parent, key) of a dotted path; list positions are numbers."""
    parts = path.split(".")
    for part in parts[:-1]:
        node = _step(node, part, path)
    last = parts[-1]
    _step(node, last, path)
    return node, (int(last) if isinstance(node, list) else last)


def _step(node, part, path):
    if isinstance(node, list):
        if not part.isdigit() or int(part) >= len(node):
            raise ExperimentError(f"unknown setting {path!r}")
        return node[int(part)]
    if not isinstance(node, dict) or part not in node:
        raise ExperimentError(f"unknown setting {path!r}")
    return node[part]


def get(section: dict, path: str):
    parent, key = _walk(section, path)
    return parent[key]


def apply(section: dict, changes: dict) -> dict:
    """A deep copy of one strategy section with `changes` applied. Only existing settings
    can change, keeping their type."""
    out = copy.deepcopy(section)
    for path, value in changes.items():
        parent, key = _walk(out, path)
        old = parent[key]
        if isinstance(old, bool) != isinstance(value, bool) or (
                isinstance(old, (int, float)) and not isinstance(value, (int, float))) or (
                isinstance(old, str) and not isinstance(value, str)):
            raise ExperimentError(f"{path}: {value!r} has a different type than {old!r}")
        parent[key] = value
    return out


def variant_rules(rules: dict, strategy: str, changes: dict) -> dict:
    """The whole rules.json with one strategy's section changed (a copy)."""
    out = copy.deepcopy(rules)
    if strategy not in out.get("strategies", {}):
        raise ExperimentError(f"rules.json has no strategy {strategy!r}")
    out["strategies"][strategy] = apply(out["strategies"][strategy], changes)
    return out


def validate(rules: dict, strategy: str, changes: dict, config: dict | None = None):
    """Load the variant with core/profile.py; ExperimentError if the bot would refuse it."""
    if paths.CONFIG_FILE.exists() and config is None:
        config = json.loads(paths.CONFIG_FILE.read_text())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.json"
        path.write_text(json.dumps(variant_rules(rules, strategy, changes)), encoding="utf-8")
        try:
            return profiles.load(strategy, path, config)
        except profiles.ConfigError as e:
            raise ExperimentError(f"variant is not a valid strategy: {e}") from None


def check(exp: dict):
    for key in ("id", "strategy_id", "hypothesis", "reason", "changes"):
        if key not in exp:
            raise ExperimentError(f"experiment is missing {key!r}")
    if not ID_RE.match(exp["id"]):
        raise ExperimentError(f"experiment id {exp['id']!r} must look like EXP-name")
    if not isinstance(exp["changes"], dict) or not exp["changes"]:
        raise ExperimentError("experiment changes must be a non-empty object")


def load(exp_id: str, folder: Path | None = None) -> dict:
    folder = folder or paths.BACKTEST_EXPERIMENTS_DIR
    path = folder / f"{exp_id}.json"
    try:
        exp = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ExperimentError(f"no experiment {exp_id!r} in {folder}") from None
    except ValueError as e:
        raise ExperimentError(f"{path.name}: invalid JSON: {e}") from None
    check(exp)
    return exp


def list_all(folder: Path | None = None, strategy: str | None = None) -> list[dict]:
    folder = folder or paths.BACKTEST_EXPERIMENTS_DIR
    out = []
    for path in sorted(folder.glob("EXP-*.json")):
        try:
            exp = json.loads(path.read_text(encoding="utf-8"))
            check(exp)
        except (ValueError, ExperimentError) as e:
            out.append({"id": path.stem, "invalid": str(e)})
            continue
        if strategy is None or exp["strategy_id"] == strategy:
            out.append(exp)
    return out


def save(exp: dict, folder: Path | None = None, overwrite: bool = False) -> Path:
    check(exp)
    folder = folder or paths.BACKTEST_EXPERIMENTS_DIR
    path = (folder / f"{exp['id']}.json").resolve()
    if path == paths.RULES_FILE.resolve() or path.parent == paths.CONFIG_DIR.resolve():
        raise ExperimentError("refusing to write into config/: experiments never touch production rules")
    if path.exists() and not overwrite:
        return path
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(exp, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- proposals

def _later(text: str, minutes: int) -> str:
    t = dt.datetime.combine(dt.date(2000, 1, 3), hhmm(text)) + dt.timedelta(minutes=minutes)
    return t.strftime("%H:%M")


def _ideas(section: dict) -> list[tuple]:
    """(slug, changes, hypothesis, reason) for the blocks this strategy actually uses."""
    ideas = []
    for i, b in enumerate(section.get("entry", [])):
        kind, label = b.get("type"), b.get("label") or b.get("type")
        if kind == "min_rvol":
            ideas.append(("rvol-lower", {f"entry.{i}.min": round(b["min"] * 0.75, 2)},
                          f"A lower relative-volume threshold ({label}) admits more breakouts without "
                          "lowering their average result.",
                          "The volume check is the last entry filter; it may be stricter than needed."))
        elif kind == "min_gap_pct":
            ideas.append(("gap-higher", {f"entry.{i}.min_pct": round(b["min_pct"] + 1.0, 2)},
                          f"A larger opening gap ({label}) selects stronger moves and a better average "
                          "trade, at the cost of fewer trades.",
                          "The gap threshold defines the whole watchlist; its effect on trade quality "
                          "is untested."))
        elif kind == "rsi_below":
            ideas.append(("rsi-deeper", {f"entry.{i}.max": round(b["max"] / 2, 2)},
                          f"Waiting for a deeper pullback ({label}) improves the entry price enough to "
                          "offset fewer signals.",
                          "The pullback depth is a single fixed number that has not been varied."))
        elif kind == "below_vwap_sigma":
            ideas.append(("vwap-wider", {f"entry.{i}.sigma": round(b["sigma"] + 0.5, 2)},
                          f"Requiring a larger stretch below VWAP ({label}) gives entries with more "
                          "room to revert.",
                          "Shallow stretches may be noise rather than dislocations."))
    stop = section.get("stop", {})
    if stop.get("type") == "low_of_day_minus_pct":
        ideas.append(("stop-wider", {"stop.pct": round(stop["pct"] * 2, 2)},
                      "A wider initial stop reduces premature stop-outs enough to offset the smaller "
                      "position it implies (risk per trade is fixed).",
                      "Stop distance drives both position size and how often trades are stopped out."))
    elif stop.get("type") == "atr_multiple":
        ideas.append(("stop-wider", {"stop.multiple": round(stop["multiple"] + 1.0, 2)},
                      "A wider ATR stop lets multi-day pullbacks recover instead of stopping out.",
                      "Stop distance drives both position size and how often trades are stopped out."))
    elif stop.get("type") == "sigma_below_entry":
        ideas.append(("stop-wider", {"stop.sigma": round(stop["sigma"] + 0.5, 2)},
                      "A wider stop gives mean-reversion trades room to work.",
                      "Most mean-reversion losses may come from stops inside normal noise."))
    for i, b in enumerate(section.get("manage", [])):
        if b.get("type") == "breakeven":
            ideas.append(("breakeven-later", {f"manage.{i}.at_R": round(b["at_R"] + 0.5, 2)},
                          "Moving the stop to breakeven later lets more winners develop.",
                          "Early breakeven protects capital but may cut trends short."))
    for i, b in enumerate(section.get("exit", [])):
        if b.get("type") == "time_stop" and b.get("days"):
            ideas.append(("hold-longer", {f"exit.{i}.days": int(math.ceil(b["days"] * 1.5))},
                          "Holding longer before the time stop captures more of the recovery.",
                          "The time limit is a fixed guess."))
        elif b.get("type") == "time_stop" and b.get("minutes"):
            ideas.append(("hold-longer", {f"exit.{i}.minutes": int(b["minutes"] * 2)},
                          "Allowing more time before the time stop lets more trades reach the target.",
                          "The time limit is a fixed guess."))
    tf = section.get("time_filter", {})
    if section.get("holding") == "intraday" and tf.get("earliest_entry_et") and \
            hhmm(tf["earliest_entry_et"]) < dt.time(11, 0):
        ideas.append(("entry-later", {"time_filter.earliest_entry_et": _later(tf["earliest_entry_et"], 25)},
                      "Waiting longer after the open avoids the noisiest part of the session.",
                      "The opening half hour is the most volatile; the current start time is untested."))
    return ideas


def propose(strategy: str, rules: dict, baseline: dict | None = None) -> list[dict]:
    """Experiment ideas for one strategy, from its blocks and (if available) the baseline
    run's measured results. Each is a hypothesis to test, not a recommendation."""
    section = rules["strategies"][strategy]
    base_run = (baseline or {}).get("metadata", {}).get("run_id")
    funnel = (baseline or {}).get("metrics", {}).get("funnel", {}) or {}
    trades = (baseline or {}).get("trades", []) or []
    initial = sum(1 for t in trades if str(t.get("exit_reason", "")).startswith("initial stop"))
    blockers = sorted(((k, v) for k, v in funnel.items() if k != "pass"), key=lambda x: -x[1])
    evidence = (f"Baseline run {base_run}: {len(trades)} trades, {initial} ended at the initial stop; "
                f"most frequent blocking check: {blockers[0][0] if blockers else 'n/a'}."
                if base_run else "No baseline run yet: proposed from the configuration alone.")
    out = []
    for slug, changes, hypothesis, reason in _ideas(section):
        try:
            validate(rules, strategy, changes)
        except ExperimentError:
            continue
        out.append({"id": f"EXP-{strategy}-{slug}", "strategy_id": strategy,
                    "title": f"{section.get('label', strategy)}: {slug.replace('-', ' ')}",
                    "status": "proposed", "created": dt.date.today().isoformat(),
                    "hypothesis": hypothesis, "reason": reason, "evidence": evidence,
                    "changes": changes,
                    "baseline_values": {k: get(section, k) for k in changes},
                    "data_periods": "Choose on the validation segment only. The out-of-sample "
                                    "segment is reported once as an untouched-data check.",
                    "approval": "Not applied. Changing config/rules.json requires a human review."})
    return out


# ---------------------------------------------------------------- comparison

def _bootstrap_diff(a: list[float], b: list[float], samples: int, seed: int):
    """95% bootstrap range of mean(b) - mean(a); None with fewer than 5 trades each."""
    if len(a) < 5 or len(b) < 5:
        return None
    rng = random.Random(seed)
    diffs = sorted(sum(rng.choice(b) for _ in b) / len(b) - sum(rng.choice(a) for _ in a) / len(a)
                   for _ in range(samples))
    return [round(diffs[int(0.025 * samples)], 2), round(diffs[int(0.975 * samples) - 1], 2)]


def _segment_nets(run: dict, segment: str) -> list[float]:
    seg = run["metrics"].get("periods", {}).get("segments", {}).get(segment, {})
    if not seg.get("start"):
        return []
    return [float(t["net_pnl"]) for t in run["trades"] if t.get("status") == "closed"
            and seg["start"] <= str(t.get("exit_time_et", ""))[:10] <= seg["end"]]


def _delta(v: dict, b: dict, seg: str):
    a, c = b.get(seg, {}).get("net_pnl"), v.get(seg, {}).get("net_pnl")
    return None if a is None or c is None else round(c - a, 2)


def compare(baseline: dict, variants: list[dict], cfg: dict, usage_entry: dict | None) -> dict:
    """Comparison of loaded runs (runs.load_run). Ranking uses the validation segment."""
    val = cfg["validation"]
    columns = [baseline] + variants
    rows = []
    for seg in ("in_sample", "validation", "out_of_sample"):
        for key in COMPARE_KEYS:
            rows.append({"segment": seg, "metric": key,
                         "values": [r["metrics"].get("periods", {}).get("segments", {})
                                    .get(seg, {}).get(key) for r in columns]})
    for key in ("net_pnl", "max_drawdown_pct", "completed_trades", "total_commission",
                "total_slippage", "profit_factor", "win_rate_pct"):
        rows.append({"segment": "full", "metric": key, "values": [r["metrics"].get(key) for r in columns]})
    findings = []
    base_val = _segment_nets(baseline, "validation")
    b_seg = baseline["metrics"].get("periods", {}).get("segments", {})
    for run in variants:
        meta = run["metadata"]
        diff = _bootstrap_diff(base_val, _segment_nets(run, "validation"),
                               val["bootstrap_samples"], val["bootstrap_seed"])
        v_seg = run["metrics"].get("periods", {}).get("segments", {})
        dv, doos = _delta(v_seg, b_seg, "validation"), _delta(v_seg, b_seg, "out_of_sample")
        if diff is None:
            verdict = "not enough validation trades to compare (fewer than 5 each)"
        elif diff[0] > 0:
            verdict = "better average trade on validation data (bootstrap range above zero)"
        elif diff[1] < 0:
            verdict = "worse average trade on validation data (bootstrap range below zero)"
        else:
            verdict = "no clear difference on validation data (bootstrap range includes zero)"
        findings.append({
            "experiment_id": meta.get("experiment_id"), "run_id": meta.get("run_id"),
            "changes": (meta.get("experiment") or {}).get("changes", {}),
            "hypothesis": (meta.get("experiment") or {}).get("hypothesis"),
            "validation_net_pnl_delta": dv, "out_of_sample_net_pnl_delta": doos,
            "mean_trade_diff_ci95": diff, "verdict": verdict,
            "holds_out_of_sample": None if not doos or not dv else ((dv > 0) == (doos > 0)),
            "material": bool(diff and (diff[0] > 0 or diff[1] < 0))})
    return {"strategy_id": baseline["metadata"].get("strategy_id"),
            "baseline_run": baseline["metadata"].get("run_id"),
            "variant_runs": [r["metadata"].get("run_id") for r in variants],
            "labels": ["Baseline"] + [r["metadata"].get("experiment_id") for r in variants],
            "dataset_id": baseline["metadata"].get("dataset_id"),
            "same_dataset": all(r["metadata"].get("dataset_id") == baseline["metadata"].get("dataset_id")
                                for r in variants),
            "rows": rows, "findings": findings, "walk_forward": walk_forward(columns),
            "variants_evaluated_on_dataset": len((usage_entry or {}).get("variants", [])),
            "selection_rule": "Variants are judged on the validation segment. Out-of-sample results are "
                              "shown only to check that a difference holds on untouched data; they are "
                              "never used to pick a variant.",
            "approval": "No rule change is applied automatically. A variant that holds up must be "
                        "reviewed and copied into config/rules.json by a person."}


def walk_forward(columns: list[dict]) -> dict | None:
    """For each fold, pick the variant that did best in the PREVIOUS fold and record how
    that pick did in this fold: 'choose by recent results' vs the baseline."""
    folds = [r["metrics"].get("periods", {}).get("folds", []) for r in columns]
    k = min((len(f) for f in folds), default=0)
    if k < 2:
        return None
    labels = ["Baseline"] + [r["metadata"].get("experiment_id") for r in columns[1:]]
    steps, chosen_total, base_total = [], 0.0, 0.0
    for i in range(1, k):
        prev = [f[i - 1].get("net_pnl") or 0.0 for f in folds]
        pick = max(range(len(columns)), key=lambda j: prev[j])
        got, base = folds[pick][i].get("net_pnl") or 0.0, folds[0][i].get("net_pnl") or 0.0
        chosen_total += got
        base_total += base
        steps.append({"fold": i + 1, "chosen": labels[pick], "chosen_net_pnl": round(got, 2),
                      "baseline_net_pnl": round(base, 2)})
    return {"steps": steps, "chosen_total": round(chosen_total, 2), "baseline_total": round(base_total, 2)}
