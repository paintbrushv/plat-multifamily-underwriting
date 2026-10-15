/* The browser app (plat-uw serve): a deal list on the left, the four screens on the right, and the
   user guide and glossary as pages. Routes live in the URL hash: #/deal/<id>/<screen>, #/new,
   #/guide/<name>[/<anchor>]. */
(function (root) {
  'use strict';
  var PUW = root.PUW, core = root.PUWCore, h = PUW.h, append = PUW.append, clear = PUW.clear;
  var doc = root.document;
  var config = JSON.parse(doc.getElementById('puw-config').textContent);
  var bridge = root.PUWBridge.create(config);
  var listEl = doc.getElementById('deal-list');
  var main = doc.getElementById('main');
  var appRoot = doc.getElementById('app');
  var guideRoot = doc.getElementById('guide');
  var SCREENS = ['intake', 'assumptions', 'results', 'pricing'];
  var silent = false;

  var app = new PUW.App({ root: appRoot, bridge: bridge, config: config, screens: PUW.screens, screen: 'intake',
    onDealsChanged: function () { loadDeals(); } });
  app.onShow = function (name) {
    var target = app.dealId ? '#/deal/' + encodeURIComponent(app.dealId) + '/' + name : '#/new';
    if (root.location.hash !== target) { silent = true; root.location.hash = target; }
    markCurrent();
  };

  function loadDeals() {
    return bridge.callTool('list_deals', {}).then(function (env) {
      clear(listEl);
      if (!env.ok) { append(listEl, h('li', {}, PUW.errorNotice(env))); return env; }
      if (!env.deals.length) { append(listEl, h('li', { class: 'small muted' }, 'No deals yet.')); return env; }
      env.deals.forEach(function (d) {
        append(listEl, h('li', {}, h('button', { type: 'button', class: 'deal-link', 'data-deal': d.deal_id,
          onclick: function () { root.location.hash = '#/deal/' + encodeURIComponent(d.deal_id) + '/' + screenFor(d); } }, [
          h('span', { class: 'n' }, d.name || d.deal_id),
          h('span', { class: 'm' }, [String(d.deal_type || '').replace('_', '-'), ' · ', String((d.steps_done || []).length), ' of 8 steps',
            core.isNum(d.levered_irr) ? ' · IRR ' + core.pct(d.levered_irr) : ''])
        ])));
      });
      markCurrent();
      return env;
    });
  }
  function screenFor(d) {
    var done = d.steps_done || [];
    if (done.indexOf('price') !== -1) { return 'pricing'; }
    if (done.indexOf('run') !== -1) { return 'results'; }
    if (done.indexOf('ingest') !== -1) { return 'assumptions'; }
    return 'intake';
  }
  function markCurrent() {
    Array.prototype.forEach.call(listEl.querySelectorAll('.deal-link'), function (b) {
      if (b.getAttribute('data-deal') === app.dealId && !guideRoot.childNodes.length) { b.setAttribute('aria-current', 'page'); }
      else { b.removeAttribute('aria-current'); }
    });
  }

  function showApp() { guideRoot.hidden = true; clear(guideRoot); appRoot.hidden = false; }

  function home() {
    showApp();
    app.selectDeal(null);
    app.current = 'intake';
    app.setTitle('Welcome');
    app.renderSteps();
    clear(app.body);
    var start = config.start || {};
    append(app.body, h('section', { class: 'section' }, [
      h('h2', {}, 'Underwrite a multifamily deal'),
      h('p', {}, start.what || ''),
      h('ol', {}, (start.steps || []).map(function (s) { return h('li', {}, [h('strong', {}, s.label + ': '), s.text]); })),
      h('div', { class: 'actions' }, [
        h('button', { type: 'button', class: 'btn primary', onclick: function (e) { trySample(e.target); } }, 'Try the sample deal'),
        h('a', { class: 'btn', href: '#/new' }, 'New deal'),
        h('a', { class: 'btn link', href: '#/guide/user-guide' }, 'Read the user guide')
      ]),
      h('p', { class: 'hint' }, start.sample || '')
    ]));
  }

  function trySample(button) {
    app.act(button, 'load_sample_deal', {}, 'Loading the sample…').then(function (env) {
      if (!env.ok) { clear(app.body); append(app.body, PUW.errorNotice(env)); return; }
      root.location.hash = '#/deal/' + encodeURIComponent(env.deal_id) + '/intake';
    });
  }

  function guide(name, anchor) {
    appRoot.hidden = true;
    guideRoot.hidden = false;
    markCurrent();
    clear(guideRoot);
    append(guideRoot, PUW.loading('Loading the guide…'));
    fetch('api/guide/' + encodeURIComponent(name), { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (env) {
      clear(guideRoot);
      if (!env.ok) { append(guideRoot, PUW.errorNotice(env)); return; }
      var article = h('article', { class: 'guide', 'aria-label': env.title });
      article.innerHTML = env.html; // rendered and escaped on the server (engine/ui/markdown.py)
      append(guideRoot, article);
      var target = anchor ? doc.getElementById(anchor) : null;
      if (target) { target.scrollIntoView(); } else { main.scrollTop = 0; root.scrollTo(0, 0); }
    }, function () {
      clear(guideRoot);
      append(guideRoot, PUW.errorNotice({ error: { code: 'OFFLINE', message: 'The guide could not be loaded', fix: 'Check that plat-uw serve is running.' } }));
    });
  }
  guideRoot.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href^="#"]');
    if (!a || a.getAttribute('href').indexOf('#/') === 0) { return; }
    var target = doc.getElementById(a.getAttribute('href').slice(1));
    if (target) { e.preventDefault(); target.scrollIntoView({ behavior: 'smooth' }); }
  });

  function route() {
    if (silent) { silent = false; return; }
    var hash = root.location.hash || '#/';
    var parts = hash.replace(/^#\/?/, '').split('/').map(function (p) { try { return decodeURIComponent(p); } catch (e) { return p; } });
    if (parts[0] === 'guide') { guide(parts[1] || 'user-guide', parts[2]); return; }
    showApp();
    if (parts[0] === 'new') { app.selectDeal(null); app.show('intake'); return; }
    if (parts[0] === 'deal' && parts[1]) {
      var screen = SCREENS.indexOf(parts[2]) !== -1 ? parts[2] : 'intake';
      if (parts[1] !== app.dealId || !app.deal) {
        app.selectDeal(parts[1]);
        app.setTitle(PUW.screens[screen].title);
        clear(app.body);
        append(app.body, PUW.loading('Loading the deal…'));
        app.refreshDeal().then(function (env) {
          if (!env || !env.ok) { clear(app.body); append(app.body, PUW.errorNotice(env || {})); return; }
          app.show(screen);
        });
        markCurrent();
        return;
      }
      app.show(screen);
      return;
    }
    home();
  }

  doc.getElementById('btn-sample').addEventListener('click', function (e) { trySample(e.currentTarget); });
  doc.getElementById('btn-new').addEventListener('click', function () { root.location.hash = '#/new'; });
  root.addEventListener('hashchange', route);
  PUW.applyHostContext({});
  loadDeals().then(route);
})(window);
