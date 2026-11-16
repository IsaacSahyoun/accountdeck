"""The manual reset journey through the real server and dashboard, on synthetic Ultra accounts."""
import json
import threading
from unittest import mock

from accountdeck.core import DeckError
from accountdeck.providers.errors import ProviderError
from accountdeck.server import Server
from accountdeck.usage import claude_usage
from tests.test_dashboard import DashboardTestCase, MENU, SHEET, button, more
from tests.test_switch_safety import DeckCase


class ResetDashboardTest(DashboardTestCase, DeckCase):
    def setUp(self):
        DeckCase.setUp(self)
        self.target = self.spare("claude_cli")
        self.server = Server(("127.0.0.1", 0), self.engine)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.browser.open(self.server)

    def open_resets(self):
        self.browser.click(f"document.querySelector('[data-keep=\"menu-{self.target['id']}\"]')")
        self.browser.wait(f"getComputedStyle({MENU}).opacity === '1'", "the menu to finish opening")
        self.browser.click(button("Resets…", MENU))
        self.browser.wait(f"{SHEET}.textContent.includes('remaining') || "
                          f"{SHEET}.textContent.includes('unsaved local result')", "the account's reset offers or local block")
        self.browser.js(f"Promise.all({SHEET}.getAnimations().map(a => a.finished))")

    def after_wait(self):
        # Finish the user action before jumping the fake clock and moving the rows underneath it.
        self.browser.wait(f"!{SHEET}.open && !AD.app.state.report", "the report dismissal to finish")
        self.engine.clock.advance(3601)
        now = self.engine.clock()
        self.engine.bump()
        self.browser.js("AD.api.poll()")
        self.browser.wait(f"AD.app.state.now >= {now}", "the advanced demo clock to reach the page")

    def count_finished_checks(self):
        self.browser.js("(() => { window.resetFinished = 0; const action = AD.api.action; "
                        "AD.api.action = async (name, ...args) => { try { return await action(name, ...args); } "
                        "finally { if (name === 'reset-status') resetFinished++; } }; })()")

    def test_account_level_reset_reason_is_shown_once(self):
        original = self.engine.cli.adapter.reset_status
        cases = (({"eligible": False}, "Claude has not made Ultra resets available with this login."),
                 ({"cooldown_until": self.engine.clock() + 600}, "Claude requires a wait before another reset."))
        for fields, message in cases:
            with self.subTest(fields=fields):
                def status(credentials):
                    return {**original(credentials), **fields}
                with mock.patch.object(self.engine.cli.adapter, "reset_status", side_effect=status):
                    self.open_resets()
                    try:
                        self.assertEqual(self.browser.js(f"{SHEET}.textContent").count(message), 1)
                    finally:
                        self.browser.click(button("Close", SHEET))
                        self.browser.wait(f"!{SHEET}.open", "the reset sheet to close")

    def test_only_the_chosen_offer_has_a_usage_block(self):
        self.engine.store.observe(self.target["id"], claude_usage({"seven_day": {"utilization": None}}))
        self.open_resets()
        text = self.browser.js(f"{SHEET}.textContent")
        self.assertIn("Fable reset", text)
        self.assertIn("Full reset", text)
        self.assertEqual(text.count("Claude's usage is missing or unclear for a limit this reset affects."), 1)
        self.assertIn("Claude requires another offer to be used first.", text)
        self.assertNotIn("Use Fable", text)
        self.assertNotIn("Use Full", text)

    def test_busy_reset_can_retry_the_same_confirmation_without_another_check(self):
        submit, attempts = self.engine.submit, []
        def busy_once(op):
            attempts.append(op)
            if len(attempts) == 1:
                raise DeckError("Synthetic busy job", "busy")
            return submit(op)
        with mock.patch.object(self.engine, "submit", side_effect=busy_once), \
                mock.patch.object(self.engine.cli.adapter, "reset_status", wraps=self.engine.cli.adapter.reset_status) as status:
            self.open_resets()
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait(f"{SHEET}.textContent.includes('Try again')", "the unsent confirmation to be retryable")
            self.browser.click(button("Try again", SHEET))
            self.browser.wait(f"{SHEET}.textContent.includes('cannot be undone')", "the original confirmation")
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait(f"AD.dom.modalView() === 'report' && {SHEET}.textContent.includes('usage was refreshed')", "the reset result")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(status.call_count, 3)

    def test_recovery_busy_message_does_not_imply_a_running_job(self):
        self.open_resets()
        message = "Finish the current sign-in or recovery first."
        with mock.patch.object(self.engine.resets, "_account", side_effect=DeckError(message, "busy")), \
                mock.patch.object(self.engine.cli.adapter, "redeem_reset") as redeem:
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait(f"{SHEET}.textContent.includes('Try again')", "the unsent reset message")
            text = self.browser.js(f"{SHEET}.textContent")
            self.assertEqual(text.count(message), 1)
            self.assertNotIn("after the current action finishes", text)
            self.assertIn("No reset was sent.", text)
            redeem.assert_not_called()

    def test_discarded_confirmation_says_no_reset_was_sent(self):
        self.open_resets()
        self.engine.resets.plans.clear()
        with mock.patch.object(self.engine.cli.adapter, "redeem_reset") as redeem:
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait(f"{SHEET}.textContent.includes('Check again')", "the rejected confirmation")
            self.assertEqual(self.browser.js(f"{SHEET}.querySelector('h2').textContent"), "Reset not started")
            self.assertIn("No new reset was sent", self.browser.js(f"{SHEET}.textContent"))
            self.assertEqual(self.browser.js(f"{SHEET}.textContent").count("before confirming."), 1)
            redeem.assert_not_called()

    def test_renewal_refusal_does_not_offer_a_useless_recheck(self):
        self.open_resets()
        with mock.patch.object(self.engine.resets, "_account", side_effect=DeckError("Renew in Claude Code.", "login_needed")):
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait(f"{SHEET}.textContent.includes('Renew in Claude Code.')", "the renewal refusal")
            self.assertFalse(self.browser.js(f"!!{button('Check again', SHEET)}"))
            self.assertIn("No new reset was sent.", self.browser.js(f"{SHEET}.textContent"))

    def test_missing_account_refusal_does_not_imply_a_reset_was_sent(self):
        self.open_resets()
        with mock.patch.object(self.engine.resets, "_account", side_effect=DeckError("Synthetic missing account", "not_found")):
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait(f"{SHEET}.textContent.includes('Synthetic missing account')", "the missing account refusal")
            self.assertEqual(self.browser.js(f"{SHEET}.querySelector('h2').textContent"), "Reset not started")

    def test_lost_connection_does_not_claim_a_reset_was_unsent(self):
        self.open_resets()
        self.browser.js("(() => { const action = AD.api.action; AD.api.action = (name, ...args) => "
                        "name === 'reset-use' ? Promise.reject(Object.assign(new Error('Synthetic lost connection'), "
                        "{status: 0, code: 'stale_plan'})) : action(name, ...args); })()")
        self.browser.click(button("Use Fable reset…", SHEET))
        self.browser.click(button("Use Fable reset", SHEET))
        self.browser.wait(f"{SHEET}.textContent.includes('Check again')", "the unknown result")
        self.assertEqual(self.browser.js(f"{SHEET}.querySelector('h2').textContent"), "Check the reset result")
        self.assertNotIn("No reset was sent", self.browser.js(f"{SHEET}.textContent"))

    def test_local_admission_errors_say_no_reset_started(self):
        for code in ("read_only", "vault"):
            with self.subTest(code=code):
                self.open_resets()
                try:
                    with mock.patch.object(self.engine.resets, "_account", side_effect=DeckError("Synthetic refusal", code)), \
                            mock.patch.object(self.engine.cli.adapter, "redeem_reset") as redeem:
                        self.browser.click(button("Use Fable reset…", SHEET))
                        self.browser.click(button("Use Fable reset", SHEET))
                        self.browser.wait(f"{SHEET}.textContent.includes('Synthetic refusal')", "the admission refusal")
                        self.assertEqual(self.browser.js(f"{SHEET}.querySelector('h2').textContent"), "Reset not started")
                        self.assertIn("No new reset was sent", self.browser.js(f"{SHEET}.textContent"))
                        redeem.assert_not_called()
                finally:
                    self.browser.click(button("Close", SHEET))
                    self.browser.wait(f"!{SHEET}.open", "the sheet to close")

    def test_expired_busy_confirmation_does_not_promise_it_was_kept(self):
        self.open_resets()
        self.browser.click(button("Use Fable reset…", SHEET))
        self.engine.clock.advance(121)
        with mock.patch.object(self.engine.resets, "_account", side_effect=DeckError("Synthetic busy", "busy")):
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait(f"{SHEET}.textContent.includes('Synthetic busy')", "the busy response")
            self.assertNotIn("is kept", self.browser.js(f"{SHEET}.textContent"))
            self.assertIn("expires", self.browser.js(f"{SHEET}.textContent"))

    def test_server_failure_does_not_claim_a_reset_was_unsent(self):
        self.open_resets()
        self.browser.js("(() => { const action = AD.api.action; AD.api.action = (name, ...args) => "
                        "name === 'reset-use' ? Promise.reject(Object.assign(new Error('Synthetic server error'), "
                        "{status: 500, code: 'stale_plan'})) : action(name, ...args); })()")
        self.browser.click(button("Use Fable reset…", SHEET))
        self.browser.click(button("Use Fable reset", SHEET))
        self.browser.wait(f"{SHEET}.textContent.includes('Synthetic server error')", "the unknown result")
        self.assertEqual(self.browser.js(f"{SHEET}.querySelector('h2').textContent"), "Check the reset result")

    def test_unsaved_refusal_has_a_truthful_title_and_recheck(self):
        save = self.engine.resets._save
        def fail_close(row, journal):
            if journal["attempt"]["state"] == "settled":
                raise OSError("Synthetic write failure")
            return save(row, journal)
        with mock.patch.object(self.engine.resets, "_save", side_effect=fail_close), \
                mock.patch.object(self.engine.cli.adapter, "redeem_reset", return_value={"result": "ineligible"}):
            self.open_resets()
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait("AD.dom.modalView() === 'report'", "the known refusal")
            self.assertEqual(self.browser.js(f"{SHEET}.querySelector('h2').textContent"), "Reset not used")
            self.assertIn("Open Resets for this account", self.browser.js(f"{SHEET}.textContent"))
            self.browser.click(button("Done", SHEET))
            self.browser.wait(f"!{SHEET}.open && !AD.app.state.report", "the report to close")
            self.open_resets()
            self.assertIn("unsaved local result", self.browser.js(f"{SHEET}.textContent"))
            self.assertNotIn("still unconfirmed", self.browser.js(f"{SHEET}.textContent"))

    def test_unsaved_refusal_stays_visible_when_provider_reads_fail(self):
        save = self.engine.resets._save
        def fail_close(row, journal):
            if journal["attempt"]["state"] == "settled":
                raise OSError("Synthetic write failure")
            return save(row, journal)
        with mock.patch.object(self.engine.resets, "_save", side_effect=fail_close), \
                mock.patch.object(self.engine.cli.adapter, "redeem_reset", return_value={"result": "ineligible"}):
            self.open_resets()
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait("AD.dom.modalView() === 'report'", "the known refusal")
            self.browser.click(button("Done", SHEET))
            self.browser.wait(f"!{SHEET}.open && !AD.app.state.report", "the report to close")
            with mock.patch.object(self.engine.cli.adapter, "reset_profile", side_effect=DeckError("Offline", "network")) as profile:
                self.browser.js(f"AD.resets.open({json.dumps(self.target['id'])})")
                self.browser.wait(f"{SHEET}.textContent.includes('unsaved local result') || "
                                  f"{SHEET}.textContent.includes('Offline')", "the completed recheck")
                text = self.browser.js(f"{SHEET}.textContent")
                self.assertIn("unsaved local result", text)
                self.assertNotIn("no reset offers", text)
                self.assertNotIn("Offline", text)
                self.assertFalse(self.browser.js(f"!!{button('Use Fable reset…', SHEET)}"))
                profile.assert_not_called()

    def test_unverifiable_offer_waits_for_a_complete_saved_reading(self):
        from accountdeck.usage import claude_usage
        status = self.engine.cli.adapter.reset_status
        def full_offer(credentials):
            result = status(credentials)
            for grant in result["grants"]:
                if grant["id"] == result["next_grant_id"]:
                    grant.update(gives_back_weekly=False, clears=["seven_day", "seven_day_oauth_apps"])
            return result
        with mock.patch.object(self.engine.cli.adapter, "reset_status", side_effect=full_offer), \
                mock.patch.object(self.engine.cli.adapter, "redeem_reset") as redeem:
            self.open_resets()
            self.assertFalse(self.browser.js(f"!!{button('Use Full reset…', SHEET)}"))
            self.assertIn("usage", self.browser.js(f"{SHEET}.textContent"))
            self.engine.store.observe(self.target["id"], claude_usage({
                "seven_day": {"utilization": 50}, "seven_day_oauth_apps": {"utilization": 20}}))
            self.browser.click(button("Check again", SHEET))
            self.browser.wait(f"!!{button('Use Full reset…', SHEET)}", "the newly verifiable offer")
            redeem.assert_not_called()

    def test_failed_reset_is_not_called_a_switch(self):
        self.open_resets()
        self.browser.click(button("Use Fable reset…", SHEET))
        self.engine.clock.advance(121)
        self.browser.click(button("Use Fable reset", SHEET))
        self.browser.wait("AD.dom.modalView() === 'report'", "the expired confirmation result")
        self.assertEqual(self.browser.js(f"{SHEET}.querySelector('h2').textContent"), "Reset not used")

    def test_cancelled_reset_and_pending_row_are_not_called_switches(self):
        self.open_resets()
        self.browser.click(button("Use Fable reset…", SHEET))
        entered, release = threading.Event(), threading.Event()
        read = self.engine.cli.adapter.reset_status
        def held(credentials):
            entered.set()
            if not release.wait(15):
                raise AssertionError("The test did not release the read")
            return read(credentials)
        with mock.patch.object(self.engine.cli.adapter, "reset_status", side_effect=held):
            self.browser.click(button("Use Fable reset", SHEET))
            try:
                self.assertTrue(entered.wait(10))
                self.browser.wait("AD.dom.modalView() === 'progress'", "reset progress")
                self.browser.wait("AD.app.state.pending && AD.app.state.pending.kind === 'claude-reset' && "
                                  f"AD.app.state.pending.account === {json.dumps(self.target['id'])}", "the pending reset row")
                row = f"document.getElementById('row-{self.target['id']}')"
                self.assertNotIn("Switching", self.browser.js(f"{row}.textContent"))
                self.browser.click(button("Cancel", SHEET))
                self.browser.wait("!document.querySelector('[aria-busy=true]')", "cancel request to finish")
            finally:
                release.set()
            self.browser.wait("AD.dom.modalView() === 'report'", "cancelled reset result")
            self.assertEqual(self.browser.js(f"{SHEET}.querySelector('h2').textContent"), "Reset cancelled")

    def test_refused_and_unknown_reset_banners_are_warnings(self):
        for error in (ProviderError("network", http=400), DeckError("No answer", "timeout")):
            entered, release = threading.Event(), threading.Event()
            def held(*args):
                entered.set()
                if not release.wait(15):
                    raise AssertionError("The test did not release the request")
                raise error
            with mock.patch.object(self.engine.cli.adapter, "redeem_reset", side_effect=held):
                self.open_resets()
                self.browser.click(button("Use Fable reset…", SHEET))
                self.browser.click(button("Use Fable reset", SHEET))
                try:
                    self.assertTrue(entered.wait(10))
                    self.browser.wait("AD.dom.modalView() === 'progress' && AD.app.state.pending && "
                                      "AD.app.state.pending.steps.some(s => s.id === 'send' && s.status === 'active')",
                                      "the held reset request to reach the page")
                    self.browser.click(button("Hide", SHEET))
                    self.browser.wait(f"!{SHEET}.open && AD.dom.modalView() === null", "the reset progress to hide")
                finally:
                    release.set()
                self.browser.wait("!!document.querySelector('[data-kind=report]')", "the reset report banner")
                banner = "document.querySelector('[data-kind=report]')"
                self.assertEqual(self.browser.js(f"{banner}.dataset.level"), "warning")
                if error.code == "timeout":
                    self.assertIn("will not send", self.browser.js(f"{banner}.textContent"))
                self.browser.click(button("Done", banner))
                self.after_wait()

    def test_fable_then_full_each_need_their_own_confirmation(self):
        before = self.signed_in("claude_cli")
        claim = self.engine.cli.adapter.redeem_reset
        with mock.patch.object(self.engine.cli.adapter, "redeem_reset", wraps=claim) as sent:
            for kind in ("Fable", "Full"):
                self.open_resets()
                self.browser.click(button(f"Use {kind} reset…", SHEET))
                self.browser.wait(f"{SHEET}.textContent.includes('cannot be undone')", "the explicit confirmation")
                self.assertIn(self.target["label"], self.browser.js(f"{SHEET}.textContent"))
                self.browser.click(button(f"Use {kind} reset", SHEET))
                self.browser.wait(f"AD.dom.modalView() === 'report' && {SHEET}.textContent.includes('usage was refreshed')", "the confirmed result")
                self.browser.click(button("Done", SHEET))
                self.after_wait()
            self.assertEqual(sent.call_count, 2)
        self.assertEqual(self.signed_in("claude_cli"), before)
        self.assertEqual(self.browser.problems, [])

    def test_cancel_spends_nothing(self):
        with mock.patch.object(self.engine.cli.adapter, "redeem_reset", wraps=self.engine.cli.adapter.redeem_reset) as claim:
            self.open_resets()
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.js("(() => { window.resetActions = []; const action = AD.api.action; "
                            "AD.api.action = (name, ...args) => { resetActions.push(name); return action(name, ...args); }; })()")
            self.browser.click(button("Cancel", SHEET))
            self.assertFalse(self.browser.js(f"{SHEET}.open"))
            self.assertNotIn("reset-use", self.browser.js("resetActions"))
            self.assertFalse(self.engine.runner.busy)
            claim.assert_not_called()
            row = self.engine.store.account(self.target["id"])
            self.assertIsNone(self.engine.resets._journal(row)["attempt"])

    def test_unknown_result_is_explicit_and_recheck_never_claims(self):
        with mock.patch.object(self.engine.cli.adapter, "redeem_reset", side_effect=DeckError("No answer", "timeout")) as sent:
            self.open_resets()
            self.browser.click(button("Use Fable reset…", SHEET))
            self.browser.click(button("Use Fable reset", SHEET))
            self.browser.wait(f"AD.dom.modalView() === 'report' && {SHEET}.textContent.includes('unconfirmed')", "the unconfirmed result")
            self.browser.click(button("Done", SHEET))
            self.after_wait()
            self.open_resets()
            self.assertTrue(self.browser.js(f"{SHEET}.textContent.includes('still unconfirmed')"))
            self.assertEqual(self.browser.js(f"({SHEET}.textContent.match(/still unconfirmed/g) || []).length"), 1)
            self.assertFalse(self.browser.js(f"!!{button('Use Fable reset…', SHEET)}"))
            self.assertEqual(sent.call_count, 1)

    def test_close_while_loading_never_reopens_a_late_sheet(self):
        self.count_finished_checks()
        entered, release = threading.Event(), threading.Event()
        read = self.engine.cli.adapter.reset_status
        def held(credentials):
            entered.set()
            if not release.wait(10):
                raise AssertionError("The test did not release the read")
            return read(credentials)
        with mock.patch.object(self.engine.cli.adapter, "reset_status", side_effect=held):
            self.browser.click(more(self.target))
            self.browser.click(button("Resets…", MENU))
            try:
                self.assertTrue(entered.wait(10))
                self.browser.click(button("Close", SHEET))
            finally:
                release.set()
            self.browser.wait("resetFinished === 1", "the old response to reach the page")
            self.assertFalse(self.browser.js(f"{SHEET}.open"))

    def test_late_response_cannot_replace_another_accounts_reset_sheet(self):
        self.count_finished_checks()
        token = self.vault.get(self.target["id"])["credentials"]["claudeAiOauth"]["accessToken"]
        entered, release = threading.Event(), threading.Event()
        read = self.engine.cli.adapter.reset_status
        def held(credentials):
            if credentials["claudeAiOauth"]["accessToken"] == token:
                entered.set()
                if not release.wait(15):
                    raise AssertionError("The test did not release the first response")
            return read(credentials)
        with mock.patch.object(self.engine.cli.adapter, "reset_status", side_effect=held):
            self.browser.click(more(self.target))
            self.browser.click(button("Resets…", MENU))
            try:
                self.assertTrue(entered.wait(10))
                self.browser.click(button("Close", SHEET))
                self.target = self.active("claude_cli")
                self.open_resets()
            finally:
                release.set()
            self.browser.wait("resetFinished === 2", "both responses to reach the page")
            self.assertIn(self.target["label"], self.browser.js(f"{SHEET}.textContent"))

    def test_non_ultra_and_other_clients_have_no_reset_menu(self):
        saved = self.vault.get(self.target["id"])
        saved["credentials"]["claudeAiOauth"]["rateLimitTier"] = "default_claude_max_5x"
        self.vault.put(self.target["id"], saved)
        self.engine.bump()
        self.browser.js("AD.api.poll()")
        self.browser.wait(f"AD.app.state.accounts.some(a => a.id === {json.dumps(self.target['id'])} && !a.can_reset)", "the non-Ultra state")
        for target in (self.target, self.rows("codex")[0], self.rows("muse")[0]):
            self.browser.click(more(target))
            self.assertFalse(self.browser.js(f"!!{button('Resets…', MENU)}"))
            self.browser.press("Escape")

    def test_account_text_is_masked_and_never_becomes_html(self):
        label = '<img src=x onerror="window.resetXss=true">@example.test'
        self.engine.store.update(self.target["id"], label=label)
        self.engine.bump()
        self.browser.js("AD.api.poll()")
        self.browser.wait(f"AD.app.state.accounts.some(a => a.label === {json.dumps(label)})", "the updated label")
        self.target["label"] = label
        self.open_resets()
        self.assertFalse(self.browser.js("!!window.resetXss"))
        self.assertFalse(self.browser.js(f"!!{SHEET}.querySelector('img[src=x]')"))
        self.browser.js("AD.dom.setHideEmails(true)")
        self.assertNotIn(self.target["email"], self.browser.js(f"{SHEET}.textContent"))
