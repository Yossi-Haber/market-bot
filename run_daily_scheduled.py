"""Safe, date-aware launcher for the Windows scheduled daily report."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
RUN_AFTER = day_time(17, 15)
MARKET_CLOSE = day_time(16, 0)
OUTPUT = ROOT / "output"
LOG_ROOT = ROOT / "logs" / "daily_scheduler"
STATE_ROOT = LOG_ROOT / "state"
STATUS_PATH = OUTPUT / "daily_run_status.json"
DELIVERY_PATH = OUTPUT / "daily_report_delivery.json"
REPORT_PATH = OUTPUT / "daily_report_card_v3.png"
REPORT_DATA_PATH = OUTPUT / "daily_report_data.json"
VISUAL_VALIDATION_PATH = OUTPUT / "daily_report_v3_visual_validation.json"
DEFAULT_SLEEP_DELAY_SECONDS = 60


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter date, used to calculate the NYSE Good Friday closure."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed_fixed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_market_holiday(day: date) -> str | None:
    """Return a standard full NYSE closure name, or None.

    This deliberately covers recurring full-day holidays only; exceptional
    closures are not inferable without an external calendar service.
    Early closes are not modeled. The launcher uses a conservative 16:00 ET
    minimum and consequently will not run before the regular close.
    """
    year = day.year
    fixed = {
        _observed_fixed(date(year, 1, 1)): "New Year's Day",
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        _easter_sunday(year) - timedelta(days=2): "Good Friday",
        _last_weekday(year, 5, 0): "Memorial Day",
        _observed_fixed(date(year, 7, 4)): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        _observed_fixed(date(year, 12, 25)): "Christmas Day",
    }
    if year >= 2022:
        fixed[_observed_fixed(date(year, 6, 19))] = "Juneteenth"
    # NYSE does not observe New Year's on the prior Friday when Jan 1 is Saturday.
    if date(year, 1, 1).weekday() == 5:
        fixed.pop(date(year, 1, 1) - timedelta(days=1), None)
    return fixed.get(day)


def classify_day(day: date) -> tuple[bool, str]:
    if day.weekday() >= 5:
        return False, "WEEKEND"
    holiday = us_market_holiday(day)
    if holiday:
        return False, f"US_MARKET_HOLIDAY:{holiday}"
    return True, "TRADING_DAY"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def completed_for(day: date) -> bool:
    key = day.isoformat()
    state = _read_json(STATE_ROOT / f"{key}.json")
    if (
        state.get("status") == "SUCCESS"
        and state.get("market_date") == key
        and state.get("report_exists") is True
        and state.get("telegram_status") == "SENT"
    ):
        return True
    status = _read_json(STATUS_PATH)
    if (
        status.get("status") != "SUCCESS"
        or status.get("market_date") != key
        or status.get("telegram_sent") is not True
        or status.get("telegram_status") != "SENT"
    ):
        return False
    delivery = _read_json(DELIVERY_PATH)
    return (
        delivery.get("status") == "SENT"
        and delivery.get("report_date") == key
        and REPORT_PATH.is_file()
        and _read_json(REPORT_DATA_PATH).get("market_date") == key
        and _read_json(VISUAL_VALIDATION_PATH).get("valid") is True
    )


def post_report_sleep_eligible(
    *, pipeline_success: bool, report_exists: bool, telegram_status: str,
    completion_persisted: bool, retry_pending: bool | None, dry_run: bool,
    trading_day: bool = True,
) -> tuple[bool, str]:
    """Apply the fail-closed conditions for post-report sleep."""
    if dry_run:
        return False, "DRY_RUN"
    if not trading_day:
        return False, "NON_TRADING_DAY"
    if not pipeline_success:
        return False, "PIPELINE_NOT_SUCCESSFUL"
    if not report_exists:
        return False, "REPORT_ARTIFACT_MISSING"
    if telegram_status != "SENT":
        return False, "TELEGRAM_NOT_CONFIRMED_SENT"
    if not completion_persisted:
        return False, "COMPLETION_NOT_PERSISTED"
    if retry_pending is not False:
        return False, "RETRY_STATE_NOT_CONFIRMED_CLEAR"
    return True, "ELIGIBLE"


def sleep_configuration(environ: dict[str, str] | None = None) -> tuple[bool, int]:
    """Read opt-in sleep config; disabled by default and on invalid delay."""
    environ = os.environ if environ is None else environ
    enabled = environ.get("MARKETBOT_SLEEP_AFTER_REPORT", "false").strip().lower() in {"1", "true", "yes", "on"}
    try:
        delay = int(environ.get("MARKETBOT_SLEEP_DELAY_SECONDS", str(DEFAULT_SLEEP_DELAY_SECONDS)))
    except (TypeError, ValueError):
        return False, DEFAULT_SLEEP_DELAY_SECONDS
    if delay < 0 or delay > 3600:
        return False, DEFAULT_SLEEP_DELAY_SECONDS
    return enabled, delay


def _scheduler_environment() -> dict[str, str]:
    """Read only scheduler flags from .env without mutating process secrets."""
    values = dotenv_values(ROOT / ".env")
    selected = {
        key: str(values[key])
        for key in ("MARKETBOT_SLEEP_AFTER_REPORT", "MARKETBOT_SLEEP_DELAY_SECONDS")
        if values.get(key) is not None
    }
    selected.update({
        key: os.environ[key]
        for key in ("MARKETBOT_SLEEP_AFTER_REPORT", "MARKETBOT_SLEEP_DELAY_SECONDS")
        if key in os.environ
    })
    return selected


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_uint32),
        ("BatteryFullLifeTime", ctypes.c_uint32),
    ]


def battery_warning(logger: logging.Logger) -> None:
    """Log only available battery state; never estimate runtime."""
    if os.name != "nt":
        logger.info("BATTERY_STATUS unavailable platform=%s", os.name)
        return
    status = SYSTEM_POWER_STATUS()
    try:
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))
    except (AttributeError, OSError) as exc:
        logger.warning("BATTERY_STATUS unavailable error=%s", type(exc).__name__)
        return
    if not ok:
        logger.warning("BATTERY_STATUS unavailable winerror=%s", ctypes.get_last_error())
        return
    on_battery = status.ACLineStatus == 0
    percent = None if status.BatteryLifePercent == 255 else int(status.BatteryLifePercent)
    logger.info("BATTERY_STATUS on_battery=%s charge_percent=%s", on_battery, percent if percent is not None else "unknown")
    # BatteryFlag: bit 1 = low, bit 2 = critical; 255 indicates unknown.
    if on_battery and (status.BatteryFlag & 0x06 or percent is None):
        logger.warning("BATTERY_CHARGE_MAY_BE_INSUFFICIENT Windows reports low/critical or unknown battery charge; critical-battery protections remain enabled")


def request_system_sleep() -> None:
    """Request Windows standby (not hibernate/shutdown) through PowrProf."""
    if os.name != "nt":
        raise OSError("WINDOWS_SLEEP_UNAVAILABLE")
    powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    suspend = powrprof.SetSuspendState
    suspend.argtypes = (ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte)
    suspend.restype = ctypes.c_ubyte
    # bHibernate=False requests standby; wake events remain enabled.
    if not suspend(0, 0, 0):
        error = ctypes.get_last_error()
        raise OSError(error, "SetSuspendState failed")


def _flush_logs(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        handler.flush()


def maybe_sleep_after_report(logger: logging.Logger, *, delay_seconds: int, dry_run: bool = False) -> bool:
    """Wait for logs to flush, then issue the opt-in Windows standby request."""
    if dry_run:
        logger.info("SLEEP_INELIGIBLE reason=DRY_RUN")
        return False
    battery_warning(logger)
    logger.info("SLEEP_REQUEST_DELAY seconds=%d", delay_seconds)
    _flush_logs(logger)
    time.sleep(delay_seconds)
    requested_at = datetime.now(timezone.utc).isoformat()
    logger.info("SLEEP_REQUEST timestamp=%s mechanism=SetSuspendState_standby", requested_at)
    _flush_logs(logger)
    try:
        request_system_sleep()
    except Exception as exc:
        logger.error("SLEEP_REQUEST_FAILED error_class=%s winerror=%s", type(exc).__name__, getattr(exc, "winerror", None))
        return False
    logger.info("SLEEP_REQUEST_ACCEPTED")
    _flush_logs(logger)
    return True


def decision(now: datetime, *, completed: bool) -> dict[str, object]:
    """Evaluate a timezone-aware instant. ``now`` may use any timezone."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_now = now.astimezone()
    et_now = now.astimezone(ET)
    trading_day, day_reason = classify_day(et_now.date())
    close_eligible = et_now.timetz().replace(tzinfo=None) >= MARKET_CLOSE
    schedule_eligible = et_now.timetz().replace(tzinfo=None) >= RUN_AFTER
    eligible = trading_day and close_eligible and schedule_eligible and not completed
    if completed:
        reason = "ALREADY_COMPLETED"
    elif not trading_day:
        reason = day_reason
    elif not close_eligible:
        reason = "BEFORE_MARKET_CLOSE"
    elif not schedule_eligible:
        reason = "BEFORE_SCHEDULE_TIME"
    else:
        reason = "ELIGIBLE"
    return {
        "local_time": local_now.isoformat(),
        "new_york_time": et_now.isoformat(),
        "market_date": et_now.date().isoformat(),
        "trading_day": trading_day,
        "trading_day_status": day_reason,
        "market_close_eligible": close_eligible,
        "schedule_time_eligible": schedule_eligible,
        "completed": completed,
        "eligible": eligible,
        "reason": reason,
    }


def _logger(day_key: str) -> logging.Logger:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("market_bot.daily_scheduler")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_ROOT / f"{day_key}_scheduler.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console)
    return logger


@contextmanager
def _single_instance(day_key: str) -> Iterator[bool]:
    """Hold a per-day OS file lock; stale lock files are harmless."""
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    handle = (LOG_ROOT / f"{day_key}.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _write_completion(day_key: str, payload: dict) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    path = STATE_ROOT / f"{day_key}.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=STATE_ROOT, delete=False) as stream:
        temp_path = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp_path.replace(path)


def _run_pipeline(day_key: str, logger: logging.Logger, *, sleep_config: tuple[bool, int] | None = None) -> int:
    command = [sys.executable, str(ROOT / "run_daily.py"), "--market-date", day_key]
    logger.info("PIPELINE_COMMAND=%s", subprocess.list2cmdline(command))
    started = datetime.now(timezone.utc).isoformat()
    logger.info("PIPELINE_START=%s", started)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (LOG_ROOT / f"{day_key}_scheduler.log").open("a", encoding="utf-8") as log_stream:
        result = subprocess.run(command, cwd=ROOT, stdout=log_stream, stderr=subprocess.STDOUT, check=False)
    ended = datetime.now(timezone.utc).isoformat()
    status = _read_json(STATUS_PATH)
    delivery = _read_json(DELIVERY_PATH)
    telegram_status = str(status.get("telegram_status") or delivery.get("status", "UNKNOWN"))
    report_data = _read_json(REPORT_DATA_PATH)
    visual_validation = _read_json(VISUAL_VALIDATION_PATH)
    report_exists = (
        REPORT_PATH.is_file()
        and report_data.get("market_date") == day_key
        and visual_validation.get("valid") is True
    )
    telegram_confirmed = (
        status.get("telegram_sent") is True
        and telegram_status == "SENT"
        and delivery.get("status") == "SENT"
        and delivery.get("report_date") == day_key
    )
    pipeline_success = (
        result.returncode == 0
        and status.get("status") == "SUCCESS"
        and status.get("market_date") == day_key
    )
    logger.info("PIPELINE_END=%s exit_status=%s", ended, result.returncode)
    logger.info("PIPELINE_STATUS=%s", status.get("status", "UNKNOWN"))
    logger.info("REPORT_ARTIFACT path=%s status=%s", REPORT_PATH, "READY" if report_exists else "MISSING_OR_DATE_MISMATCH")
    logger.info("TELEGRAM_DELIVERY_STATUS=%s confirmed=%s", telegram_status, telegram_confirmed)
    eligible, sleep_reason = post_report_sleep_eligible(
        pipeline_success=pipeline_success,
        report_exists=report_exists,
        telegram_status=telegram_status if telegram_confirmed else "UNKNOWN_OR_FAILED",
        completion_persisted=False,
        retry_pending=False if pipeline_success else None,
        dry_run=False,
        trading_day=True,
    )
    logger.info("RETRY_ELIGIBILITY=%s", "NO_RETRY_NEEDED" if pipeline_success else "TASK_SCHEDULER_RETRIES_MAY_BE_PENDING")
    logger.info("SLEEP_ELIGIBILITY=%s reason=%s", "PENDING_COMPLETION" if eligible else "NO", sleep_reason)
    if pipeline_success and report_exists and telegram_confirmed:
        completion = {
            "status": "SUCCESS",
            "market_date": day_key,
            "started_at": started,
            "ended_at": ended,
            "exit_status": result.returncode,
            "report_path": str(REPORT_PATH),
            "report_exists": True,
            "report_date": report_data.get("market_date"),
            "telegram_confirmed": True,
            "telegram_status": telegram_status,
        }
        _write_completion(day_key, completion)
        logger.info("SCHEDULER_COMPLETED market_date=%s", day_key)
        logger.info("COMPLETION_PERSISTED market_date=%s", day_key)
        sleep_enabled, delay_seconds = sleep_config or sleep_configuration()
        if sleep_enabled:
            sleep_ok, sleep_reason = post_report_sleep_eligible(
                pipeline_success=True, report_exists=True, telegram_status="SENT",
                completion_persisted=True, retry_pending=False, dry_run=False,
            )
            logger.info("SLEEP_ELIGIBILITY=%s reason=%s", "YES" if sleep_ok else "NO", sleep_reason)
            if sleep_ok:
                maybe_sleep_after_report(logger, delay_seconds=delay_seconds)
        else:
            logger.info("SLEEP_ELIGIBILITY=NO reason=MARKETBOT_SLEEP_AFTER_REPORT_DISABLED")
        return 0
    logger.error("SCHEDULER_FAILED market_date=%s exit_status=%s pipeline_status=%s", day_key, result.returncode, status.get("status", "MISSING"))
    logger.info("SLEEP_ELIGIBILITY=NO reason=FAILURE_OR_UNCONFIRMED_DELIVERY")
    logger.info("RETRY_ELIGIBILITY=TASK_SCHEDULER_RETRIES_MAY_BE_PENDING; leaving system awake")
    return result.returncode or 1


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Market Bot daily pipeline safely from Task Scheduler.")
    parser.add_argument("--dry-run", action="store_true", help="Show eligibility and command without launching providers.")
    parser.add_argument("--simulate-post-report", action="store_true", help="Exercise post-report/sleep decisions without providers, Telegram, or actual Sleep.")
    args = parser.parse_args(argv)
    if args.simulate_post_report:
        scenarios = {
            "successful_report_and_telegram": dict(pipeline_success=True, report_exists=True, telegram_status="SENT", completion_persisted=True, retry_pending=False, dry_run=False),
            "failed_telegram": dict(pipeline_success=True, report_exists=True, telegram_status="FAILED", completion_persisted=False, retry_pending=False, dry_run=False),
            "unknown_telegram": dict(pipeline_success=True, report_exists=True, telegram_status="UNKNOWN", completion_persisted=False, retry_pending=False, dry_run=False),
            "pipeline_failure": dict(pipeline_success=False, report_exists=True, telegram_status="SENT", completion_persisted=False, retry_pending=None, dry_run=False),
            "retry_pending": dict(pipeline_success=False, report_exists=True, telegram_status="FAILED", completion_persisted=False, retry_pending=True, dry_run=False),
            "dry_run": dict(pipeline_success=True, report_exists=True, telegram_status="SENT", completion_persisted=True, retry_pending=False, dry_run=True),
            "missing_report": dict(pipeline_success=True, report_exists=False, telegram_status="SENT", completion_persisted=False, retry_pending=False, dry_run=False),
        }
        for name, conditions in scenarios.items():
            allowed, reason = post_report_sleep_eligible(**conditions)
            print(f"{name}: sleep_eligible={allowed} reason={reason} sleep_invoked=False")
        return 0
    instant = now or datetime.now(timezone.utc)
    et_date = instant.astimezone(ET).date().isoformat()
    logger = _logger(et_date)
    logger.info("CALENDAR_LIMITATION early closes and extraordinary one-off closures are not modeled; execution is gated at 16:00 ET and scheduled no earlier than 17:15 ET")
    result = decision(instant, completed=completed_for(date.fromisoformat(et_date)))
    logger.info("SCHEDULER_CHECK local_time=%s new_york_time=%s market_date=%s trading_day=%s status=%s close_eligible=%s schedule_eligible=%s completed=%s reason=%s",
                result["local_time"], result["new_york_time"], et_date, result["trading_day"], result["trading_day_status"], result["market_close_eligible"], result["schedule_time_eligible"], result["completed"], result["reason"])
    command = [sys.executable, str(ROOT / "run_daily.py"), "--market-date", et_date]
    if args.dry_run:
        print(f"Local time: {result['local_time']}")
        print(f"New York time: {result['new_york_time']}")
        print(f"Market date: {et_date}")
        print(f"Trading day: {result['trading_day']} ({result['trading_day_status']})")
        print(f"Market-close eligible (16:00 ET): {result['market_close_eligible']}")
        print(f"Scheduled-time eligible (17:15 ET): {result['schedule_time_eligible']}")
        print(f"Already completed: {result['completed']}")
        print(f"Decision: {result['reason']}")
        print("Pipeline command (not launched): " + subprocess.list2cmdline(command))
        print(f"Would execute now: {result['eligible']}")
        print("Sleep invoked: False (dry-run)")
        return 0
    if not result["eligible"]:
        logger.info("SCHEDULER_SKIPPED reason=%s", result["reason"])
        sleep_enabled, delay_seconds = sleep_configuration(_scheduler_environment())
        if result["reason"] == "ALREADY_COMPLETED" and result["trading_day"] and result["schedule_time_eligible"] and sleep_enabled:
            with _single_instance(et_date) as acquired:
                if acquired and completed_for(date.fromisoformat(et_date)):
                    logger.info("ALREADY_COMPLETED market_date=%s; no duplicate pipeline or Telegram delivery", et_date)
                    logger.info("SLEEP_ELIGIBILITY=YES reason=ALREADY_COMPLETED_AND_DELIVERY_CONFIRMED")
                    if maybe_sleep_after_report(logger, delay_seconds=delay_seconds):
                        logger.info("SLEEP_REQUEST_RETURNED_AFTER_WAKE")
                else:
                    logger.info("SLEEP_ELIGIBILITY=NO reason=RUN_LOCKED_OR_COMPLETION_NOT_CONFIRMED")
        return 0
    with _single_instance(et_date) as acquired:
        if not acquired:
            logger.info("SCHEDULER_SKIPPED reason=RUN_ALREADY_IN_PROGRESS")
            return 0
        if completed_for(date.fromisoformat(et_date)):
            logger.info("SCHEDULER_SKIPPED reason=ALREADY_COMPLETED_AFTER_LOCK")
            return 0
        try:
            return _run_pipeline(et_date, logger, sleep_config=sleep_configuration(_scheduler_environment()))
        except KeyboardInterrupt:
            logger.exception("SCHEDULER_INTERRUPTED")
            return 1
        except Exception:
            logger.exception("SCHEDULER_LAUNCH_FAILED")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
