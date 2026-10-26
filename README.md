# AccountDeck 2 for Mac

AccountDeck 2 keeps Claude and Codex usage, reset times, and saved logins in one local dashboard.
It lives in your menu bar. Use it with your own accounts on your own Mac user account.

## Requirements

- An Apple silicon Mac (M1 or newer), with macOS Sonoma 14 or newer.
- Internet access for the first install and for sign-in and usage checks.
- Claude Code, Claude Desktop, or Codex. Use whichever you have; you do not need all three.
  Install and sign in to each client you want to manage. Codex needs its command-line tool.

The installer downloads a pinned Python runtime and its libraries. You do not need Python,
Homebrew, Node, developer tools, or an administrator password. Intel Macs are not supported.

## Install in three steps

1. Download `AccountDeck2-mac.zip` and double-click it in Finder to unzip it.
2. Open the `AccountDeck2-mac` folder. Double-click **Install AccountDeck.command**.
   If macOS blocks it, follow the short Gatekeeper instructions below.
3. Wait for “AccountDeck 2 is installed.” The app opens its dashboard and adds a menu bar icon.
   Allow access to the named login Keychain items when prompted so it can save your logins.

Keep the package folder if you want the uninstall shortcut. You can move or delete it after installing.
To open the app again, use Spotlight or `~/Applications/AccountDeck 2.app`.

## First run

**Claude Code:** AccountDeck saves your current terminal login once it can verify it.
If the list stays empty, check your internet connection and sign in to Claude Code first.
Or choose **Add Account**. A separate Terminal window opens for sign-in. Close that window
when sign-in is finished; only then is the account saved. A private browser window helps you
choose a different account. Repeat to save more accounts.

**Claude Desktop and Codex:** Their sections show the current login when it can be read.
Choose **Save Current Account** to keep it, or **Add Account** for another login.
Desktop may close briefly to save a complete sign-in. Codex adds accounts in a separate folder.
If a client is not installed, leave its section alone.

**Switch:** Choose **Switch…** beside a saved account and review the plan. Claude Code terminals
follow the new login on their next message. Desktop switches may close and reopen Claude;
save Chat and Cowork work first. Codex switches wait for its sessions to close.
Settings, MCP definitions, plugins, projects, and transcripts stay shared.

**Auto-switch:** This applies to Claude Code only. It starts on at 95% used and needs at least
two saved accounts. Change it in **Settings → Auto-Switch**, or turn it off. It waits for a usable
account with a recent reading. **Leave out** excludes an account. An automatic switch has a
10-minute **Undo**. Desktop and Codex only switch when you ask.

**Start at login:** Turn it on in **Settings → Menu Bar & Login**. Closing the dashboard leaves
usage checks running. Choose **Quit AccountDeck 2** from its menu to stop them.

## Uninstall

Quit AccountDeck 2, then double-click **Uninstall AccountDeck.command** in the package folder.
This removes the app, runtime, and start-at-login entry. It keeps saved accounts and settings.

From Terminal, `bash uninstall.sh` does the same thing. To erase saved data too, run
`bash uninstall.sh --delete-data` from the package folder and type `delete` when asked.
The app's vault key remains in login Keychain as **AccountDeck2 vault**; the key alone holds no logins.

## Help and common questions

**macOS blocks the installer (Gatekeeper).** This is an unsigned, unnotarized download.
For a copy you trust, right-click **Install AccountDeck.command**, choose **Open**, then **Open** again.
On newer macOS versions, try opening it once, then use **System Settings → Privacy & Security →
Open Anyway**. See [Apple's instructions](https://support.apple.com/guide/mac-help/mh40616/mac).
If it is still blocked, open Terminal, type `cd `, drag the unpacked folder into Terminal, and press
Return. Then run either of these commands from that folder:

```sh
bash install.sh
```

Or remove the quarantine flag from that trusted package, then double-click the installer again:

```sh
xattr -dr com.apple.quarantine .
```

The same steps apply to the uninstall shortcut. Do not use `sudo` or disable Gatekeeper system-wide.

**Why avoid `/login`, `/logout`, and Log Out?** After saving your accounts, use AccountDeck's
**Add Account** and **Switch…** to change accounts. Logging out in a client can revoke a sign-in
that AccountDeck saved. Use **Sign In Again** if a saved login expires or is revoked.

**Codex says file storage is required.** AccountDeck exchanges only `auth.json`. In
`~/.codex/config.toml`, set `cli_auth_credentials_store = "file"` (replace an existing value;
do not add a duplicate). Then sign in to Codex with file storage and choose **Save Current Account**.
Keychain-backed Codex logins cannot be switched by this app. Do not share `auth.json`.

**Claude Code is read-only.** Custom `CLAUDE_CONFIG_DIR` or `CLAUDE_SECURESTORAGE_CONFIG_DIR`
settings disable terminal login management. Another running copy of AccountDeck can also make it
read-only. Quit the other copy and use your normal Claude Code configuration.

**No usage, or an old reading?** Open the client and confirm you are signed in. Check the network,
then choose Refresh. Last-known readings are labelled; missing usage is never treated as zero.
Client storage formats and usage services can change. An unsupported format may need an app update.

**Where is my data?** Code, runtime, settings, usage history, logs, and the encrypted vault live in
`~/Library/Application Support/AccountDeck2`. The app lives in `~/Applications`.
Do not send your data folder to someone else. Send the distribution zip instead.

**Privacy.** AccountDeck has no cloud account or telemetry. Storage and the dashboard are local;
the server listens only on `127.0.0.1`. Saved sign-ins are encrypted with a key in your login Keychain.
Sign-in, usage, and service-status checks contact the providers. Installation downloads the runtime
from GitHub and libraries from PyPI. The dashboard uses no CDNs.

## Development

In a source checkout, see `AGENTS.md` for safety rules. Never develop against real logins or the live service.
From the repository root, use the installed runtime:

```sh
PY="$HOME/Library/Application Support/AccountDeck2/runtime/python/bin/python3.14"
ACCOUNTDECK_SAFE_MODE=1 "$PY" -E -s -m unittest discover -s tests -t .
"$PY" -E -s run.py --demo first-run --port 0 --no-browser
```

Build and test the shareable package:

```sh
tools/build_mac_package.sh
tools/mac_package_test.sh
```

The builder writes `dist/AccountDeck2-mac.zip`. It includes only distribution files, rejects
personal data and unexpected files in the app tree, and preserves executable permissions.
The package test installs and uninstalls under a temporary prefix in safe mode.
`PY` can select another compatible development runtime. No live install is needed for package testing.
