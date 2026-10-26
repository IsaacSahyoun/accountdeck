"""How much terminal capacity is left, in "accounts' worth" (the menu bar totals). Pure: state + now in.

Only Claude Code (terminal) accounts count; Desktop "claude" rows are the same people. An account
that needs sign-in, that the owner left out, or whose 5-hour or weekly reading is unknown is not
counted, only named. Each counted window's share left at `now` (which may be after the snapshot):
not started → 100; a reset that passed since the reading → 100, assumed (approximate); blocked or
out → 0; else 100 − used. An account whose weekly limit is out has no usable 5-hour (or Fable 5.1)
room either. The totals are those shares summed over 100: 4.2 means four accounts and a fifth of one.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .rules import countdown_text
from .core import is_frozen

STALE = 3600            # a reading older than this makes its totals approximate
OUT_LEFT = 0.5          # ≤ this much left is out (rules.band)
MIN_GAIN = 0.05         # a refill must add at least this much to be the "next refill"
FABLE = "Fable"         # the Fable 5.1 weekly window's model
# Face thresholds, in accounts' worth: plenty from 2.0, watch from 0.75, alarm below; muted when nothing counts.
PLENTY, WATCH = 2.0, 0.75


@dataclass(frozen=True)
class Row:
    """One counted account. Shares are percent left (0–100); `fable` is None when not reported."""
    id: str
    label: str
    active: bool
    five: float
    weekly: float
    fable: float | None
    weekly_out: bool
    five_approx: bool
    weekly_approx: bool
    fable_approx: bool


@dataclass(frozen=True)
class Refill:
    label: str
    window: str         # "5-hour" | "weekly"
    gain: float         # accounts' worth added to that total
    at: float


@dataclass(frozen=True)
class Pool:
    rows: tuple = ()
    signin: tuple = ()      # labels of accounts that need sign-in
    left_out: tuple = ()
    unread: tuple = ()
    refill: Refill | None = None

    @property
    def capacity(self) -> int:
        return len(self.rows)

    @property
    def five(self) -> float:
        # 5-hour use also spends the weekly limit, so no account adds more 5-hour room than weekly room.
        return sum(min(r.five, r.weekly) for r in self.rows) / 100

    @property
    def weekly(self) -> float:
        return sum(r.weekly for r in self.rows) / 100

    @property
    def fable(self) -> float:
        return sum(r.fable for r in self.rows if r.fable is not None) / 100

    @property
    def fable_capacity(self) -> int:
        return sum(r.fable is not None for r in self.rows)

    @property
    def five_approx(self) -> bool:
        return any(r.five_approx for r in self.rows)

    @property
    def weekly_approx(self) -> bool:
        return any(r.weekly_approx for r in self.rows)

    @property
    def fable_approx(self) -> bool:
        return any(r.fable_approx for r in self.rows if r.fable is not None)


def _num(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def left_at(window, at, now):
    """(percent left at `now`, assumed) for one window read at `at`, or None when unknown."""
    if not isinstance(window, dict):
        return None
    if window.get("started") is False:
        return 100.0, False
    reset = _num(window.get("reset"))
    if reset is not None and reset <= now and (at is None or at < reset):
        return 100.0, True                  # resetting: the refill is assumed until a reading confirms it
    if window.get("blocked") or window.get("severity") == "out":
        return 0.0, False
    used = _num(window.get("used"))
    if used is None:
        return None
    return min(100.0, max(0.0, 100.0 - used)), False


def _windows(account):
    windows = [w for w in account.get("windows") or () if isinstance(w, dict)]
    five = next((w for w in windows if w.get("kind") == "session"), None)
    weekly = next((w for w in windows if w.get("kind") == "weekly"), None)
    fable = next((w for w in windows if w.get("kind") == "model" and str(w.get("model") or "").casefold()
                  == FABLE.casefold()), None)
    return five, weekly, fable


def _signin(account) -> bool:
    return account.get("health", "ok") != "ok" or (account.get("credential") or {}).get("state") == "reauth_required"


def _row(account, now):
    """A Row, or None when the account's 5-hour or weekly share is unknown."""
    at = _num(account.get("last_good_at"))
    if at is None:
        return None
    five_w, weekly_w, fable_w = _windows(account)
    five, weekly, fable = left_at(five_w, at, now), left_at(weekly_w, at, now), left_at(fable_w, at, now)
    if five is None or weekly is None:
        return None
    stale = now - at > STALE
    out = weekly[0] <= OUT_LEFT or bool(weekly_w.get("blocked"))
    label = str(account.get("label") or account.get("id") or "")
    return Row(str(account.get("id") or label), label, bool(account.get("active")),
               0.0 if out else five[0], weekly[0], None if fable is None else (0.0 if out else fable[0]), out,
               stale or five[1], stale or weekly[1], fable is not None and (stale or fable[1]))


def _refill(rows, accounts, now):
    """The soonest scheduled reset that adds room to the 5-hour or weekly total."""
    best = None
    for row in rows:
        five_w, weekly_w, _ = _windows(accounts[row.id])
        # A 5-hour refill adds nothing while the weekly limit is out.
        for name, window, left in (("5-hour", None if row.weekly_out else five_w, row.five),
                                   ("weekly", weekly_w, row.weekly)):
            reset = _num((window or {}).get("reset"))
            gain = (row.weekly - min(left, row.weekly) if name == "5-hour" else 100.0 - left) / 100
            if reset is None or reset <= now or gain < MIN_GAIN:
                continue
            if best is None or reset < best.at:
                best = Refill(row.label, name, gain, reset)
    return best


def summarize(state, now) -> Pool:
    rows, signin, left_out, unread, by_id = [], [], [], [], {}
    for account in (state or {}).get("accounts") or ():
        if not isinstance(account, dict) or account.get("provider") != "claude_cli":
            continue
        label = str(account.get("label") or account.get("id") or "")
        if is_frozen(account):     # another computer's sign-in: never counted here, like a left-out account
            left_out.append(label)
        elif _signin(account):
            signin.append(label)
        elif account.get("excluded"):
            left_out.append(label)
        else:
            row = _row(account, now)
            if row is None:
                unread.append(label)
            else:
                rows.append(row)
                by_id[row.id] = account
    return Pool(tuple(rows), tuple(signin), tuple(left_out), tuple(unread), _refill(rows, by_id, now))


# ---- words ----

def amount(value: float, approx: bool = False) -> str:
    """"4.2", or "~4.2" when approximate; one decimal rounded half up like model.js."""
    return ("~" if approx else "") + f"{math.floor(value * 10 + 0.5) / 10:.1f}"


def tone(total: float, capacity: int) -> str:
    """The totals' colour: plenty | watch | alarm, muted when nothing is counted."""
    if capacity <= 0:
        return "muted"
    return "plenty" if total >= PLENTY else "watch" if total >= WATCH else "alarm"


def _accounts(n: int) -> str:
    return f"{n} account" + ("" if n == 1 else "s")


def _total_line(name, total, capacity, approx) -> str:
    share = round(100 * total / capacity) if capacity else 0
    return f"{name} {amount(total, approx)} of {_accounts(capacity)} ({share}%)"


def _share(value, approx) -> str:
    return f"{'~' if approx else ''}{math.floor(value + 0.5)}%"


def _account_line(row: Row) -> str:
    five = _share(row.five, row.five_approx) + (" (weekly out)" if row.weekly_out else "")
    parts = [f"5-hour {five}", f"weekly {_share(row.weekly, row.weekly_approx)}"]
    if row.fable is not None:
        parts.append(f"Fable 5.1 {_share(row.fable, row.fable_approx)}")
    return f"{row.label}{' (in use)' if row.active else ''}: " + " · ".join(parts) + " left"


def _named(count_text: str, labels) -> list[str]:
    return [f"{len(labels)} {count_text} ({', '.join(labels)})"] if labels else []


def tooltip_lines(pool: Pool, now) -> list[str]:
    """The totals' detail text: totals, the next refill, each counted account, then who is not counted."""
    lines = ["Claude Code terminals: room left in accounts' worth"]
    if pool.capacity:
        lines.append(_total_line("5-hour", pool.five, pool.capacity, pool.five_approx))
        lines.append(_total_line("Weekly", pool.weekly, pool.capacity, pool.weekly_approx))
        if pool.fable_capacity:
            lines.append(_total_line("Fable 5.1", pool.fable, pool.fable_capacity, pool.fable_approx))
        if pool.refill is not None:
            refill = pool.refill
            lines.append(f"Next refill: {refill.label} +{amount(refill.gain)} ({refill.window}) in "
                         f"{countdown_text(refill.at - now)}")
        if pool.five_approx or pool.weekly_approx or pool.fable_approx:
            lines.append("~ an old reading, or a reset assumed to have refilled")
        lines.append("")
        lines += [_account_line(row) for row in pool.rows]
    else:
        lines.append("No terminal account can be counted right now.")
    extra = _named("needs sign-in", pool.signin) + _named("left out", pool.left_out) + _named("not read yet", pool.unread)
    if extra:
        lines += ([""] if pool.capacity else []) + extra
    return lines


def tray_line(pool: Pool) -> str | None:
    """A one-line summary ("Terminals 5h 4.2 · wk 5.6 / 6"), or None when there are no terminal accounts."""
    if not pool.capacity:
        return "Terminals: none counted" if pool.signin or pool.left_out or pool.unread else None
    return (f"Terminals 5h {amount(pool.five, pool.five_approx)} · wk {amount(pool.weekly, pool.weekly_approx)}"
            f" / {pool.capacity}")
