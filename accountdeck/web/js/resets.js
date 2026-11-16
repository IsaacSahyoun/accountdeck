/* Manual Claude Ultra resets. Only the server chooses a grant; the page confirms its opaque plan. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};
  const el = (...args) => AD.dom.el(...args);
  const mask = (text) => AD.dom.maskEmail(text);
  const close = () => AD.dom.closeModal();
  let request = 0;
  let account = null;

  function sheet(title, body, actions) {
    AD.dom.openModal({ title, body, actions, view: 'resets', onClose: () => { request += 1; } });
  }

  function accountText(account) {
    return mask([account.label, account.email].filter(Boolean).join(' · '));
  }

  function accountLine(value) {
    account = value;
    return el('p', { class: 'plan-lead reset-account', text: accountText(account) });
  }

  document.addEventListener('ad:hide-emails', () => {
    const line = document.querySelector('.reset-account');
    if (line && account) line.textContent = accountText(account);
  });

  function expiry(offer) {
    return offer.expires == null ? 'No expiry reported.' : `Expires ${new Date(offer.expires * 1000).toLocaleString()}.`;
  }

  function show(data) {
    const body = [accountLine(data.account)];
    if (data.message) body.push(el('p', { role: 'status', text: data.message }));
    if (!data.offers.length && !data.unconfirmed) body.push(el('p', { text: 'Claude has no reset offers for this account.' }));
    for (const offer of data.offers) {
      body.push(el('section', 'reset-offer',
        el('h3', { text: offer.title }),
        el('p', { text: `${offer.count} remaining. ${expiry(offer)}` }),
        el('p', { text: offer.effect }),
        offer.reason && offer.reason !== data.message ? el('p', { class: 'plan-muted', text: offer.reason }) : null));
    }
    const actions = [AD.dom.button('Close', close), AD.dom.button('Check again', () => open(data.account.id))];
    const offer = data.offers.find((item) => item.usable);
    if (offer && data.plan_id) actions.push(AD.dom.button(`Use ${offer.title}…`, () => confirm(data, offer), { kind: 'primary' }));
    sheet('Claude Ultra resets', body, actions);
  }

  function confirm(data, offer) {
    let submitted = false;
    const send = AD.dom.button(`Use ${offer.title}`, async () => {
      if (submitted) return;
      submitted = true;
      send.disabled = true;
      const ticket = request;
      try {
        const result = await AD.api.action('reset-use', { id: data.account.id, plan_id: data.plan_id });
        if (ticket === request && AD.dom.modalView() === 'resets') AD.switch.watch(result);
      } catch (error) {
        if (ticket !== request || AD.dom.modalView() !== 'resets') return;
        if (error.status === 409 && error.code === 'busy') {
          sheet('Reset not started', [accountLine(data.account), el('p', { role: 'alert', text: error.message }),
            el('p', { text: 'No reset was sent. If this confirmation expires, check the offers again.' })],
          [AD.dom.button('Close', close), AD.dom.button('Try again', () => confirm(data, offer), { kind: 'primary' })]);
          return;
        }
        if ([400, 404, 409].includes(error.status)) {
          const actions = [AD.dom.button('Close', close)];
          if (['stale_plan', 'invalid'].includes(error.code)) {
            actions.push(AD.dom.button('Check again', () => open(data.account.id)));
          }
          sheet('Reset not started', [accountLine(data.account), el('p', { role: 'alert', text: error.message }),
            el('p', { text: 'No new reset was sent.' })], actions);
          return;
        }
        sheet('Check the reset result', [el('p', { role: 'alert', text: error.message }),
          el('p', { text: 'Check the account before trying again. This confirmation will not be sent twice.' })],
        [AD.dom.button('Close', close), AD.dom.button('Check again', () => open(data.account.id))]);
      }
    }, { kind: 'primary' });
    sheet(`Use ${offer.title}?`, [accountLine(data.account),
      el('p', { text: offer.effect }), el('p', { text: `${offer.count} remaining. ${expiry(offer)}` }),
      el('p', { text: 'This spends one reset on this account and cannot be undone. Your signed-in account stays the same.' })],
    [AD.dom.button('Cancel', close, { attrs: { 'data-autofocus': '' } }), send]);
  }

  async function open(id) {
    const ticket = ++request;
    sheet('Claude Ultra resets', el('p', { role: 'status', text: 'Checking this account’s reset offers…' }),
      [AD.dom.button('Close', close)]);
    try {
      const data = await AD.api.action('reset-status', { id });
      if (ticket === request && AD.dom.modalView() === 'resets') show(data);
    } catch (error) {
      if (ticket !== request || AD.dom.modalView() !== 'resets') return;
      sheet('Could not check resets', el('p', { role: 'alert', text: error.message }),
        [AD.dom.button('Close', close), AD.dom.button('Check again', () => open(id))]);
    }
  }

  AD.resets = { open };
})();
