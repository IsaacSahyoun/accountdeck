/* AccountDeck 2 — switching and sign-in flows in the one modal.
 * Every flow that changes an app's sign-in first asks the service for a plan (switch-plan), shows
 * it, and sends the plan id with the user's consent ("idle" or "now"); the service checks the plan
 * again before acting. Views: plan (a sheet), confirm-end (an alert), progress,
 * report and login. The progress, report and login views have banner stand-ins (banners.js) while
 * the modal is closed. Button order is the Mac's: the default button last (rightmost), Cancel
 * beside it, any alternative on the far left. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;

  // The action that carries out each plan purpose.
  const ACTIONS = {
    switch: 'switch', add: 'add', capture: 'capture', recover: 'recover',
    'finish-login': 'finish-login', 'cancel-login': 'cancel-login',
  };
  const STEP_ICONS = { done: 'check', active: 'refresh', warning: 'warning', failed: 'x' };
  // An operation started here normally shows in the next state; give up waiting for it after this.
  const SHOW_UP_MS = 10000;

  let last = null;
  let watching = null;          // {op, at, seen} for the operation started from this page
  let reviewed = null;          // plan id of the needs_reconfirm prompt already shown
  let awaitingLogin = false;    // a sign-in was started here: show it once the state has it
  let stepKey = null;
  const sigs = { progress: null, report: null, login: null };

  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const spacer = () => el('span', { class: 'spacer', 'aria-hidden': 'true' });

  // ---- Building blocks ----------------------------------------------------------------------------

  function section(title, content) {
    return el('section', 'plan-section', el('h3', { class: 'plan-heading', text: title }), content);
  }

  function item(icon, text) {
    return el('li', 'plan-item', AD.dom.svgIcon(icon), el('span', { text: mask(text) }));
  }

  function failure(err) {
    AD.dom.toast(err.message, { level: err.code === 'invalid' ? 'info' : 'critical' });
  }

  // ---- Plans ------------------------------------------------------------------------------------------

  function planTitle(plan) {
    if (plan.provider === 'claude_cli' && plan.target) return fill(STRINGS.modal.switchTerminals, { label: mask(plan.target.label) });
    const title = { codex: STRINGS.modal.switchCodex, muse: STRINGS.modal.switchMuse }[plan.provider];
    if (plan.purpose === 'switch' && plan.target && title) return fill(title, { label: mask(plan.target.label) });
    if (plan.provider === 'muse' && STRINGS.modal.purposeMuse[plan.purpose]) return STRINGS.modal.purposeMuse[plan.purpose];
    return STRINGS.modal.purpose[plan.purpose] || STRINGS.modal.progressTitle;
  }

  function targetLine(plan) {
    const target = plan.target;
    const meta = [STRINGS.apps[plan.provider], AD.format.capitalised(target.plan), target.email ? mask(target.email) : null];
    const mark = plan.provider === 'claude_cli'
      ? AD.dom.avatar(target.label, { size: 'large' })
      : AD.dom.svgIcon(plan.provider, { size: 20 });
    return el('div', 'plan-target', mark,
      el('div', 'plan-target-text', el('p', { class: 'plan-target-name', text: mask(target.label) }),
        el('p', { class: 'plan-target-meta', text: meta.filter(Boolean).join(' · ') })));
  }

  /** A Codex session can be ended from here; a Muse one is never signalled (the switch waits for it). */
  function consumerRow(plan, consumer) {
    const kinds = plan.provider === 'muse' ? STRINGS.modal.museConsumerKind : STRINGS.modal.consumerKind;
    const what = kinds[consumer.kind] || kinds.other;
    const parts = [what, consumer.parent ? fill(STRINGS.modal.consumerFrom, { parent: consumer.parent }) : null,
      fill(STRINGS.modal.consumerStarted, { clock: AD.model.clock(consumer.started, AD.app.clockNow()) })];
    return el('li', 'consumer', el('span', { text: parts.filter(Boolean).join(' · ') }),
      plan.provider === 'muse' ? null : AD.dom.button(STRINGS.button.endCodex, () => confirmEnd(plan, consumer), { small: true }));
  }

  function planBody(plan, options) {
    const consumers = (plan.running || {}).codex_consumers || [];
    const parts = [];
    if (options.reconfirm) parts.push(el('p', 'plan-reconfirm', AD.dom.svgIcon('info'), el('span', { text: STRINGS.modal.reconfirm })));
    if (options.error) parts.push(el('p', { class: 'modal-error', role: 'alert', text: options.error }));
    if (plan.target) parts.push(targetLine(plan));
    if (plan.saves_outgoing && plan.current) {
      parts.push(el('p', { class: 'plan-note', text: fill(STRINGS.modal.savesOutgoing, { label: mask(plan.current.label) }) }));
    }
    const now = [];
    if (consumers.length) {
      now.push(el('p', { text: fill(plan.provider === 'muse' ? STRINGS.modal.museConsumers : STRINGS.modal.consumers,
        { n: consumers.length }) }));
      now.push(el('ul', 'consumer-list', consumers.map((consumer) => consumerRow(plan, consumer))));
    }
    if (now.length) parts.push(section(STRINGS.modal.running, now));
    const stays = (plan.preserved || []).map((text) => item('check', text));
    if ((plan.will || []).length || stays.length) {
      parts.push(el('details', { class: 'disclosure', 'data-keep': 'plan-more' }, el('summary', { text: STRINGS.modal.will }),
        (plan.will || []).length ? el('ol', 'plan-steps', plan.will.map((text) => el('li', { text: mask(text) }))) : null,
        stays.length ? el('p', { class: 'plan-heading', text: STRINGS.modal.stays }) : null,
        stays.length ? el('ul', 'plan-list', stays) : null));
    }
    const heads = (plan.warnings || []).map((w) => item('warning', w.message));
    if (heads.length) parts.push(section(STRINGS.modal.headsUp, el('ul', 'plan-list', heads)));
    if ((plan.blockers || []).length) {
      parts.push(el('div', { class: 'plan-blockers', role: 'alert' }, el('p', { class: 'plan-blocked', text: STRINGS.modal.blocked }),
        plan.blockers.map((b) => el('p', { text: b.message }))));
    } else if (plan.provider === 'claude_cli' && plan.remember_available && plan.purpose === 'switch') {
      parts.push(el('label', 'check', el('input', { type: 'checkbox', id: 'remember-idle' }), el('span', { text: STRINGS.modal.remember })));
    }
    return el('div', 'plan', parts);
  }

  function remembered() {
    const box = document.getElementById('remember-idle');
    return !!(box && box.checked);
  }

  function planActions(plan) {
    const cancel = AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { dataset: { choice: 'cancel' } });
    if ((plan.blockers || []).length) {
      cancel.setAttribute('data-autofocus', '');
      return [cancel];
    }
    // Only a Codex or Muse switch has sessions to wait for. While one is open, waiting is the one way on.
    const consumers = ((plan.running || {}).codex_consumers || []).length;
    const switching = plan.purpose === 'switch';
    const idleOffered = switching && consumers > 0;
    const preferIdle = idleOffered && plan.default_button === 'idle';
    const idle = idleOffered ? AD.dom.button(STRINGS.button.waitCodex, () => execute(plan, 'idle', remembered()),
      { kind: preferIdle ? 'primary' : undefined, dataset: { choice: 'idle' } }) : null;
    const now = AD.dom.button(switching ? STRINGS.button.switchNow : STRINGS.button.continueNow,
      () => execute(plan, 'now', remembered()),
      { kind: preferIdle ? undefined : 'primary', disabled: idleOffered, dataset: { choice: 'now' } });
    const preferred = preferIdle ? idle : now;
    (preferred.disabled ? cancel : preferred).setAttribute('data-autofocus', '');
    // The default button goes last; the other choice sits on the far left.
    const other = preferred === idle ? now : idle;
    return [other, other ? spacer() : null, cancel, preferred];
  }

  /** Show a plan. options: reconfirm (the sessions changed), error (why the last try failed). */
  function showPlan(plan, options = {}) {
    if (!plan) return;
    AD.dom.openModal({ title: planTitle(plan), body: planBody(plan, options), actions: planActions(plan), view: 'plan' });
  }

  function confirmEnd(plan, consumer) {
    AD.dom.openModal({
      title: STRINGS.modal.endTitle, body: el('p', { text: STRINGS.modal.endBody }), view: 'confirm-end', size: 'alert',
      actions: [
        AD.dom.button(STRINGS.button.cancel, () => showPlan(plan), { attrs: { 'data-autofocus': '' } }),
        AD.dom.button(STRINGS.button.endCodex, async () => {
          try {
            await AD.api.action('stop-consumer', { provider: 'codex', pid: consumer.pid, plan_id: plan.plan_id });
          } catch (err) {
            showPlan(plan, { error: err.message });
            return;
          }
          await replan(plan);
        }, { kind: 'danger' }),
      ],
    });
  }

  function replan(plan) {
    return plan.purpose === 'switch' && plan.target ? open(plan.target.id) : openPurpose(plan.provider, plan.purpose);
  }

  /** Carry out a plan with the user's consent; a changed situation shows the fresh plan instead. */
  async function execute(plan, consent, remember = false) {
    const body = { plan_id: plan.plan_id, consent, provider: plan.provider };
    if (plan.purpose === 'add') body.method = 'browser';
    if (plan.purpose === 'switch') Object.assign(body, { id: plan.target.id, remember_idle: remember });
    let result;
    try {
      result = await AD.api.action(ACTIONS[plan.purpose], body);
    } catch (err) {
      if ((err.code === 'needs_reconfirm' || err.code === 'confirm_required') && err.plan) {
        showPlan(err.plan, { reconfirm: err.code === 'needs_reconfirm' });
      } else if (err.code === 'stale_plan') {
        await replan(plan);
      } else {
        showPlan(plan, { error: err.message });
      }
      return;
    }
    if (plan.purpose === 'add') awaitingLogin = true;
    if (result && result.beside) {
      // The app ran this action beside another client's job. That job stays the pending one, and its
      // progress view and Cancel button are not this action's: a notice says how this one went.
      watching = null;
      AD.dom.closeModal();
      return;
    }
    watch(result);
  }

  function watch(result) {
    watching = result && result.op ? { op: result.op, at: Date.now(), seen: false } : null;
    openProgress(last && last.pending);
  }

  /** Codex and Muse always review the plan. Only the terminal flow may skip review
   * when the service says no confirmation is needed ("Ask before switching when Claude is idle" off). */
  async function open(accountId) {
    let result;
    try {
      result = await AD.api.action('switch-plan', { id: accountId });
    } catch (err) {
      failure(err);
      return;
    }
    const plan = result && result.plan;
    if (plan && plan.provider === 'claude_cli' && plan.confirmation_required === false && !(plan.blockers || []).length) {
      await execute(plan, 'now');
      return;
    }
    showPlan(plan);
  }

  /** Start a flow other than a switch: add, capture, recover, finish-login or cancel-login. */
  async function openPurpose(provider, purpose) {
    let result;
    try {
      result = await AD.api.action('switch-plan', { provider, purpose });
    } catch (err) {
      failure(err);
      return;
    }
    showPlan(result && result.plan);
  }

  // ---- Progress ------------------------------------------------------------------------------------------

  function progressTitle(pending) {
    if (pending && pending.kind === 'switch' && pending.label) return fill(STRINGS.progress.title, { label: mask(pending.label) });
    return STRINGS.modal.progressTitle;
  }

  function stepItem(step) {
    const icon = STEP_ICONS[step.status];
    return el('li', { class: 'step', dataset: { status: step.status } },
      icon ? AD.dom.svgIcon(icon, { className: 'step-icon' }) : el('span', { class: 'step-dot', 'aria-hidden': 'true' }),
      el('span', { class: 'step-label', text: mask(step.label) }),
      el('span', { class: 'sr-only', text: ` (${STRINGS.progress.step[step.status] || step.status})` }),
      step.detail ? el('span', { class: 'step-detail', text: mask(step.detail) }) : null);
  }

  function progressBody(pending) {
    if (!pending) return el('p', { class: 'progress-now', text: STRINGS.modal.progressTitle });
    const waiting = pending.waiting_on || [];
    const active = (pending.steps || []).find((s) => s.status === 'active');
    return el('div', 'progress',
      el('p', { class: 'progress-now', text: mask(active ? active.label : pending.message || '') }),
      waiting.length ? el('p', { class: 'progress-waiting', text: fill(STRINGS.progress.waitingOn, { names: AD.format.sessionNames(waiting) }) }) : null,
      el('div', { class: 'progress-bar', 'aria-hidden': 'true' }),
      el('ol', 'steps', (pending.steps || []).map(stepItem)));
  }

  function progressActions(pending) {
    const hide = AD.dom.button(STRINGS.button.hide, () => AD.dom.closeModal(), { kind: 'primary', dataset: { choice: 'hide' } });
    if (!pending || !pending.cancellable) return [hide];
    return [AD.dom.button(STRINGS.button.cancel, () => AD.api.action('cancel', { op: pending.id }), { dataset: { choice: 'cancel' } }), hide];
  }

  function progressSig(pending) {
    return JSON.stringify([pending, AD.dom.hideEmails()]);
  }

  function openProgress(pending) {
    sigs.progress = pending ? progressSig(pending) : null;
    // Closing the progress view only hides it: the operation goes on and the banner stands in.
    AD.dom.openModal({ title: progressTitle(pending), body: progressBody(pending), actions: progressActions(pending), view: 'progress' });
  }

  function announceStep(pending) {
    const active = pending && (pending.steps || []).find((s) => s.status === 'active');
    const key = pending ? `${pending.id}|${active ? active.id : pending.phase}` : null;
    if (stepKey && key && key !== stepKey && AD.dom.modalView() === 'progress') {
      AD.dom.announce(active ? active.label : pending.message);
    }
    stepKey = key;
  }

  /** The operation ended while its progress view was open: show its report, or close the view. */
  function finishProgress(state) {
    const report = state.report;
    if (report && (!watching || report.op === watching.op)) {
      watching = null;
      openReport(report);
      return;
    }
    if (watching && !watching.seen && Date.now() - watching.at < SHOW_UP_MS) return;
    watching = null;
    AD.dom.closeModal();
  }

  /** Keep the progress view in step with state.pending; options.open opens it (the banner's button). */
  function renderPending(state, options = {}) {
    last = state;
    const pending = state && state.pending;
    if (pending && watching && pending.id === watching.op) watching.seen = true;
    announceStep(pending);
    const reconfirm = pending && pending.phase === 'needs_reconfirm' && pending.plan;
    if (options.open) {
      if (reconfirm) {
        reviewed = pending.plan.plan_id;
        showPlan(pending.plan, { reconfirm: true });
      } else if (pending) {
        openProgress(pending);
      }
      return;
    }
    if (AD.dom.modalView() !== 'progress') return;
    if (!pending) {
      finishProgress(state);
      return;
    }
    if (reconfirm && reviewed !== pending.plan.plan_id) {
      reviewed = pending.plan.plan_id;
      showPlan(pending.plan, { reconfirm: true });
      return;
    }
    const sig = progressSig(pending);
    if (sig === sigs.progress) return;
    sigs.progress = sig;
    AD.dom.setModalBody(progressBody(pending));
    AD.dom.setModalActions(progressActions(pending));
  }

  // ---- Report -----------------------------------------------------------------------------------------------

  function reportTitle(report) {
    if (report.kind === 'claude-reset') {
      if (report.outcome === 'cancelled') return 'Reset cancelled';
      if (report.outcome === 'failed' || report.verified === 'failed') return 'Reset not used';
      return report.verified === 'unconfirmed' ? 'Reset unconfirmed' : 'Reset result';
    }
    if (report.outcome === 'failed') return STRINGS.report.failedTitle;
    if (report.outcome === 'cancelled') return STRINGS.report.cancelledTitle;
    if (report.kind === 'switch' && report.label) return fill(STRINGS.report.switched, { label: mask(report.label) });
    return report.summary || STRINGS.report.title;
  }

  function reportBody(report) {
    const parts = [];
    if (report.summary) parts.push(el('p', { class: 'plan-lead', text: mask(report.summary) }));
    if (report.verify_reason && report.verified !== 'verified') parts.push(el('p', { class: 'plan-muted', text: mask(report.verify_reason) }));
    if (report.error && report.error.message) {
      parts.push(el('p', { class: 'modal-error', role: 'alert', text: mask(AD.format.errorText(report.error.code, report.error.message)) }));
    }
    return el('div', 'report', parts);
  }

  function reportSig(report) {
    return JSON.stringify([report, AD.dom.hideEmails()]);
  }

  function openReport(report) {
    sigs.report = reportSig(report);
    const actions = [];
    if (report.manifest) {
      actions.push(AD.dom.button(STRINGS.button.explain, () => {
        AD.dom.closeModal();
        AD.details.showManifest(report.manifest);
      }, { kind: 'ghost', dataset: { choice: 'explain' } }), spacer());
    }
    actions.push(AD.dom.button(STRINGS.button.done, () => AD.dom.closeModal(),
      { kind: 'primary', dataset: { choice: 'done' }, attrs: { 'data-autofocus': '' } }));
    AD.dom.openModal({
      title: reportTitle(report), body: reportBody(report), actions, view: 'report',
      // Seeing the report is what dismisses it, however the view is closed.
      onClose: () => {
        if (last && last.report && last.report.op === report.op) AD.api.action('dismiss-report', {}).catch(failure);
      },
    });
  }

  /** Keep the report view in step with state.report; options.open opens it (the banner's button). */
  function renderReport(state, options = {}) {
    last = state;
    const report = state && state.report;
    if (options.open) {
      if (report) openReport(report);
      return;
    }
    if (AD.dom.modalView() !== 'report') return;
    if (!report) {
      AD.dom.closeModal();
      return;
    }
    const sig = reportSig(report);
    if (sig === sigs.report) return;
    sigs.report = sig;
    AD.dom.setModalBody(reportBody(report));
  }

  // ---- Sign-in in progress ---------------------------------------------------------------------------

  function signInLink(url, small) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (err) {
      return null;
    }
    const loopback = parsed.protocol === 'http:' && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost');
    if (parsed.protocol !== 'https:' && !loopback) return null;
    return el('a', { class: ['button', small && 'button-small'], href: parsed.href, target: '_blank',
      rel: 'noopener noreferrer', 'data-keep': 'login-open' }, STRINGS.login.open, AD.dom.svgIcon('external'));
  }

  /** The title and lead text of a sign-in in progress: Muse's or Codex's. */
  function loginText(login) {
    if (login.provider === 'muse') return [STRINGS.login.museTitle, STRINGS.login.muse];
    return [STRINGS.login.codexTitle, STRINGS.login.codex];
  }

  /** The controls of a sign-in in progress (the login view and its banner): cancel, the provider's sign-in
   * page and Save Sign-In. options.small for a banner. */
  function loginActions(login, options = {}) {
    const small = !!options.small;
    const actions = [AD.dom.button(STRINGS.login.cancel, () => openPurpose(login.provider, 'cancel-login'),
      { small, attrs: { 'data-keep': 'login-cancel' } })];
    const link = signInLink(login.url, small);
    if (link) actions.push(link);
    actions.push(AD.dom.button(STRINGS.login.saveCodex, () => openPurpose(login.provider, 'finish-login'), { kind: 'primary', small }));
    return actions;
  }

  function loginBody(login) {
    return [el('p', { text: loginText(login)[1] }),
      login.user_code ? el('p', { class: 'move-path', text: fill(STRINGS.login.deviceCode, { code: login.user_code }) }) : null];
  }

  function openLogin(login) {
    sigs.login = JSON.stringify(login);
    AD.dom.openModal({ title: loginText(login)[0], body: loginBody(login), actions: loginActions(login), view: 'login' });
  }

  /** Keep the login view in step with state.login. A sign-in started here opens it as soon as the
   * state shows it and no other view is open; options.open opens it (the banner). */
  function renderLogin(state, options = {}) {
    last = state;
    const login = state && state.login;
    const view = AD.dom.modalView();
    if (login && (options.open || (awaitingLogin && !view))) {
      awaitingLogin = false;
      openLogin(login);
      return;
    }
    if (view !== 'login') return;
    if (!login) {
      AD.dom.closeModal();
      return;
    }
    const sig = JSON.stringify(login);
    if (sig === sigs.login) return;
    sigs.login = sig;
    AD.dom.setModalBody(loginBody(login));
    AD.dom.setModalActions(loginActions(login));
  }

  AD.switch = { open, openPurpose, showPlan, watch, renderPending, renderReport, renderLogin, loginActions, loginText };
})();
