/* Two small inline-SVG charts (no chart library): grouped bars by year, and lines over x values.
   Every chart carries a title, a text description and a table fallback is shown beside it by the
   screens, so color is never the only signal. */
(function (root) {
  'use strict';
  var core = root.PUWCore;
  var NS = 'http://www.w3.org/2000/svg';
  var doc = root.document;

  function s(tag, attrs, text) {
    var el = doc.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, String(attrs[k])); });
    if (text !== undefined) { el.textContent = text; }
    return el;
  }
  function frame(title, desc, width, height) {
    var svg = s('svg', { viewBox: '0 0 ' + width + ' ' + height, class: 'chart', role: 'img', preserveAspectRatio: 'xMidYMid meet' });
    var tid = 'c' + Math.random().toString(36).slice(2, 8);
    svg.setAttribute('aria-labelledby', tid + 't ' + tid + 'd');
    svg.appendChild(s('title', { id: tid + 't' }, title));
    svg.appendChild(s('desc', { id: tid + 'd' }, desc));
    return svg;
  }

  /** bars({title, desc, labels, series: [{name, values, cls}], format}) */
  function bars(opts) {
    var W = 560, H = 230, L = 64, R = 10, T = 12, B = 30;
    var svg = frame(opts.title, opts.desc, W, H);
    var all = [0];
    opts.series.forEach(function (se) { se.values.forEach(function (v) { if (core.isNum(v)) { all.push(v); } }); });
    var ticks = core.niceTicks(Math.min.apply(null, all), Math.max.apply(null, all), 4);
    var y = core.scale(ticks[0], ticks[ticks.length - 1], H - B, T);
    ticks.forEach(function (t) {
      svg.appendChild(s('line', { x1: L, x2: W - R, y1: y(t), y2: y(t), class: t === 0 ? 'axis' : 'grid' }));
      svg.appendChild(s('text', { x: L - 6, y: y(t) + 4, 'text-anchor': 'end' }, opts.format(t)));
    });
    var n = opts.labels.length, band = (W - L - R) / Math.max(1, n), k = opts.series.length;
    var bw = Math.min(28, (band * 0.7) / k);
    opts.labels.forEach(function (label, i) {
      var cx = L + band * i + band / 2;
      opts.series.forEach(function (se, j) {
        var v = se.values[i];
        if (!core.isNum(v)) { return; }
        var x = cx - (bw * k) / 2 + j * bw;
        var y0 = y(0), y1 = y(v);
        var rect = s('rect', { x: x, y: Math.min(y0, y1), width: Math.max(1, bw - 2), height: Math.max(1, Math.abs(y1 - y0)), class: se.cls, rx: 2 });
        rect.appendChild(s('title', {}, se.name + ', ' + label + ': ' + opts.format(v)));
        svg.appendChild(rect);
      });
      svg.appendChild(s('text', { x: cx, y: H - 10, 'text-anchor': 'middle' }, label));
    });
    return svg;
  }

  /** lines({title, desc, xs, series: [{name, values, cls, dot}], xFormat, yFormat, marker}) */
  function lines(opts) {
    var W = 560, H = 230, L = 52, R = 14, T = 12, B = 30;
    var svg = frame(opts.title, opts.desc, W, H);
    var ys = [];
    opts.series.forEach(function (se) { se.values.forEach(function (v) { if (core.isNum(v)) { ys.push(v); } }); });
    if (!ys.length || !opts.xs.length) { return svg; }
    var ticks = core.niceTicks(Math.min.apply(null, ys), Math.max.apply(null, ys), 4);
    var y = core.scale(ticks[0], ticks[ticks.length - 1], H - B, T);
    var x = core.scale(opts.xs[0], opts.xs[opts.xs.length - 1], L + 8, W - R - 8);
    ticks.forEach(function (t) {
      svg.appendChild(s('line', { x1: L, x2: W - R, y1: y(t), y2: y(t), class: 'grid' }));
      svg.appendChild(s('text', { x: L - 6, y: y(t) + 4, 'text-anchor': 'end' }, opts.yFormat(t)));
    });
    opts.xs.forEach(function (xv) { svg.appendChild(s('text', { x: x(xv), y: H - 10, 'text-anchor': 'middle' }, opts.xFormat(xv))); });
    if (core.isNum(opts.marker)) {
      svg.appendChild(s('line', { x1: x(opts.marker), x2: x(opts.marker), y1: T, y2: H - B, class: 'base' }));
    }
    opts.series.forEach(function (se) {
      var d = '';
      se.values.forEach(function (v, i) { if (core.isNum(v)) { d += (d ? ' L ' : 'M ') + x(opts.xs[i]).toFixed(1) + ' ' + y(v).toFixed(1); } });
      svg.appendChild(s('path', { d: d, class: se.cls }));
      se.values.forEach(function (v, i) {
        if (!core.isNum(v)) { return; }
        var c = s('circle', { cx: x(opts.xs[i]), cy: y(v), r: 3.5, class: se.dot });
        c.appendChild(s('title', {}, se.name + ' at ' + opts.xFormat(opts.xs[i]) + ': ' + opts.yFormat(v)));
        svg.appendChild(c);
      });
    });
    return svg;
  }

  root.PUWCharts = { bars: bars, lines: lines };
})(window);
