/* AccountDeck 2 — the overview: the answers that matter at a glance, for the Claude Code
 * (terminal) accounts only.
 *   In use      — the account every terminal uses, how much is left and when its limits reset;
 *   Auto-switch — on or off (a switch right there), who takes over next and when, and why the
 *                 others wait;
 *   Next reset  — the soonest refill across the accounts;
 *   Fable 5.1   — which account has the most Fable 5.1 weekly room.
 * render() runs on every state and clock tick; a card is rebuilt only when its signature changed.
 * Countdowns are [data-reset] text the shell's clock patches in place. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS, fill } = AD.format;
  const A = AD.accounts;
  const O = STRINGS.overview;

  const QUEUE_SHOWN = 4;
  const BOARD_SHOWN = 3;                      // the full ranking is the table's "Most Fable 5.1 room" order
  const HEADLINE_TONES = { ready: 'good', watch: 'accent', low: 'warning', critical: 'critical', out: 'critical', signin: 'warning' };
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);

  /** An account's name; a click brings its row into view. */
  function nameLink(label, id) {
    if (!id) return el('span', { text: mask(label) });
    return AD.dom.button(mask(label), () => AD.rows.spotlight(id), { kind: 'link', className: 'name-link',
      title: fill(O.show, { label: mask(label) }) });
  }

  function card(key, heading, parts, extra = {}) {
    const id = `ov-${key}-title`;
    return el('section', Object.assign({ class: ['card', `ov-${key}`], 'aria-labelledby': id }, extra),
      el('div', 'card-head', el('h2', { class: 'card-heading', id }, heading.title), heading.aside || null), parts);
  }

  function countdown(reset, secs, template) {
    return el('time', { dataset: { reset, resetTemplate: template, resetDone: STRINGS.window.resetting },
      text: fill(template, { countdown: AD.model.countdownText(secs) }) });
  }

  // ---- In use ------------------------------------------------------------------------------------

  function headlineBadge(account) {
    const headline = account.headline || {};
    if (!headline.text) return null;
    const tone = HEADLINE_TONES[headline.kind] || 'neutral';
    return AD.meter.badge(tone, mask(headline.text), { icon: tone === 'good' ? null : undefined, dot: tone === 'good' });
  }

  function figure(account, ctx) {
    const bind = A.binding(account, ctx.nowMs);
    if (!bind) return el('p', { class: 'figure-caption', text: O.waiting });
    const { win, d } = bind;
    const value = ctx.display === 'left' ? Math.max(0, Math.round(d.left)) : (d.used > 100 ? '100+' : Math.round(d.used));
    const timed = isNum(d.secs) && d.secs > 0;
    return el('div', 'figure-row',
      el('p', 'figure-line',
        el('span', { class: 'figure', dataset: { severity: d.severity }, text: `${value}%` }),
        el('span', { class: 'figure-unit', text: ctx.display === 'left' ? 'left' : 'used' })),
      el('p', 'figure-caption', fill(O.limitCaption, { window: win.label }),
        timed ? [' · ', countdown(d.reset, d.secs, d.state === 'out' ? 'back in {countdown}' : 'resets in {countdown}')] : null));
  }

  function inUseCard(state, ctx) {
    const account = A.active(state);
    if (!account) {
      return card('inuse', { title: O.inUse },
        el('div', 'figure-block', el('p', { class: 'ov-title', text: O.nobodyInUse }), el('p', { class: 'ov-line', text: O.nobodyInUseLine })));
    }
    const meta = [AD.format.capitalised(account.plan), account.email ? mask(account.email) : null].filter(Boolean).join(' · ');
    const parts = [
      el('div', 'who', AD.dom.avatar(account.label, { size: 'large', active: true }),
        el('div', 'who-text', el('p', 'who-name', nameLink(account.label, account.id)),
          meta ? el('p', { class: 'who-meta', text: meta }) : null)),
      figure(account, ctx),
      el('div', 'limit-stack', A.limits(account).map((win) => AD.meter.limit(win, account, Object.assign({ compact: true }, ctx)))),
    ];
    return card('inuse', { title: O.inUse, aside: headlineBadge(account) }, parts);
  }

  function inUseSig(state, ctx) {
    const account = A.active(state);
    if (!account) return 'none';
    const derived = (account.windows || []).map((w) => AD.model.deriveWindow(w, account, ctx.nowMs).state);
    return JSON.stringify([A.signature(account), derived, ctx.display, ctx.threshold, ctx.hide]);
  }

  // ---- Auto-switch ---------------------------------------------------------------------------------

  function autoToggle(state) {
    const auto = A.autoswitch(state);
    const provider = A.provider(state);
    const input = el('input', { type: 'checkbox', class: 'switch', role: 'switch', checked: !!auto.enabled,
      'aria-label': O.autoToggle, title: O.autoToggle, disabled: !!(provider && provider.readonly_reason),
      'data-keep': 'ov-auto-toggle' });
    input.addEventListener('change', () => {
      const on = input.checked;
      input.disabled = true;
      AD.api.settings({ cli_autoswitch: on }).catch((err) => {
        input.checked = !on;
        AD.dom.toast(err.message, { level: 'critical' });
      }).finally(() => { input.disabled = false; });
    });
    return input;
  }

  function queueRow(row) {
    const why = row.skip ? mask(row.skip) : (isNum(row.room) ? fill(O.room, { n: Math.round(row.room) }) : '');
    return el('li', { class: 'queue-row', dataset: { account: row.id, skipped: row.skip ? 'true' : null } },
      nameLink(row.label, row.id), el('span', { class: 'queue-why', text: why, title: why }));
  }

  function autoCard(state) {
    const auto = A.autoswitch(state);
    const active = A.active(state);
    const parts = [];
    if (!auto.enabled) {
      parts.push(el('div', 'figure-block', el('p', { class: 'ov-title', text: O.autoOff }),
        el('p', { class: 'ov-line', text: active ? fill(O.autoOffLine, { label: mask(active.label) }) : O.autoOffNoActive })));
      const best = A.bestPick(state);
      if (best) {
        parts.push(el('p', 'ov-line', `${O.best} `, el('strong', null, nameLink(best.account.label, best.account.id)),
          isNum(best.headroom) ? ` · ${fill(O.left, { n: Math.round(best.headroom) })}` : ''));
      }
    } else {
      // The account in use is never a target, so the queue leaves it out.
      const queue = (auto.next_up || []).filter((r) => !active || r.id !== active.id);
      const pick = queue.find((r) => !r.skip) || null;
      const rest = queue.filter((r) => r !== pick).slice(0, QUEUE_SHOWN);
      if (pick) {
        parts.push(el('div', 'ov-next', AD.dom.avatar(pick.label),
          el('p', 'who-name', nameLink(pick.label, pick.id)),
          isNum(pick.room) ? AD.meter.badge('accent', fill(O.room, { n: Math.round(pick.room) }),
            { title: fill(O.roomTip, { n: Math.round(pick.room), t: auto.threshold }) }) : null));
        parts.push(el('p', { class: 'ov-line', text: active
          ? fill(O.takesOver, { label: mask(active.label), n: auto.threshold })
          : fill(O.takesOverPlain, { n: auto.threshold }) }));
      } else {
        parts.push(el('div', 'figure-block', el('p', { class: 'ov-title', text: O.nobody }),
          el('p', { class: 'ov-line', text: rest.length ? O.nobodyLine : O.nobodyAlone })));
      }
      if (auto.hold) parts.push(el('p', 'ov-hold', AD.dom.svgIcon('pause'), el('span', { text: fill(O.hold, { reason: mask(auto.hold) }) })));
      if (rest.length) {
        parts.push(el('div', null, el('p', { class: 'queue-head', id: 'ov-queue-head', text: pick ? O.then : O.waitingList }),
          el('ol', { class: 'queue', 'aria-labelledby': 'ov-queue-head' }, rest.map(queueRow))));
      }
    }
    const settings = AD.dom.button('', () => AD.settings.open(AD.app.state, 'autoswitch'),
      { kind: 'icon', icon: 'gear', label: O.settingsLink, title: O.settingsLink, attrs: { 'data-keep': 'ov-auto-settings' } });
    return card('auto', { title: O.auto, aside: el('div', 'card-tools', settings, autoToggle(state)) }, parts);
  }

  function autoSig(state, ctx) {
    const auto = A.autoswitch(state);
    const active = A.active(state);
    const provider = A.provider(state);
    return JSON.stringify([auto.enabled, auto.threshold, auto.next_up, auto.hold, active && [active.id, active.label],
      state.now_band && state.now_band.best && state.now_band.best.claude_cli, provider && provider.readonly_reason, ctx.hide]);
  }

  // ---- Next reset ----------------------------------------------------------------------------------

  /** What the card shows, from the terminal rows alone: a refill from the last hour first, else the
   * soonest reset ahead, else all clear; before any reading, "unknown". */
  function resetPick(state, nowMs) {
    const accounts = A.terminals(state);
    if (state.ready === false || !accounts.some((a) => isNum(a.last_good_at))) return { kind: 'unknown', row: null };
    const rows = AD.model.stripItems(state, nowMs).filter((r) => r.provider === 'claude_cli');
    const refilled = rows.find((r) => r.chip === 'refilled');
    if (refilled) return { kind: 'refilled', row: refilled };
    const next = rows.find((r) => r.chip !== 'refilled' && r.secs > 0);
    return next ? { kind: 'next_reset', row: next } : { kind: 'all_clear', row: null };
  }

  function resetCard(state, pick, ctx) {
    const { kind, row } = pick;
    const parts = [];
    let tier = null;
    if (kind === 'next_reset') {
      const account = A.terminals(state).find((a) => a.id === row.account);
      const win = account && (account.windows || []).find((w) => w.id === row.window);
      tier = AD.model.tier(row.secs);
      const usage = isNum(row.left) ? AD.model.formatNumber({ used: 100 - row.left }, ctx.display) : STRINGS.window.notReported;
      parts.push(el('div', 'figure-block',
        el('div', 'figure-row',
          el('p', 'figure-line', el('span', { class: 'figure', role: 'timer', 'aria-live': 'off', dataset: { reset: row.reset },
            text: AD.model.countdownText(row.secs) })),
          el('p', { class: 'figure-caption', text: fill(O.refillsAt, { clock: AD.model.clock(row.reset, ctx.nowMs) }) })),
        el('p', 'ov-line', el('strong', null, nameLink(row.label, row.account)), ` · ${fill(O.resetWindow, { window: row.window_label })} · ${usage}`)));
      if (win) parts.push(AD.meter.meter(win, { account, nowMs: ctx.nowMs, display: ctx.display, size: 'large' }));
    } else if (kind === 'refilled') {
      parts.push(el('div', 'figure-block', el('p', 'ov-title ov-title-icon', AD.dom.svgIcon('check-circle'), O.refilled),
        el('p', { class: 'ov-line', text: fill(O.refilledLine, { label: mask(row.label), window: String(row.window_label || '').toLowerCase() }) })));
    } else if (kind === 'all_clear') {
      parts.push(el('div', 'figure-block', el('p', { class: 'ov-title', text: O.allClear }), el('p', { class: 'ov-line', text: O.allClearLine })));
    } else {
      parts.push(el('div', 'figure-block', el('p', { class: 'ov-title', text: O.unknownTitle }), el('p', { class: 'ov-line', text: O.unknownLine })));
    }
    // data-tier lets the countdown take the accent once the reset is near; the clock keeps it current.
    return card('reset', { title: O.nextReset }, parts, { dataset: { kind, tier } });
  }

  function resetSig(pick, ctx) {
    const row = pick.row;
    return JSON.stringify([pick.kind, row && [row.account, row.label, row.window, row.reset, row.left], ctx.display, ctx.hide]);
  }

  // ---- Fable 5.1 room ----------------------------------------------------------------------------------

  function fableLabel(state, board) {
    if (board.length) return board[0].win.label;
    for (const account of A.terminals(state)) {
      const win = A.fableWindow(account);
      if (win && win.label) return win.label;
    }
    return O.fableFallback;
  }

  function boardRow(row, index, leader, nowMs) {
    const lead = row.id === leader;
    const value = row.d.left === null ? O.notReported : fill(O.left, { n: Math.round(row.d.left) });
    return el('li', { class: 'board-row', dataset: { account: row.id, leader: lead ? 'true' : null } },
      el('span', { class: 'board-rank' }, lead ? AD.dom.svgIcon('crown', { label: O.fableTag }) : String(index + 1)),
      el('span', 'board-name', nameLink(row.label, row.id)),
      el('span', { class: 'board-value', text: value }),
      AD.meter.meter(row.win, { account: row.account, nowMs, display: 'left' }));
  }

  function fableCard(state, ctx) {
    const board = A.fableBoard(A.terminals(state), ctx.nowMs);
    const label = fableLabel(state, board);
    const heading = { title: [el('span', { class: 'fable-mark', 'aria-hidden': 'true' }), fill(O.fableRoom, { label })] };
    if (!board.length) {
      return card('fable', heading, el('p', 'ov-line', el('strong', { text: O.noFable }), ` — ${O.noFableLine}`), { dataset: { board: 'empty' } });
    }
    const leader = A.fableLeader(board);
    const top = board[0];
    const parts = [];
    if (leader) {
      parts.push(el('div', 'figure-block', el('p', 'ov-title', nameLink(top.label, top.id)),
        el('p', { class: 'ov-line', text: top.d.reset
          ? fill(O.leaderLine, { n: Math.round(top.d.left), clock: AD.model.clock(top.d.reset, ctx.nowMs) })
          : fill(O.left, { n: Math.round(top.d.left) }) })));
    } else {
      parts.push(el('p', { class: 'ov-title', text: O.tie }));
    }
    parts.push(el('ol', { class: 'board', 'aria-label': fill(O.fableRoom, { label }) },
      board.slice(0, BOARD_SHOWN).map((row, index) => boardRow(row, index, leader, ctx.nowMs))));
    return card('fable', heading, parts, { dataset: { board: 'ready' } });
  }

  function fableSig(state, ctx) {
    const board = A.fableBoard(A.terminals(state), ctx.nowMs);
    return JSON.stringify([board.map((r) => [r.id, r.label, r.win.label, r.win.reset, r.d.state, r.d.used]),
      A.fableLeader(board), ctx.hide]);
  }

  // ---- Render ----------------------------------------------------------------------------------------

  /** Render the overview at nowMs; cheap when nothing but the time changed. Returns whether it shows. */
  function render(state, nowMs) {
    const host = document.getElementById('overview');
    if (!host || !state) return false;
    if (!A.provider(state) || !A.terminals(state).length) {
      host.replaceChildren();
      return false;
    }
    const auto = A.autoswitch(state);
    const ctx = { nowMs, display: A.display(state), threshold: auto.enabled && isNum(auto.threshold) ? auto.threshold : null,
      hide: AD.dom.hideEmails() };
    const pick = resetPick(state, nowMs);
    AD.dom.patchSigned(host, [
      { key: 'inuse', sig: inUseSig(state, ctx), build: () => inUseCard(state, ctx) },
      { key: 'auto', sig: autoSig(state, ctx), build: () => autoCard(state) },
      { key: 'reset', sig: resetSig(pick, ctx), build: () => resetCard(state, pick, ctx) },
      { key: 'fable', sig: fableSig(state, ctx), build: () => fableCard(state, ctx) },
    ]);
    return true;
  }

  AD.overview = { render, resetPick };
})();
