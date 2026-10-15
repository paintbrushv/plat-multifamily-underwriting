/* Screen 3: results (run_deal). Headline tiles, the capital plan by year (three buckets, which one
   binds), the annual cash flow, the exit on forward NOI and the exit-cap sensitivity grid, with two
   inline SVG charts (NOI and cash flow by year; IRR against the exit cap). */
(function (root) {
  'use strict';
  var PUW = root.PUW, core = root.PUWCore, charts = root.PUWCharts, h = PUW.h, append = PUW.append, clear = PUW.clear;

  function render(app, body) {
    if (!app.dealId) { append(body, PUW.empty('No deal yet', 'Create a deal or try the sample first.')); return; }
    var env = app.dealCache().run;
    var last = app.deal && app.deal.ok ? app.deal.last_run : null;
    var stale = isStale(app);
    var runBtn = h('button', { type: 'button', class: 'btn primary', onclick: function () {
      app.act(runBtn, 'run_deal', { deal_id: app.dealId }, 'Running…').then(function () { app.show('results'); });
    } }, env && env.ok ? 'Run again' : last ? 'Run again to see the full results' : 'Run the deal');
    var actions = app.bridge.canCallTools ? h('div', { class: 'actions' }, [runBtn,
      h('span', { class: 'hint' }, 'Validates the deal and runs the monthly cash flow. Same inputs, same numbers.')]) : app.readOnlyNote();
    if (!env || (!env.ok && !env.error)) {
      append(body, h('section', { class: 'section', id: 'headline', tabindex: '-1', 'aria-labelledby': 'res-h' }, [
        h('h2', { id: 'res-h' }, 'Results'),
        last ? [h('p', {}, ['Last run ', core.dateTime(last.at), stale ? ' (out of date: the assumptions changed since)' : '', '.']),
          tiles(app, { noi_year_1: last.noi_year_1, cash_on_cash_year_1: last.cash_on_cash_year_1, levered_irr: last.levered_irr,
            unlevered_irr: last.unlevered_irr, levered_equity_multiple: last.levered_equity_multiple,
            dscr: { minimum: last.minimum_dscr }, exit: { cap_rate: last.exit_cap_rate } })] :
          h('p', {}, 'The deal has not been run yet. Check the documents and review the assumptions first.'),
        actions
      ]));
      return;
    }
    if (!env.ok) { append(body, [PUW.errorNotice(env, missingList(env)), actions]); return; }
    var m = env.metrics || {};
    var annual = env.annual_cash_flow || [];
    append(body, [
      stale ? h('div', { class: 'notice warn' }, 'The assumptions changed after this run. Run again to bring the results up to date.') : null,
      h('section', { class: 'section', id: 'headline', tabindex: '-1', 'aria-labelledby': 'res-h' }, [
        h('h2', { id: 'res-h' }, ['Headline returns', env.validation ? PUW.statusBadge(env.validation.status) : null]),
        h('p', { class: 'lead', 'aria-live': 'polite' }, env.summary),
        tiles(app, m), actions
      ]),
      annual.length ? h('section', { class: 'section', 'aria-labelledby': 'cf-h' }, [
        h('h2', { id: 'cf-h' }, 'Cash flow by year'),
        charts.bars({
          title: 'NOI and cash flow after debt by analysis year',
          desc: annual.map(function (r) { return 'Year ' + r.analysis_year + ': NOI ' + core.usd(r.net_operating_income) + ', cash flow after debt ' + core.usd(r.cash_flow_after_debt); }).join('; '),
          labels: annual.map(function (r) { return 'Yr ' + r.analysis_year; }),
          series: [{ name: 'NOI', values: annual.map(function (r) { return r.net_operating_income; }), cls: 's1' },
            { name: 'Cash flow after debt', values: annual.map(function (r) { return r.cash_flow_after_debt; }), cls: 's2' }],
          format: core.usdShort
        }),
        h('div', { class: 'legend' }, [h('span', {}, [h('span', { class: 'sw', style: 'background:var(--chart-1)' }), 'NOI']),
          h('span', {}, [h('span', { class: 'sw', style: 'background:var(--chart-2)' }), 'Cash flow after debt'])]),
        annualTable(app, annual)
      ]) : null,
      capitalByYear(app, env.capital_plan),
      exitSection(app, m.exit || {}, env.exit_cap_grid)
    ]);
  }

  function isStale(app) {
    var steps = (app.deal && app.deal.ok && app.deal.steps) || [];
    return steps.some(function (s) { return s.step === 'run' && s.stale; });
  }

  function missingList(env) {
    var missing = env.missing_to_run || [];
    var errors = (env.validation && env.validation.errors) || [];
    if (!missing.length && !errors.length) { return null; }
    return h('ul', {}, missing.map(function (n) { return h('li', {}, 'Missing: ' + n); }).concat(
      errors.map(function (e) { return h('li', {}, [e.message, ' — ', e.fix]); })));
  }

  function tile(label, value, detail) {
    return h('div', { class: 'tile' }, [h('dt', {}, label), h('dd', {}, [h('span', { class: 'v' }, value), detail ? h('span', { class: 'd' }, detail) : null])]);
  }

  function tiles(app, m) {
    var d = m.dscr || {};
    var exit = m.exit || {};
    return h('dl', { class: 'tiles' }, [
      tile([app.t('NOI', 'NOI'), ', year 1'], core.usd(m.noi_year_1), m.noi_stabilized_year !== undefined && m.noi_stabilized_year !== null ?
        'Stabilized (year ' + m.stabilized_year + '): ' + core.usd(m.noi_stabilized_year) : null),
      tile([app.t('CoC', 'Cash-on-cash'), ', year 1'], core.pct(m.cash_on_cash_year_1)),
      tile(['Levered ', app.t('IRR', 'IRR')], core.pct(m.levered_irr), m.unlevered_irr !== undefined ? 'Unlevered ' + core.pct(m.unlevered_irr) : null),
      tile([app.t('EM', 'Equity multiple'), ', levered'], core.times(m.levered_equity_multiple),
        m.unlevered_equity_multiple !== undefined && m.unlevered_equity_multiple !== null ? 'Unlevered ' + core.times(m.unlevered_equity_multiple) : null),
      tile(['Minimum ', app.t('DSCR', 'DSCR'), ' (', app.t('NCF', 'NCF'), ')'], core.times(d.minimum),
        d.minimum_noi_before_capex !== undefined ? 'On NOI: ' + core.times(d.minimum_noi_before_capex) : null),
      m.going_in_cap_rate !== undefined ? tile([app.t('Cap rate', 'Going-in cap')], core.pct(m.going_in_cap_rate)) : null,
      tile([app.t('Exit cap', 'Exit cap')], core.pct(exit.cap_rate), exit.value !== undefined && exit.value !== null ? 'Value ' + core.usd(exit.value) : null)
    ]);
  }

  function annualTable(app, annual) {
    var cols = [['effective_gross_income', 'Income (EGI)'], ['total_opex', 'Operating expenses'], ['net_operating_income', 'NOI'],
      ['capex_charge', 'Capex charge'], ['cash_flow_before_debt', 'Cash flow before debt'], ['debt_service', 'Debt service'],
      ['cash_flow_after_debt', 'Cash flow after debt']];
    var totals = {};
    cols.forEach(function (c) { totals[c[0]] = annual.reduce(function (a, r) { return a + (r[c[0]] || 0); }, 0); });
    return h('div', { class: 'tablewrap', style: 'margin-top:10px' }, h('table', { class: 'data' }, [
      h('caption', {}, 'Operating cash flow by analysis year (the sale is under Exit). The capex charge is the larger of recurring capex and the replacement reserve.'),
      h('thead', {}, h('tr', {}, [h('th', { scope: 'col' }, 'Year')].concat(cols.map(function (c) { return h('th', { scope: 'col', class: 'num' }, c[1]); })))),
      h('tbody', {}, annual.map(function (r) {
        return h('tr', {}, [h('th', { scope: 'row' }, ['Year ' + r.analysis_year, h('div', { class: 'small muted' }, core.month(r.start) + ' – ' + core.month(r.end))])].concat(
          cols.map(function (c) { return h('td', { class: 'num' + (r[c[0]] < 0 ? ' neg' : '') }, core.usd(r[c[0]])); })));
      }).concat([h('tr', { class: 'total' }, [h('th', { scope: 'row' }, 'Total')].concat(cols.map(function (c) { return h('td', { class: 'num' }, core.usd(totals[c[0]])); })))]))
    ]));
  }

  function capitalByYear(app, plan) {
    if (!plan) { return null; }
    var imm = plan.immediate_capex || {};
    var charge = plan.below_noi_capex_charge || {};
    var years = charge.by_analysis_year || [];
    return h('section', { class: 'section', 'aria-labelledby': 'cap-h' }, [
      h('h2', { id: 'cap-h' }, 'Capital plan by year'),
      h('dl', { class: 'kv' }, [
        h('dt', {}, [app.t('Immediate capex', '1. Immediate capex at close')]),
        h('dd', {}, [core.usd(imm.amount), imm.funding ? ' · funded ' + String(imm.funding).replace(/_/g, ' ') : '']),
        h('dt', {}, [app.t('Recurring capex', '2. Recurring capex')]),
        h('dd', {}, charge.recurring_capex ? core.usd(charge.recurring_capex.per_unit_annual_year_1) + ' per unit in year 1, growing ' +
          core.pct(charge.recurring_capex.growth_rate, 1) + ' a year' : '—'),
        h('dt', {}, [app.t('Replacement reserve', '3. Replacement reserve')]),
        h('dd', {}, 'A floor under the charge, not an addition')
      ]),
      years.length ? h('div', { class: 'tablewrap', style: 'margin-top:10px' }, h('table', { class: 'data' }, [
        h('caption', {}, charge.rule || 'The charge below NOI is the larger of recurring capex and the reserve.'),
        h('thead', {}, h('tr', {}, [h('th', { scope: 'col' }, 'Year'), h('th', { scope: 'col', class: 'num' }, 'Recurring capex'),
          h('th', { scope: 'col', class: 'num' }, 'Reserve floor'), h('th', { scope: 'col', class: 'num' }, 'Charge below NOI'),
          h('th', { scope: 'col' }, 'Binds')])),
        h('tbody', {}, years.map(function (y) {
          var binds = y.binding === 'recurring_capex' ? 'Recurring capex' : y.binding === 'replacement_reserve' ? 'Reserve' : String(y.binding || '—');
          return h('tr', {}, [h('th', { scope: 'row' }, 'Year ' + y.analysis_year),
            h('td', { class: 'num' }, core.usd(y.recurring_capex_by_vintage)), h('td', { class: 'num' }, core.usd(y.replacement_reserve_floor)),
            h('td', { class: 'num' }, h('strong', {}, core.usd(y.below_noi_capex_charge))),
            h('td', {}, h('span', { class: 'badge flag-warn' }, binds))]);
        }))
      ])) : null
    ]);
  }

  function exitSection(app, exit, grid) {
    var w = exit.noi_window || {};
    var points = (grid && grid.points) || [];
    return h('section', { class: 'section', 'aria-labelledby': 'exit-h' }, [
      h('h2', { id: 'exit-h' }, 'Exit'),
      h('dl', { class: 'kv' }, [
        h('dt', {}, 'Sale month'), h('dd', {}, core.month(exit.month)),
        h('dt', {}, [app.t('Forward NOI', 'NOI valued')]), h('dd', {}, [core.usd(exit.noi), w.start ? ' · ' + (w.basis === 'trailing' ? 'trailing' : 'forward') +
          ' twelve months, ' + core.month(w.start) + ' to ' + core.month(w.end) : '']),
        h('dt', {}, [app.t('Exit cap', 'Exit cap')]), h('dd', {}, core.pct(exit.cap_rate)),
        h('dt', {}, 'Exit value'), h('dd', {}, core.usd(exit.value)),
        exit.sale_costs !== undefined && exit.sale_costs !== null ? [h('dt', {}, 'Sale costs'), h('dd', {}, core.usd(exit.sale_costs))] : null,
        exit.loan_payoff !== undefined && exit.loan_payoff !== null ? [h('dt', {}, 'Loan payoff'), h('dd', {}, core.usd(exit.loan_payoff))] : null,
        h('dt', {}, 'Net sale proceeds'), h('dd', {}, core.usd(exit.net_sale_proceeds))
      ]),
      points.length ? [h('h3', {}, 'Exit-cap sensitivity'),
        h('p', { class: 'hint' }, 'Only the sale moves: each point reprices the exit at the base exit cap plus the offset; the hold\'s cash flows are unchanged.'),
        PUW.exitGrid.chart(points), PUW.exitGrid.table(points)] : null
    ]);
  }

  PUW.screens.results = { title: 'Results', render: render };
})(window);
