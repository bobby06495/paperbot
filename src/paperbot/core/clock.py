"""Built-in scheduler: the daily jobs of the trading day, on the US Eastern clock.

Replaces the Windows Task Scheduler tasks with one cross-platform loop inside
the running bot (`paper-bot start`), so the same thing happens on Windows,
macOS, Linux and in a container. ET times come from zoneinfo, so daylight
saving needs no re-registration.

Jobs (weekdays, ET):
  rotate_logs    09:25          archive earlier days' logs, cap the safety log,
                                trim trades.csv (in-process; bot.log is closed
                                and reopened around it)
  prefilter      09:55 ... 12:55 every 30 min, only for strategies whose
                                universe is the morning watchlist (background)
  daily_summary  16:05          performance summary + push (background)

A job runs a time slot once. If the bot starts or reconnects after a slot,
the latest slot still within the job's grace period runs immediately; older
slots are skipped. A background job whose previous run is still going skips
the slot. Every run is recorded in logs/scheduler_state.json, which the
dashboard reads. Holidays are not modelled (the jobs are harmless then).
"""
import dataclasses
import datetime as dt
import subprocess
from pathlib import Path
from typing import Callable

from paperbot import paths
from paperbot.core.common import ET, hhmm, log
from paperbot.runtime import runstate

STATE_FILE = paths.LOGS_DIR / "scheduler_state.json"
PREFILTER_TIMES = ("09:55", "10:25", "10:55", "11:25", "11:55", "12:25", "12:55")


@dataclasses.dataclass
class Job:
    name: str
    times: tuple[str, ...]  # "HH:MM" ET
    grace: dt.timedelta  # how late a missed slot may still run
    command: list[str] | None = None  # background: a subprocess (python -m ...)
    call: Callable[[], None] | None = None  # in-process: a function
    enabled: Callable[[], bool] = lambda: True


class Clock:
    def __init__(self, jobs: list[Job], state_path: Path | None = None,
                 popen=subprocess.Popen):
        self.jobs = jobs
        self.state_path = state_path or STATE_FILE
        self._popen = popen
        self._running: dict[str, subprocess.Popen] = {}
        try:
            self.state = runstate.read_json_state(self.state_path) or {}
        except runstate.StateCorrupt:
            self.state = {}  # only bookkeeping: worst case a job runs again today
        self.state.setdefault("jobs", {})

    def _slot(self, job: Job, now: dt.datetime) -> dt.datetime | None:
        """The latest slot of `job` that is due now (and still within grace), or None."""
        if now.weekday() >= 5 or not job.enabled():
            return None
        today = [now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                 for t in sorted(hhmm(x) for x in job.times)]
        past = [s for s in today if s <= now]
        if not past or now - past[-1] > job.grace:
            return None
        last = self.state["jobs"].get(job.name, {}).get("slot")
        return None if last and last >= past[-1].isoformat() else past[-1]

    def due(self, now: dt.datetime) -> list[tuple[Job, dt.datetime]]:
        out = []
        for job in self.jobs:
            slot = self._slot(job, now)
            if slot is not None:
                out.append((job, slot))
        return out

    def _record(self, job: Job, **fields):
        entry = self.state["jobs"].setdefault(job.name, {})
        entry.update(fields)
        self.state["updated"] = dt.datetime.now(ET).isoformat(timespec="seconds")
        try:
            runstate.write_json_atomic(self.state_path, self.state)
        except OSError as e:
            log.warning("scheduler state not saved: %r", e)

    def _reap(self):
        """Record background jobs that have finished."""
        for name, proc in list(self._running.items()):
            code = proc.poll()
            if code is None:
                continue
            del self._running[name]
            job = next(j for j in self.jobs if j.name == name)
            status = "ok" if code == 0 else "failed"
            self._record(job, status=status, exit_code=code,
                         finished=dt.datetime.now(ET).isoformat(timespec="seconds"))
            (log.info if code == 0 else log.error)(
                "%sscheduled job %s finished with exit code %s",
                "" if code == 0 else "ALERT ", name, code)

    def tick(self, now: dt.datetime | None = None):
        """Run every job that is due. Never raises into the trading loop."""
        now = now or dt.datetime.now(ET)
        try:
            self._reap()
            for job, slot in self.due(now):
                self._run(job, slot, now)
        except Exception as e:  # noqa: BLE001 - scheduling must never stop trading
            log.error("ALERT scheduler error: %r", e)

    def _run(self, job: Job, slot: dt.datetime, now: dt.datetime):
        started = now.isoformat(timespec="seconds")
        if job.name in self._running:
            self._record(job, slot=slot.isoformat(), status="skipped",
                         detail="previous run still going", started=started)
            log.warning("scheduled job %s skipped at %s: previous run still going",
                        job.name, slot.strftime("%H:%M"))
            return
        log.info("scheduled job %s (%s ET slot)", job.name, slot.strftime("%H:%M"))
        if job.command:
            try:
                proc = self._popen(job.command, cwd=str(paths.PROJECT_ROOT))
            except OSError as e:
                self._record(job, slot=slot.isoformat(), status="failed", detail=repr(e),
                             started=started)
                log.error("ALERT scheduled job %s could not start: %r", job.name, e)
                return
            self._running[job.name] = proc
            self._record(job, slot=slot.isoformat(), status="running", pid=proc.pid,
                         started=started, detail="")
            return
        try:
            job.call()
            self._record(job, slot=slot.isoformat(), status="ok", started=started, detail="",
                         finished=dt.datetime.now(ET).isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001
            self._record(job, slot=slot.isoformat(), status="failed", detail=repr(e)[:300],
                         started=started)
            log.error("ALERT scheduled job %s failed: %r", job.name, e)

    def summary(self) -> dict:
        """For status output and the dashboard: {job: {times, slot, status, ...}}."""
        return {j.name: {"times": list(j.times), "enabled": j.enabled(),
                         **self.state["jobs"].get(j.name, {})} for j in self.jobs}


def default_jobs(profile, rotate: Callable[[], None]) -> list[Job]:
    """The trading-day jobs for the running strategy profile."""
    python = str(paths.PYTHON)
    watchlist = profile.universe.get("source") == "watchlist"
    return [
        Job("rotate_logs", ("09:25",), dt.timedelta(hours=6), call=rotate),
        Job("prefilter", PREFILTER_TIMES, dt.timedelta(minutes=25),
            command=[python, "-m", "paperbot.market.morning_prefilter"],
            enabled=lambda: watchlist),
        Job("daily_summary", ("16:05",), dt.timedelta(hours=3),
            command=[python, "-m", "paperbot.reporting.compute_perf"]),
    ]
