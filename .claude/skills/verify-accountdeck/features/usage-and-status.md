# Usage meters, resets and status

Status: draft. The tests named here are in the final check, and `look_sweep.py` drew every scenario below on 2026-11-12. Nobody has driven these by hand through this skill.

## Sub-features
- Each account shows its limits: 5-hour, weekly, and a model's weekly limit where the client reports one.
- A note per limit: "Resets in 40m", "Refilled at 2:06 PM", "Starts with your next message", "Back in 1h 30m".
- The card "In use by your terminals", the Next reset card and the Auto-switch card on the left.
- A row says when its reading is old, when checks are paused, when sign-in is needed, and when the provider has an incident.
- Banners for an incident, a refused check, a pause and read-only mode.
- Refresh, by the button at the top right and by the menu bar's Refresh Now.

## How to get to it (user's view)
- Dashboard: the left column and every row. The page asks the app for a new state on a timer and when the window comes back to the front.
- Menu bar item: the status lines at the top of the menu, and notifications when an account is close to its limit.

## Driving it
Each state has a scenario: `resets-soon`, `refilled`, `not-started`, `resetting`, `stale`, `rate-limited`, `outage`, `blocked-cloudflare`, `needs-signin`, `fable-binding`, `codex-blocked`, `read-only`. To see one:
```sh
"$PY" -E -s tools/verify/look_sweep.py shoot "$PY" "$COPY" "$PROOF/look" outage stale
```
That writes `outage-light.png`, `outage-dark.png`, `outage.txt` (the words on the page) and `outage-cut.txt` (the words the page cuts off), and the same for `stale`.

Tests to reuse: `tests/test_readings.py` (`RefillTest`, `UpkeepTest`, `SchedulerTest`, `RetryAfterTest`, `MuseAnswerAgeTest`, `FlagsTest`, `ExampleStatesTest`); `tests/test_scenarios.py`; `tests/test_dashboard.py`, `ScenariosDrawTest` and `OtherClientsRowsTest`; `tests/test_second_review.py`, `OldPausesAreCappedTest`. `refill_probe.py` asks whether a refill is recorded for a Claude Code account.

## Gotchas
- The demo clock runs from its start. Pass `--now` (the tools do) when two runs must show the same numbers.
- Known flaw: the note beside a meter is cut off in the Desktop, Codex and Muse rows at the window's first width. See `rows-and-menus.md`.
- A row says a thing once. The screenshot compare of 2026-11-12 found "Paused" in a badge and again under the name; `OtherClientsRowsTest.test_a_row_says_a_provider_incident_once` holds that.

## Only on the real Mac
- Real usage numbers from each provider, and a real rate limit or incident.
- Notifications.
