/* How a screen talks to the tools. Two transports with one interface:

   - MCP Apps (the screen runs in a host's sandboxed iframe): JSON-RPC 2.0 over postMessage per the
     MCP Apps extension (io.modelcontextprotocol/ui, 2026-01-26): ui/initialize, then
     ui/notifications/initialized; the host sends ui/notifications/tool-input and tool-result; the
     screen calls tools/call (when the host offers serverTools), ui/message, ui/open-link,
     ui/update-model-context and reports ui/notifications/size-changed.
   - Browser (plat-uw serve): POST /api/tools/<name> with a JSON body returns the same envelope the
     MCP tool returns; files upload as multipart to /api/upload.

   bridge.callTool(name, args) always resolves to the tool's envelope ({ok, summary, next_step, ...}
   or {ok: false, error: {code, message, fix}}); it never rejects. */
(function (root) {
  'use strict';
  var core = root.PUWCore;
  var PROTOCOL_VERSION = '2026-01-26';

  function failure(code, message, fix) {
    return { ok: false, summary: message, next_step: fix, error: { code: code, message: message, fix: fix } };
  }

  // ── browser transport ──────────────────────────────────────────────────
  function browserBridge(config) {
    var headers = { 'Content-Type': 'application/json', 'X-Plat-UW': '1' };
    function readJson(response) {
      return response.text().then(function (text) {
        try { return JSON.parse(text); } catch (e) { return null; }
      }).then(function (body) {
        if (body && typeof body === 'object' && ('ok' in body)) { return body; }
        if (response.status === 401 || response.status === 403) {
          return failure('NOT_AUTHORIZED', 'This server did not accept the request (' + response.status + ').',
            'Reload the page and sign in again.');
        }
        return failure('HTTP_' + response.status, 'The server answered ' + response.status + '.',
          'Reload the page; if it persists, check the terminal running plat-uw serve.');
      });
    }
    function offline() {
      return failure('OFFLINE', 'The app could not reach its server.',
        'Check that plat-uw serve is still running, then try again.');
    }
    return {
      mode: 'browser',
      canCallTools: true,
      canMessage: false,
      canOpenLinks: true,
      hostName: 'browser',
      connect: function () { return Promise.resolve({ mode: 'browser' }); },
      callTool: function (name, args) {
        return fetch('api/tools/' + encodeURIComponent(name), {
          method: 'POST', headers: headers, credentials: 'same-origin', body: JSON.stringify(args || {})
        }).then(readJson, offline);
      },
      upload: function (dealId, kind, file) {
        var form = new FormData();
        form.append('deal_id', dealId);
        form.append('kind', kind);
        form.append('file', file, file.name);
        return fetch('api/upload', {
          method: 'POST', headers: { 'X-Plat-UW': '1' }, credentials: 'same-origin', body: form
        }).then(readJson, offline);
      },
      fileLink: function (dealId, file) {
        return 'files/' + encodeURIComponent(dealId) + '/' + String(file.path).split('/').map(encodeURIComponent).join('/');
      },
      openLink: function (url) { root.open(url, '_blank', 'noopener'); return Promise.resolve(true); },
      sendMessage: function () { return Promise.resolve(false); },
      updateContext: function () { return Promise.resolve(false); },
      on: function () {},
      requestDisplayMode: function () { return Promise.resolve(null); },
      displayModes: []
    };
  }

  // ── MCP Apps transport ─────────────────────────────────────────────────
  function mcpBridge(config) {
    var parent = root.parent;
    var nextId = 1;
    var pending = {};
    var handlers = { toolInput: [], toolResult: [], toolCancelled: [], context: [], teardown: [] };
    var bridge = {
      mode: 'pending', canCallTools: false, canMessage: false, canOpenLinks: false, canUpdateContext: false,
      hostName: '', hostContext: {}, hostCapabilities: {}, displayModes: []
    };

    function post(message) { parent.postMessage(message, '*'); }
    function request(method, params, timeoutMs) {
      var id = nextId++;
      return new Promise(function (resolve, reject) {
        var timer = timeoutMs ? setTimeout(function () {
          delete pending[id];
          reject(new Error('timeout'));
        }, timeoutMs) : null;
        pending[id] = { resolve: resolve, reject: reject, timer: timer };
        post({ jsonrpc: '2.0', id: id, method: method, params: params || {} });
      });
    }
    function notify(method, params) { post({ jsonrpc: '2.0', method: method, params: params || {} }); }
    function emit(kind, payload) { handlers[kind].forEach(function (fn) { try { fn(payload); } catch (e) { /* keep going */ } }); }

    root.addEventListener('message', function (event) {
      if (event.source !== parent) { return; }
      var msg = event.data;
      if (!msg || msg.jsonrpc !== '2.0') { return; }
      if (msg.id !== undefined && msg.id !== null && !msg.method) {
        var entry = pending[msg.id];
        if (!entry) { return; }
        delete pending[msg.id];
        if (entry.timer) { clearTimeout(entry.timer); }
        if (msg.error) { entry.reject(new Error((msg.error && msg.error.message) || 'request failed')); }
        else { entry.resolve(msg.result); }
        return;
      }
      var params = msg.params || {};
      switch (msg.method) {
        case 'ui/notifications/tool-input': emit('toolInput', params.arguments || {}); break;
        case 'ui/notifications/tool-input-partial': break;
        case 'ui/notifications/tool-result': emit('toolResult', core.toolEnvelope(params)); break;
        case 'ui/notifications/tool-cancelled': emit('toolCancelled', params.reason || 'cancelled'); break;
        case 'ui/notifications/host-context-changed':
          bridge.hostContext = Object.assign({}, bridge.hostContext, params);
          emit('context', bridge.hostContext);
          break;
        case 'ui/resource-teardown':
          emit('teardown', params.reason || '');
          post({ jsonrpc: '2.0', id: msg.id, result: {} });
          break;
        case 'ping': post({ jsonrpc: '2.0', id: msg.id, result: {} }); break;
        default:
          if (msg.id !== undefined && msg.id !== null) {
            post({ jsonrpc: '2.0', id: msg.id, error: { code: -32601, message: 'Method not found' } });
          }
      }
    });

    function watchSize() {
      if (!root.ResizeObserver) { return; }
      var last = '', timer = null;
      var observer = new root.ResizeObserver(function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          var el = root.document.documentElement;
          var width = Math.ceil(el.scrollWidth), height = Math.ceil(el.scrollHeight);
          var key = width + 'x' + height;
          if (key === last) { return; }
          last = key;
          notify('ui/notifications/size-changed', { width: width, height: height });
        }, 60);
      });
      observer.observe(root.document.body);
    }

    bridge.connect = function () {
      if (!parent || parent === root) {
        bridge.mode = 'standalone';
        return Promise.resolve({ mode: 'standalone' });
      }
      return request('ui/initialize', {
        appInfo: { name: 'plat-underwriting-' + config.screen, version: config.version },
        appCapabilities: { availableDisplayModes: ['inline', 'fullscreen'] },
        protocolVersion: PROTOCOL_VERSION
      }, 2500).then(function (result) {
        result = result || {};
        var caps = result.hostCapabilities || {};
        bridge.mode = 'mcp';
        bridge.hostCapabilities = caps;
        bridge.hostContext = result.hostContext || {};
        bridge.hostName = (result.hostInfo && result.hostInfo.name) || 'host';
        bridge.canCallTools = !!caps.serverTools;
        bridge.canMessage = !!caps.message;
        bridge.canOpenLinks = !!caps.openLinks;
        bridge.canUpdateContext = !!caps.updateModelContext;
        bridge.displayModes = bridge.hostContext.availableDisplayModes || [];
        notify('ui/notifications/initialized', {});
        watchSize();
        emit('context', bridge.hostContext);
        return { mode: 'mcp' };
      }, function () {
        bridge.mode = 'standalone';
        return { mode: 'standalone' };
      });
    };
    bridge.on = function (kind, fn) { if (handlers[kind]) { handlers[kind].push(fn); } };
    bridge.callTool = function (name, args) {
      if (!bridge.canCallTools) {
        return Promise.resolve(failure('HOST_CANNOT_CALL_TOOLS', 'This assistant cannot run tools from the screen.',
          'Ask the assistant to call ' + name + ' for you.'));
      }
      return request('tools/call', { name: name, arguments: args || {} }).then(core.toolEnvelope, function (err) {
        return failure('HOST_REFUSED', 'The assistant did not run ' + name + (err && err.message ? ' (' + err.message + ')' : '') + '.',
          'Ask the assistant to call ' + name + ' for you.');
      });
    };
    bridge.upload = function (dealId, kind, file) {
      return new Promise(function (resolve) {
        var reader = new root.FileReader();
        reader.onload = function () {
          var text = String(reader.result || '');
          resolve(bridge.callTool('add_document', {
            deal_id: dealId, kind: kind, filename: file.name, content_base64: text.slice(text.indexOf(',') + 1)
          }));
        };
        reader.onerror = function () { resolve(failure('READ_FAILED', 'The file could not be read.', 'Choose the file again.')); };
        reader.readAsDataURL(file);
      });
    };
    bridge.fileLink = function (dealId, file) { return file.url || null; };
    bridge.openLink = function (url) {
      if (!bridge.canOpenLinks) { return Promise.resolve(false); }
      return request('ui/open-link', { url: url }).then(function () { return true; }, function () { return false; });
    };
    bridge.sendMessage = function (text) {
      if (!bridge.canMessage) { return Promise.resolve(false); }
      return request('ui/message', { role: 'user', content: [{ type: 'text', text: text }] })
        .then(function (r) { return !(r && r.isError); }, function () { return false; });
    };
    bridge.updateContext = function (text) {
      if (!bridge.canUpdateContext) { return Promise.resolve(false); }
      return request('ui/update-model-context', { content: [{ type: 'text', text: text }] })
        .then(function () { return true; }, function () { return false; });
    };
    bridge.requestDisplayMode = function (mode) {
      if (bridge.displayModes.indexOf(mode) === -1) { return Promise.resolve(null); }
      return request('ui/request-display-mode', { mode: mode }).then(function (r) { return r && r.mode; }, function () { return null; });
    };
    return bridge;
  }

  root.PUWBridge = {
    create: function (config) { return config.mode === 'browser' ? browserBridge(config) : mcpBridge(config); },
    failure: failure,
    PROTOCOL_VERSION: PROTOCOL_VERSION
  };
})(window);
