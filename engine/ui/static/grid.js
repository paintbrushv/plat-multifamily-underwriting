/* The exit-cap sensitivity grid as a chart and a table (Results and Pricing both show it). */
(function (root) {
  'use strict';
  var PUW = root.PUW, core = root.PUWCore, charts = root.PUWCharts, h = PUW.h;

  function gridChart(points) {
    var xs = points.map(function (p) { return p.exit_cap_rate; });
    var base = points.filter(function (p) { return p.offset_bps === 0; })[0];
    return h('figure', { style: 'margin:0' }, [charts.lines({
      title: 'Levered and unlevered IRR against the exit cap rate',
      desc: points.map(function (p) { return 'Exit cap ' + core.pct(p.exit_cap_rate) + ': levered ' + core.pct(p.levered_irr) + ', unlevered ' + core.pct(p.unlevered_irr); }).join('; '),
      xs: xs, xFormat: function (v) { return core.pct(v, 2); }, yFormat: function (v) { return core.pct(v, 0); },
      marker: base ? base.exit_cap_rate : null,
      series: [{ name: 'Levered IRR', values: points.map(function (p) { return p.levered_irr; }), cls: 'line1', dot: 'dot1' },
        { name: 'Unlevered IRR', values: points.map(function (p) { return p.unlevered_irr; }), cls: 'line2', dot: 'dot2' }]
    }), h('div', { class: 'legend' }, [h('span', {}, [h('span', { class: 'sw l2', style: 'background:var(--chart-1)' }), 'Levered IRR (solid)']),
      h('span', {}, [h('span', { class: 'sw l2', style: 'background:var(--chart-2)' }), 'Unlevered IRR (dashed)']),
      h('span', {}, 'Dotted line: the base case')])]);
  }

  function gridTable(points) {
    return h('div', { class: 'tablewrap', style: 'margin-top:10px' }, h('table', { class: 'data' }, [
      h('thead', {}, h('tr', {}, [h('th', { scope: 'col' }, 'Offset'), h('th', { scope: 'col', class: 'num' }, 'Exit cap'),
        h('th', { scope: 'col', class: 'num' }, 'Exit value'), h('th', { scope: 'col', class: 'num' }, 'Levered IRR'),
        h('th', { scope: 'col', class: 'num' }, 'Unlevered IRR')])),
      h('tbody', {}, points.map(function (p) {
        return h('tr', { class: p.offset_bps === 0 ? 'base' : null }, [h('th', { scope: 'row' }, core.bps(p.offset_bps)),
          h('td', { class: 'num' }, core.pct(p.exit_cap_rate)), h('td', { class: 'num' }, core.usd(p.exit_value)),
          h('td', { class: 'num' + (p.levered_irr < 0 ? ' neg' : '') }, core.pct(p.levered_irr)),
          h('td', { class: 'num' + (p.unlevered_irr < 0 ? ' neg' : '') }, core.pct(p.unlevered_irr))]);
      }))
    ]));
  }

  PUW.exitGrid = { chart: gridChart, table: gridTable };
})(window);
