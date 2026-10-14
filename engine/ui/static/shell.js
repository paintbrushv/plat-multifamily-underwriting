/* The frame every screen shares: DOM helpers, the seven-step header, "New? Start here", export
   links, glossary tooltips, empty / loading / error states, and the app object that holds the deal
   state and routes between screens (inside one MCP App iframe a step on another screen becomes a
   request to the assistant; in the browser app it simply switches screens). */
(function (root) {
  'use strict';
  var core = root.PUWCore;
  var doc = root.document;
  var uid = 0;

  // ── DOM ────────────────────────────────────────────────────────────────
  /** h('div', {class: 'x', onclick: fn}, [children]) — text is always set as text, never as HTML. */
  function h(tag, attrs, children) {
    var el = doc.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (name) {
      var value = attrs[name];
      if (value === null || value === undefined || value === false) { return; }
      if (name.indexOf('on') === 0 && typeof value === 'function') { el.addEventListener(name.slice(2), value); }
      else if (name === 'class') { el.className = value; }
      else if (name === 'text') { el.textContent = value; }
      else if (name === 'value') { el.value = value; }
      else if (name === 'checked') { el.checked = !!value; }
      else if (value === true) { el.setAttribute(name, ''); }
      else { el.setAttribute(name, String(value)); }
    });
    append(el, children);
    return el;
  }
  function append(el, children) {
    if (children === null || children === undefined || children === false) { return el; }
    if (!Array.isArray(children)) { children = [children]; }
    children.forEach(function (child) {
      if (child === null || child === undefined || child === false) { return; }
      if (Array.isArray(child)) { append(el, child); return; }
      el.appendChild(typeof child === 'string' || typeof child === 'number' ? doc.createTextNode(String(child)) : child);
    });
    return el;
  }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } return el; }
  function nextId(prefix) { uid += 1; return (prefix || 'puw') + '-' + uid; }

  // ── glossary tooltips ──────────────────────────────────────────────────
  var tip = null;
  function ensureTip() {
    if (tip) { return tip; }
    tip = h('div', { class: 'tip', role: 'tooltip', hidden: true });
    doc.body.appendChild(tip);
    doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') { hideTip(); } });
    doc.addEventListener('click', function (e) {
      if (!e.target.closest || !e.target.closest('.term')) { hideTip(); }
    });
    return tip;
  }
  function showTip(button, text) {
    var t = ensureTip();
    t.textContent = text;
    t.hidden = false;
    var r = button.getBoundingClientRect();
    var top = r.bottom + root.scrollY + 6;
    var left = Math.max(8, Math.min(r.left + root.scrollX, root.scrollX + doc.documentElement.clientWidth - 316));
    t.style.top = top + 'px';
    t.style.left = left + 'px';
  }
  function hideTip() { if (tip) { tip.hidden = true; } }
  /** A jargon word with its glossary definition on hover, focus or tap (and for screen readers). */
  function term(word, label, terms) {
    var text = terms && terms[word];
    if (!text) { return doc.createTextNode(label || word); }
    var descId = nextId('term');
    var button = h('button', { type: 'button', class: 'term', 'aria-describedby': descId }, label || word);
    var desc = h('span', { id: descId, class: 'sr-only' }, text);
    button.addEventListener('mouseenter', function () { showTip(button, text); });
    button.addEventListener('mouseleave', hideTip);
    button.addEventListener('focus', function () { showTip(button, text); });
    button.addEventListener('blur', hideTip);
    button.addEventListener('click', function (e) { e.preventDefault(); showTip(button, text); });
    return h('span', {}, [button, desc]);
  }

  // ── states ─────────────────────────────────────────────────────────────
  function loading(text) {
    return h('div', { class: 'loading', role: 'status' }, [h('span', { class: 'spinner', 'aria-hidden': 'true' }), text || 'Loading…']);
  }
  /** A refusal envelope as an error notice with the fix up front. */
  function errorNotice(env, extra) {
    var err = (env && env.error) || {};
    return h('div', { class: 'notice error', role: 'alert' }, [
      h('h3', {}, err.message ? sentence(err.message) : 'Something went wrong.'),
      err.fix ? h('p', { class: 'fix' }, ['How to fix it: ', err.fix]) : null,
      extra || null,
      err.code ? h('div', { class: 'code' }, 'Code ' + err.code) : null
    ]);
  }
  function sentence(text) { text = String(text); return text.charAt(0).toUpperCase() + text.slice(1) + (/[.!?]$/.test(text) ? '' : '.'); }
  function empty(title, body, actions) {
    return h('div', { class: 'empty-state' }, [h('h2', {}, title), body ? h('p', {}, body) : null,
      actions ? h('div', { class: 'actions', style: 'justify-content:center' }, actions) : null]);
  }
  function statusBadge(status) {
    var s = String(status || 'UNKNOWN');
    var cls = /^(PASS|converged)$/i.test(s) ? 'pass' : /^(BLOCKED|FAIL|infeasible)$/i.test(s) ? 'blocked' : 'other';
    return h('span', { class: 'status ' + cls }, s);
  }

  // ── the app ────────────────────────────────────────────────────────────
  function App(opts) {
    this.config = opts.config;
    this.bridge = opts.bridge;
    this.screens = opts.screens;
    this.terms = opts.config.terms || {};
    this.root = opts.root;
    this.mode = opts.bridge.mode;
    this.dealId = null;
    this.deal = null;            // the get_deal envelope
    this.cache = {};             // dealId -> {ingest, run, price, review, edit, describe}
    this.current = opts.screen;  // the screen shown
    this.opener = null;          // the tool that opened this MCP App
    this.onDealsChanged = opts.onDealsChanged || function () {};
    this.editor = null;
    try { this.editor = root.sessionStorage.getItem('puw-editor'); } catch (e) { this.editor = null; }
    this.live = h('div', { class: 'sr-only', 'aria-live': 'polite', role: 'status' });
    this.frame();
  }

  App.prototype.t = function (word, label) { return term(word, label, this.terms); };
  App.prototype.announce = function (text) {
    var live = this.live;
    live.textContent = '';
    setTimeout(function () { live.textContent = text; }, 30);
  };
  App.prototype.dealCache = function () {
    if (!this.dealId) { return {}; }
    if (!this.cache[this.dealId]) { this.cache[this.dealId] = {}; }
    return this.cache[this.dealId];
  };
  App.prototype.rememberEditor = function (name) {
    this.editor = name;
    try { root.sessionStorage.setItem('puw-editor', name); } catch (e) { /* sandboxed: memory only */ }
  };

  /** Build the persistent chrome: top bar, step header, start-here and export panels, the body. */
  App.prototype.frame = function () {
    var self = this;
    clear(this.root);
    this.titleEl = h('h1', {}, '');
    this.dealEl = h('div', { class: 'deal' }, '');
    this.stepsEl = h('ol', { class: 'steps', 'aria-label': 'Underwriting steps' });
    this.captionEl = h('p', { class: 'step-caption', 'aria-hidden': 'true' });
    this.startPanel = h('section', { class: 'panel', hidden: true, 'aria-label': 'Start here' });
    this.exportPanel = h('section', { class: 'panel', hidden: true, 'aria-label': 'Export files', id: 'export' });
    this.hintEl = h('div', {});
    this.body = h('div', { class: 'body' });
    var startBtn = h('button', { type: 'button', class: 'btn link', 'aria-expanded': 'false',
      onclick: function () { self.toggleStart(startBtn); } }, 'New? Start here');
    var exportBtn = h('button', { type: 'button', class: 'btn link', 'aria-expanded': 'false',
      onclick: function () { self.toggleExport(exportBtn); } }, 'Export files');
    this.startBtn = startBtn;
    this.exportBtn = exportBtn;
    this.fullBtn = h('button', { type: 'button', class: 'btn link', hidden: true,
      onclick: function () { self.bridge.requestDisplayMode('fullscreen'); } }, 'Full screen');
    append(this.root, [
      this.live,
      h('div', { class: 'topbar' }, [h('div', { class: 'title' }, [this.titleEl, this.dealEl]),
        h('div', { class: 'links' }, [startBtn, exportBtn, this.fullBtn])]),
      h('nav', { 'aria-label': 'Steps' }, [this.stepsEl, this.captionEl]),
      this.startPanel, this.exportPanel, this.hintEl, this.body
    ]);
    this.renderSteps();
  };

  App.prototype.setTitle = function (title) {
    this.titleEl.textContent = title;
    var name = this.deal && this.deal.ok ? this.deal.name : (this.dealName || '');
    this.dealEl.textContent = this.dealId ? (name ? name + ' · ' : '') + this.dealId : 'No deal selected';
  };

  App.prototype.renderSteps = function () {
    var self = this;
    var model = core.stepStates(this.deal && this.deal.ok ? this.deal : null);
    var screenOf = { intake: ['documents', 'describe'], assumptions: ['check', 'assumptions'], results: ['results'],
      pricing: ['price'], export: ['export'] };
    var here = screenOf[this.current] || [];
    clear(this.stepsEl);
    var caption = [];
    core.STEPS.forEach(function (step, index) {
      var state = self.deal && self.deal.ok ? model.states[step.id] : 'unknown';
      var current = here.indexOf(step.id) !== -1 && (here.length === 1 || step.id === (self.focusStep || here[0]));
      var words = { done: 'done', stale: 'out of date', blocked: 'blocked', todo: 'to do', unknown: '' }[state] || '';
      var button = h('button', {
        type: 'button', class: 'step', 'data-state': state, 'aria-current': current ? 'step' : null,
        'aria-label': 'Step ' + (index + 1) + ': ' + step.label + (words ? ', ' + words : '') +
          (model.next === step.id ? ', next' : ''),
        onclick: function () { self.go(step.screen, step.anchor, step.id); }
      }, [h('span', { class: 'n', 'aria-hidden': 'true' }, state === 'done' ? '✓' : String(index + 1)),
        h('span', { class: 'l' }, step.label)]);
      self.stepsEl.appendChild(h('li', {}, button));
      if (current) { caption.push('Step ' + (index + 1) + ' of ' + core.STEPS.length + ': ' + step.label + (words ? ' (' + words + ')' : '')); }
    });
    this.captionEl.textContent = caption.join(' · ');
  };

  /** Show a step. Same screen: scroll to it. Other screen: switch (browser) or ask the assistant (MCP). */
  App.prototype.go = function (screen, anchor, stepId) {
    var self = this;
    this.focusStep = stepId || null;
    if (screen === 'export') {
      this.toggleExport(this.exportBtn, true);
      return;
    }
    if (screen === this.current || this.mode === 'browser') {
      if (screen !== this.current) { this.show(screen); }
      this.renderSteps();
      var target = doc.getElementById(anchor);
      if (target) { target.scrollIntoView({ block: 'start', behavior: 'smooth' }); if (target.focus) { target.focus({ preventScroll: true }); } }
      return;
    }
    var ask = this.askFor(screen);
    this.renderSteps();
    if (this.bridge.canMessage) {
      this.bridge.sendMessage(ask).then(function (sent) {
        self.hint(sent ? 'Asked the assistant: "' + ask + '"' : null, sent ? null : ask);
      });
    } else {
      this.hint(null, ask);
    }
  };
  /** The sentence to give the assistant to open a screen it alone can open. */
  App.prototype.askFor = function (screen) {
    var id = this.dealId ? " for deal '" + this.dealId + "'" : '';
    switch (screen) {
      case 'intake': return 'Show me the deal intake' + id + ' (get_deal).';
      case 'assumptions': return 'Check the documents and show me the assumptions' + id + ' (ingest_deal, then review_assumptions).';
      case 'results': return 'Run the deal' + id + ' and show me the results (run_deal).';
      case 'pricing': return 'Solve the price' + id + ' (solve_price). Ask me for the target cash-on-cash and the 5-year Treasury yield first.';
      default: return 'Show me the export files' + id + ' (export_deal).';
    }
  };
  /** A hint for hosts that cannot act from the screen: "Ask the assistant to …". */
  App.prototype.hint = function (done, ask) {
    clear(this.hintEl);
    if (!done && !ask) { return; }
    var self = this;
    var body = done ? [h('p', {}, done)] : [
      h('p', {}, [h('strong', {}, 'Ask the assistant to do this step. '), 'This assistant cannot run it from the screen. Say:']),
      h('p', {}, h('q', {}, ask)),
      root.navigator && root.navigator.clipboard ? h('button', { type: 'button', class: 'btn', onclick: function () {
        root.navigator.clipboard.writeText(ask).then(function () { self.announce('Copied.'); }, function () {});
      } }, 'Copy') : null];
    append(this.hintEl, h('div', { class: 'notice', role: 'status' }, body));
  };
  /** The read-only banner (the host shows the screen but cannot run tools from it). */
  App.prototype.readOnlyNote = function (what) {
    if (this.bridge.canCallTools) { return null; }
    var ask = what || this.askFor(this.current);
    return h('div', { class: 'notice' }, [
      h('strong', {}, 'View only. '),
      'This assistant shows the screen but cannot run steps from it. Ask the assistant instead: ', h('q', {}, ask)
    ]);
  };

  App.prototype.toggleStart = function (button, force) {
    var open = force === undefined ? this.startPanel.hidden : force;
    this.startPanel.hidden = !open;
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open) { return; }
    var self = this;
    var cfg = this.config.start || {};
    clear(this.startPanel);
    append(this.startPanel, [
      h('button', { type: 'button', class: 'btn link close', onclick: function () { self.toggleStart(button, false); } }, 'Close'),
      h('h2', {}, 'New? Start here'),
      h('p', {}, cfg.what || ''),
      h('ol', {}, (cfg.steps || []).map(function (s) { return h('li', {}, [h('strong', {}, s.label + ': '), s.text]); })),
      h('p', { class: 'hint' }, cfg.sample || ''),
      this.mode === 'browser' ? h('p', {}, h('a', { href: '#/guide/user-guide' }, 'Read the full user guide')) :
        h('p', { class: 'hint' }, 'The full guide is the guide://user-guide resource; ask the assistant for it.')
    ]);
    this.startPanel.focus && this.startPanel.setAttribute('tabindex', '-1');
    this.startPanel.focus();
  };

  App.prototype.toggleExport = function (button, force) {
    var open = force === undefined ? this.exportPanel.hidden : force;
    this.exportPanel.hidden = !open;
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open) { return; }
    var self = this;
    clear(this.exportPanel);
    var close = h('button', { type: 'button', class: 'btn link close', onclick: function () { self.toggleExport(button, false); } }, 'Close');
    append(this.exportPanel, [close, h('h2', {}, 'Export files')]);
    this.exportPanel.setAttribute('tabindex', '-1');
    this.exportPanel.focus();
    if (!this.dealId) { append(this.exportPanel, h('p', { class: 'hint' }, 'Pick or create a deal first.')); return; }
    if (!this.bridge.canCallTools) { append(this.exportPanel, this.readOnlyNote(this.askFor('export'))); return; }
    var holder = h('div', {}, loading('Listing the files…'));
    append(this.exportPanel, holder);
    this.bridge.callTool('export_deal', { deal_id: this.dealId }).then(function (env) {
      clear(holder);
      if (!env.ok) { append(holder, errorNotice(env)); return; }
      append(holder, [h('p', { class: 'hint' }, env.summary), self.fileList(env.files || [])]);
    });
  };

  var KEY_FILES = ['run/underwriting.xlsx', 'assumptions.html', 'run/assumptions.html', 'price/assumptions.html',
    'price/backsolve_summary.json', 'run/results.json'];
  App.prototype.fileList = function (files) {
    var self = this;
    var ordered = files.slice().sort(function (a, b) {
      var ia = KEY_FILES.indexOf(a.path), ib = KEY_FILES.indexOf(b.path);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || String(a.path).localeCompare(String(b.path));
    });
    return h('ul', { class: 'files' }, ordered.map(function (file) {
      var link = self.bridge.fileLink(self.dealId, file);
      var name;
      if (link && self.mode === 'browser') {
        name = h('a', { href: link, class: 'path', target: file.path.slice(-5) === '.html' ? '_blank' : null,
          rel: 'noopener', download: file.path.slice(-5) === '.html' ? null : file.path.split('/').pop() }, file.path);
      } else if (link && self.bridge.canOpenLinks) {
        name = h('button', { type: 'button', class: 'btn link path', onclick: function () { self.bridge.openLink(link); } }, file.path);
      } else {
        name = h('span', { class: 'path' }, file.path);
      }
      return h('li', {}, [
        h('div', { class: 'what' }, [name, h('div', { class: 'small muted' }, file.description || ''),
          file.local_path ? h('div', { class: 'path small muted' }, file.local_path) : null]),
        h('div', { class: 'small muted num' }, core.bytes(file.bytes))
      ]);
    }));
  };

  /** Load the deal's status (get_deal) when the host lets the screen call tools. */
  App.prototype.refreshDeal = function () {
    var self = this;
    if (!this.dealId || !this.bridge.canCallTools) { return Promise.resolve(this.deal); }
    return this.bridge.callTool('get_deal', { deal_id: this.dealId }).then(function (env) {
      self.deal = env;
      if (env.ok) { self.dealName = env.name; }
      self.renderSteps();
      self.setTitle(self.screens[self.current] ? self.screens[self.current].title : '');
      return env;
    });
  };

  /** Remember a tool's result for this deal (screens read the last ingest/run/price from here). */
  App.prototype.record = function (tool, env) {
    if (!env) { return; }
    if (env.deal_id && env.deal_id !== this.dealId) { this.selectDeal(env.deal_id, true); }
    var cache = this.dealCache();
    var slot = { ingest_deal: 'ingest', review_assumptions: 'review', edit_assumptions: 'edit', run_deal: 'run',
      solve_price: 'price', describe_deal: 'describe', get_deal: 'deal', create_deal: 'created',
      load_sample_deal: 'created' }[tool];
    if (slot === 'deal') { this.deal = env; if (env.ok) { this.dealName = env.name; } }
    else if (slot) { cache[slot] = env; }
    if (tool === 'ingest_deal' || tool === 'edit_assumptions') { delete cache.run; delete cache.price; delete cache.review; }
    if (tool === 'describe_deal' || tool === 'add_document') { delete cache.ingest; delete cache.review; }
  };

  App.prototype.selectDeal = function (dealId, keep) {
    if (dealId === this.dealId) { return; }
    this.dealId = dealId || null;
    this.deal = null;
    this.dealName = '';
    if (!keep) { this.hint(null, null); }
  };

  /** Run a write tool from a button: busy state, the result announced, the deal refreshed. */
  App.prototype.act = function (button, tool, args, busyText) {
    var self = this;
    var label = button ? button.textContent : '';
    if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); button.textContent = busyText || 'Working…'; }
    return this.bridge.callTool(tool, args).then(function (env) {
      if (button) { button.disabled = false; button.removeAttribute('aria-busy'); button.textContent = label; }
      self.record(tool, env);
      self.announce(env.ok ? env.summary : 'Error: ' + ((env.error && env.error.message) || env.summary));
      if (env.ok && self.mode === 'mcp') {
        self.bridge.updateContext('In the ' + self.current + ' screen the analyst ran ' + tool + ': ' + env.summary);
      }
      if (env.ok && tool !== 'get_deal') { self.onDealsChanged(); }
      return (env.ok ? self.refreshDeal() : Promise.resolve()).then(function () { return env; });
    });
  };

  /** Render a screen into the body. */
  App.prototype.show = function (name) {
    var screen = this.screens[name];
    if (!screen) { return; }
    this.current = name;
    this.setTitle(screen.title);
    this.renderSteps();
    clear(this.body);
    if (this.config.mode !== 'browser') {
      this.fullBtn.hidden = (this.bridge.displayModes || []).indexOf('fullscreen') === -1;
    }
    screen.render(this, this.body);
    if (this.onShow) { this.onShow(name); }
  };

  /** Apply the host theme (light/dark) and font variables when the host sends them. */
  function applyHostContext(context) {
    var html = doc.documentElement;
    if (context && (context.theme === 'light' || context.theme === 'dark')) { html.setAttribute('data-theme', context.theme); }
    var vars = context && context.styles && context.styles.variables;
    if (vars) {
      ['--font-sans', '--font-mono'].forEach(function (name) {
        if (typeof vars[name] === 'string' && vars[name].length < 300) { html.style.setProperty(name, vars[name]); }
      });
    }
  }

  root.PUW = {
    h: h, append: append, clear: clear, nextId: nextId, term: term, loading: loading, errorNotice: errorNotice,
    empty: empty, statusBadge: statusBadge, sentence: sentence, App: App, applyHostContext: applyHostContext,
    screens: {}
  };
})(window);
