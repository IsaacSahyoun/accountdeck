/* AccountDeck 2 — the copy catalogue and text helpers.
 * Mac wording throughout: buttons and menu items in title case ("Switch Now", "Rename…"), labels and
 * descriptions in sentence case. Countdown and clock rules live in AD.model so the page and
 * contract.py never disagree. Templates use {name} placeholders; fill() substitutes them. */
(function () {
  'use strict';
  const AD = window.AD = window.AD || {};

  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

  function freeze(object) {
    for (const value of Object.values(object)) {
      if (value && typeof value === 'object') freeze(value);
    }
    return Object.freeze(object);
  }

  const STRINGS = freeze({
    app: 'AccountDeck 2',
    apps: { codex: 'Codex', claude_cli: 'Claude Code', muse: 'Muse Code' },

    shell: {
      version: 'AccountDeck {version}', updated: 'Updated {age} ago', updatedNow: 'Updated just now',
      checking: 'Checking now…', paused: 'Usage checks paused', stillChecking: 'Still checking… last update {age} ago',
      refreshed: 'Accounts refreshed.', themeChanged: 'Appearance: {mode}',
      quitTitle: 'Quit AccountDeck 2?',
      quitBody: 'Usage checks, auto-switch and alerts stop until you open it again. Claude, Codex and Muse keep running as they are.',
      shortcuts: 'Keyboard Shortcuts', demo: 'Demo clock', demoOffset: '{sign}{offset} from the demo time',
      demoNow: 'At the demo time', switching: 'Switching…',
    },
    connection: {
      offlineTitle: "Can't reach AccountDeck 2", offline: "The app isn't responding. {reopen}",
      signinTitle: 'This page is signed out', closedTitle: 'AccountDeck 2 has quit',
      network: "Can't reach AccountDeck 2.", failed: "That didn't go through. Try again.",
    },
    status: { ok: 'Operational', degraded: 'Degraded', outage: 'Incident', unknown: 'Status unknown' },

    window: {
      resetsIn: 'Resets in {countdown}', resetsAt: 'Resets at {clock}', backIn: 'Back in {countdown}',
      resetting: 'Resetting…', resettingTip: 'Should have reset at {clock} — confirming',
      expected: 'Refill expected · unconfirmed', notStarted: 'Not started', startsNext: 'Starts with your next message',
      refilled: 'Refilled at {clock}', notReported: 'Not reported',
    },

    overview: {
      inUse: 'In use by your terminals', nobodyInUse: 'No account in use',
      nobodyInUseLine: 'Switch to an account below and every terminal follows it.',
      limitCaption: '{window} limit', waiting: 'Waiting for the first reading',
      auto: 'Auto-switch', autoToggle: 'Switch accounts automatically',
      autoOff: 'Auto-switch is off', autoOffLine: 'Terminals stay on {label} until you switch.',
      autoOffNoActive: 'Pick an account below when you need one.',
      takesOver: 'Takes over when {label} reaches {n}% (the notch on its weekly and Fable bars).',
      takesOverPlain: 'Takes over when the account in use reaches {n}% (the notch on its weekly and Fable bars).',
      room: '{n}% room', roomTip: '{n} points below the {t}% switch point',
      nobody: 'No account can take over', nobodyLine: 'Every other account is in use, out of room or left out.',
      nobodyAlone: 'Add another terminal account so auto-switch has somewhere to go.',
      then: 'Then', waitingList: 'Waiting', hold: 'On hold: {reason}', best: 'Most room right now:',
      settingsLink: 'Auto-Switch Settings…',
      nextReset: 'Next reset', refilled: 'Refilled', refilledLine: "{label}'s {window} limit is full again.",
      allClear: 'All clear', allClearLine: 'Nothing with much used resets in the next 24 hours.',
      unknownTitle: 'Waiting for readings', unknownLine: 'Usage appears after your next message in Claude Code.',
      resetWindow: '{window} limit', refillsAt: 'Refills at {clock}',
      fableRoom: '{label} room', fableFallback: 'Fable weekly', noFable: 'No readings yet',
      noFableLine: 'They appear after a Fable message in Claude Code.',
      leaderLine: '{n}% left · resets {clock}', tie: 'Tied for the most room', fableTag: 'Most Fable room',
      left: '{n}% left', notReported: 'not reported', show: 'Show {label} in the list',
    },

    accounts: {
      title: 'Claude Code', count: '{n} accounts', countOne: '1 account',
      sub: 'Every open terminal follows the account in use.', add: 'Add Account',
      sorts: {
        room: 'Most room left', fable: 'Most Fable room', weekly: 'Most weekly room',
        reset: 'Soonest weekly reset', manual: 'My order',
      },
      emptyTitle: 'No terminal accounts yet',
      emptyText: 'Your current Claude Code login is saved automatically when it can be verified. To start, install Claude Code and sign in, or choose Add Account. Use a private browser window to choose a different account.',
      unavailableTitle: "Claude Code accounts aren't available", unavailableText: 'This build has no Claude Code (terminal) support.',
      colAccount: 'Account',
      tip: 'Switch accounts here, not with /login or /logout in Claude Code. Open terminals follow on their next message.',
      readOnlyOther: 'Another AccountDeck 2 is managing your sign-ins. Quit that copy to make changes here.',
      undo: 'Undo', undoLeft: 'Undo available for {countdown}', cancelAdd: 'Cancel',
      addSheet: {
        title: 'Add a Terminal Account',
        body: 'A Terminal window opens. Sign in there with the account you want to add, then close that window to finish.',
        hint: 'If the browser signs in to the wrong account, use a private window.', open: 'Open Terminal',
      },
    },

    // One account row, the same for every client.
    row: {
      switch: 'Switch', switchReview: 'Switch…', signIn: 'Sign In Again', save: 'Save Current Account',
      more: 'More actions for {label}',
      blockedSignin: 'Usage is unavailable until you sign in again.',
      liveExpired: 'Usage updates after your next message in Claude Code.',
      checked: 'Checked {age} ago', paused: 'Checks paused until {clock}',
      setupSignIn: 'Use Add Account to sign in to this account again before switching.',
      menu: {
        leaveOut: 'Leave Out of Auto-Switch', include: 'Let Auto-Switch Use It', rename: 'Rename…', remove: 'Remove…',
        removeActive: 'Switch to another account before removing this one.',
      },
      rename: { title: 'Rename Account', label: 'Account name', save: 'Rename' },
      remove: {
        title: 'Remove “{label}”?', button: 'Remove',
        body: "AccountDeck forgets this {app} login. The account itself isn't affected, and you can add it again later.",
        inUse: '{app} stays signed in to it, and the account shows here as not saved.',
        elsewhere: 'This removes the saved row and its history from AccountDeck on this Mac.',
      },
    },

    chip: {
      current: 'In use', next: 'Next up', best: 'Most room', leftOut: 'Left out', notSaved: 'Not saved',
      incident: 'Paused · incident', cloudflare: 'Check refused by Cloudflare', error: "Couldn't refresh",
    },

    button: {
      switchNow: 'Switch Now', continueNow: 'Continue Now',
      cancel: 'Cancel', done: 'Done', hide: 'Hide', progress: 'Show Progress', review: 'Review…',
      retry: 'Try Again', viewReport: 'Show Report', explain: 'Explain Last Switch', recover: 'Recover…',
      refresh: 'Refresh', resume: 'Resume Checks', dismiss: 'Dismiss', reset: 'Reset', quit: 'Quit',
      endCodex: 'End Session', waitCodex: 'Wait for Them to Finish', importPC: 'Import from PC…',
    },
    modal: {
      switchTerminals: 'Switch your terminals to {label}?',
      switchCodex: 'Switch Codex to {label}?', switchMuse: 'Switch Muse to {label}?',
      reconfirm: 'The running sessions changed. Please confirm again.',
      savesOutgoing: "Your current account ({label}) isn't saved yet — it will be saved so you can switch back.",
      running: 'Running now', will: 'What will happen', stays: 'What stays with you',
      headsUp: 'Heads-up', remember: "Don't ask again when Claude is idle",
      blocked: 'This cannot start yet.',
      consumers: '{n} Codex sessions are running. Switch when they finish, or end them one by one.',
      museConsumers: '{n} Muse sessions are open. The switch waits until you close them.',
      progressTitle: 'Working…',
      purpose: {
        add: 'Add a Codex account', capture: 'Save the account Codex is signed in to?',
        recover: 'Recover the previous sign-in?', 'finish-login': 'Save the new Codex account?',
        'cancel-login': 'Stop adding the Codex account?',
      },
      purposeMuse: {
        add: 'Add a Muse account', capture: 'Save the account Muse is signed in to?',
        recover: 'Recover the previous Muse sign-in?', 'finish-login': 'Save the new Muse account?',
        'cancel-login': 'Stop adding the Muse account?',
      },
      endTitle: 'End this Codex session?', endBody: 'It stops right away; anything it was doing is interrupted.',
      consumerFrom: 'from {parent}', consumerStarted: 'started {clock}',
      consumerKind: { tui: 'Codex in a terminal', exec: 'A Codex command', app_server_child: 'Codex in an editor', other: 'A Codex session' },
      museConsumerKind: { tui: 'Muse in a terminal', app_server_child: 'Muse session host (muse serve)', other: 'A Muse session' },
    },
    progress: {
      title: 'Switching to {label}', waitingOn: 'Waiting for {names}',
      step: { pending: 'waiting', active: 'in progress', done: 'done', skipped: 'skipped', warning: 'needs attention', failed: 'failed' },
    },
    session: { busy: 'working', idle: 'idle', unknown: 'status unknown' },
    report: {
      switched: 'Switched to {label}',
      title: 'Last switch', failedTitle: "The switch didn't finish", cancelledTitle: 'Switch cancelled',
    },
    login: {
      codexTitle: 'Finish signing in with ChatGPT', codex: 'Your new Codex account appears here once you have signed in.',
      museTitle: 'Finish signing in with Meta',
      muse: 'Open the sign-in page, check the code matches, and approve it. Then choose Save Sign-In.',
      open: 'Open Sign-In Page', saveCodex: 'Save Sign-In', cancel: 'Cancel Sign-In',
      deviceCode: 'Device code: {code}',
    },
    outage: { link: 'Open the status page' },
    alerts: {
      more: '{message} (+{n} more)', imminent: "{label}'s {window} limit resets in {countdown}",
      resetting: "{label}'s {window} limit is resetting", ready: '{label} is ready again.',
      blocked: 'Allow notifications for this page in your browser settings to get alerts.',
      unsupported: "This browser can't show notifications.", enabled: 'Browser notifications are on.',
    },

    settings: {
      title: 'Settings',
      tabs: { general: 'General', autoswitch: 'Auto-Switch', notifications: 'Notifications', menubar: 'Menu Bar & Login', advanced: 'Advanced', about: 'About' },
      appearance: 'Appearance', numbers: 'Show usage as', used: 'Used', left: 'Left',
      privacy: 'Privacy and confirmation',
      hideEmails: 'Hide email addresses', hideEmailsHint: 'Masks addresses on this screen only.',
      confirmIdle: 'Ask before switching when Claude is idle',
      confirmIdleHint: 'Shows the confirmation even when no Claude session would be interrupted.',
      auto: 'Switch accounts automatically', autoHint: 'Before an account runs out, every terminal moves to the next one.',
      threshold: 'Switch when an account reaches', thresholdHint: 'Of its weekly or Fable weekly limit.',
      thresholdUp: 'Raise the switch point', thresholdDown: 'Lower the switch point',
      order: 'Next account', orderBy: 'Order', aboutVersion: 'Version {version}', orders: { soonest_reset: 'Soonest reset first', most_room: 'Most room left first', manual: 'My own order' },
      orderNow: 'Order right now', orderManual: 'Your order (top goes first)', orderEmpty: 'Add terminal accounts to see the order.',
      orderReset: 'Reset to Automatic Order', moveUp: 'Move {label} up', moveDown: 'Move {label} down',
      early: 'Use weekly allowance before it resets', earlyHint: 'Keeps your terminals on the account whose week resets soonest, so its unused allowance is spent first.',
      alertsOn: 'Show alerts in AccountDeck', alertsOnHint: 'Low limits, refills and switches appear on this page.',
      browser: 'Browser notifications', browserOn: 'On in this browser.', browserOff: 'Off. The browser asks for your permission once.',
      browserBlocked: "Blocked in this browser's site settings.", browserUnsupported: "This browser can't show notifications.",
      enable: 'Turn On…', turnOff: 'Turn Off',
      test: 'Test notification', testHint: 'If nothing appears, choose Turn On Notifications… in the AccountDeck 2 menu bar item.',
      testButton: 'Send Test', testSent: 'Test notification sent.',
      mac: 'Mac notifications are off',
      macOff: 'macOS is blocking notifications from AccountDeck 2. Choose Turn On Notifications… in its menu bar item, or allow them in System Settings › Notifications.',
      loginHint: 'Listed in System Settings › General › Login Items, where you can also turn it off.',
      pause: 'Pause usage checks', pauseHint: 'Background usage readings stop until you turn this back on.',
      importPC: 'Import accounts from your PC', importPCHint: 'Open an encrypted .adeck bundle exported by AccountDeck 2 on Windows.',
      shortcuts: 'Keyboard shortcuts', shortcutsButton: 'Show…', diagnostics: 'Diagnostics', diagnosticsButton: 'Show…',
      diagnosticsHint: 'A recent log for troubleshooting. It never contains tokens, cookies, keys or emails.',
      quit: 'Quit AccountDeck 2…',
    },

    details: {
      title: 'Activity & troubleshooting', activity: 'Recent activity', setup: 'Shared setup', explain: 'Last switch explained',
      diagnostics: 'Diagnostics', diagnosticsButton: 'Show Diagnostics…',
      noActivity: 'Your account changes will appear here.', loading: 'Loading the explanation…', notRecorded: 'not recorded',
      review: 'Needs review', preserved: 'Configuration preserved', notConfigured: 'Not configured', yes: 'Yes', no: 'No',
      footer: 'Logs never contain tokens, cookies, keys or emails.',
    },
    theme: { label: 'Appearance', system: 'Auto', light: 'Light', dark: 'Dark' },
    help: {
      logout: "Use Switch or Add Account here; never use Claude's own Log Out to change accounts — it can end that sign-in on Anthropic's side.",
    },
    shortcuts: [
      [['⌘', 'R'], 'Refresh all accounts'],
      [['⌘', ','], 'Settings'],
      [['↑', '↓'], 'Move between accounts'],
      [['Return'], 'Switch to the focused account'],
      [['1', '–', '9'], 'Go to account 1 to 9'],
      [['T'], 'Change the appearance'],
      [['Esc'], 'Close a sheet, menu or Settings'],
      [['?'], 'Show these shortcuts'],
    ],
  });

  /** Substitute {name} placeholders; unknown names stay as written so a missing value is visible. */
  function fill(template, values) {
    return String(template).replace(/\{(\w+)\}/g, (match, name) =>
      (values && values[name] !== undefined && values[name] !== null ? String(values[name]) : match));
  }

  /** Age of a reading, as in contract.age_text: "42m" (at least 1m), "3h" under 48 h, else "2d". */
  function ageText(secs) {
    if (!isNum(secs)) return '—';
    const minutes = Math.floor(Math.max(0, secs) / 60);
    if (minutes < 60) return `${Math.max(minutes, 1)}m`;
    if (minutes < 48 * 60) return `${Math.floor(minutes / 60)}h`;
    return `${Math.floor(minutes / 1440)}d`;
  }

  /** Age of `ts` (epoch s) at nowMs; callers add their own words ("Checked {age} ago"). */
  function ago(ts, nowMs) {
    return isNum(ts) ? ageText(nowMs / 1000 - ts) : '—';
  }

  /** Open sessions as words: “name” (working), “other” (idle). */
  function sessionNames(sessions) {
    return sessions.map((s) => `“${s.name}” (${STRINGS.session[s.status] || STRINGS.session.unknown})`).join(', ');
  }

  /** "Max" from "max": a plan name for display. */
  function capitalised(text) {
    return typeof text === 'string' && text ? text[0].toUpperCase() + text.slice(1) : null;
  }

  // ---- The platform block ------------------------------------------------------------------------
  // /api/state may carry `platform: {os, features, labels}`. This build is Mac-only: the page has Mac
  // copy built in and reads the block, when present, only to let the service adjust a few hints or
  // hide a feature it reports as unavailable. A missing or malformed block changes nothing.
  const MAC_LABELS = freeze({
    section: 'Menu Bar & Login',
    status_icon: 'Show in the menu bar',
    status_icon_hint: 'The menu bar item shows the terminal room left. Click it for every account.',
    start_on_login: 'Open at login',
    reopen_hint: 'Reopen AccountDeck 2 from Applications.',
    open_hint: 'Open AccountDeck 2 from Applications.',
    closed_hint: 'Open it again from Applications to see your accounts.',
    system_error: "macOS didn't allow that. Try again.",
  });
  // Server labels that are allowed to replace the built-in copy (the rest stay the page's own).
  const SERVER_LABELS = new Set(['reopen_hint', 'open_hint', 'closed_hint', 'system_error', 'status_icon_hint']);
  let platform = { features: {}, labels: {}, notifications: null };
  // The app's own window (a WKWebView) names itself in the user agent. It has no browser
  // notifications; Mac notifications come from the menu bar item instead.
  const APP_WINDOW = /AccountDeck2App\//.test((window.navigator && window.navigator.userAgent) || '');

  /** Follow the platform block of a new state (or its absence). */
  function setPlatform(block) {
    const ok = block && typeof block === 'object';
    platform = {
      features: ok && block.features && typeof block.features === 'object' ? block.features : {},
      labels: ok && block.labels && typeof block.labels === 'object' ? block.labels : {},
      notifications: ok ? block.notifications : null,
    };
  }

  /** True inside AccountDeck 2's own window rather than a browser. */
  function appWindow() {
    return APP_WINDOW;
  }

  /** macOS's notification permission for AccountDeck 2 as the service reports it
   * (platform.notifications.state: "authorized", "denied", "not_determined", …), or null. */
  function notificationState() {
    const value = platform.notifications;
    const state = value && typeof value === 'object' ? value.state : null;
    return typeof state === 'string' && state ? state : null;
  }

  /** False only when the service explicitly reports a feature (menu_bar, start_on_login) as unavailable. */
  function feature(name) {
    return platform.features[name] !== false;
  }

  /** The Mac copy for `name`; a few hints may come from the service. */
  function label(name) {
    const server = platform.labels[name];
    if (SERVER_LABELS.has(name) && typeof server === 'string' && server) return server;
    return MAC_LABELS[name] || '';
  }

  /** An error's message for the page: the service's own words; code "system" (macOS refused)
   * without a message gets the Mac wording. */
  function errorText(code, message) {
    if (code === 'system' && !message) return label('system_error');
    return message;
  }

  AD.format = { STRINGS, fill, ageText, ago, sessionNames, capitalised, setPlatform, feature, label, errorText, appWindow, notificationState };
})();
