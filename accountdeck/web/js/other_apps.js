/* Claude Desktop and Codex use the existing plan/consent flows. No login is changed by rendering. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  let drawn = null;

  function action(label, run, key, disabled) {
    return AD.dom.button(label, run, { small: true, disabled, attrs: { 'data-keep': key } });
  }

  function accountRow(account, state, nowMs) {
    const provider = account.provider;
    const disabled = !!(state.read_only || state.pending || state.login);
    const frozen = AD.accounts.frozen(account);
    let button = null;
    if (!frozen && account.saved === false) {
      button = action('Save Current Account', () => AD.switch.openPurpose(provider, 'capture'), `save-${account.id}`, disabled);
    } else if (!frozen && AD.accounts.needsSignin(account)) {
      button = action('Sign In Again', () => AD.switch.openPurpose(provider, 'add'), `signin-${account.id}`, disabled);
    } else if (!frozen && !account.active && account.switchable) {
      button = action('Switch…', () => AD.switch.open(account.id), `switch-${account.id}`, disabled);
    }
    const setup = !frozen && account.saved !== false && !account.switchable
      ? (provider === 'claude' && account.imported_from === 'pc'
        ? 'Open Claude Desktop once on this Mac to finish importing this account.'
        : (provider === 'claude' && account.active
          ? 'Close Claude Desktop, then choose Save Current Account to finish saving this login.'
          : 'Use Add Account to sign in to this account again before switching.')) : null;
    const note = [account.active ? 'In use' : null, account.saved === false ? 'Not saved yet' : null,
      frozen ? AD.accounts.ownerLabel(account) : null, setup ? null : (account.headline || {}).text].filter(Boolean).join(' · ');
    return el('li', { class: ['row', account.active && 'is-current'] },
      el('div', 'row-id', el('div', 'row-text',
        el('h4', { class: 'row-name', text: mask(account.label) }),
        el('p', { class: 'row-sub', text: mask(note) }),
        setup ? el('p', { class: 'row-note', text: setup }) : null)),
      el('div', 'row-limits', (account.windows || []).map((win) => AD.meter.limit(win, account,
        { nowMs, display: AD.accounts.display(state), threshold: null, compact: true }))),
      el('div', 'row-actions', button));
  }

  function section(provider, title, hint, state, nowMs) {
    const accounts = (state.accounts || []).filter((a) => a.provider === provider);
    const disabled = !!(state.read_only || state.pending || state.login);
    return el('section', 'other-provider',
      el('header', 'section-head',
        el('div', null, el('h3', { class: 'section-title', text: title }), el('p', { class: 'section-sub', text: hint })),
        el('div', 'section-actions',
          action('Save Current Account', () => AD.switch.openPurpose(provider, 'capture'), `capture-${provider}`, disabled),
          action('Add Account', () => AD.switch.openPurpose(provider, 'add'), `add-${provider}`, disabled))),
      accounts.length ? el('ol', { class: 'card rows', 'aria-label': `${title} accounts` },
        accounts.map((a) => accountRow(a, state, nowMs)))
        : el('div', 'card empty-state', el('p', { class: 'empty-text', text: provider === 'codex'
          ? 'Sign in to Codex first, then choose Save Current Account. Or use Add Account. Codex must use file storage for its login.'
          : 'Sign in to Claude Desktop first, then choose Save Current Account. Or use Add Account to sign in to another account.' })));
  }

  function render(state, nowMs) {
    const host = document.getElementById('other-accounts');
    if (!host) return;
    const sig = JSON.stringify([(state.accounts || []).filter((a) => a.provider !== 'claude_cli'),
      state.read_only, state.pending, state.login, AD.accounts.display(state), AD.dom.hideEmails(), Math.floor(nowMs / 60000)]);
    if (sig === drawn) return;
    drawn = sig;
    const next = el('div', 'stack',
      section('claude', 'Claude Desktop', 'Save Chat and Cowork work before switching. Desktop may close and reopen.', state, nowMs),
      section('codex', 'Codex', 'Close Codex sessions before switching. Settings and history stay shared.', state, nowMs));
    if (host.firstElementChild) AD.dom.swap(host.firstElementChild, next);
    else host.append(next);
  }

  AD.otherApps = { render };
})();
