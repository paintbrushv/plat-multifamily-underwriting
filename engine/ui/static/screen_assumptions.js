/* Screen 2: ingest and assumptions. The gate results first (PASS / BLOCKED with the fix for each
   blocker), the market data status and the capital plan, then the assumption register as an
   editable table: filter chips, value / default / source, "why this default", inline edits with a
   note (edit_assumptions, stamped with the editor's name) and the deltas they made. */
(function (root) {
  'use strict';
  var PUW = root.PUW, core = root.PUWCore, h = PUW.h, append = PUW.append, clear = PUW.clear;

  var FAMILY = { analyst: 'Analyst', deal: 'Deal', market: 'Market', house: 'House', engine_fallback: 'Fallback', unknown: 'Other' };
  var CHIPS = [
    { id: 'needs_review', label: 'Needs review', test: function (r) { return (r.filters || []).indexOf('review') !== -1; } },
    { id: 'edited', label: 'Edited', test: function (r) { return (r.filters || []).indexOf('edited') !== -1; } },
    { id: 'defaults', label: 'Defaults', test: function (r) { return (r.filters || []).indexOf('house') !== -1; } },
    { id: 'enforced', label: 'Enforced in pricing', test: function (r) { return r.guardrail && r.guardrail.state === 'enforced'; } },
    { id: 'all', label: 'All', test: function () { return true; } }
  ];

  function render(app, body) {
    if (!app.dealId) { append(body, PUW.empty('No deal yet', 'Create a deal or try the sample first.')); return; }
    var cache = app.dealCache();
    var gateHolder = h('div', {});
    var registerHolder = h('div', {});
    append(body, [app.readOnlyNote(), gateHolder, registerHolder]);
    renderGate(app, gateHolder, cache.ingest);
    if (!app.state) { app.state = {}; }
    var view = app.state.assumptions || (app.state.assumptions = { filter: 'needs_review', changes: null });
    if (app.focusChip) { view.filter = app.focusChip; app.focusChip = null; }
    if (cache.edit && !view.changes) { view.changes = cache.edit; view.filter = cache.edit.ok ? 'edited' : view.filter; delete cache.edit; }
    if (cache.review && cache.review.ok && cache.review.filter === 'all' && view.filter !== 'enforced') {
      renderRegister(app, registerHolder, cache.review, view);
    } else if (app.bridge.canCallTools && (cache.ingest && cache.ingest.ok || stepDone(app, 'ingest'))) {
      loadRegister(app, registerHolder, view);
    } else if (cache.review && cache.review.ok) {
      renderRegister(app, registerHolder, cache.review, view);
    } else if (cache.review) {
      append(registerHolder, PUW.errorNotice(cache.review));
    }
  }

  function stepDone(app, step) {
    var steps = (app.deal && app.deal.ok && app.deal.steps) || [];
    return steps.some(function (s) { return s.step === step && s.done; });
  }

  function loadRegister(app, holder, view) {
    clear(holder);
    append(holder, h('section', { class: 'section', id: 'register', 'aria-busy': 'true' }, [h('h2', {}, 'Assumptions'), PUW.loading('Loading the assumption register…')]));
    var priced = view.filter === 'enforced';
    app.bridge.callTool('review_assumptions', { deal_id: app.dealId, filter: 'all', case: priced ? 'priced' : 'deal' }).then(function (env) {
      if (env.ok && !priced) { app.dealCache().review = env; }
      clear(holder);
      if (!env.ok) {
        if (priced && env.error && env.error.code === 'NOT_PRICED') {
          view.filter = 'needs_review';
          append(holder, PUW.errorNotice(env));
          return;
        }
        append(holder, PUW.errorNotice(env));
        return;
      }
      renderRegister(app, holder, env, view);
    });
  }

  // ── gate, market data, capital plan ────────────────────────────────────
  function renderGate(app, holder, env) {
    clear(holder);
    var last = app.deal && app.deal.ok ? app.deal.last_ingest : null;
    var checkBtn = h('button', { type: 'button', class: 'btn primary', onclick: function () {
      app.act(checkBtn, 'ingest_deal', { deal_id: app.dealId }, 'Checking…').then(function (res) {
        if (!res.ok) { clear(holder); append(holder, [PUW.errorNotice(res)]); return; }
        app.show('assumptions');
      });
    } }, last ? 'Check documents again' : 'Check documents');
    var actions = app.bridge.canCallTools ? h('div', { class: 'actions' }, [checkBtn,
      h('span', { class: 'hint' }, 'Reads the T12 and rent roll, ties them to their own totals and resolves every assumption. Your edits are kept.')]) : null;
    if (!env) {
      append(holder, h('section', { class: 'section', id: 'gate', tabindex: '-1', 'aria-labelledby': 'gate-h' }, [
        h('h2', { id: 'gate-h' }, [app.t('Ingest gate', 'Document check'), last ? PUW.statusBadge(last.gate_status) : null]),
        last ? h('p', {}, ['Last checked ', core.dateTime(last.at), ': ', String(last.units), ' units, ', String(last.assumptions),
          ' assumptions, ', String(last.needing_review), ' flagged for review; validation ', last.validation_status, '.',
          last.blockers && last.blockers.length ? ' Blockers: ' + last.blockers.join(', ') + '.' : '']) :
          h('p', {}, 'The documents have not been checked yet. Add the T12 and rent roll and the analysis period first.'),
        last && last.missing_to_run && last.missing_to_run.length ? h('div', { class: 'notice warn' },
          'Before the deal can run it still needs: ' + last.missing_to_run.join(', ') + ' (deal facts).') : null,
        actions
      ]));
      return;
    }
    if (!env.ok) { append(holder, [PUW.errorNotice(env), actions]); return; }
    var gate = env.gate || {};
    var validation = env.validation || {};
    append(holder, [
      h('section', { class: 'section', id: 'gate', tabindex: '-1', 'aria-labelledby': 'gate-h' }, [
        h('h2', { id: 'gate-h' }, [app.t('Ingest gate', 'Document check'), PUW.statusBadge(gate.status)]),
        h('p', {}, env.summary),
        (gate.blockers || []).length ? h('div', { class: 'notice error', role: 'alert' }, [
          h('h3', {}, 'Blocked: fix these, then check again'),
          h('ul', {}, gate.blockers.map(function (b) {
            return h('li', {}, [h('strong', {}, b.message || b.code), h('div', { class: 'fix' }, ['How to fix it: ', b.fix]), h('div', { class: 'code' }, b.code)]);
          }))]) : null,
        reconciliation(gate),
        (validation.errors || []).length ? h('div', { class: 'notice error' }, [h('h3', {}, 'Validation ' + validation.status),
          h('ul', {}, validation.errors.map(function (e) { return h('li', {}, [e.message, h('div', { class: 'fix' }, ['How to fix it: ', e.fix])]); }))]) :
          h('p', { class: 'small muted' }, ['Validation ', validation.status || '—', validation.warning_count ? ' with ' + validation.warning_count +
            ' warnings (each is a review prompt, not an error).' : '.']),
        (env.missing_to_run || []).length ? h('div', { class: 'notice warn' }, 'Before the deal can run it still needs: ' +
          env.missing_to_run.join(', ') + '. Add them under Deal facts.') : null,
        actions
      ]),
      h('div', { class: 'cols' }, [marketData(env.market_data), capitalPlan(app, env.capital_plan)])
    ]);
  }

  function reconciliation(gate) {
    var rows = [];
    var t12 = gate.t12_reconciliation || {};
    [['opex', 'T12 operating expenses'], ['income', 'T12 income']].forEach(function (pair) {
      var c = t12[pair[0]];
      if (c) { rows.push([pair[1], c.status, core.usd(c.parsed_total), core.usd(c.statement_total), core.pct(c.variance_pct)]); }
    });
    var rr = gate.rent_roll_reconciliation || {};
    [['units', 'Rent roll units', core.int], ['vacant_units', 'Vacant units', core.int],
      ['market_rent_total', 'Market rent (monthly)', core.usd], ['rent_charges_total', 'Rent charges (monthly)', core.usd]].forEach(function (c) {
      var r = rr[c[0]];
      if (r) { rows.push([c[1], r.status, c[2](r.parsed), c[2](r.export), '']); }
    });
    if (!rows.length) { return null; }
    return h('div', { class: 'tablewrap' }, h('table', { class: 'data' }, [
      h('caption', {}, 'Each document checked against its own totals'),
      h('thead', {}, h('tr', {}, [h('th', { scope: 'col' }, 'Check'), h('th', { scope: 'col' }, 'Result'),
        h('th', { scope: 'col', class: 'num' }, 'Parsed'), h('th', { scope: 'col', class: 'num' }, 'Document says'),
        h('th', { scope: 'col', class: 'num' }, 'Variance')])),
      h('tbody', {}, rows.map(function (r) {
        return h('tr', {}, [h('th', { scope: 'row' }, r[0]), h('td', {}, PUW.statusBadge(r[1])), h('td', { class: 'num' }, r[2]),
          h('td', { class: 'num' }, r[3]), h('td', { class: 'num' }, r[4])]);
      }))
    ]));
  }

  function marketData(md) {
    if (!md) { return null; }
    var lines = md.lines || [];
    return h('section', { class: 'section', 'aria-labelledby': 'md-h' }, [
      h('h2', { id: 'md-h' }, ['Market data', h('span', { class: 'badge ' + (md.enabled ? 'flag-ok' : 'flag-muted') }, md.enabled ? 'On' : 'Off')]),
      h('p', {}, md.summary || ''),
      lines.length ? h('details', {}, [h('summary', {}, 'Details'), h('pre', { class: 'small', style: 'white-space:pre-wrap;overflow-wrap:anywhere' }, lines.join('\n'))]) : null,
      h('p', { class: 'hint' }, 'Missing market data never blocks: each slot falls back to deal evidence or a house default and is flagged for review.')
    ]);
  }

  function capitalPlan(app, plan) {
    if (!plan) { return null; }
    var binds = plan.binding_year_1;
    return h('section', { class: 'section', 'aria-labelledby': 'cp-h' }, [
      h('h2', { id: 'cp-h' }, 'Capital plan'),
      h('ol', { style: 'margin:0;padding-left:20px' }, (plan.buckets || []).map(function (b) {
        var term = { 1: 'Immediate capex', 2: 'Recurring capex', 3: 'Replacement reserve' }[b.bucket];
        return h('li', { style: 'margin:0 0 8px' }, [
          h('strong', {}, [app.t(term, b.label)]), ': ', h('span', { class: 'num' }, b.value_display), ' ',
          h('span', { class: 'badge f-' + family(b.source) }, FAMILY[family(b.source)]),
          b.review_required ? h('span', { class: 'badge flag-review' }, 'Needs review') : null,
          binds === b.key ? h('span', { class: 'badge flag-warn' }, 'Binds in year 1') : null,
          h('div', { class: 'small muted' }, b.note)
        ]);
      })),
      h('p', { class: 'hint' }, [plan.rule || '', plan.charge_year_1_per_unit !== undefined ? ' Year-1 charge: ' +
        core.usd(plan.charge_year_1_per_unit) + ' per unit.' : ''])
    ]);
  }

  function family(source) {
    var s = String(source || '');
    if (s === 'analyst') { return 'analyst'; }
    if (s.indexOf('deal:') === 0) { return 'deal'; }
    if (s.indexOf('market:') === 0) { return 'market'; }
    if (s.indexOf('house:') === 0) { return 'house'; }
    if (s === 'engine_fallback') { return 'engine_fallback'; }
    return 'unknown';
  }

  // ── the register ───────────────────────────────────────────────────────
  function renderRegister(app, holder, env, view) {
    clear(holder);
    var rows = env.rows || [];
    var priced = env.case === 'priced';
    var counts = {};
    CHIPS.forEach(function (c) { counts[c.id] = rows.filter(c.test).length; });
    var listHolder = h('div', { id: 'register-rows' });
    var chipBar = h('div', { class: 'chips', role: 'group', 'aria-label': 'Filter the assumptions' }, CHIPS.map(function (c) {
      if (c.id === 'enforced' && !priced && !stepDone(app, 'price')) { return null; }
      return h('button', { type: 'button', class: 'chip', 'aria-pressed': view.filter === c.id ? 'true' : 'false',
        onclick: function () {
          var wasPriced = view.filter === 'enforced';
          view.filter = c.id;
          if ((c.id === 'enforced') !== wasPriced && app.bridge.canCallTools) { loadRegister(app, holder, view); return; }
          renderRegister(app, holder, env, view);
        } }, [c.label, h('span', { class: 'c' }, priced === (c.id === 'enforced') || c.id !== 'enforced' ? String(counts[c.id]) : '')]);
    }));
    var chip = CHIPS.filter(function (c) { return c.id === view.filter; })[0] || CHIPS[0];
    var shown = rows.filter(chip.test);
    var sc = env.summary_counts || {};
    append(holder, h('section', { class: 'section', id: 'register', tabindex: '-1', 'aria-labelledby': 'reg-h' }, [
      h('h2', { id: 'reg-h' }, priced ? 'Assumptions at the solved price' : 'Assumptions'),
      h('p', { class: 'lead' }, [String(sc.total || rows.length), ' assumptions: ', String(sc.from_deal_evidence || 0), ' from the deal\'s documents, ',
        String(sc.from_market || 0), ' from market data, ', String(sc.house_defaults || 0), ' ', app.t('House default', 'house defaults'), ', ',
        String(sc.edited_by_user || 0), ' edited. ', String(sc.needing_review || 0), ' need review.']),
      view.changes ? changesNotice(view.changes) : null,
      chipBar,
      priced ? h('p', { class: 'hint' }, ['Pricing raised these to their ', app.t('House floor', 'house floors'), '. Underwriting (Results) keeps your values.']) : null,
      listHolder
    ]));
    var groups = [];
    var byGroup = {};
    shown.forEach(function (r) {
      if (!byGroup[r.group]) { byGroup[r.group] = []; groups.push(r.group); }
      byGroup[r.group].push(r);
    });
    if (!shown.length) {
      append(listHolder, PUW.empty('Nothing in this filter', chip.id === 'edited' ? 'No assumption has been edited yet.' : 'Try another filter.'));
      return;
    }
    groups.forEach(function (g) {
      append(listHolder, h('div', { class: 'reg-group' }, [
        h('h3', {}, [g, h('span', { class: 'c' }, String(byGroup[g].length))]),
        h('div', { class: 'reg-head', 'aria-hidden': 'true' }, [h('span', {}, 'Assumption'), h('span', {}, 'Value'), h('span', {}, 'Default (source)'), h('span', {}, '')]),
        h('div', { class: 'reg-list' }, byGroup[g].map(function (r) { return rowEl(app, r, priced, function () { reload(app, holder, view); }, view); }))
      ]));
    });
  }

  function reload(app, holder, view) { delete app.dealCache().review; loadRegister(app, holder, view); }

  function changesNotice(changes) {
    if (!changes.ok) { return PUW.errorNotice(changes); }
    return h('div', { class: 'notice ok', role: 'status' }, [
      h('h3', {}, 'Saved'),
      h('ul', { class: 'changes' }, (changes.changed || []).map(function (c) {
        var before = c.before && c.before.value_display, after = c.after && c.after.value_display;
        return h('li', {}, [h('strong', {}, c.label || c.key), ': ', before || '—', ' → ', c.cleared ? 'back to the default (' + (after || '—') + ')' : after || '—',
          c.delta && !c.cleared ? h('span', { class: 'delta' }, ' (' + c.delta + ' vs default)') : null]);
      })),
      h('p', { class: 'small' }, 'Earlier results and prices are now out of date: run the deal again to see the effect.')
    ]);
  }

  function rowEl(app, r, priced, onSaved, view) {
    var fam = r.source_family || family(r.source);
    var def = r.default || {};
    var edit = r.user_edit;
    var guard = r.guardrail;
    var enforced = guard && guard.state === 'enforced';
    var cls = 'reg-row' + (edit ? ' edited' : '') + (enforced ? ' enforced' : '');
    var flags = (r.flags || []).map(function (f) {
      var map = { review: ['Needs review', 'flag-review'], edited: ['Edited', 'flag-edited'], guardrail_warned: ['Below house floor', 'flag-warn'],
        guardrail_enforced: ['Floor enforced', 'flag-enforced'], low_confidence: ['Low confidence', 'flag-warn'], pinned: ['Set by pricing', 'flag-enforced'] };
      var m = map[f] || [f.replace(/_/g, ' '), 'flag-warn'];
      return h('span', { class: 'badge ' + m[1] }, m[0]);
    });
    var why = r.why || {};
    var editorHolder = h('div', {});
    var editBtn = !priced && app.bridge.canCallTools ? h('button', { type: 'button', class: 'btn', 'aria-expanded': 'false',
      'aria-label': 'Edit ' + r.label, onclick: function () {
        var open = editBtn.getAttribute('aria-expanded') === 'true';
        editBtn.setAttribute('aria-expanded', open ? 'false' : 'true');
        clear(editorHolder);
        if (!open) { append(editorHolder, editor(app, r, onSaved, view)); }
      } }, 'Edit') : null;
    return h('div', { class: cls, id: 'row-' + r.key, tabindex: '-1' }, [
      h('div', { class: 'who' }, [h('span', { class: 'label' }, r.label), h('span', { class: 'key' }, r.key), flags]),
      h('div', {}, [h('span', { class: 'cap' }, 'Value'), h('span', { class: 'v' }, r.value_display), r.unit_label ? h('span', { class: 'small muted' }, ' ' + r.unit_label) : null,
        edit ? h('div', {}, [h('span', { class: 'delta' }, edit.delta_display ? edit.delta_display + ' vs default' : ''),
          edit.note ? h('div', { class: 'note' }, '“' + edit.note + '”') : null,
          edit.edited_by ? h('div', { class: 'small muted' }, 'by ' + edit.edited_by + (edit.edited_at ? ', ' + edit.edited_at : '')) : null]) : null,
        enforced ? h('div', { class: 'small' }, 'Raised from ' + guard.original_display + ' to the floor ' + guard.floor_display) : null]),
      h('div', {}, [h('span', { class: 'cap' }, 'Default'), h('span', { class: 'v' }, def.display || '—'),
        h('div', {}, [h('span', { class: 'badge f-' + (def.source_family || fam) }, FAMILY[def.source_family || fam] || 'Other'),
          h('span', { class: 'src' }, def.source || r.source)])]),
      h('div', {}, editBtn),
      h('details', {}, [h('summary', {}, 'Why this default'), h('div', { class: 'body' }, [
        why.rationale ? h('p', {}, why.rationale) : null,
        why.basis || why.default_basis ? h('p', { class: 'muted' }, why.basis || why.default_basis) : null,
        r.breakdown ? h('ul', { class: 'small' }, r.breakdown.map(function (b) { return h('li', {}, [h('strong', {}, b.label + ': '), b.text]); })) : null,
        guard ? h('p', {}, [h('strong', {}, 'House floor: '), guard.basis || '']) : null,
        h('p', { class: 'small muted' }, 'Value source: ' + r.source + (r.confidence ? ' · confidence ' + r.confidence : '') + (r.as_of ? ' · as of ' + r.as_of : ''))
      ])]),
      editorHolder
    ]);
  }

  function editor(app, r, onSaved, view) {
    var id = 'edit-' + r.key.replace(/[^\w-]/g, '_');
    var mode = core.editMode(r);
    var input = mode === 'json' ? h('textarea', { id: id, rows: '3', 'aria-describedby': id + '-hint ' + id + '-err' }, core.editText(r)) :
      h('input', { type: 'text', id: id, value: core.editText(r), inputmode: mode === 'scalar' ? 'decimal' : null,
        class: mode === 'scalar' ? 'num' : null, autocomplete: 'off', 'aria-describedby': id + '-hint ' + id + '-err' });
    var note = h('input', { type: 'text', id: id + '-note', maxlength: '300', autocomplete: 'off', placeholder: 'Why you changed it' });
    var err = h('div', { class: 'err', id: id + '-err', 'aria-live': 'polite' });
    var save = h('button', { type: 'button', class: 'btn primary', onclick: function () {
      var parsed = core.parseEdit(r, input.value);
      if (!parsed.ok) { err.textContent = parsed.error; input.setAttribute('aria-invalid', 'true'); input.focus(); return; }
      input.removeAttribute('aria-invalid'); err.textContent = '';
      var change = {};
      change[r.key] = { value: parsed.value, note: note.value.trim() || 'edited in the assumptions screen' };
      submit(app, save, change, onSaved, view);
    } }, 'Save');
    var reset = r.user_edit ? h('button', { type: 'button', class: 'btn', onclick: function () {
      var change = {};
      change[r.key] = null;
      submit(app, reset, change, onSaved, view);
    } }, 'Back to default') : null;
    setTimeout(function () { input.focus(); }, 0);
    return h('div', { class: 'editor' }, [
      h('div', {}, [h('label', { for: id }, 'New value' + (core.unitKind(r.unit) === 'pct' ? ' (%)' : core.unitKind(r.unit) === 'usd' ? ' ($)' : '')), input,
        h('div', { class: 'small muted', id: id + '-hint' }, core.editHint(r)), err]),
      h('div', {}, [h('label', { for: id + '-note' }, 'Note'), note]),
      h('div', { class: 'actions', style: 'margin:0' }, [save, reset])
    ]);
  }

  /** Ask for the editor's name once per session, then call edit_assumptions. */
  function submit(app, button, edits, onSaved, view) {
    withEditor(app, function (name) {
      app.act(button, 'edit_assumptions', { deal_id: app.dealId, edits: edits, editor: name }, 'Saving…').then(function (env) {
        view.changes = env;
        onSaved();
      });
    });
  }

  function withEditor(app, then) {
    if (app.editor) { then(app.editor); return; }
    var input = h('input', { type: 'text', id: 'editor-name', autocomplete: 'name', maxlength: '100' });
    var err = h('div', { class: 'err', 'aria-live': 'polite' });
    var previous = root.document.activeElement;
    var backdrop;
    function close() { root.document.body.removeChild(backdrop); if (previous && previous.focus) { previous.focus(); } }
    var form = h('form', { class: 'dialog', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'editor-h', onsubmit: function (e) {
      e.preventDefault();
      var name = input.value.trim();
      if (!name) { err.textContent = 'Enter your name.'; input.focus(); return; }
      app.rememberEditor(name);
      close();
      then(name);
    } }, [
      h('h2', { id: 'editor-h' }, 'Who is making this edit?'),
      h('p', { class: 'hint' }, 'Every edit is stamped with your name and the date. You are asked once per session.'),
      h('label', { for: 'editor-name' }, 'Your name'), input, err,
      h('div', { class: 'actions' }, [h('button', { type: 'submit', class: 'btn primary' }, 'Continue'),
        h('button', { type: 'button', class: 'btn', onclick: close }, 'Cancel')])
    ]);
    backdrop = h('div', { class: 'dialog-backdrop', onkeydown: function (e) { if (e.key === 'Escape') { close(); } } }, form);
    root.document.body.appendChild(backdrop);
    input.focus();
  }

  PUW.screens.assumptions = { title: 'Check and assumptions', render: render };
})(window);
