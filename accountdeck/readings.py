"""Storing a usage reading of a saved account. One place for all four clients, so a refill is noticed whichever
client the account belongs to."""
from . import intel


def record(store, key, usage, *, reset=None):
    """Stores a successful reading, dated by its own `checked` time, and records the refills it shows against
    the reading the account had before it."""
    previous = (store.account(key) or {}).get("last_good_usage")
    store.observe(key, usage, at=usage.get("checked"), **({"reset": reset} if reset is not None else {}))
    refills(store, key, previous, usage)


def refills(store, key, previous, usage):
    """Records each limit that refilled between two readings of one account. The store keeps one row per
    account, window and reset time, so the same refill reported twice is recorded once."""
    for refill in intel.detect_refill(previous, usage):
        store.record_refill(key, refill["window"], reset_at=refill["reset_at"], confirmed_at=refill["confirmed_at"],
                            previous_used=refill["previous_used"])
