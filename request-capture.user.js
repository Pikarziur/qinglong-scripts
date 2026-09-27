// ==UserScript==
// @name         请求抓取器（签到 / 日常任务）
// @namespace    https://workbuddy.cn/
// @version      1.0.1
// @description  抓取网页 XHR/fetch 请求，自动高亮签到/日常任务类接口，支持标记、复制为 curl、导出 JSON（可存到桌面）。
// @author       WorkBuddy
// @match        *://*/*
// @grant        none
// @run-at       document-start
// ==/UserScript==

(function () {
  'use strict';

  // ===== 配置区 =====
  // 命中这些关键词的 URL 会被自动标记为「疑似签到/任务」(带 ★)
  const KEYWORDS = ['签到', 'sign', 'checkin', 'check', 'task', 'daily', 'clock', 'attendance', 'collect'];
  const MAX_BODY = 200 * 1024; // 响应体最多存 200KB，超出截断

  const store = []; // 抓到的请求记录

  // ===== 工具 =====
  function matchesKeyword(text) {
    const t = (text || '').toLowerCase();
    return KEYWORDS.some(k => t.includes(k.toLowerCase()));
  }
  function safeStringify(v) {
    if (v === undefined || v === null) return '';
    if (typeof v === 'string') return v;
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }
  function fmtHeaders(h) {
    if (!h) return '';
    if (typeof h === 'string') return h;
    return Object.entries(h).map(([k, v]) => k + ': ' + v).join('\n');
  }

  // ===== 钩子：XMLHttpRequest =====
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;

  XMLHttpRequest.prototype.open = function (method, url) {
    this.__rc_method = method;
    this.__rc_url = (typeof url === 'string') ? url : String(url);
    this.__rc_reqHeaders = {};
    this.__rc_reqBody = '';
    this.__rc_done = false;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    if (this.__rc_reqHeaders) this.__rc_reqHeaders[k] = v;
    return origSetHeader.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (body !== undefined && body !== null) this.__rc_reqBody = safeStringify(body);
    const self = this;
    const onDone = function () {
      if (self.__rc_done) return;
      self.__rc_done = true;
      let respBody = '';
      try { respBody = (typeof self.responseText === 'string') ? self.responseText : ''; } catch (e) {}
      if (respBody.length > MAX_BODY) respBody = respBody.slice(0, MAX_BODY) + '\n...(已截断)';
      pushRecord({
        method: self.__rc_method,
        url: self.__rc_url,
        reqHeaders: self.__rc_reqHeaders || {},
        reqBody: self.__rc_reqBody || '',
        status: self.status,
        respHeaders: (self.getAllResponseHeaders ? self.getAllResponseHeaders() : '') || '',
        respBody: respBody
      });
    };
    this.addEventListener('readystatechange', function () {
      if (self.readyState === 4) onDone();
    });
    this.addEventListener('load', onDone);
    this.addEventListener('error', onDone);
    return origSend.apply(this, arguments);
  };

  // ===== 钩子：fetch =====
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    const url = (typeof input === 'string') ? input : (input && input.url) || String(input);
    const method = (init.method || (input && input.method) || 'GET').toUpperCase();
    let reqHeaders = {};
    const readHeaders = function (h) {
      if (!h) return;
      if (typeof h.forEach === 'function') { try { h.forEach((v, k) => { reqHeaders[k] = v; }); } catch (e) {} }
      else if (h.entries) { try { for (const [k, v] of h.entries()) reqHeaders[k] = v; } catch (e) {} }
      else { reqHeaders = Object.assign({}, h); }
    };
    readHeaders(init.headers);
    if (!init.headers && input && input.headers) readHeaders(input.headers);
    const reqBody = safeStringify(init.body || (input && input.body) || '');

    return origFetch.apply(this, arguments).then(function (resp) {
      const cloned = resp.clone();
      cloned.text().then(function (text) {
        let respBody = text || '';
        if (respBody.length > MAX_BODY) respBody = respBody.slice(0, MAX_BODY) + '\n...(已截断)';
        pushRecord({
          method: method,
          url: url,
          reqHeaders: reqHeaders,
          reqBody: reqBody,
          status: resp.status,
          respHeaders: '',
          respBody: respBody
        });
      }).catch(function () {});
      return resp;
    });
  };

  function pushRecord(r) {
    store.push({
      id: store.length + 1,
      time: Date.now(),
      method: r.method,
      url: r.url,
      reqHeaders: r.reqHeaders,
      reqBody: r.reqBody,
      status: r.status,
      respHeaders: r.respHeaders,
      respBody: r.respBody,
      marked: false,
      auto: matchesKeyword(r.url)
    });
    if (typeof render === 'function') render();
  }

  // ===== UI =====
  let panel = null, listEl = null, countEl = null, searchEl = null, bodyEl = null, mounted = false, hidden = false;

  const CSS = `
#rc-capture-panel{position:fixed;right:12px;bottom:12px;width:440px;max-height:72vh;z-index:2147483647;
  background:#1e1e2e;color:#e6e6e6;font:13px/1.4 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  border:1px solid #3a3a52;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.5);display:flex;flex-direction:column;overflow:hidden}
#rc-capture-panel *{box-sizing:border-box}
.rc-head{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#2a2a40;cursor:move}
.rc-title{font-weight:700;color:#9ad}
.rc-count{background:#3a6;color:#fff;border-radius:10px;padding:0 8px;font-size:12px}
.rc-min{margin-left:auto;background:#444;border:0;color:#fff;border-radius:6px;width:26px;height:22px;cursor:pointer}
.rc-body{display:flex;flex-direction:column;min-height:0;flex:1}
.rc-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:8px 10px;border-bottom:1px solid #3a3a52}
.rc-search{flex:1;min-width:140px;background:#15151f;color:#eee;border:1px solid #444;border-radius:6px;padding:5px 8px}
.rc-bar label{font-size:12px;color:#bbb;display:flex;align-items:center;gap:3px;cursor:pointer}
.rc-list{overflow:auto;padding:6px 8px;flex:1;min-height:80px}
.rc-empty{color:#888;padding:14px;text-align:center}
.rc-item{border:1px solid #34344a;border-radius:8px;margin-bottom:6px;background:#23233a}
.rc-item.rc-marked{border-color:#3a6;border-left:3px solid #3a6}
.rc-item.rc-auto{border-left:3px solid #c93}
.rc-row{display:flex;align-items:center;gap:6px;padding:6px 8px;flex-wrap:wrap}
.rc-star{color:#fc6;font-size:14px;width:12px;text-align:center}
.rc-method{font-weight:700;color:#6cf;background:#11304a;border-radius:4px;padding:1px 6px;font-size:11px}
.rc-status{color:#9a9;font-size:11px}
.rc-url{flex:1;min-width:120px;color:#ddd;word-break:break-all;font-size:12px}
.rc-url:hover{color:#9cf}
.rc-curl,.rc-view{background:#345;border:0;color:#cdf;border-radius:5px;padding:2px 8px;cursor:pointer;font-size:11px}
.rc-curl:hover,.rc-view:hover{background:#467}
.rc-detail{margin:0;padding:8px;background:#15151f;color:#bdf;white-space:pre-wrap;word-break:break-all;font-size:11px;max-height:300px;overflow:auto;border-top:1px solid #34344a}
.rc-actions{display:flex;flex-wrap:wrap;gap:6px;padding:8px 10px;border-top:1px solid #3a3a52}
.rc-actions button{background:#345;border:0;color:#cdf;border-radius:6px;padding:5px 9px;cursor:pointer;font-size:12px}
.rc-actions button:hover{background:#467}
.rc-tip{font-size:11px;color:#889;padding:0 10px 8px}
`;

  function buildPanel() {
    const style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);

    panel = document.createElement('div');
    panel.id = 'rc-capture-panel';
    panel.innerHTML =
      '<div class="rc-head">' +
        '<span class="rc-title">请求抓取器</span>' +
        '<span class="rc-count">0</span>' +
        '<button class="rc-min" title="隐藏/显示">_</button>' +
      '</div>' +
      '<div class="rc-body">' +
        '<div class="rc-bar">' +
          '<input class="rc-search" placeholder="过滤 URL/关键词，如 签到 / sign">' +
          '<label><input type="checkbox" class="rc-onlymark"> 仅标记</label>' +
          '<label><input type="checkbox" class="rc-onlyauto"> 仅★</label>' +
          '<label><input type="checkbox" class="rc-cookie"> 导出带Cookie</label>' +
        '</div>' +
        '<div class="rc-list"></div>' +
        '<div class="rc-actions">' +
          '<button class="rc-markauto">标记全部★</button>' +
          '<button class="rc-unmark">清空标记</button>' +
          '<button class="rc-export-sel">导出选中JSON</button>' +
          '<button class="rc-export-all">导出全部JSON</button>' +
          '<button class="rc-curl-sel">复制选中curl</button>' +
          '<button class="rc-clear">清空列表</button>' +
        '</div>' +
        '<div class="rc-tip">勾选行前的 ☑ 标记为「签到/任务」；带 ★ 的是自动命中关键词的请求。</div>' +
      '</div>';
    document.body.appendChild(panel);

    listEl = panel.querySelector('.rc-list');
    countEl = panel.querySelector('.rc-count');
    searchEl = panel.querySelector('.rc-search');
    bodyEl = panel.querySelector('.rc-body');

    panel.querySelector('.rc-min').addEventListener('click', function () {
      hidden = !hidden;
      bodyEl.style.display = hidden ? 'none' : 'flex';
    });
    searchEl.addEventListener('input', render);
    panel.querySelector('.rc-onlymark').addEventListener('change', render);
    panel.querySelector('.rc-onlyauto').addEventListener('change', render);

    listEl.addEventListener('click', function (e) {
      const t = e.target;
      const id = t.dataset && t.dataset.id ? +t.dataset.id : 0;
      if (t.classList.contains('rc-curl')) { copyText(toCurl(byId(id), panel.querySelector('.rc-cookie').checked)); }
      else if (t.classList.contains('rc-view')) {
        const det = t.closest('.rc-item').querySelector('.rc-detail');
        if (det.style.display === 'none') {
          det.textContent = detailText(byId(id));
          det.style.display = 'block';
        } else { det.style.display = 'none'; }
      }
    });
    listEl.addEventListener('change', function (e) {
      if (e.target.classList.contains('rc-chk')) {
        const r = byId(+e.target.dataset.id);
        if (r) r.marked = e.target.checked;
        render();
      }
    });

    panel.querySelector('.rc-markauto').addEventListener('click', function () {
      store.forEach(r => { if (r.auto) r.marked = true; }); render();
    });
    panel.querySelector('.rc-unmark').addEventListener('click', function () {
      store.forEach(r => r.marked = false); render();
    });
    panel.querySelector('.rc-export-sel').addEventListener('click', function () {
      exportJSON(store.filter(r => r.marked), 'requests_marked');
    });
    panel.querySelector('.rc-export-all').addEventListener('click', function () {
      exportJSON(store, 'requests_all');
    });
    panel.querySelector('.rc-curl-sel').addEventListener('click', function () {
      const withCookie = panel.querySelector('.rc-cookie').checked;
      const lines = store.filter(r => r.marked).map(r => toCurl(r, withCookie));
      copyText(lines.join('\n\n'));
    });
    panel.querySelector('.rc-clear').addEventListener('click', function () {
      store.length = 0; render();
    });

    // 拖拽移动
    makeDraggable(panel.querySelector('.rc-head'), panel);
    render();
  }

  function byId(id) { return store.find(r => r.id === id); }

  function render() {
    if (!listEl) return;
    const q = (searchEl.value || '').toLowerCase();
    const onlyMark = panel.querySelector('.rc-onlymark').checked;
    const onlyAuto = panel.querySelector('.rc-onlyauto').checked;
    const filtered = store.filter(function (r) {
      if (onlyMark && !r.marked) return false;
      if (onlyAuto && !r.auto) return false;
      if (q && !(r.url.toLowerCase().includes(q) || safeStringify(r.reqBody).toLowerCase().includes(q))) return false;
      return true;
    }).slice().reverse();

    countEl.textContent = store.length;
    if (!filtered.length) {
      listEl.innerHTML = '<div class="rc-empty">暂无匹配请求。打开目标网页操作后，这里的请求会自动出现。</div>';
      return;
    }
    listEl.innerHTML = '';
    filtered.forEach(function (r) {
      const item = document.createElement('div');
      item.className = 'rc-item' + (r.marked ? ' rc-marked' : '') + (r.auto ? ' rc-auto' : '');
      const star = r.auto ? '★' : '';
      const status = r.status || '';
      const urlShort = r.url.length > 90 ? r.url.slice(0, 90) + '…' : r.url;
      item.innerHTML =
        '<div class="rc-row">' +
          '<input type="checkbox" class="rc-chk"' + (r.marked ? ' checked' : '') + ' data-id="' + r.id + '">' +
          '<span class="rc-star">' + star + '</span>' +
          '<span class="rc-method">' + r.method + '</span>' +
          '<span class="rc-status">' + status + '</span>' +
          '<span class="rc-url" title="' + (r.url || '').replace(/"/g, '&quot;') + '">' + urlShort + '</span>' +
          '<button class="rc-curl" data-id="' + r.id + '">curl</button>' +
          '<button class="rc-view" data-id="' + r.id + '">详情</button>' +
        '</div>' +
        '<pre class="rc-detail" style="display:none"></pre>';
      listEl.appendChild(item);
    });
  }

  function detailText(r) {
    return '时间: ' + new Date(r.time).toLocaleString() + '\n' +
      '方法: ' + r.method + '\n' +
      '状态: ' + (r.status || '') + '\n' +
      'URL: ' + r.url + '\n\n' +
      '--- 请求头 ---\n' + fmtHeaders(r.reqHeaders) + '\n\n' +
      '--- 请求体 ---\n' + (r.reqBody || '(无)') + '\n\n' +
      '--- 响应头 ---\n' + (r.respHeaders || '(fetch 无)') + '\n\n' +
      '--- 响应体(截断) ---\n' + (r.respBody || '(无)');
  }

  function toCurl(r, withCookie) {
    let cmd = "curl -X " + r.method + " '" + r.url + "'";
    const h = Object.assign({}, r.reqHeaders);
    if (withCookie && !h['Cookie'] && !h['cookie']) h['Cookie'] = document.cookie;
    for (const [k, v] of Object.entries(h)) {
      cmd += " -H '" + (k + ': ' + v).replace(/'/g, "'\\''") + "'";
    }
    if (r.reqBody) {
      const b = r.reqBody.replace(/'/g, "'\\''");
      cmd += " --data '" + b + "'";
    }
    return cmd;
  }

  // ===== 导出 / 复制 =====
  function buildExport(arr, name) {
    return JSON.stringify({
      exportedAt: new Date().toISOString(),
      note: '由「请求抓取器」导出；marked 字段 true 的为签到/日常任务类请求',
      count: arr.length,
      requests: arr.map(function (r) {
        return {
          id: r.id, marked: r.marked, auto: r.auto,
          method: r.method, url: r.url, status: r.status,
          reqHeaders: r.reqHeaders, reqBody: r.reqBody,
          respHeaders: r.respHeaders, respBody: r.respBody
        };
      })
    }, null, 2);
  }

  function tsName(base) {
    const d = new Date();
    const p = n => String(n).padStart(2, '0');
    return base + '_' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
      '_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.json';
  }

  function download(filename, text) {
    const blob = new Blob([text], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  async function saveToDisk(filename, text) {
    // 优先用文件选择器，可直接存到桌面文件夹；不支持则退回普通下载
    if (window.showSaveFilePicker) {
      try {
        const handle = await window.showSaveFilePicker({ suggestedName: filename, types: [{ description: 'JSON', accept: { 'application/json': ['.json'] } }] });
        const w = await handle.createWritable();
        await w.write(text);
        await w.close();
        flash('已保存到：' + filename + '（请在弹窗里选择桌面）');
        return;
      } catch (e) {
        if (e && e.name === 'AbortError') return; // 用户取消
      }
    }
    download(filename, text);
    flash('已下载：' + filename + '（浏览器下载目录，可移入桌面）');
  }

  function exportJSON(arr, base) {
    if (!arr.length) { flash('没有可导出的记录'); return; }
    saveToDisk(tsName(base), buildExport(arr, base));
  }

  function copyText(text) {
    if (!text) { flash('内容为空'); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { flash('已复制到剪贴板'); },
        function () { fallbackCopy(text); });
    } else { fallbackCopy(text); }
  }
  function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); flash('已复制到剪贴板'); } catch (e) { flash('复制失败，请手动选择'); }
    ta.remove();
  }

  let flashTimer = null;
  function flash(msg) {
    let el = document.getElementById('rc-flash');
    if (!el) {
      el = document.createElement('div');
      el.id = 'rc-flash';
      el.style.cssText = 'position:fixed;left:50%;top:18px;transform:translateX(-50%);z-index:2147483647;' +
        'background:#2a2a40;color:#9f9;padding:8px 14px;border-radius:8px;font:13px sans-serif;border:1px solid #3a6';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.display = 'block';
    clearTimeout(flashTimer);
    flashTimer = setTimeout(function () { el.style.display = 'none'; }, 2200);
  }

  function makeDraggable(handle, target) {
    let sx = 0, sy = 0, ox = 0, oy = 0, drag = false;
    handle.addEventListener('mousedown', function (e) {
      drag = true; sx = e.clientX; sy = e.clientY;
      const rect = target.getBoundingClientRect();
      ox = rect.left; oy = rect.top;
      document.addEventListener('mousemove', mm);
      document.addEventListener('mouseup', mu);
      e.preventDefault();
    });
    function mm(e) {
      if (!drag) return;
      target.style.left = (ox + e.clientX - sx) + 'px';
      target.style.top = (oy + e.clientY - sy) + 'px';
      target.style.right = 'auto'; target.style.bottom = 'auto';
    }
    function mu() { drag = false; document.removeEventListener('mousemove', mm); document.removeEventListener('mouseup', mu); }
  }

  // ===== 启动 =====
  function ensureUI() {
    if (mounted) return;
    if (!document.body) { setTimeout(ensureUI, 200); return; }
    mounted = true;
    buildPanel();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ensureUI);
  } else { ensureUI(); }

  // 暴露给控制台调试
  window.__rcStore = store;
})();
