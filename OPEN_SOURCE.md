# Open-source references

AccountDeck is a Python/web implementation. These projects informed its design. Recheck the pinned upstream revisions when provider behavior changes.

| Reference | Inspected revision | Influence |
|---|---|---|
| [AI Account Hub](https://github.com/AlexC1991/AI_Account_Hub) | `4ed637e670a65be44dbcad44ca8c7699615b50ed` | Unified account workflow, official-tool launching, app-server usage, separation of Desktop and CLI enrollment |
| [Claude Account Switcher](https://github.com/SnlperStripes/claude-account-switcher) | `d64fd68da061090b6b8280cfa93bc348978d0805` | Selective Desktop authentication fields, Chromium encryption format and cookie handoff |
| [ccswap](https://github.com/errhythm/ccswap) | `14df53cf3eba7c86a10d03698b958f7ba809b1c4` | Modern Claude model-scoped usage, refresh-token ownership, auth-only Codex switching |
| [claude-code-sessions](https://github.com/craigstoller/claude-code-sessions) | `c98888fa45d5a99c72f1c33a8cc90ff007284356` | Session identity/deletion behavior, avoiding account-specific connector/permission transfer, conflict-aware rollback |
| [ai-multi-instance](https://github.com/Zoltak-Dev/ai-multi-instance) | `4a977e5c2232f719fde98e023ba71f262ed7c7aa` | Claude cookie-backed usage fallback and one-time capture while Desktop is closed |
| [Codex Account Switcher](https://github.com/korsun009/codex-account-switcher) | `709ca1c91fdb0e885b7c0cc5337769149f2a20ca` | Windows DPAPI vault and shared configuration/history |
| [Codex Account Switcher](https://github.com/liuzhao1225/codex-account-switcher) | `7312cfac3184b77e756a704748a9dab04b443ef8` | Identity verification around account handoff |

Versions before 2.1 also switched Claude Desktop. The Desktop parts of this table informed that code, which 2.1 removed.

All listed projects are MIT except SnlperStripes/claude-account-switcher, which is Apache-2.0. No upstream graphical application or executable is installed by AccountDeck. The notice for adapted source is included below.

Runtime dependency: [cryptography](https://github.com/pyca/cryptography), distributed under Apache-2.0 or BSD-3-Clause. Its installed package includes its license notices.

Official interface references:

- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
- [Claude Desktop Code](https://code.claude.com/docs/en/desktop)
- [Claude remote connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)

Claude's saved-login and usage adapters use community-observed behavior and require maintenance if the app changes its storage or endpoints. Codex's installed schemas and CLI help were checked before implementing its adapter. The internal-only external-token login variant is not used.

## AccountDeck 2 additions

The v2 implementation uses the official app-server login protocol in a fresh helper home and read-only verification; it does not invoke logout for switching.

Design references: SnlperStripes (Apache-2.0), selective auth exchange and process settling; ccswap (MIT), file-store rejection and fail-closed polling; AI Account Hub (MIT), guarded synchronization; liuzhao1225 (MIT), identity checks before writes.

The design research also credits claudepot (ISC) for preflight/family discovery/status classification, CodexBar (MIT) for reset boundaries/compact rows/pace, claude-swap (MIT) for retry constants, ai-usagebar (MIT) for active-account identity, 10Lollipop (MIT) for abort-on-change and openai/codex (Apache-2.0) for daemon/login interface behavior. These are design references, not redistributed source in this finish pass. The macOS menu bar item uses the project's own ctypes/AppKit implementation; no tray or menu bar library is bundled. Keep source notices if code is later adapted.

## Terminal mode port

`accountdeck/cli_locks.py` adapts the proper-lockfile directory lock protocol from **errhythm/ccswap**, pinned above: primary OAuth refresh lock, legacy lock, 60-second stale threshold, 3-second heartbeat and 9-second acquisition budget; the config lock uses its separate 10-second stale threshold. `providers/claude_cli.py` adapts `credentials.py`'s shared-key allowlist and live-presence/live-absence merge rule, and `oauth.py`'s OAuth request shape and refresh-token fingerprint. The consume gate, passive mirror and reset-aware strategy were informed by the same MIT source.

The following MIT License applies to those adapted portions. No upstream executable is bundled or invoked.

Copyright (c) 2026 Onur Cetinkol

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
