"""Terminal credentials and read-only usage. Storage rules adapted from ccswap (MIT).

Where the live login is kept (the credentials file, or on macOS Claude Code's keychain item) is the
`CredStore` seam in platform/credstore.py; `ClaudeCLI.store` is the one this machine uses."""
import copy
import hashlib
import math
import time

from ..config import cli_custom_home
from ..core import DeckError
from ..platform import credstore
from ..platform.credstore import object_file   # also imported from here by callers that read .claude.json
from ..usage import claude_usage
from .base import OAUTH_PROFILE_URL, OAUTH_USAGE_URL
from .http import request

SHARED_KEYS = frozenset({"mcpOAuth", "mcpOAuthClientConfig", "mcpXaaIdp", "mcpXaaIdpConfig", "pluginSecrets"})
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CUSTOM_MESSAGE = "A custom CLAUDE_CONFIG_DIR isn't supported yet"


def oauth(credentials):
    value = credentials.get("claudeAiOauth")
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k]
                                              for k in ("accessToken", "refreshToken")):
        raise DeckError("Sign in to Claude Code to save this terminal account.", "login_needed")
    return value


def fingerprint(credentials):
    return hashlib.sha256(oauth(credentials)["refreshToken"].encode()).hexdigest()


def expires(credentials):
    value = oauth(credentials).get("expiresAt")
    return float(value) / 1000 if type(value) in (int, float) and math.isfinite(value) else 0.0


def scoped(credentials):
    return copy.deepcopy({k: v for k, v in credentials.items() if k not in SHARED_KEYS})


def shared(credentials):
    return copy.deepcopy({k: v for k, v in credentials.items() if k in SHARED_KEYS})


def merge(target, live):
    return {**scoped(target), **shared(live)}


def profile(credentials, account, now):
    if not isinstance(account, dict) or not isinstance(account.get("accountUuid"), str) or not account["accountUuid"]:
        raise DeckError("Claude Code's account identity is not available yet.", "identity")
    fields = oauth(credentials)
    return {"provider": "claude_cli", "identity": account["accountUuid"],
            "email": account.get("emailAddress") or "", "organizationUuid": account.get("organizationUuid") or "",
            "plan": fields.get("subscriptionType") or fields.get("rateLimitTier") or "",
            "oauthAccount": copy.deepcopy(account), "credentials": scoped(credentials),
            "fingerprint": fingerprint(credentials), "captured": now, "refreshed": now,
            "health": "ok", "excluded": False}


class ClaudeCLI:
    def __init__(self, paths, http=None, *, clock=time.time, store=None):
        paths.require_cli_paths()
        self.paths, self.http, self.clock = paths, http or request, clock
        self.store = store if store is not None else credstore.for_paths(paths)

    @property
    def readonly_reason(self):
        return CUSTOM_MESSAGE if cli_custom_home() else None

    def guard(self):
        self.paths.require_cli_paths()
        if self.readonly_reason:
            raise DeckError(self.readonly_reason, "read_only")

    def current(self):
        self.guard()
        # Double read avoids joining files across a concurrent replacement. The service
        # also cross-checks the fingerprint against saved identities before accepting it.
        for _ in range(3):
            credentials = self.store.read()
            account = object_file(self.paths.claude_user_config).get("oauthAccount")
            if (credentials == self.store.read()
                    and account == object_file(self.paths.claude_user_config).get("oauthAccount")):
                return profile(credentials, account, self.clock())
        raise DeckError("Claude Code's login is changing. Try again shortly.", "busy")

    def active_identity(self):
        try:
            return self.current()["identity"]
        except DeckError as exc:
            if exc.code == "login_needed":
                return None
            raise

    def usage(self, credentials):
        data, meta = self.http("GET", OAUTH_USAGE_URL, token=oauth(credentials)["accessToken"])
        if not isinstance(data, dict):
            raise DeckError("Terminal usage returned an unreadable answer. The last reading is kept.", "network")
        return claude_usage(data, now=self.clock())

    def resolve_account(self, credentials, *, timeout=None):
        """The account a token really belongs to, from the provider (`.claude.json` can lag the token)."""
        extra = {} if timeout is None else {"timeout": timeout}
        data, _ = self.http("GET", OAUTH_PROFILE_URL, token=oauth(credentials)["accessToken"], **extra)
        account, org = (data or {}).get("account", {}), (data or {}).get("organization", {})
        return {"accountUuid": account.get("uuid"), "emailAddress": account.get("email", account.get("email_address", "")),
                "organizationUuid": org.get("uuid", "")}

    def refresh(self, credentials, *, timeout=8):
        old = oauth(credentials)
        data, _ = self.http("POST", TOKEN_URL, json_body={"grant_type": "refresh_token",
                            "refresh_token": old["refreshToken"], "client_id": CLIENT_ID}, timeout=timeout)
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise DeckError("The terminal login renewal returned an incomplete answer.", "network")
        ttl = data.get("expires_in")
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise DeckError("The terminal login renewal returned an incomplete answer.", "network")
        result = scoped(credentials)
        updated = result["claudeAiOauth"]
        updated.update(accessToken=data["access_token"], expiresAt=(self.clock() + ttl) * 1000)
        if isinstance(data.get("refresh_token"), str) and data["refresh_token"]:
            updated["refreshToken"] = data["refresh_token"]
        if isinstance(data.get("scope"), str):
            updated["scopes"] = data["scope"].split()
        return result
