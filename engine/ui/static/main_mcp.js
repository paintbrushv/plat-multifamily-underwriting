/* Boot one screen as an MCP App: connect to the host, wait for the tool's input and result, then
   render. Hosts without MCP Apps never load this page (their tools return the same text as before);
   hosts that render it but cannot call tools from it get a read-only screen with "ask the assistant"
   hints. */
(function (root) {
  'use strict';
  var PUW = root.PUW, core = root.PUWCore, h = PUW.h, append = PUW.append, clear = PUW.clear;
  var config = JSON.parse(root.document.getElementById('puw-config').textContent);
  var bridge = root.PUWBridge.create(config);
  var app = new PUW.App({ root: root.document.getElementById('app'), bridge: bridge, config: config, screens: PUW.screens, screen: config.screen });
  var screen = PUW.screens[config.screen];
  var FILTER_CHIP = { needs_review: 'needs_review', edited: 'edited', defaults: 'defaults', enforced: 'enforced', all: 'all' };
  var toolName = null;

  app.setTitle(screen.title);
  append(app.body, PUW.loading('Waiting for the assistant…'));

  bridge.on('context', function (context) {
    PUW.applyHostContext(context);
    var info = context && context.toolInfo && context.toolInfo.tool;
    if (info && info.name) { toolName = info.name; }
  });
  bridge.on('toolInput', function (args) {
    if (args && typeof args.deal_id === 'string') { app.selectDeal(args.deal_id, true); app.setTitle(screen.title); }
    clear(app.body);
    append(app.body, PUW.loading('The assistant is running ' + (toolName || 'the step') + '…'));
  });
  bridge.on('toolCancelled', function (reason) {
    clear(app.body);
    append(app.body, h('div', { class: 'notice warn', role: 'status' }, ['The assistant stopped this step (', String(reason), '). Ask it to run the step again.']));
  });
  bridge.on('toolResult', function (env) {
    var tool = toolName || core.inferTool(env);
    if (env && env.deal_id) { app.selectDeal(env.deal_id, true); }
    app.record(tool, env);
    if (tool === 'review_assumptions' && env.ok) { app.focusChip = env.case === 'priced' ? 'enforced' : (FILTER_CHIP[env.filter] || 'needs_review'); }
    app.refreshDeal().then(function () {
      app.show(config.screen);
      if (env && env.ok === false && !app.body.querySelector('.notice.error')) {
        app.body.insertBefore(PUW.errorNotice(env), app.body.firstChild);
      }
      app.announce(env && env.summary ? env.summary : 'Updated.');
    });
  });
  bridge.connect().then(function (info) {
    if (info.mode !== 'standalone') { return; }
    clear(app.body);
    append(app.body, h('section', { class: 'section' }, [
      h('h2', {}, 'This screen runs inside an AI assistant'),
      h('p', {}, 'It is an MCP App: an assistant that supports MCP Apps shows it next to the ' + screen.title.toLowerCase() +
        ' step. To use the screens on their own, run plat-uw serve and open the browser app.')
    ]));
  });
})(window);
