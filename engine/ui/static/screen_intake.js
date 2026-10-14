/* Screen 1: deal intake. Create a deal, add its documents (drag and drop), record the deal facts
   (describe_deal) with each field's "why it matters", then "Check documents" (ingest_deal). */
(function (root) {
  'use strict';
  var PUW = root.PUW, core = root.PUWCore, h = PUW.h, append = PUW.append, clear = PUW.clear;

  var KINDS = [
    { kind: 't12', label: 'T12', term: 'T12', need: 'Required', help: 'Trailing-12 operating statement with its own total rows.' },
    { kind: 'rent_roll', label: 'Rent roll', term: 'Rent roll', need: 'Required', help: 'Unit-level export, ideally with its summary block.' },
    { kind: 'om', label: 'Offering memo', term: 'OM', need: 'Optional', help: 'PDF. Can supply the asking price and the broker exit cap.' },
    { kind: 'pca', label: 'Condition report', term: 'PCA', need: 'Optional', help: 'PCA as JSON or PDF. Recommended for anything built before 2000.' }
  ];

  function render(app, body) {
    if (!app.dealId) { renderCreate(app, body); return; }
    var cache = app.dealCache();
    var deal = app.deal && app.deal.ok ? app.deal : null;
    var opened = cache.created || cache.describe;
    if (!deal && !app.bridge.canCallTools) {
      // A host that cannot call tools: show what the opening tool returned, read-only.
      append(body, [app.readOnlyNote(), opened ? summaryNotice(opened) : null]);
      if (opened && opened.profile) { append(body, factsForm(app, { profile: opened.profile, deal_type: opened.deal_type, documents: {} }, true)); }
      return;
    }
    if (!deal) {
      append(body, PUW.loading('Loading the deal…'));
      app.refreshDeal().then(function (env) {
        clear(body);
        if (!env || !env.ok) { append(body, PUW.errorNotice(env || {})); return; }
        render(app, body);
      });
      return;
    }
    append(body, [
      opened && !opened.ok ? PUW.errorNotice(opened) : null,
      progress(app, deal),
      documents(app, deal),
      factsForm(app, deal, !app.bridge.canCallTools),
      app.readOnlyNote()
    ]);
  }

  function summaryNotice(env) {
    if (!env.ok) { return PUW.errorNotice(env); }
    return h('div', { class: 'notice ok', role: 'status' }, [h('p', {}, env.summary), env.next_step ? h('p', { class: 'small' }, ['Next: ', env.next_step]) : null]);
  }

  function renderCreate(app, body) {
    var name = h('input', { type: 'text', id: 'deal-name', autocomplete: 'off', maxlength: '200', required: true });
    var err = h('div', { class: 'err', id: 'deal-name-err', 'aria-live': 'polite' });
    var type = h('select', { id: 'deal-type' }, [h('option', { value: 'stabilized' }, 'Stabilized (priced on year-1 cash-on-cash)'),
      h('option', { value: 'value_add' }, 'Value-add (priced on the first stabilized year)')]);
    var create = h('button', { type: 'submit', class: 'btn primary' }, 'Create deal');
    var out = h('div', {});
    var form = h('form', { class: 'section', 'aria-labelledby': 'create-h', onsubmit: function (e) {
      e.preventDefault();
      if (!name.value.trim()) { err.textContent = 'Give the deal a name.'; name.setAttribute('aria-invalid', 'true'); name.focus(); return; }
      name.removeAttribute('aria-invalid'); err.textContent = '';
      app.act(create, 'create_deal', { name: name.value.trim(), deal_type: type.value }, 'Creating…').then(function (env) {
        clear(out);
        if (!env.ok) { append(out, PUW.errorNotice(env)); return; }
        app.selectDeal(env.deal_id);
        app.refreshDeal().then(function () { app.show('intake'); });
      });
    } }, [
      h('h2', { id: 'create-h' }, 'Create a deal'),
      h('p', { class: 'lead' }, 'One deal per property and per version of the underwriting you want to keep.'),
      h('div', { class: 'fields' }, [
        h('div', { class: 'field' }, [h('label', { for: 'deal-name' }, 'Deal name'), name, err]),
        h('div', { class: 'field' }, [h('label', { for: 'deal-type' }, [app.t('Stabilized', 'Deal type')]), type])
      ]),
      app.bridge.canCallTools ? h('div', { class: 'actions' }, [create,
        app.bridge.mode === 'browser' ? h('button', { type: 'button', class: 'btn', onclick: function (e) { loadSample(app, e.target, out); } }, 'Try the sample deal') : null]) :
        app.readOnlyNote('Create a deal for me (create_deal), or load the sample deal (load_sample_deal).'),
      out
    ]);
    append(body, form);
  }

  function loadSample(app, button, out) {
    app.act(button, 'load_sample_deal', {}, 'Loading…').then(function (env) {
      if (!env.ok) { clear(out); append(out, PUW.errorNotice(env)); return; }
      app.selectDeal(env.deal_id);
      app.record('load_sample_deal', env);
      app.refreshDeal().then(function () { app.show('intake'); });
    });
  }

  function progress(app, deal) {
    var steps = deal.steps || [];
    var done = steps.filter(function (s) { return s.done; }).length;
    var stale = steps.filter(function (s) { return s.stale; }).map(function (s) { return s.tool; });
    return h('section', { class: 'section', 'aria-labelledby': 'progress-h' }, [
      h('h2', { id: 'progress-h' }, 'Progress'),
      h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(steps.length || 8),
        'aria-valuenow': String(done), 'aria-label': done + ' of ' + (steps.length || 8) + ' steps done' },
        h('span', { style: 'width:' + Math.round(100 * done / Math.max(1, steps.length || 8)) + '%' })),
      h('p', { class: 'small muted' }, done + ' of ' + (steps.length || 8) + ' steps done' +
        (stale.length ? ' · out of date: ' + stale.join(', ') : '')),
      h('p', {}, [h('strong', {}, 'Next: '), core.nextText(deal)])
    ]);
  }

  // ── documents ──────────────────────────────────────────────────────────
  function documents(app, deal) {
    var docs = deal.documents || {};
    var exts = app.config.extensions || {};
    var status = h('div', { 'aria-live': 'polite' });
    return h('section', { class: 'section', id: 'documents', tabindex: '-1', 'aria-labelledby': 'docs-h' }, [
      h('h2', { id: 'docs-h' }, 'Documents'),
      h('p', { class: 'lead' }, ['Drop a file on a box, or choose it. One file per kind; adding a kind again replaces it. Up to ',
        String(app.config.max_upload_mb || 25), ' MB each.']),
      h('div', { class: 'docs' }, KINDS.map(function (k) { return dropZone(app, k, docs[k.kind], exts[k.kind] || [], status); })),
      status
    ]);
  }

  function dropZone(app, k, doc, exts, status) {
    var inputId = PUW.nextId('file');
    var input = h('input', { type: 'file', id: inputId, accept: exts.join(','), disabled: !app.bridge.canCallTools });
    var zone = h('div', { class: 'drop' + (doc ? ' has' : '') }, [
      h('div', { class: 'kind' }, [h('span', {}, [app.t(k.term, k.label)]),
        h('span', { class: 'badge ' + (k.need === 'Required' ? (doc ? 'flag-ok' : 'flag-review') : 'flag-muted') },
          doc ? 'Added' : k.need)]),
      h('div', { class: 'file' }, doc ? [h('strong', {}, doc.original_filename), ' · ', core.bytes(doc.bytes), ' · ', core.dateTime(doc.added_at)] :
        h('span', { class: 'muted' }, k.help)),
      h('label', { for: inputId, class: 'small' }, doc ? 'Replace the ' + k.label : 'Choose the ' + k.label),
      input,
      h('div', { class: 'small muted' }, exts.join(' '))
    ]);
    function send(file) {
      if (!file) { return; }
      if (exts.length && exts.indexOf('.' + String(file.name).split('.').pop().toLowerCase()) === -1) {
        clear(status); append(status, PUW.errorNotice({ error: { code: 'BAD_EXTENSION', message: file.name + ' is not an allowed ' + k.label + ' file',
          fix: 'Use one of ' + exts.join(', ') + '.' } }));
        return;
      }
      if (app.config.max_upload_mb && file.size > app.config.max_upload_mb * 1048576) {
        clear(status); append(status, PUW.errorNotice({ error: { code: 'TOO_LARGE', message: file.name + ' is ' + core.bytes(file.size) +
          '; the limit is ' + app.config.max_upload_mb + ' MB', fix: 'Use a smaller export.' } }));
        return;
      }
      clear(status); append(status, PUW.loading('Uploading ' + file.name + '…'));
      app.bridge.upload(app.dealId, k.kind, file).then(function (env) {
        app.record('add_document', env);
        clear(status);
        if (!env.ok) { append(status, PUW.errorNotice(env)); return; }
        app.announce(env.summary);
        app.onDealsChanged();
        app.refreshDeal().then(function () { app.show('intake'); });
      });
    }
    input.addEventListener('change', function () { send(input.files && input.files[0]); });
    if (app.bridge.canCallTools) {
      zone.addEventListener('dragover', function (e) { e.preventDefault(); zone.classList.add('over'); });
      zone.addEventListener('dragleave', function () { zone.classList.remove('over'); });
      zone.addEventListener('drop', function (e) {
        e.preventDefault(); zone.classList.remove('over');
        send(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]);
      });
    }
    return zone;
  }

  // ── deal facts ─────────────────────────────────────────────────────────
  function factsForm(app, deal, readOnly) {
    var fields = app.config.fields || [];
    var byName = {};
    fields.forEach(function (f) { byName[f.name] = f; });
    var profile = Object.assign({ deal_type: deal.deal_type }, deal.profile || {});
    var inputs = {};
    var errors = {};
    var out = h('div', { 'aria-live': 'polite' });

    function fieldEl(f) {
      var spec = core.fieldInput(f);
      var id = 'f-' + f.name, errId = id + '-err', whyId = id + '-why';
      var required = app.config.required_to_ingest.indexOf(f.name) !== -1;
      var toRun = app.config.required_to_run.indexOf(f.name) !== -1;
      var input;
      var described = whyId + ' ' + errId;
      if (spec.type === 'checkbox') {
        input = h('input', { type: 'checkbox', id: id, checked: boolBase(f.name), disabled: readOnly, 'aria-describedby': described });
      } else if (spec.type === 'select') {
        input = h('select', { id: id, disabled: readOnly, 'aria-describedby': described }, [h('option', { value: '' }, '—')].concat(
          (f.choices || []).map(function (c) { return h('option', { value: c, selected: profile[f.name] === c }, c); })));
      } else {
        input = h('input', { type: spec.type, id: id, inputmode: spec.inputmode || null, class: spec.numeric ? 'num' : null,
          value: core.fieldText(f, profile[f.name]), placeholder: spec.placeholder || null, autocomplete: 'off',
          readonly: readOnly, 'aria-required': required ? 'true' : null, 'aria-describedby': described });
      }
      inputs[f.name] = input;
      var err = h('div', { class: 'err', id: errId });
      errors[f.name] = err;
      input.addEventListener('blur', function () { check(f); });
      var control = spec.type === 'checkbox' ? h('div', { class: 'check' }, [input, h('label', { for: id }, label(app, f))]) :
        (spec.prefix || spec.suffix ? h('div', { class: 'inputwrap' }, [spec.prefix ? h('span', { class: 'affix pre', 'aria-hidden': 'true' }, spec.prefix) : null,
          input, spec.suffix ? h('span', { class: 'affix post', 'aria-hidden': 'true' }, spec.suffix) : null]) : input);
      return h('div', { class: 'field' }, [
        spec.type === 'checkbox' ? null : h('label', { for: id }, [label(app, f),
          required ? h('span', { class: 'req-tag' }, 'required to check') : toRun ? h('span', { class: 'req-tag' }, 'needed to run') : null]),
        control,
        h('p', { class: 'why', id: whyId }, [f.what, ' ', h('span', {}, 'Why it matters: ' + f.why)]),
        err
      ]);
    }
    function boolBase(name) {
      if (typeof profile[name] === 'boolean') { return profile[name]; }
      return !!(app.config.defaults || {})[name];
    }
    function rawValue(name) {
      var input = inputs[name];
      return input.type === 'checkbox' ? input.checked : input.value;
    }
    function check(f) {
      var parsed = core.parseField(f, rawValue(f.name));
      errors[f.name].textContent = parsed.ok ? '' : parsed.error;
      if (parsed.ok) { inputs[f.name].removeAttribute('aria-invalid'); } else { inputs[f.name].setAttribute('aria-invalid', 'true'); }
      return parsed.ok;
    }
    function collect() {
      var raw = {};
      Object.keys(inputs).forEach(function (name) {
        if (inputs[name].type === 'checkbox' && inputs[name].checked === boolBase(name)) { return; }
        raw[name] = rawValue(name);
      });
      var checked = core.checkProfile(fields.filter(function (f) { return f.name in raw; }), raw);
      Object.keys(errors).forEach(function (name) {
        errors[name].textContent = checked.errors[name] || '';
        if (checked.errors[name]) { inputs[name].setAttribute('aria-invalid', 'true'); } else { inputs[name].removeAttribute('aria-invalid'); }
      });
      return checked;
    }
    function save(button) {
      var checked = collect();
      var bad = Object.keys(checked.errors);
      if (bad.length) {
        clear(out);
        append(out, h('div', { class: 'notice error', role: 'alert' }, [h('h3', {}, 'Check ' + bad.length + ' field' + (bad.length > 1 ? 's' : '')),
          h('ul', {}, bad.map(function (n) { return h('li', {}, [h('a', { href: '#f-' + n }, label(app, byName[n])), ': ', checked.errors[n]]); }))]));
        inputs[bad[0]].focus();
        return Promise.resolve(null);
      }
      var changes = core.profileChanges(profile, checked.values);
      if (!Object.keys(changes).length) { return Promise.resolve({ ok: true, unchanged: true }); }
      changes.deal_id = app.dealId;
      return app.act(button, 'describe_deal', changes, 'Saving…').then(function (env) {
        clear(out);
        append(out, env.ok ? h('div', { class: 'notice ok', role: 'status' }, [h('p', {}, env.summary),
          env.missing_for_ingest && env.missing_for_ingest.length ? h('p', {}, 'Still needed to check the documents: ' + env.missing_for_ingest.join(', ') + '.') : null]) :
          PUW.errorNotice(env));
        if (env.ok) { Object.assign(profile, env.profile || {}); profile.deal_type = env.deal_type; }
        return env;
      });
    }

    var groups = (app.config.groups || []).map(function (g) {
      var list = g.fields.filter(function (n) { return byName[n]; }).map(function (n) { return fieldEl(byName[n]); });
      var set = h('fieldset', {}, [h('legend', {}, g.label), g.lead ? h('p', { class: 'hint' }, g.lead) : null, h('div', { class: 'fields' }, list)]);
      return g.more ? h('details', { class: 'more' }, [h('summary', {}, g.label + ' (optional)'), set]) : set;
    });
    var saveBtn = h('button', { type: 'button', class: 'btn', onclick: function () { save(saveBtn); } }, 'Save deal facts');
    var checkBtn = h('button', { type: 'button', class: 'btn primary', onclick: function () {
      save(saveBtn).then(function (env) { if (env && env.ok) { ingest(app, checkBtn, out); } });
    } }, 'Check documents');
    var missing = (deal.missing_for_ingest || []);
    return h('section', { class: 'section', id: 'facts', tabindex: '-1', 'aria-labelledby': 'facts-h' }, [
      h('h2', { id: 'facts-h' }, 'Deal facts'),
      h('p', { class: 'lead' }, ['Rates are percents here (6.5 means 6.5%). Blank fields are left as they are. Fields marked ',
        h('em', {}, 'required to check'), ' are needed before the documents can be checked; ', h('em', {}, 'needed to run'),
        ' before the deal can run (pricing finds a price instead).']),
      missing.length ? h('div', { class: 'notice warn' }, 'Still needed before checking the documents: ' + missing.join(', ') + '.') : null,
      groups,
      readOnly ? null : h('div', { class: 'actions' }, [checkBtn, saveBtn,
        h('span', { class: 'hint' }, '"Check documents" saves the facts, then reads and reconciles the T12 and rent roll.')]),
      out
    ]);
  }

  function label(app, f) {
    var text = (app.config.labels || {})[f.name] || f.name;
    var term = (app.config.label_terms || {})[f.name];
    return term ? app.t(term, text) : text;
  }

  function ingest(app, button, out) {
    app.act(button, 'ingest_deal', { deal_id: app.dealId }, 'Checking…').then(function (env) {
      clear(out);
      if (!env.ok) { append(out, PUW.errorNotice(env)); return; }
      if (app.mode === 'browser') { app.show('assumptions'); return; }
      append(out, [h('div', { class: 'notice ' + (env.gate && env.gate.status === 'BLOCKED' ? 'error' : 'ok'), role: 'status' }, [
        h('p', {}, [PUW.statusBadge(env.gate && env.gate.status), ' ', env.summary]),
        h('p', { class: 'small' }, ['Next: ', env.next_step])]),
        h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn', onclick: function () { app.go('assumptions', 'gate', 'check'); } }, 'Open the gate and assumptions'))]);
    });
  }

  PUW.screens.intake = { title: 'Deal intake', render: render };
})(window);
