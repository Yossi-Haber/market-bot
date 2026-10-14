# Market Bot

## Production daily run

Run the complete bounded pipeline and deliver the validated Visual V3 card:

```powershell
python run_daily.py
```

Safe test without Telegram:

```powershell
python run_daily.py --dry-run
```

Manual QA through Visual V3 validation:

```powershell
python run_daily.py --skip-telegram
```

Refresh news discovery and Verification V2 when needed:

```powershell
python run_daily.py --refresh-marketaux --refresh-verification
```

Optional date selection:

```powershell
python run_daily.py --market-date 2026-09-25
```

The orchestrator sends `output/daily_report_card_v3.png` only after the publication, narrative, and Visual V3 gates pass. Warnings can remain publishable when the report status is `READY_WITH_WARNINGS`. Exit codes are `0` for success or skipped/dry-run delivery, `1` for pipeline failure, `2` for validation-blocked delivery, and `3` for Telegram delivery failure, including partial delivery.

### Optional sleep after a delivered scheduled report

The scheduler can request Windows Sleep only after a successful pipeline, a
date-matching report artifact, confirmed Telegram `SENT`, and durable scheduler
completion state. It is disabled by default. To opt in after testing, add to
`.env`:

```dotenv
MARKETBOT_SLEEP_AFTER_REPORT=true
MARKETBOT_SLEEP_DELAY_SECONDS=60
```

Safe decision simulation (does not run the pipeline, send Telegram, or sleep):

```powershell
python run_daily_scheduled.py --simulate-post-report
```

Windows Sleep/lid behavior is configured separately in Power Options. A closed
lid while the laptop stays awake can drain the battery; Windows critical-battery
protection may hibernate the computer before the report completes. The scheduler
does not override those protections or change the power plan.
