/* AccountDeck 2 — an account row, the same for all four clients (Claude Code, Claude Desktop, Codex
 * and Muse Code). A row shows the account (monogram, name, badges, a status or detail line), its
 * limits, its one button and its More menu. Rows are keyed by account id and rebuilt only when
 * their data changed; countdowns are [data-reset] text the shell's clock patches.
 *
 * Keyboard: each list is one Tab stop (a roving tabindex); ↑ ↓ Home End move between its rows, Return
 * runs the focused row's button, and Tab continues into the row's own buttons. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const A = AD.accounts;
  const T = STRINGS.row;

  const SPOTLIGHT_MS = 1400;
  // Headlines the meters already show. Any other one ("Sign-in needed", "Reading is 3h old") is the
  // status line under the account's name.
  const METER_KINDS = new Set(['ready', 'watch', 'low', 'critical']);
  const STATUS_ICONS = { signin: 'warning', out: 'lock', outage: 'pause', stale: 'clock' };
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const isTerminal = (account) => account.provider === 'claude_cli';
  const sigs = new WeakMap();
  const tabStops = new WeakMap();               // list → the id of the row that holds its Tab stop

  // ---- Rename and remove ------------------------------------------------------------------------

  /** Ask for a new name, then rename the saved account. */
  function rename(account) {
    const R = T.rename;
    const input = el('input', { class: 'text-input', value: account.label, maxlength: 80, 'aria-label': R.label,
      'data-autofocus': '', autocomplete: 'off', spellcheck: 'false' });
    const save = AD.dom.button(R.save, async () => {
      const label = input.value.trim();
      if (label && label !== account.label) await AD.api.action('rename', { id: account.id, label });
      AD.dom.closeModal();
    }, { kind: 'primary' });
    input.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      save.click();
    });
    AD.dom.openModal({ title: R.title, body: input, view: 'rename', size: 'alert',
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal()), save] });
    input.select();
  }

  /** Ask first, then forget the saved account. A client stays signed in to the account it is using. */
  function remove(account) {
    const R = T.remove;
    const app = STRINGS.apps[account.provider];
    const body = A.frozen(account) ? R.elsewhere
      : [fill(R.body, { app }), account.active ? fill(R.inUse, { app }) : null].filter(Boolean).join(' ');
    AD.dom.openModal({ title: fill(R.title, { label: mask(account.label) }), view: 'remove', size: 'alert',
      body: el('p', { text: body }),
      actions: [AD.dom.button(STRINGS.button.cancel, () => AD.dom.closeModal(), { attrs: { 'data-autofocus': '' } }),
        AD.dom.button(R.button, async () => {
          await AD.api.action('remove', { id: account.id });
          AD.dom.closeModal();
        }, { kind: 'danger' })] });
  }

  // ---- The row's button and menu ----------------------------------------------------------------

  /** The row's one button, or null: Remove… for a row another computer owns, Save Current Account for a
   * login that is not saved yet, Sign In Again, or Switch for a saved account that is not in use. */
  function primaryAction(account, state, ctx) {
    if (A.frozen(account)) return { label: T.menu.remove, run: () => remove(account) };
    const cli = isTerminal(account);
    const off = { disabled: !!ctx.readonly || ctx.busy, reason: ctx.readonly };
    if (account.saved === false) return { ...off, label: T.save, run: () => AD.switch.openPurpose(account.provider, 'capture') };
    if (A.needsSignin(account)) {
      return { ...off, label: T.signIn, run: () => (cli ? AD.terminal.add() : AD.switch.openPurpose(account.provider, 'add')) };
    }
    if (account.active || !account.switchable) return null;
    const target = !!state.pending && state.pending.account === account.id;
    // A terminal switch may start at once; the other clients always show the plan first.
    return { ...off, label: target ? STRINGS.shell.switching : (cli ? T.switch : T.switchReview),
      run: () => AD.switch.open(account.id), prominent: ctx.nextId === account.id || ctx.bestId === account.id };
  }

  /** The More menu of a saved account. Only terminal accounts can be left out of auto-switch, and only
   * the terminal account in use cannot be removed (the other clients stay signed in without it). */
  function menuItems(account) {
    const M = T.menu;
    const cli = isTerminal(account);
    const locked = cli && account.active;
    return [
      cli ? { label: account.excluded ? M.include : M.leaveOut, icon: account.excluded ? 'plus-circle' : 'minus-circle',
        run: () => AD.terminal.exclude(account) } : null,
      { label: M.rename, icon: 'pencil', run: () => rename(account) },
      'separator',
      { label: M.remove, icon: 'trash', danger: true, disabled: locked, title: locked ? M.removeActive : undefined,
        run: () => remove(account) },
      locked ? { note: M.removeActive } : null,
    ].filter(Boolean);
  }

  function menuButton(account) {
    const label = fill(T.more, { label: mask(account.label) });
    const node = AD.dom.button('', () => AD.menu.open(node, menuItems(account), { label }), {
      kind: 'icon', icon: 'ellipsis', label, title: 'More',
      attrs: { 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'data-keep': `menu-${account.id}` },
    });
    return node;
  }

  // ---- The row's text ------------------------------------------------------------------------------

  function badges(account, ctx) {
    if (A.frozen(account)) return [AD.meter.badge('neutral', A.ownerLabel(account))];
    const list = [];
    if (account.active) list.push(AD.meter.badge('good', STRINGS.chip.current, { icon: null, dot: true }));
    if (ctx.nextId === account.id) list.push(AD.meter.badge('accent', STRINGS.chip.next, { icon: 'bolt' }));
    else if (ctx.bestId === account.id) list.push(AD.meter.badge('accent', STRINGS.chip.best));
    if (ctx.fableLeader === account.id) list.push(AD.meter.badge('fable', STRINGS.overview.fableTag, { icon: 'crown' }));
    if (account.excluded) list.push(AD.meter.badge('neutral', STRINGS.chip.leftOut));
    if (account.saved === false) list.push(AD.meter.badge('neutral', STRINGS.chip.notSaved));
    const error = account.error;
    if (error && !A.blocked(account)) {
      if (error.outage) list.push(AD.meter.badge('neutral', STRINGS.chip.incident, { icon: 'pause' }));
      else if (error.code === 'blocked') list.push(AD.meter.badge('neutral', STRINGS.chip.cloudflare, { title: error.message }));
      else if (error.code !== 'rate_limited') {
        list.push(AD.meter.badge('critical', STRINGS.chip.error, { title: AD.format.errorText(error.code, error.message) }));
      }
    }
    return list;
  }

  /** Paused checks say until when. "Checked 42m ago" marks an old reading when the status line is
   * busy saying something else (the "Reading is 3h old" headline says it by itself). */
  function freshness(account, nowMs) {
    if (isNum(account.paused_until) && account.paused_until > nowMs / 1000) {
      return el('p', { class: 'row-note', text: fill(T.paused, { clock: AD.model.clock(account.paused_until, nowMs) }) });
    }
    if (!isNum(account.last_good_at) || account.age_tier !== 'stale' || (account.headline || {}).kind === 'stale') return null;
    return el('p', { class: 'row-note is-stale', dataset: { ago: account.last_good_at, agoTemplate: T.checked },
      text: fill(T.checked, { age: AD.format.ago(account.last_good_at, nowMs) }) });
  }

  /** What a saved account still needs before it can be switched to, when signing in again is not it. */
  function setupNote(account) {
    if (account.saved === false || account.switchable || A.needsSignin(account)) return null;
    const desktop = account.provider === 'claude';
    if (desktop && account.imported_from === 'pc') return T.setup.importing;
    return desktop && account.active ? T.setup.saving : T.setup.signIn;
  }

  /** Under the name: the status when something needs a look, else plan and email; then any notes. */
  function detailLines(account, nowMs) {
    if (A.frozen(account)) return [];
    const lines = [];
    const headline = account.headline || {};
    if (headline.text && !METER_KINDS.has(headline.kind)) {
      lines.push(el('p', { class: 'row-status', dataset: { kind: headline.kind } },
        AD.dom.svgIcon(STATUS_ICONS[headline.kind] || 'info'), el('span', { text: mask(headline.text) })));
    } else {
      const meta = [AD.format.capitalised(account.plan), account.email ? mask(account.email) : null].filter(Boolean).join(' · ');
      if (meta) lines.push(el('p', { class: 'row-sub', text: meta, title: meta }));
    }
    for (const note of [setupNote(account), account.live_expired ? T.liveExpired : null]) {
      if (note) lines.push(el('p', { class: 'row-note', text: note }));
    }
    const fresh = freshness(account, nowMs);
    if (fresh) lines.push(fresh);
    return lines;
  }

  /** The row's limits. A table with column heads (ctx.compact off) shows the same three in every row;
   * a list without them shows the limits the account has, each with its own label. */
  function limitCells(account, ctx) {
    if (A.frozen(account)) return null;
    if (A.needsSignin(account)) return el('p', { text: T.blockedSignin });
    return (ctx.compact ? account.windows || [] : A.limits(account)).map((win) => AD.meter.limit(win, account, ctx));
  }

  // ---- A row ------------------------------------------------------------------------------------

  /** One account row. ctx: {nowMs, display, threshold, compact, readonly, busy} and, for the list that
   * auto-switch works on, {fableLeader, nextId, bestId}. */
  function row(account, state, ctx) {
    const action = primaryAction(account, state, ctx);
    const nameId = `row-${account.id}-name`;
    const actions = [];
    if (action) {
      actions.push(AD.dom.button(action.label, action.run, {
        kind: action.prominent && !action.disabled ? 'primary' : undefined, small: true, disabled: action.disabled,
        title: action.reason || undefined, className: 'row-action',
        attrs: { 'data-primary': '', 'data-keep': `primary-${account.id}`, 'aria-describedby': nameId } }));
    }
    if (account.saved !== false && !A.frozen(account)) actions.push(menuButton(account));
    return el('li', {
      class: ['row', A.blocked(account) && 'is-blocked', account.active && 'is-current', ctx.nextId === account.id && 'is-next',
        account.stale && 'is-stale', account.refreshing && 'is-refreshing'],
      id: `row-${account.id}`, tabindex: -1, 'aria-labelledby': nameId, dataset: { id: account.id },
    },
    el('div', 'row-id',
      AD.dom.avatar(account.label, { active: account.active, muted: A.blocked(account) }),
      el('div', 'row-text',
        el('div', 'row-title', el('h3', { class: 'row-name', id: nameId, text: mask(account.label) }), badges(account, ctx)),
        detailLines(account, ctx.nowMs))),
    el('div', { class: ['row-limits', A.needsSignin(account) && 'is-message'] }, limitCells(account, ctx)),
    el('div', 'row-actions', actions));
  }

  function rowSig(account, state, ctx) {
    const derived = (account.windows || []).map((w) => AD.model.deriveWindow(w, account, ctx.nowMs).state);
    return JSON.stringify([A.signature(account), derived, state.pending ? state.pending.account || true : null,
      ctx.display, ctx.threshold, AD.dom.hideEmails(), ctx.fableLeader === account.id, ctx.nextId === account.id,
      ctx.bestId === account.id, ctx.readonly, ctx.busy]);
  }

  /** Keep `list` (an <ol>) showing `accounts` as rows: unchanged rows keep their node and focus. */
  function patchRows(list, accounts, state, ctx) {
    const make = (account) => {
      const node = row(account, state, ctx);
      sigs.set(node, rowSig(account, state, ctx));
      return node;
    };
    AD.dom.patchList(list, accounts, 'id', make,
      (node, account) => (sigs.get(node) === rowSig(account, state, ctx) ? null : make(account)));
    const rows = Array.from(list.children);
    setTabStop(list, rows.find((node) => node.getAttribute('data-id') === tabStops.get(list)) || rows[0]);
  }

  // ---- Keyboard ---------------------------------------------------------------------------------

  /** Exactly one row of a list is in the Tab order: the one last focused, else the first. */
  function setTabStop(list, keep) {
    for (const node of list.children) node.tabIndex = node === keep ? 0 : -1;
  }

  /** Make a row its list's Tab stop, and keep it so when the list is drawn again. */
  function takeTabStop(node) {
    tabStops.set(node.parentElement, node.getAttribute('data-id'));
    setTabStop(node.parentElement, node);
  }

  function focusRow(node, scroll = true) {
    if (!node) return false;
    takeTabStop(node);
    node.focus({ preventScroll: true });
    if (scroll) node.scrollIntoView({ block: 'nearest' });
    return true;
  }

  /** Arrow keys and Return on the rows of `list` (bound once). */
  function bindKeys(list) {
    list.addEventListener('focusin', (event) => {
      const node = event.target.closest('.row');
      if (node && node.parentElement === list) takeTabStop(node);
    });
    list.addEventListener('keydown', (event) => {
      if (!event.target.matches('.row') || event.metaKey || event.ctrlKey || event.altKey) return;
      const rows = Array.from(list.children);
      const index = rows.indexOf(event.target);
      let next = null;
      if (event.key === 'ArrowDown') next = rows[Math.min(rows.length - 1, index + 1)];
      else if (event.key === 'ArrowUp') next = rows[Math.max(0, index - 1)];
      else if (event.key === 'Home') next = rows[0];
      else if (event.key === 'End') next = rows[rows.length - 1];
      else if (event.key === 'Enter' || event.key === ' ') {
        const primary = event.target.querySelector('[data-primary]');
        if (primary && !primary.disabled) {
          event.preventDefault();
          primary.click();
        }
        return;
      }
      if (next) {
        event.preventDefault();
        focusRow(next);
      }
    });
  }

  /** Focus the terminal row at `index` (the 1–9 keys); false when there is none. */
  function focusIndex(index) {
    return focusRow(document.querySelectorAll('#accounts .row')[index] || null);
  }

  /** Scroll to a row and flash it (from the overview's names). */
  function spotlight(id) {
    const node = document.getElementById(`row-${id}`);
    if (!node) return;
    focusRow(node, false);
    node.scrollIntoView({ block: 'center', behavior: AD.theme.reducedMotion() ? 'auto' : 'smooth' });
    node.classList.remove('is-spotlit');
    void node.offsetWidth;                      // restart the animation when spotlit twice
    node.classList.add('is-spotlit');
    window.setTimeout(() => node.classList.remove('is-spotlit'), SPOTLIGHT_MS);
  }

  AD.rows = { patchRows, bindKeys, focusIndex, spotlight };
})();
