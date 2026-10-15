/* Screen 4: pricing (solve_price). The target cash-on-cash and the 5-year Treasury benchmark (never
   invented: the analyst supplies the rate, its date and its source), the solve, the pricing panel
   (status, price, $/unit, loan and its sizing basis, caps, IRRs, floors enforced) and the asking
   price against the solved price. */
(function (root) {
  'use strict';
  var PUW = root.PUW, core = root.PUWCore, h = PUW.h, append = PUW.append, clear = PUW.clear;

  function render(app, body) {
    if (!app.dealId) { append(body, PUW.empty('No deal yet', 'Create a deal or try the sample first.')); return; }
    var env = app.dealCache().price;
    var deal = app.deal && app.deal.ok ? app.deal : null;
    var out = h('div', { 'aria-live': 'polite' });
    if (env && !env.ok) { append(body, PUW.errorNotice(env)); }
    append(body, [form(app, deal, env, out), out]);
    if (env && env.ok) { append(out, panel(app, deal, env)); }
    else if (deal && deal.last_price) { append(out, lastPrice(app, deal)); }
  }

  function form(app, deal, env, out) {
    var defaults = (deal && deal.pricing_defaults) || {};
    var given = (env && env.pricing) || {};
    var target = h('input', { type: 'text', id: 'p-target', class: 'num', inputmode: 'decimal', autocomplete: 'off',
      value: core.trimZeros(((given.target_coc || Number(defaults.target_coc) || 0.08) * 100).toFixed(4)),
      'aria-describedby': 'p-target-why p-target-err' });
    var bench = h('input', { type: 'text', id: 'p-bench', class: 'num', inputmode: 'decimal', autocomplete: 'off',
      placeholder: defaults.benchmark_5yr_treasury ? 'sample: ' + core.trimZeros((Number(defaults.benchmark_5yr_treasury) * 100).toFixed(4)) : 'e.g. 4.05',
      'aria-describedby': 'p-bench-why p-bench-err' });
    var asOf = h('input', { type: 'date', id: 'p-asof', 'aria-describedby': 'p-asof-err' });
    var source = h('input', { type: 'text', id: 'p-source', maxlength: '200', autocomplete: 'off', placeholder: 'e.g. the Treasury daily yield curve' });
    var errs = { target: h('div', { class: 'err', id: 'p-target-err' }), bench: h('div', { class: 'err', id: 'p-bench-err' }),
      asof: h('div', { class: 'err', id: 'p-asof-err' }) };
    var solve = h('button', { type: 'submit', class: 'btn primary' }, 'Solve the price');
    var rate = { name: 'rate', type: 'rate', minimum: 0, maximum: 0.25 };
    var sample = !!defaults.benchmark_5yr_treasury;
    var f = h('form', { class: 'section', id: 'pricing', tabindex: '-1', 'aria-labelledby': 'price-h', novalidate: true, onsubmit: function (e) {
      e.preventDefault();
      var t = core.parseField({ name: 'target', type: 'rate', minimum: 0.0001, maximum: 0.5 }, target.value);
      var b = core.parseField(rate, bench.value);
      var d = core.parseField({ name: 'as_of', type: 'date' }, asOf.value);
      errs.target.textContent = t.ok ? (t.value === null ? 'Enter the target cash-on-cash.' : '') : t.error;
      errs.bench.textContent = b.ok ? (b.value === null && !sample ? 'Enter today’s 5-year Treasury yield.' : '') : b.error;
      errs.asof.textContent = d.ok ? '' : d.error;
      [[target, errs.target], [bench, errs.bench], [asOf, errs.asof]].forEach(function (pair) {
        if (pair[1].textContent) { pair[0].setAttribute('aria-invalid', 'true'); } else { pair[0].removeAttribute('aria-invalid'); }
      });
      var bad = [errs.target, errs.bench, errs.asof].filter(function (x) { return x.textContent; });
      if (bad.length) { (errs.target.textContent ? target : errs.bench.textContent ? bench : asOf).focus(); return; }
      var args = { deal_id: app.dealId, target_coc: String(t.value) };
      if (b.value !== null) {
        args.benchmark_5yr_treasury = String(b.value);
        if (d.value) { args.benchmark_as_of = d.value; }
        if (source.value.trim()) { args.benchmark_source = source.value.trim(); }
      }
      clear(out);
      append(out, PUW.loading('Searching for the price… this takes a few seconds.'));
      app.act(solve, 'solve_price', args, 'Solving…').then(function (res) {
        clear(out);
        append(out, res.ok ? panel(app, app.deal && app.deal.ok ? app.deal : deal, res) : PUW.errorNotice(res));
        var heading = root.document.getElementById('solved-h');
        if (heading) { heading.focus(); }
      });
    } }, [
      h('h2', { id: 'price-h' }, 'Solve the price'),
      h('p', { class: 'lead' }, ['The highest price whose cash-on-cash still meets your target, with the ', app.t('House floor', 'house floors'),
        ' enforced. Stabilized deals price on year 1; value-add on the first stabilized year.']),
      h('div', { class: 'fields' }, [
        h('div', { class: 'field' }, [h('label', { for: 'p-target' }, [app.t('Target CoC', 'Target cash-on-cash')]),
          h('div', { class: 'inputwrap' }, [target, h('span', { class: 'affix post', 'aria-hidden': 'true' }, '%')]),
          h('p', { class: 'why', id: 'p-target-why' }, 'House default 8%.'), errs.target]),
        h('div', { class: 'field' }, [h('label', { for: 'p-bench' }, ['5-year Treasury yield (', app.t('Benchmark', 'benchmark'), ')']),
          h('div', { class: 'inputwrap' }, [bench, h('span', { class: 'affix post', 'aria-hidden': 'true' }, '%')]),
          h('p', { class: 'why', id: 'p-bench-why' }, sample ? 'Blank uses the sample’s illustrative ' +
            core.pct(defaults.benchmark_5yr_treasury) + ' (' + (defaults.benchmark_as_of || '') + '), not a market quote.' :
            'The loan is priced at this plus the agency spread.'), errs.bench]),
        h('div', { class: 'field' }, [h('label', { for: 'p-asof' }, 'Yield as of'), asOf, h('p', { class: 'why' }, 'Default: today.'), errs.asof]),
        h('div', { class: 'field' }, [h('label', { for: 'p-source' }, 'Where the yield came from'), source])
      ]),
      h('div', { class: 'notice' }, [h('strong', {}, 'We never invent a benchmark. '),
        'The price search refuses to run until you give today’s 5-year Treasury yield, its date and its source; no rate is assumed for you.']),
      app.bridge.canCallTools ? h('div', { class: 'actions' }, [solve]) : app.readOnlyNote()
    ]);
    return f;
  }

  function lastPrice(app, deal) {
    var p = deal.last_price;
    return h('section', { class: 'section', 'aria-labelledby': 'solved-h' }, [
      h('h2', { id: 'solved-h', tabindex: '-1' }, ['Last solved price', PUW.statusBadge(p.status)]),
      h('p', {}, ['Solved ', core.dateTime(p.at), ': ', core.usd(p.price), ' (', core.usd(p.price_per_unit), ' per unit) at a ',
        core.pct(p.target_coc), ' target; benchmark ', p.benchmark ? core.pct(p.benchmark.rate) + ' as of ' + p.benchmark.as_of : '—', '.']),
      h('p', { class: 'hint' }, 'Solve again to see the full pricing panel.')
    ]);
  }

  function k(label, value, detail, text) {
    return h('div', { class: 'tile' }, [h('dt', {}, label), h('dd', {}, [h('span', { class: 'v' + (text ? ' t' : '') }, value), detail ? h('span', { class: 'd' }, detail) : null])]);
  }

  function panel(app, deal, env) {
    var p = env.pricing || {};
    var loan = p.loan || {};
    var dscr = p.minimum_dscr || {};
    var sizing = { ncf_after_capex_charge: 'NCF after the capex charge', ncf_after_lender_reserve: 'NCF after the lender reserve',
      noi_before_capex: 'NOI before capex' }[loan.sizing_basis] || loan.sizing_basis;
    var ltv = p.price && loan.amount ? loan.amount / p.price : null;
    var bench = p.benchmark || {};
    return h('section', { class: 'section', 'aria-labelledby': 'solved-h' }, [
      h('h2', { id: 'solved-h', tabindex: '-1' }, ['Solved price', PUW.statusBadge(p.status)]),
      h('p', { class: 'lead' }, env.summary),
      (env.prefilled_from_deal || []).length ? h('div', { class: 'notice warn' }, 'Used the deal’s recorded inputs for ' +
        env.prefilled_from_deal.join(', ') + ' (the sample’s illustrative values, not market quotes).') : null,
      p.price === null || p.price === undefined ? h('div', { class: 'notice error' }, [h('h3', {}, 'No price meets the target'),
        h('p', {}, 'Even the lowest price searched misses the target. Look at the floors enforced, the capex charge and the target.')]) : null,
      h('dl', { class: 'tiles' }, [
        k('Price', core.usd(p.price), p.price_per_unit ? core.usd(p.price_per_unit) + ' per unit (' + p.units + ' units)' : null),
        k([app.t('CoC', 'Cash-on-cash'), ' achieved'], core.pct(p.achieved_coc), 'Target ' + core.pct(p.target_coc) + ' on ' + String(p.coc_basis || '').replace(/_/g, ' ')),
        k('Loan', core.usd(loan.amount), [core.pct(loan.rate), ' rate', ltv ? ', ' + core.pct(ltv, 1) + ' of price' : '', loan.io_months ? ', ' + loan.io_months + ' mo IO' : ''].join('')),
        k(['Loan sized on'], sizing || '—', loan.sizing_method ? String(loan.sizing_method).replace(/_/g, ' ') : null, true),
        k('Equity', core.usd(p.equity), 'Down payment, costs, fees and immediate capex'),
        k([app.t('Cap rate', 'Going-in cap')], core.pct(p.going_in_cap_rate)),
        k([app.t('Exit cap', 'Exit cap')], core.pct(p.exit_cap_rate), p.exit_cap_mode ? String(p.exit_cap_mode).replace(/_/g, ' ') : null),
        k(['Levered ', app.t('IRR', 'IRR')], core.pct(p.levered_irr), 'Unlevered ' + core.pct(p.unlevered_irr)),
        k(['Minimum ', app.t('DSCR', 'DSCR')], core.times(dscr.minimum), 'On NOI: ' + core.times(dscr.minimum_noi_before_capex))
      ]),
      h('p', { class: 'small muted' }, ['Benchmark ', core.pct(bench.rate), ' as of ', bench.as_of || '—', ' (', bench.source || 'no source given', ').']),
      floors(app, p.floors_enforced || []),
      compare(app, deal, p),
      p.exit_cap_grid && p.exit_cap_grid.points ? [h('h3', {}, 'Exit-cap sensitivity at the solved price'),
        PUW.exitGrid.table(p.exit_cap_grid.points)] : null
    ]);
  }

  function floors(app, keys) {
    var enforcedNote = h('p', { class: 'hint' }, 'Pricing raises any assumption below its house floor to the floor; underwriting keeps your value.');
    if (!keys.length) { return [h('h3', {}, 'Floors enforced'), h('p', {}, 'None: every assumption was at or above its floor.'), enforcedNote]; }
    return [h('h3', {}, ['Floors enforced (', String(keys.length), ')']), h('ul', {}, keys.map(function (key) {
      return h('li', {}, h('button', { type: 'button', class: 'btn link', onclick: function () { openRow(app, key); } }, key));
    })), enforcedNote];
  }

  function openRow(app, key) {
    if (app.mode === 'browser') {
      app.focusChip = 'enforced';
      app.show('assumptions');
      setTimeout(function () { var row = root.document.getElementById('row-' + key); if (row) { row.scrollIntoView({ block: 'center' }); row.focus(); } }, 900);
      return;
    }
    var ask = "Show me the assumption " + key + " at the solved price for deal '" + app.dealId + "' (review_assumptions with filter='enforced', case='priced').";
    if (app.bridge.canMessage) { app.bridge.sendMessage(ask).then(function (sent) { app.hint(sent ? 'Asked the assistant: "' + ask + '"' : null, sent ? null : ask); }); }
    else { app.hint(null, ask); }
  }

  function compare(app, deal, p) {
    var ask = deal && deal.profile ? deal.profile.purchase_price : null;
    var run = deal ? deal.last_run : null;
    if (!core.isNum(ask) || !core.isNum(p.price)) { return null; }
    var diff = p.price - ask;
    var units = p.units;
    function row(label, a, b, fmt, delta) {
      return h('tr', {}, [h('th', { scope: 'row' }, label), h('td', { class: 'num' }, fmt(a)), h('td', { class: 'num' }, fmt(b)), h('td', { class: 'num' }, delta || '')]);
    }
    return [h('h3', {}, 'Asking price vs solved price'), h('div', { class: 'tablewrap' }, h('table', { class: 'data' }, [
      h('caption', {}, 'The asking price is the purchase price in the deal facts; its returns are from the last run.'),
      h('thead', {}, h('tr', {}, [h('th', { scope: 'col' }, ''), h('th', { scope: 'col', class: 'num' }, 'Asking'),
        h('th', { scope: 'col', class: 'num' }, 'Solved'), h('th', { scope: 'col', class: 'num' }, 'Difference')])),
      h('tbody', {}, [
        row('Price', ask, p.price, core.usd, (diff >= 0 ? '+' : '') + core.usd(diff) + ' (' + (diff >= 0 ? '+' : '') + core.pct(diff / ask, 1) + ')'),
        units ? row('Per unit', ask / units, p.price_per_unit, core.usd, core.usd(p.price_per_unit - ask / units)) : null,
        run ? row('Cash-on-cash, year 1', run.cash_on_cash_year_1, p.cash_on_cash_year_1, core.pct) : null,
        run ? row('Levered IRR', run.levered_irr, p.levered_irr, core.pct) : null
      ])
    ]))];
  }

  PUW.screens.pricing = { title: 'Pricing', render: render };
})(window);
