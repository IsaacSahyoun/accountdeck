"""Provider adapters: Claude Code (terminal), Codex and Muse Code, their HTTP and RPC transports, failure
classification and status sources. The engine builds the adapters with make_providers()."""
from __future__ import annotations

from .base import CredentialState, Reading
from .claude_cli import ClaudeCLI
from .codex import RPC, ActiveIdentity, Codex, TokenState, daemon_settings, refresh_due, token_state
from .errors import ProviderError, classify_http
from .muse import Muse

__all__ = ["CredentialState", "Reading",
           "RPC", "ActiveIdentity", "Codex", "TokenState", "daemon_settings", "refresh_due", "token_state",
           "ProviderError", "classify_http", "Muse", "make_providers"]


def make_providers(paths, http=None, procs=None):
    """{"codex", "claude_cli", "muse"} adapters for one machine layout."""
    return {"codex": Codex(paths, procs), "claude_cli": ClaudeCLI(paths, http), "muse": Muse(paths, procs)}
