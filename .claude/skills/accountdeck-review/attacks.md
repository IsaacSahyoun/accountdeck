# What breaks AccountDeck

The attacks that found real bugs in the three reviews of 2026-11-12. A reviewer traces each one through the change. A builder writes a test for each one the change can reach. The reviewer brief and the `accountdeck-login-change` skill both read this file, so add a new attack here and nowhere else.

## A login on disk
A switch must never lose a login, mix two accounts' pieces, or file a login under the wrong account.
1. **The app dies between two writes.** List every write in order. After each one, say what is on disk, who the client would open as, and what Recover does next. Do it for Recover's own writes too.
2. **Pieces of two accounts in one login.** One account's name beside another account's token or cookies. Include the files the client writes by itself, such as a token Claude Code or Codex renews.
3. **Recover pressed once, twice, and after a failed press.** Can it loop, block for good, skip a record, or leave a leftover record? What does the user see and what can they do?
4. **The client changes its own login in the middle.** Claude Code or Codex renews its token, or the user signs in as someone else, between the app's read and its write.
5. **A file that cannot be read, or is half written.** Is it kept before it is replaced? Is "I cannot tell" ever treated as "it is fine"?
6. **Data an older version wrote.** Open records, set-aside records, saved pauses and settings from the installed version, read by the new code on its first start. Builds before 2.1 also saved accounts, logins and switch records for Claude Desktop, which the new code does not manage: it must never list, change or delete them, and never open a file in Claude Desktop's data folder (`~/Library/Application Support/Claude`).
7. **The outside is down.** The provider cannot be reached, the Keychain is locked, the disk is full.

## Two jobs at once
8. **A job running beside another client's wait.** What each one reads and writes, and who goes first.
9. **Check, wait, then act on the old answer.** Any check followed by a wait must be made again when the wait ends: idle sessions, a cancel, a confirmation that ran out, which account is in use. The name for this bug is a check-then-act race.
10. **One stuck item blocks the rest.** A record, a client or a job that cannot finish must not keep the others from their turn.
11. **The same step from three threads**, or the same action clicked twice.

## The page and the server
The dashboard is a web page served on `127.0.0.1`. Any website the user visits can try to call that address.
12. **Every route keeps every request rule.** The `Host` check, the page's own `Origin`, the `X-AccountDeck` header, a JSON body, the session cookie. A new route with one rule missing is a hole.
13. **A GET changes nothing, and no answer carries a login, a token or a key.**
14. **No HTML is built from account text**, and no inline script is added.
15. **The page acts on what the server answered**, not on what it knew before the click.
16. **Both engines.** The app's window is WebKit. Does the change use anything WebKit lacks?
17. **What the page looks like.** Words cut in the middle, a thing said twice, a sheet with no way out. A test that reads text does not see these.
18. **An old page and a new server.** A window left open across an install runs the old page against the new answers.

## Every way in
19. **All three clients.** A fix for one client's switch usually has two siblings.
20. **The dashboard and the menu bar.** Both start a switch.
21. **The reverse of every new state.** If a thing can be left out, set aside, paused or held, how does it come back?

## The tests
22. **Does the test fail without the fix?** Five tests in the third review did not.
23. **Does the test control what it claims to control?** A test that sleeps and hopes the job is waiting by then proves nothing on a busy Mac.
24. **Does the test run the user's path?** A test that sets a value and reads it back proves the test.
