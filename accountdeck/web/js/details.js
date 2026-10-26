/* AccountDeck 2 — "Activity & troubleshooting", one disclosure at the bottom of the accounts: the
 * last switch explained (once asked for), recent activity, the shared setup and Diagnostics.
 * Rebuilt only when its data changed; its open state survives through data-keep. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const { STRINGS } = AD.format;
  const D = STRINGS.details;

  const EVENTS_SHOWN = 20;
  const TIME_KEYS = new Set(['at', 'started', 'finished']);
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);

  let last = null;
  let drawn = null;
  let manifest = null;                // {op, data, error} once showManifest ran

  function words(key) {
    const text = String(key).replace(/[_-]+/g, ' ');
    return text ? text[0].toUpperCase() + text.slice(1) : text;
  }

  function value(key, item, nowMs) {
    if (item === null || item === undefined) return el('span', { class: 'is-quiet', text: D.notRecorded });
    if (typeof item === 'boolean') return D[item ? 'yes' : 'no'];
    if (typeof item === 'number' && TIME_KEYS.has(key)) return AD.model.clock(item, nowMs);
    if (Array.isArray(item)) {
      if (!item.length) return el('span', { class: 'is-quiet', text: '—' });
      if (item.every((entry) => entry === null || typeof entry !== 'object')) return item.map((entry) => mask(entry)).join(', ');
      return el('ol', 'manifest-list', item.map((entry) => el('li', null, value(key, entry, nowMs))));
    }
    if (typeof item === 'object') return fields(item, nowMs);
    return mask(item);
  }

  function fields(object, nowMs) {
    return el('dl', 'manifest', Object.keys(object).map((key) => el('div', 'manifest-row',
      el('dt', { text: words(key) }), el('dd', null, value(key, object[key], nowMs)))));
  }

  function activity(state, nowMs) {
    const events = (state.events || []).slice(0, EVENTS_SHOWN);
    if (!events.length) return el('p', { class: 'is-quiet', text: D.noActivity });
    return el('ol', 'events', events.map((event) => el('li', 'event',
      el('time', { text: AD.model.clock(event.at, nowMs) }), el('span', { text: mask(event.message) }))));
  }

  function setup(state) {
    const info = state.setup || {};
    const groups = (info.groups || []).map((group) => {
      let status = D.notConfigured;
      if (group.error) status = D.review;
      else if ((group.servers || []).length) status = group.servers.join(', ');
      else if (group.present) status = D.preserved;
      return el('div', 'manifest-row', el('dt', { text: group.label }), el('dd', { text: status }));
    });
    return [info.message ? el('p', { text: info.message }) : null, groups.length ? el('dl', 'manifest', groups) : null,
      info.note ? el('p', { class: 'is-quiet', text: info.note }) : null];
  }

  function explain(nowMs) {
    if (!manifest) return null;
    let body;
    if (manifest.error) body = el('p', { class: 'modal-error', role: 'alert', text: manifest.error });
    else if (!manifest.data) body = el('p', { class: 'is-quiet', text: D.loading });
    else body = fields(manifest.data, nowMs);
    return el('section', { class: 'details-part', id: 'details-explain', tabindex: -1 },
      el('h3', { class: 'details-heading', text: D.explain }), body);
  }

  /** Show the diagnostics log in a wide sheet. */
  async function diagnostics() {
    try {
      const data = await AD.api.get('/api/diagnostics');
      AD.dom.openModal({ title: D.diagnostics, view: 'diagnostics', size: 'wide',
        body: [el('p', { class: 'modal-hint', text: D.footer }), el('pre', { tabindex: 0, text: (data.lines || []).join('\n') })],
        actions: [AD.dom.button(STRINGS.button.done, () => AD.dom.closeModal(), { kind: 'primary', attrs: { 'data-autofocus': '' } })] });
    } catch (err) {
      AD.dom.toast(err.message, { level: 'critical' });
    }
  }

  function build(state, nowMs) {
    return el('details', { class: 'details-panel', 'data-keep': 'details-panel' },
      el('summary', { text: D.title }),
      el('div', 'details-body',
        explain(nowMs),
        el('section', 'details-part', el('h3', { class: 'details-heading', text: D.activity }), activity(state, nowMs)),
        el('section', 'details-part', el('h3', { class: 'details-heading', text: D.setup }), setup(state)),
        el('div', 'details-actions', AD.dom.button(D.diagnosticsButton, diagnostics, { small: true }))));
  }

  function draw(force) {
    const root = document.getElementById('details');
    if (!root || !last) return;
    const sig = JSON.stringify([last.events, last.setup, manifest, AD.dom.hideEmails()]);
    if (!force && sig === drawn) return;
    drawn = sig;
    const next = build(last, AD.app.clockNow());
    if (root.firstElementChild) AD.dom.swap(root.firstElementChild, next);
    else root.appendChild(next);
  }

  /** A new state: activity and setup follow it. */
  function render(state) {
    if (!state) return;
    last = state;
    draw(false);
  }

  /** Explain a switch: load its manifest, open the panel and bring the explanation into view. */
  async function showManifest(op) {
    manifest = { op, data: null, error: null };
    draw(true);
    const reveal = () => {
      const panel = document.querySelector('#details .details-panel');
      if (panel) panel.open = true;
      const part = document.getElementById('details-explain');
      if (part) {
        part.scrollIntoView({ block: 'start', behavior: AD.theme.reducedMotion() ? 'auto' : 'smooth' });
        part.focus({ preventScroll: true });
      }
    };
    reveal();
    try {
      // Without an operation id the service answers with the latest manifest.
      const data = await AD.api.get(op ? `/api/manifest?op=${encodeURIComponent(op)}` : '/api/manifest');
      manifest = { op, data: data || {}, error: null };
    } catch (err) {
      manifest = { op, data: null, error: err.message };
    }
    draw(true);
    reveal();
  }

  AD.details = { render, showManifest, diagnostics };
})();
