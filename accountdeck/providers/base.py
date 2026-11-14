"""Shared provider vocabulary: credential sets, endpoints, credential states and readings."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .. import __version__

OAUTH_HOST = "api.anthropic.com"
OAUTH_PATH = "/api/oauth/"
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = f"AccountDeck2/{__version__} (+local)"


class CredentialState(str, Enum):
    """How an account's usage can be read; the value is what the state contract shows."""
    LIVE = "live"                        # active in the official app, which owns the token
    FRESH = "fresh"                      # saved token valid for more than 3 minutes
    EXPIRED = "expired"                  # saved token expired: the last good reading is shown
    REAUTH_REQUIRED = "reauth_required"  # the saved sign-in no longer works: the user signs in again
    ACCESS_EXPIRED = "access_expired"    # Codex: the access token lapsed; the client renews it when it runs
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Reading:
    """One usage read. `usage` None means nothing new: keep the last good reading.

    `outcomes` lists (route, kind, http_class) per request made ("ok" for success), for the
    observed-outage rule. `credential` is a state the adapter asks the
    engine to record (None = unchanged). `meta` holds secret-free diagnostics only."""
    usage: dict | None
    source: str | None = None            # "oauth" | "helper"
    error: Exception | None = None       # a DeckError (usually ProviderError) when nothing was read
    outcomes: tuple = ()
    credential: str | None = None
    checked: float = 0.0
    meta: dict = field(default_factory=dict)
