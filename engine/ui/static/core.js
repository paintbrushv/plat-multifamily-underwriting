/* Pure helpers shared by every screen: number formatting (the same conventions as the assumption
   register page), deal-profile input parsing and validation (mirroring engine/workflow/fields.py),
   assumption-edit parsing, the seven-step model and chart scales. No DOM access here, so the
   functions run under Node for tests (module.exports) and in the page (window.PUWCore). */
(function (root) {
  'use strict';
  var NA = '—';

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function toNum(v) {
    if (isNum(v)) { return v; }
    if (typeof v === 'string' && v.trim() !== '' && isFinite(Number(v))) { return Number(v); }
    return null;
  }
  function group3(intText) { return intText.replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
  function fixed(n, digits) {
    var text = Math.abs(n).toFixed(digits);
    var parts = text.split('.');
    return group3(parts[0]) + (parts.length > 1 ? '.' + parts[1] : '');
  }
  function sign(n, text) { return (n < 0 && Number(text.replace(/[^0-9]/g, '')) !== 0 ? '-' : '') + text; }

  /** $1,234,567 (no cents); cents: true gives $12.50. */
  function usd(v, opts) {
    var n = toNum(v);
    if (n === null) { return v === null || v === undefined || v === '' ? NA : String(v); }
    var digits = opts && opts.cents ? 2 : 0;
    var body = fixed(n, digits);
    return sign(n, '$' + body);
  }
  /** $1.2M / $950K for chart axes. */
  function usdShort(v) {
    var n = toNum(v);
    if (n === null) { return NA; }
    var a = Math.abs(n);
    var text;
    if (a >= 1e6) { text = '$' + trimZeros((a / 1e6).toFixed(a >= 1e7 ? 1 : 2)) + 'M'; }
    else if (a >= 1e3) { text = '$' + trimZeros((a / 1e3).toFixed(a >= 1e5 ? 0 : 1)) + 'K'; }
    else { text = '$' + a.toFixed(0); }
    return (n < 0 ? '-' : '') + text;
  }
  function trimZeros(text) { return text.indexOf('.') === -1 ? text : text.replace(/\.?0+$/, ''); }
  /** 0.0588 -> 5.88% (two decimals, as the pricing header shows). */
  function pct(v, digits) {
    var n = toNum(v);
    if (n === null) { return v === null || v === undefined || v === '' ? NA : String(v); }
    var d = digits === undefined ? 2 : digits;
    return sign(n, fixed(n * 100, d) + '%');
  }
  /** 1.386 -> 1.39x */
  function times(v) {
    var n = toNum(v);
    if (n === null) { return v === null || v === undefined || v === '' ? NA : String(v); }
    return sign(n, fixed(n, 2) + 'x');
  }
  /** an offset in bps: 25 -> +25 bps, 0 -> base */
  function bps(v) {
    var n = toNum(v);
    if (n === null) { return NA; }
    if (n === 0) { return 'base'; }
    return (n > 0 ? '+' : '-') + fixed(n, 0) + ' bps';
  }
  function int(v) {
    var n = toNum(v);
    return n === null ? NA : sign(n, fixed(n, 0));
  }
  /** 2031-10 -> Oct 2031 */
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function month(text) {
    var m = /^(\d{4})-(\d{2})/.exec(String(text || ''));
    if (!m) { return text ? String(text) : NA; }
    return MONTHS[Number(m[2]) - 1] + ' ' + m[1];
  }
  function dateTime(text) {
    var m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(String(text || ''));
    if (!m) { return text ? String(text) : NA; }
    return MONTHS[Number(m[2]) - 1] + ' ' + Number(m[3]) + ', ' + m[1] + (m[4] ? ' ' + m[4] + ':' + m[5] + ' UTC' : '');
  }
  function bytes(v) {
    var n = toNum(v);
    if (n === null) { return NA; }
    if (n >= 1048576) { return trimZeros((n / 1048576).toFixed(1)) + ' MB'; }
    if (n >= 1024) { return trimZeros((n / 1024).toFixed(0)) + ' KB'; }
    return n + ' B';
  }
  function escapeHtml(text) {
    return String(text === null || text === undefined ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ── deal-profile fields (engine/workflow/fields.py) ────────────────────
  var MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;
  var DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

  /** How a field is entered in the form: rates as percents, money in dollars. */
  function fieldInput(field) {
    switch (field.type) {
      case 'rate': return { type: 'text', inputmode: 'decimal', suffix: '%', numeric: true };
      case 'float': return { type: 'text', inputmode: 'decimal', prefix: isMoney(field) ? '$' : '', numeric: true };
      case 'int': return { type: 'text', inputmode: 'numeric', numeric: true };
      case 'month': return { type: 'month', placeholder: 'YYYY-MM' };
      case 'date': return { type: 'date', placeholder: 'YYYY-MM-DD' };
      case 'bool': return { type: 'checkbox' };
      case 'choice': return { type: 'select' };
      default: return { type: 'text' };
    }
  }
  function isMoney(field) {
    return /dollars|\$/.test(field.what || '') && !/mills/.test(field.what || '');
  }
  /** The text a stored value shows in its input (0.065 -> "6.5" for a rate). */
  function fieldText(field, value) {
    if (value === null || value === undefined) { return ''; }
    if (field.type === 'rate' && isNum(value)) { return trimZeros((value * 100).toFixed(4)); }
    if (field.type === 'float' && isNum(value)) { return trimZeros(value.toFixed(2)); }
    return String(value);
  }
  function stripNumber(text) { return String(text).replace(/[,$%\s]/g, ''); }

  /** Parse and check one field's input text: {ok, value} or {ok: false, error}; empty -> value null. */
  function parseField(field, raw) {
    if (field.type === 'bool') { return { ok: true, value: !!raw }; }
    var text = raw === null || raw === undefined ? '' : String(raw).trim();
    if (text === '') { return { ok: true, value: null }; }
    var value;
    if (field.type === 'int' || field.type === 'float' || field.type === 'rate') {
      var cleaned = stripNumber(text);
      if (!/^-?(\d+\.?\d*|\.\d+)$/.test(cleaned)) { return { ok: false, error: 'Enter a number.' }; }
      value = Number(cleaned);
      if (field.type === 'rate') { value = Math.round(value * 1e8) / 1e10; }
      if (field.type === 'int' && Math.floor(value) !== value) { return { ok: false, error: 'Enter a whole number.' }; }
      if (field.minimum !== undefined && field.minimum !== null && value < field.minimum) {
        return { ok: false, error: 'At least ' + limitText(field, field.minimum) + '.' };
      }
      if (field.maximum !== undefined && field.maximum !== null && value > field.maximum) {
        return { ok: false, error: 'At most ' + limitText(field, field.maximum) + '.' };
      }
      return { ok: true, value: value };
    }
    if (field.type === 'month' && !MONTH_RE.test(text)) { return { ok: false, error: 'Use a month as YYYY-MM, e.g. 2026-11.' }; }
    if (field.type === 'date' && !DATE_RE.test(text)) { return { ok: false, error: 'Use a date as YYYY-MM-DD.' }; }
    if (field.choices && field.choices.length && field.choices.indexOf(text) === -1) {
      return { ok: false, error: 'Choose one of: ' + field.choices.join(', ') + '.' };
    }
    if (text.length > 200) { return { ok: false, error: '200 characters at most.' }; }
    return { ok: true, value: text };
  }
  function limitText(field, n) {
    if (field.type === 'rate') { return trimZeros((n * 100).toFixed(2)) + '%'; }
    return group3(String(n));
  }
  /** Check a whole form: {values, errors} with cross-field checks (end after start). */
  function checkProfile(fields, raw) {
    var values = {}, errors = {};
    fields.forEach(function (field) {
      if (!(field.name in raw)) { return; }
      var parsed = parseField(field, raw[field.name]);
      if (parsed.ok) { values[field.name] = parsed.value; } else { errors[field.name] = parsed.error; }
    });
    if (values.start && values.end && values.end <= values.start && !errors.end) {
      errors.end = 'The end month must be after the start month.';
    }
    return { values: values, errors: errors };
  }
  /** Only the fields whose value changed (null clears nothing: describe_deal skips nulls). */
  function profileChanges(profile, values) {
    var out = {};
    Object.keys(values).forEach(function (name) {
      var v = values[name];
      if (v === null || v === undefined) { return; }
      if (profile && profile[name] === v) { return; }
      out[name] = v;
    });
    return out;
  }

  // ── assumption edits ───────────────────────────────────────────────────
  function unitKind(unit) {
    unit = String(unit || '');
    if (unit.indexOf('decimal') === 0) { return 'pct'; }
    if (unit.indexOf('usd') === 0) { return 'usd'; }
    if (unit === 'enum') { return 'enum'; }
    return 'num';
  }
  /** How a register row can be edited inline: scalar, list (comma separated) or json. */
  function editMode(row) {
    var v = row.value;
    if (Array.isArray(v) && v.every(isNum)) { return 'list'; }
    if (v !== null && typeof v === 'object') { return 'json'; }
    if (typeof v === 'string' || unitKind(row.unit) === 'enum') { return 'text'; }
    return 'scalar';
  }
  function editText(row) {
    var mode = editMode(row), kind = unitKind(row.unit), v = row.value;
    function one(x) { return kind === 'pct' ? trimZeros((x * 100).toFixed(4)) : trimZeros(Number(x).toFixed(4)); }
    if (v === null || v === undefined) { return ''; }
    if (mode === 'list') { return v.map(one).join(', '); }
    if (mode === 'json') { return JSON.stringify(v); }
    if (mode === 'scalar' && isNum(v)) { return one(v); }
    return String(v);
  }
  function editHint(row) {
    var mode = editMode(row), kind = unitKind(row.unit);
    if (mode === 'json') { return 'JSON, as shown (advanced). Ask the assistant if unsure.'; }
    if (mode === 'text') { return 'The exact value, as shown.'; }
    var unit = kind === 'pct' ? 'percent (6.5 means 6.5%)' : kind === 'usd' ? 'dollars' : 'a number';
    return mode === 'list' ? 'One value per year, comma separated, in ' + unit + '.' : 'In ' + unit + '.';
  }
  /** Parse the edit box: {ok, value} in the engine's units, or {ok: false, error}. */
  function parseEdit(row, raw) {
    var text = String(raw === null || raw === undefined ? '' : raw).trim();
    var mode = editMode(row), kind = unitKind(row.unit);
    if (text === '') { return { ok: false, error: 'Enter a value (or use "Back to default").' }; }
    function one(part) {
      var cleaned = stripNumber(part);
      if (!/^-?(\d+\.?\d*|\.\d+)$/.test(cleaned)) { return null; }
      var n = Number(cleaned);
      return kind === 'pct' ? Math.round(n * 1e8) / 1e10 : n;
    }
    if (mode === 'json') {
      try { return { ok: true, value: JSON.parse(text) }; }
      catch (e) { return { ok: false, error: 'That is not valid JSON.' }; }
    }
    if (mode === 'text') { return { ok: true, value: text }; }
    if (mode === 'list') {
      var parts = text.split(/[,;\s]+/).filter(Boolean).map(one);
      if (parts.some(function (p) { return p === null; })) { return { ok: false, error: 'Enter numbers separated by commas.' }; }
      if (Array.isArray(row.value) && row.value.length && parts.length !== row.value.length) {
        return { ok: false, error: 'Enter ' + row.value.length + ' values, one per year.' };
      }
      return { ok: true, value: parts };
    }
    var n = one(text);
    if (n === null) { return { ok: false, error: 'Enter a number.' }; }
    return { ok: true, value: n };
  }

  // ── the seven steps an analyst sees ────────────────────────────────────
  var STEPS = [
    { id: 'documents', label: 'Documents', screen: 'intake', anchor: 'documents' },
    { id: 'describe', label: 'Describe', screen: 'intake', anchor: 'facts' },
    { id: 'check', label: 'Check', screen: 'assumptions', anchor: 'gate' },
    { id: 'assumptions', label: 'Assumptions', screen: 'assumptions', anchor: 'register' },
    { id: 'results', label: 'Results', screen: 'results', anchor: 'headline' },
    { id: 'price', label: 'Price', screen: 'pricing', anchor: 'pricing' },
    { id: 'export', label: 'Export', screen: 'export', anchor: 'export' }
  ];
  /** Each step's state from get_deal: done | stale | blocked | todo, and which one is next. */
  function stepStates(deal) {
    var byStep = {};
    ((deal && deal.steps) || []).forEach(function (s) { byStep[s.step] = s; });
    function rec(name) { return byStep[name] || {}; }
    var docs = (deal && deal.documents) || {};
    var missing = (deal && deal.missing_for_ingest) || [];
    var ingest = (deal && deal.last_ingest) || null;
    var states = {};
    states.documents = docs.t12 && docs.rent_roll ? 'done' : 'todo';
    states.describe = rec('describe').done && missing.indexOf('start') === -1 && missing.indexOf('end') === -1 ? 'done' : 'todo';
    if (rec('ingest').done) {
      states.check = rec('ingest').stale ? 'stale' : (ingest && ingest.gate_status === 'BLOCKED') ? 'blocked' : 'done';
    } else { states.check = 'todo'; }
    states.assumptions = states.check === 'done' && (rec('edit').done || rec('run').done) ? 'done' : 'todo';
    states.results = rec('run').done ? (rec('run').stale ? 'stale' : 'done') : 'todo';
    states.price = rec('price').done ? (rec('price').stale ? 'stale' : 'done') : 'todo';
    states.export = rec('run').done ? 'done' : 'todo';
    var next = null;
    for (var i = 0; i < STEPS.length; i++) {
      if (states[STEPS[i].id] !== 'done') { next = STEPS[i].id; break; }
    }
    return { states: states, next: next };
  }

  var NEXT_TEXT = {
    documents: { todo: 'Add the T12 and the rent roll.' },
    describe: { todo: 'Enter the analysis start and end months, and the price, financing and property-tax millage.' },
    check: { todo: 'Check the documents.', stale: 'Check the documents again: the documents or the deal facts changed.',
      blocked: 'Fix the document-check blockers, then check again.' },
    assumptions: { todo: 'Review the flagged assumptions, then run the deal.' },
    results: { todo: 'Run the deal.', stale: 'Run the deal again: the assumptions changed.' },
    price: { todo: 'Solve the price.', stale: 'Solve the price again: the assumptions changed.' },
    export: { todo: 'Run the deal, then export the workbook and the assumption register.' }
  };
  /** The next step in plain words (the tools' next_step is written for the assistant). */
  function nextText(deal) {
    var model = stepStates(deal);
    if (!model.next) { return 'All done. Export the workbook and the assumption register, or change an assumption and run again.'; }
    var texts = NEXT_TEXT[model.next];
    return texts[model.states[model.next]] || texts.todo;
  }

  // ── charts ─────────────────────────────────────────────────────────────
  /** Rounded axis ticks covering [lo, hi]. */
  function niceTicks(lo, hi, count) {
    if (!isNum(lo) || !isNum(hi)) { return [0, 1]; }
    if (lo === hi) { hi = lo === 0 ? 1 : lo + Math.abs(lo) * 0.1; }
    var span = hi - lo, n = count || 4;
    var raw = span / n, mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var step = [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; }).filter(function (s) { return s >= raw; })[0];
    var start = Math.floor(lo / step) * step, ticks = [];
    for (var t = start; t <= hi + step * 1e-9; t += step) { ticks.push(Math.round(t / step) * step); }
    if (ticks[ticks.length - 1] < hi) { ticks.push(ticks[ticks.length - 1] + step); }
    return ticks;
  }
  function scale(d0, d1, r0, r1) {
    return function (v) { return d1 === d0 ? (r0 + r1) / 2 : r0 + (v - d0) * (r1 - r0) / (d1 - d0); };
  }

  /** The result a tool call returned, from an MCP CallToolResult or a plain envelope. */
  function toolEnvelope(result) {
    if (!result || typeof result !== 'object') {
      return { ok: false, summary: 'No result.', error: { code: 'NO_RESULT', message: 'The tool returned nothing.', fix: 'Try again.' } };
    }
    if ('ok' in result && ('summary' in result || 'error' in result)) { return result; }
    var sc = result.structuredContent;
    if (sc && typeof sc === 'object' && 'ok' in sc) { return sc; }
    var text = (result.content || []).filter(function (c) { return c && c.type === 'text'; }).map(function (c) { return c.text; }).join('');
    try {
      var parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object') { return parsed; }
    } catch (e) { /* fall through */ }
    return { ok: false, summary: text || 'The tool returned an unreadable result.',
      error: { code: result.isError ? 'TOOL_ERROR' : 'UNREADABLE_RESULT', message: text || 'Unreadable result.',
        fix: 'Ask the assistant to run the step again.' } };
  }

  /** Which tool produced an envelope, when the host does not say (hostContext.toolInfo). */
  function inferTool(env) {
    if (!env || typeof env !== 'object') { return null; }
    if ('pricing' in env) { return 'solve_price'; }
    if ('metrics' in env) { return 'run_deal'; }
    if ('gate' in env) { return 'ingest_deal'; }
    if ('rows' in env) { return 'review_assumptions'; }
    if ('changed' in env) { return 'edit_assumptions'; }
    if ('steps' in env && 'documents' in env) { return 'get_deal'; }
    if ('updated' in env && 'profile' in env) { return 'describe_deal'; }
    if ('pricing_defaults' in env && Array.isArray(env.documents)) { return 'load_sample_deal'; }
    if ('deal_id' in env && 'deal_type' in env && 'name' in env) { return 'create_deal'; }
    if ('files' in env && 'register_resource' in env) { return 'export_deal'; }
    return null;
  }

  var core = {
    NA: NA, isNum: isNum, toNum: toNum, usd: usd, usdShort: usdShort, pct: pct, times: times, bps: bps, int: int,
    month: month, dateTime: dateTime, bytes: bytes, escapeHtml: escapeHtml, trimZeros: trimZeros,
    fieldInput: fieldInput, fieldText: fieldText, parseField: parseField, checkProfile: checkProfile,
    profileChanges: profileChanges, unitKind: unitKind, editMode: editMode, editText: editText, editHint: editHint,
    parseEdit: parseEdit, STEPS: STEPS, stepStates: stepStates, nextText: nextText, niceTicks: niceTicks, scale: scale,
    toolEnvelope: toolEnvelope, inferTool: inferTool
  };
  if (typeof module !== 'undefined' && module.exports) { module.exports = core; }
  else { root.PUWCore = core; }
})(typeof window !== 'undefined' ? window : this);
