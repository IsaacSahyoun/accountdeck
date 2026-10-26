/* AccountDeck 2 — the rows of the accounts table. A row shows the account (monogram, name, badges,
 * a detail or status line), its three limits — 5-hour, weekly and Fable 5.1 weekly, plus any other
 * model limit that binds — and its action and More menu. Rows are keyed by account id and rebuilt
 * only when their data changed; countdowns are [data-reset] text the shell's clock patches.
 *
 * Keyboard: the list is one Tab stop (a roving tabindex); ↑ ↓ Home End move between rows, Return
 * runs the focused row's action, and Tab continues into the row's own buttons. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const A = AD.accounts;
  const T = STRINGS.accounts;

  const SPOTLIGHT_MS = 1400;
  const STATUS_KINDS = new Set(['signin', 'out', 'outage']);
  const STATUS_ICONS = { signin: 'warning', out: 'lock', outage: 'pause' };
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const sigs = new WeakMap();
  let lastFocused = null;                       // the row id that holds the list's Tab stop

  // ---- The row's action and menu ----------------------------------------------------------------

  /** The row's one button: Switch, Sign In Again, or nothing for the account in use. */
  function primaryAction(account, state, ctx) {
    if (A.frozen(account)) return { label: 'Remove…', run: () => AD.move.remove(account), disabled: false };
    const reason = ctx.readonly;
    const busy = !!state.pending;
    if (A.needsSignin(account)) return { label: T.signIn, run: () => AD.terminal.add(), disabled: !!reason || busy, reason };
    if (account.active) return null;
    const target = busy && state.pending.account === account.id;
    return { label: target ? STRINGS.shell.switching : T.switch, run: () => AD.switch.open(account.id),
      disabled: !!reason || busy, reason, prominent: ctx.nextId === account.id || ctx.bestId === account.id };
  }

  function menuItems(account) {
    const M = T.menu;
    return [
      { label: account.excluded ? M.include : M.leaveOut, icon: account.excluded ? 'plus-circle' : 'minus-circle',
        run: () => AD.terminal.exclude(account) },
      { label: M.rename, icon: 'pencil', run: () => AD.terminal.rename(account) },
      'separator',
      { label: M.remove, icon: 'trash', danger: true, disabled: account.active, title: account.active ? M.removeActive : undefined,
        run: () => AD.terminal.remove(account) },
      account.active ? { note: M.removeActive } : null,
    ].filter(Boolean);
  }

  function menuButton(account) {
    const key = `menu-${account.id}`;
    const node = AD.dom.button('', () => AD.menu.open(node, menuItems(account), { label: fill(T.more, { label: mask(account.label) }) }), {
      kind: 'icon', icon: 'ellipsis', label: fill(T.more, { label: mask(account.label) }), title: 'More',
      attrs: { 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'data-keep': key },
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

  /** "Checked 42m ago" once a reading is stale; paused checks say until when. */
  function freshness(account, nowMs) {
    if (isNum(account.paused_until) && account.paused_until > nowMs / 1000) {
      return el('p', { class: 'row-note', text: fill(T.paused, { clock: AD.model.clock(account.paused_until, nowMs) }) });
    }
    if (!isNum(account.last_good_at) || account.age_tier !== 'stale' || (account.headline || {}).kind === 'stale') return null;
    return el('p', { class: 'row-note is-stale', dataset: { ago: account.last_good_at, agoTemplate: T.checked },
      text: fill(T.checked, { age: AD.format.ago(account.last_good_at, nowMs) }) });
  }

  /** Under the name: the status when something needs a look, else plan and email; then any notes. */
  function detailLines(account, nowMs) {
    if (A.frozen(account)) return [];
    const lines = [];
    const headline = account.headline || {};
    if (headline.text && STATUS_KINDS.has(headline.kind)) {
      lines.push(el('p', { class: 'row-status', dataset: { kind: headline.kind } },
        AD.dom.svgIcon(STATUS_ICONS[headline.kind] || 'info'), el('span', { text: mask(headline.text) })));
    } else {
      const meta = [AD.format.capitalised(account.plan), account.email ? mask(account.email) : null].filter(Boolean).join(' · ');
      if (meta) lines.push(el('p', { class: 'row-sub', text: meta, title: meta }));
    }
    if (account.live_expired) lines.push(el('p', { class: 'row-note', text: T.liveExpired }));
    const fresh = freshness(account, nowMs);
    if (fresh) lines.push(fresh);
    return lines;
  }

  function limitCells(account, ctx) {
    if (A.frozen(account)) return null;
    if (A.needsSignin(account)) return el('p', { text: T.blockedSignin });
    return A.limits(account).map((win) => AD.meter.limit(win, account, ctx));
  }

  // ---- A row ------------------------------------------------------------------------------------

  /** One account row. ctx: {nowMs, display, threshold, fableLeader, nextId, bestId, readonly}. */
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
    if (!A.frozen(account)) actions.push(menuButton(account));
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
      ctx.bestId === account.id, ctx.readonly]);
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
    syncTabStop(list);
  }

  // ---- Keyboard ---------------------------------------------------------------------------------

  /** Exactly one row is in the Tab order: the one last focused, else the first. */
  function syncTabStop(list) {
    const rows = Array.from(list.children);
    const keep = rows.find((node) => node.getAttribute('data-id') === lastFocused) || rows[0];
    for (const node of rows) node.tabIndex = node === keep ? 0 : -1;
  }

  function focusRow(node, scroll = true) {
    if (!node) return false;
    const list = node.parentElement;
    for (const other of list.children) other.tabIndex = other === node ? 0 : -1;
    lastFocused = node.getAttribute('data-id');
    node.focus({ preventScroll: true });
    if (scroll) node.scrollIntoView({ block: 'nearest' });
    return true;
  }

  /** Arrow keys and Return on the rows of `list` (bound once). */
  function bindKeys(list) {
    list.addEventListener('focusin', (event) => {
      const node = event.target.closest('.row');
      if (node && node.parentElement === list) {
        lastFocused = node.getAttribute('data-id');
        for (const other of list.children) other.tabIndex = other === node ? 0 : -1;
      }
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

  /** Focus the row at `index` (the 1–9 keys); false when there is none. */
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
