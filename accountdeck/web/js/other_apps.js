/* AccountDeck 2 — the Codex and Muse Code sections. Each has a title, Save Current
 * Account and Add Account, and its accounts as rows (rows.js draws them, the same rows as the terminal
 * section). Drawing changes no login: every button opens a plan to review first. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS } = AD.format;
  const el = (...args) => AD.dom.el(...args);

  const CLIENTS = [
    { provider: 'codex', hint: 'Close Codex sessions before switching. Settings and history stay shared.',
      empty: 'Sign in to Codex first, then choose Save Current Account. Or use Add Account. Codex must use file storage for its login.' },
    { provider: 'muse', hint: 'Close Muse sessions before switching. Settings and history stay shared.',
      empty: 'Use Add Account to sign in to a Muse account here. To save the account Muse uses now, Muse must keep its login '
        + 'in a file: run env -u TBH_CREDENTIAL_BACKEND muse logout, add export TBH_CREDENTIAL_BACKEND=file to ~/.zshrc, '
        + 'open a new terminal, run muse login, then choose Save Current Account.' },
  ];
  let sections = null;                           // built once: [{provider, node, buttons, list, empty}]

  /** One client's section: its header, its list of rows and the words shown while it has no account. */
  function section({ provider, hint, empty }) {
    const title = STRINGS.apps[provider];
    const open = (purpose) => () => AD.switch.openPurpose(provider, purpose);
    const buttons = [AD.dom.button(STRINGS.row.save, open('capture'), { small: true }),
      AD.dom.button(STRINGS.accounts.add, open('add'), { small: true })];
    const list = el('ol', { class: 'table rows', 'aria-label': `${title} accounts` });
    AD.rows.bindKeys(list);
    const none = el('div', 'card empty-state', el('p', { class: 'empty-text', text: empty }));
    const node = el('section', 'other-provider',
      el('header', 'section-head',
        el('div', null, el('h2', { class: 'section-title', text: title }), el('p', { class: 'section-sub', text: hint })),
        el('div', 'section-actions', buttons)),
      list, none);
    return { provider, node, buttons, list, empty: none };
  }

  /** Render the two sections at nowMs. */
  function render(state, nowMs) {
    const host = document.getElementById('other-accounts');
    if (!host) return;
    if (!sections || !host.contains(sections[0].node)) {
      sections = CLIENTS.map(section);
      host.replaceChildren(el('div', 'stack', sections.map((part) => part.node)));
    }
    const readonly = state.read_only === true ? STRINGS.accounts.readOnlyOther : null;
    const busy = !!(state.pending || state.login);
    const ctx = { nowMs, display: AD.accounts.display(state), threshold: null, ownLimits: true, readonly, busy };
    for (const part of sections) {
      const accounts = (state.accounts || []).filter((a) => a.provider === part.provider);
      for (const node of part.buttons) node.disabled = !!readonly || busy;
      part.list.hidden = !accounts.length;
      part.empty.hidden = accounts.length > 0;
      AD.rows.patchRows(part.list, accounts, state, ctx);
    }
  }

  AD.otherApps = { render };
})();
