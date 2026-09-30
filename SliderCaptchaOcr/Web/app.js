(function () {
  var disk = [];
  var busy = false;
  var hoverCell = null;
  var dismissed = {};
  var tabsOpen = false;
  var LEAVE_MS = 480;
  var syncLocal = false;
  var showTraceAll = false;
  var showTraceIds = {};
  var traceOffIds = {};
  var traceCache = {};
  var traceGen = 0;
  var memRename = {};
  try { syncLocal = localStorage.getItem('sliderOcr.syncLocal') === '1'; } catch (e) {}
  try {
    showTraceIds = JSON.parse(localStorage.getItem('sliderOcr.showTraceIds') || '{}') || {};
  } catch (e) { showTraceIds = {}; }
  try {
    traceOffIds = JSON.parse(localStorage.getItem('sliderOcr.traceOffIds') || '{}') || {};
  } catch (e) { traceOffIds = {}; }

  function isGone(item) {
    if (!item) return true;
    if (dismissed[item.id]) return true;
    if (item.diskId && dismissed[item.diskId]) return true;
    return false;
  }
  function cardTime(item, name) {
    var created = item && item.created;
    if (created) return String(created);
    var id = String((item && (item.id || item.group)) || name || '');
    var m = id.match(/^Card_(\d{8})_(\d{6})/);
    if (m) {
      var d = m[1], t = m[2];
      return d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8)
        + 'T' + t.slice(0, 2) + ':' + t.slice(2, 4) + ':' + t.slice(4, 6);
    }
    m = id.match(/^Card_(\d{12,})$/);
    if (m) {
      var ts = Number(m[1]);
      if (ts > 1e12) ts = ts / 1000;
      if (isFinite(ts) && ts > 0) {
        var dt = new Date(ts * 1000);
        if (!isNaN(dt.getTime())) {
          var p = function (n) { return (n < 10 ? '0' : '') + n; };
          return dt.getFullYear() + '-' + p(dt.getMonth() + 1) + '-' + p(dt.getDate())
            + 'T' + p(dt.getHours()) + ':' + p(dt.getMinutes()) + ':' + p(dt.getSeconds());
        }
      }
    }
    return id;
  }
  function splitDisk() {
    var groups = {};
    disk.forEach(function (r) {
      if (isGone(r)) return;
      var g = r.group || r.id || '未分组';
      (groups[g] = groups[g] || []).push(r);
    });
    var names = Object.keys(groups);
    names.sort(function (a, b) {
      var ia = groups[a][0], ib = groups[b][0];
      var ca = /^Card_/.test(a) || (ia && /^Card_/.test(ia.id || '')) ? 1 : 0;
      var cb = /^Card_/.test(b) || (ib && /^Card_/.test(ib.id || '')) ? 1 : 0;
      if (ca !== cb) return ca - cb;
      if (ca) {
        var ta = cardTime(ia, a), tb = cardTime(ib, b);
        if (ta < tb) return -1;
        if (ta > tb) return 1;
      }
      if (a < b) return -1;
      if (a > b) return 1;
      return 0;
    });
    return { groups: groups, names: names };
  }
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function asDataURL(s) {
    s = (s || '').trim();
    if (!s) return '';
    if (s.indexOf('data:') === 0) return s;
    if (/^https?:\/\//i.test(s)) return '';
    return 'data:image/png;base64,' + s.replace(/\s+/g, '');
  }
  function looksUrl(s) {
    return /^https?:\/\//i.test((s || '').trim());
  }
  function looksB64(s) {
    s = (s || '').replace(/\s+/g, '');
    if (s.indexOf('data:image/') === 0) return true;
    return s.length >= 32 && /^[A-Za-z0-9+/]+=*$/.test(s);
  }
  function nameFromUrl(raw) {
    try {
      var p = new URL(raw).pathname.split('/').pop();
      return p ? decodeURIComponent(p) : '';
    } catch (e) {
      return '';
    }
  }
  function toast(msg) {
    var text = (msg && msg.message) ? msg.message : String(msg || '');
    if (!text) return;
    if (/Failed to fetch|NetworkError|Network request failed/i.test(text)) {
      text = '连不上对照页，请先运行 python -m utils.SliderCaptchaOcr';
    }
    var el = document.createElement('div');
    el.className = 'toast';
    el.textContent = text;
    $('toasts').appendChild(el);
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 3200);
  }
  async function resolveInput(raw) {
    raw = (raw || '').trim();
    if (!raw) return '';
    if (looksUrl(raw)) {
      var res = await fetch('/api/fetch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: raw })
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(data.error || '拉取图片失败');
      return data.data;
    }
    if (!looksB64(raw) && raw.indexOf('data:') !== 0) {
      throw new Error('请输入图片 URL、dataURL 或 base64');
    }
    return asDataURL(raw);
  }
  function previewOk(dataURL) {
    return new Promise(function (resolve, reject) {
      var im = new Image();
      im.onload = function () { resolve(dataURL); };
      im.onerror = function () { reject(new Error('不是有效图片')); };
      im.src = dataURL;
    });
  }
  function fileToURL(file) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () { resolve(r.result); };
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  }
  function guessRole(name) {
    var n = (name || '').toLowerCase();
    if (/识别|boxed/.test(n) && !/滑块|slider/.test(n)) return 'boxed';
    if (/滑块|slider|block|piece|puzzle|target/.test(n)) return 'slider';
    if (/背景|bg|gap|background/.test(n)) return 'bg';
    return null;
  }
  function folderId(item) {
    return (item && (item.diskId || item.id)) || '';
  }
  function persistTraceIds() {
    try { localStorage.setItem('sliderOcr.showTraceIds', JSON.stringify(showTraceIds)); } catch (e) {}
    try { localStorage.setItem('sliderOcr.traceOffIds', JSON.stringify(traceOffIds)); } catch (e) {}
  }
  function persistTraceAll() {
    try { localStorage.setItem('sliderOcr.showTrace', showTraceAll ? '1' : '0'); } catch (e) {}
  }
  function moveTracePref(from, to) {
    if (!from || !to || from === to) return;
    if (showTraceIds[from]) {
      showTraceIds[to] = true;
      delete showTraceIds[from];
    }
    if (traceOffIds[from]) {
      traceOffIds[to] = true;
      delete traceOffIds[from];
    }
    persistTraceIds();
  }
  function isInlineImg(s) {
    return typeof s === 'string' && (s.indexOf('data:') === 0 || looksB64(s));
  }
  function applyRecognizeData(item, data) {
    if (!item || !data) return;
    item.x = data.x;
    item.y = data.y;
    item.conf = data.conf;
    item.pad_x = data.pad_x;
    item.ph = data.ph;
    item.sx = data.sx;
    item.sy = data.sy;
    item.tdist = data.tdist;
    item.error = data.error || null;
    item.boxed = data.boxed || null;
  }
  function boxedReady(item) {
    return !!(item && item.boxed && item.x != null);
  }
  function isTraceOff(item) {
    if (!item) return false;
    if (traceOffIds[item.id]) return true;
    if (item.diskId && traceOffIds[item.diskId]) return true;
    return false;
  }
  function someTracesOn() {
    return disk.some(function (item) {
      return boxedReady(item) && wantsTrace(item);
    });
  }
  function wantsTrace(item) {
    if (!item || isGone(item)) return false;
    if (isTraceOff(item)) return false;
    if (showTraceAll) return true;
    if (showTraceIds[item.id]) return true;
    if (item.diskId && showTraceIds[item.diskId]) return true;
    return false;
  }
  function stampTraceIds(on) {
    disk.forEach(function (item) {
      if (!item || !item.id || isGone(item)) return;
      var keys = [item.id];
      if (folderId(item)) keys.push(folderId(item));
      keys.forEach(function (k) {
        if (!k) return;
        if (on && boxedReady(item)) {
          showTraceIds[k] = true;
          delete traceOffIds[k];
        } else if (!on) {
          delete showTraceIds[k];
          delete traceOffIds[k];
        }
      });
    });
    persistTraceIds();
  }
  function forgetTrace(item) {
    if (!item) return;
    dropTrace(traceKey(item));
    var card = document.querySelector('article.card[data-id="' + attrSel(item.id) + '"]');
    var svg = card && card.querySelector('svg.trace');
    if (svg) svg.innerHTML = '';
  }
  function setTraceAll(on) {
    showTraceAll = !!on;
    persistTraceAll();
    stampTraceIds(on);
    if (on) {
      disk.forEach(function (item) {
        if (!isGone(item) && boxedReady(item)) forgetTrace(item);
      });
    }
    render();
  }
  function syncAllTraceBox() {
    var box = $('show-trace');
    if (box) box.checked = !!showTraceAll;
  }
  function setTraceOn(id, on) {
    var item = findItem(id);
    var keys = [id];
    if (item) {
      keys.push(item.id);
      if (folderId(item)) keys.push(folderId(item));
    }
    keys.forEach(function (k) {
      if (!k) return;
      if (on) {
        showTraceIds[k] = true;
        delete traceOffIds[k];
      } else {
        delete showTraceIds[k];
        traceOffIds[k] = true;
      }
    });
    persistTraceIds();
    if (!someTracesOn()) {
      showTraceAll = false;
      persistTraceAll();
    }
    var card = document.querySelector('article.card[data-id="' + attrSel(id) + '"]');
    if (card) card.classList.toggle('show-trace', !!on);
    if (on) {
      if (item) forgetTrace(item);
      else dropTrace(id);
      paintTraces();
    } else if (card) {
      var svg = card.querySelector('svg.trace');
      if (svg) svg.innerHTML = '';
    }
    syncAllTraceBox();
  }

  function safeName(s) {
    s = String(s == null ? '' : s).trim();
    s = s.replace(/[\\/:*?"<>|\s]+/g, '_').replace(/^[._]+|[._]+$/g, '');
    return s;
  }

  function nameTaken(name, except) {
    var low = String(name || '').toLowerCase();
    if (!low) return false;
    return disk.some(function (r) {
      if ((r.group || r.id) === except) return false;
      var ids = [r.id, r.group, r.diskId];
      for (var i = 0; i < ids.length; i++) {
        if (!ids[i]) continue;
        if (String(ids[i]).toLowerCase() === low) return true;
      }
      return false;
    });
  }

  function colorOf(name) {
    var h = 0;
    for (var i = 0; i < name.length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
    var hues = [210, 198, 185, 172, 25, 340, 155];
    return 'hsl(' + (hues[Math.abs(h) % hues.length]) + ' 62% 48%)';
  }

  function chip(label, val) {
    return '<span class="chip">' + esc(label)
      + ' <b>' + esc(val == null || val === '' ? '-' : val) + '</b></span>';
  }
  function confChip(v) {
    var lv = 'none';
    var text = '-';
    if (v != null && v !== '') {
      var n = Number(v);
      if (isFinite(n)) {
        text = Math.round(n * 100) + '%';
        if (n >= 0.8) lv = 'hi';
        else if (n >= 0.55) lv = 'mid';
        else lv = 'lo';
      }
    }
    return '<span class="chip conf ' + lv + '">可信 <b>' + esc(text) + '</b></span>';
  }
  function cellHTML(item, role, label) {
    var src = item[role];
    var filled = !!src;
    var canUpload = !filled && role !== 'boxed';
    var cls = 'cell' + (filled ? ' has' : (canUpload ? ' empty' : ''));
    var fig;
    if (filled) {
      if (role === 'boxed') {
        fig = '<figure><div class="shot"><img src="' + esc(src) + '" alt="" draggable="false">'
          + '<svg class="trace" aria-hidden="true"></svg></div></figure>';
      } else {
        fig = '<figure><img src="' + esc(src) + '" alt="" draggable="false"></figure>';
      }
    } else if (role === 'boxed' && item.error) {
      fig = '<figure><p class="ph err">' + esc(item.error) + '</p></figure>';
    } else if (!canUpload) {
      fig = '<figure><p class="ph">未识别</p></figure>';
    } else {
      fig = '<figure><div class="ways">'
        + '<div class="way file">'
          + '<button type="button" class="ghost" data-pick="' + esc(item.id) + '" data-role="' + role + '">上传图片</button>'
          + '<input type="file" accept="image/*">'
        + '</div>'
        + '<div class="way link">'
          + '<input type="text" class="src" placeholder="图片地址或 base64" autocomplete="off" spellcheck="false">'
          + '<button type="button" class="ghost" data-apply="' + esc(item.id) + '" data-role="' + role + '">确认</button>'
        + '</div>'
        + '<button type="button" class="ghost" data-switch>改用地址</button>'
        + '</div></figure>';
    }
    return '<div class="' + cls + '" data-id="' + esc(item.id) + '" data-role="' + role + '"'
      + (canUpload ? ' data-way="file"' : '') + '>'
      + '<div class="lab">' + esc(label) + '</div>'
      + fig
      + '</div>';
  }
  function cardHTML(item) {
    var hasBox = !!item.boxed;
    var err = item.error;
    var ok = hasBox && item.x != null;
    var pending = !hasBox && !err;
    var on = hasBox && wantsTrace(item);
    var cls = (pending ? 'pending' : (ok ? 'ok' : 'fail')) + (on ? ' show-trace' : '');
    var run = item.id
      ? '<button type="button" class="ghost run" data-run="' + esc(item.id) + '">'
        + (hasBox || err ? '重新识别' : '识别') + '</button>'
      : '';
    var del = item.id
      ? '<button type="button" class="ghost del" data-del="' + esc(item.id) + '">删除</button>'
      : '';
    var traceTog = item.id
      ? '<label class="trace-tog"' + (hasBox ? ' title="在识别图上叠拟人滑动轨迹"' : ' title="先识别再查看"') + '>'
        + '<input type="checkbox" data-trace="' + esc(item.id) + '"'
        + (on ? ' checked' : '') + (hasBox ? '' : ' disabled') + '>轨迹</label>'
      : '';
    var chips = '<span class="chip dist">x=<b>' + esc(item.x == null ? '-' : item.x) + '</b></span>'
      + '<span class="chip xy">y=<b>' + esc(item.y == null ? '-' : item.y) + '</b></span>'
      + confChip(item.conf);
    return '<article class="card ' + cls + '" data-id="' + esc(item.id) + '" data-group="' + esc(item.group || item.id) + '">'
      + '<header>' + chips + traceTog + run + del + '</header>'
      + '<div class="trio">'
      + cellHTML(item, 'bg', '背景')
      + cellHTML(item, 'slider', '滑块')
      + cellHTML(item, 'boxed', '识别')
      + '</div></article>';
  }

  function fitTabs() {
    var bar = $('tabs');
    var more = $('tab-more');
    if (!bar || !more) return;
    bar.classList.toggle('open', tabsOpen);
    var first = bar.firstElementChild;
    var last = bar.lastElementChild;
    var multi = !!(first && last && last.offsetTop > first.offsetTop + 4);
    if (tabsOpen) {
      more.textContent = '收起';
      more.classList.remove('hidden');
      return;
    }
    more.textContent = '展开';
    more.classList.toggle('hidden', !multi);
  }

  function renderActs() {
    var acts = '<label class="sync" title="打开后，添加、删除和改名会改本地文件夹">'
      + '<input type="checkbox" id="sync-local"' + (syncLocal ? ' checked' : '') + '>写入本地</label>';
    acts += '<label class="sync" title="给所有已识别的卡片叠拟人滑动轨迹">'
      + '<input type="checkbox" id="show-trace"'
      + (showTraceAll ? ' checked' : '') + '>查看轨迹</label>';
    acts += '<button type="button" id="rerun-all">全部重新识别</button>';
    acts += '<button type="button" id="add-card">添加卡片</button>';
    acts += '<button type="button" id="reload">刷新本地</button>';
    $('acts').innerHTML = acts;
  }

  function renderTabs() {
    var split = splitDisk();
    var html = '';
    split.names.forEach(function (name) {
      html += '<button type="button" data-g="' + esc(name) + '">' + esc(name)
        + '<span class="tab-del" data-g-del="' + esc(name) + '" title="删除此组">×</span>'
        + '</button>';
    });
    $('tabs').innerHTML = html;
    requestAnimationFrame(function () { requestAnimationFrame(fitTabs); });
  }

  function render() {
    var split = splitDisk();
    renderActs();
    renderTabs();
    var board = '';
    split.names.forEach(function (name) {
      var c = colorOf(name);
      board += '<section class="group" data-group="' + esc(name) + '" style="--c:' + c + '"><h2>'
        + '<span class="g-name" data-rename="' + esc(name) + '" title="点击改名">' + esc(name) + '</span>'
        + '</h2><div class="list">'
        + split.groups[name].map(function (item) { return cardHTML(item); }).join('') + '</div></section>';
    });
    board += '<button type="button" class="add-foot" id="add-card-foot">添加卡片</button>';
    $('board').innerHTML = board;
    $('empty').classList.toggle('hidden', disk.some(function (r) { return !isGone(r); }));
    if (disk.some(wantsTrace)) paintTraces();
  }

  function traceKey(item) {
    return folderId(item) || item.id;
  }

  function traceStart(item) {
    var sx = Number(item.sx);
    var sy = Number(item.sy);
    if (isFinite(sx) && isFinite(sy)) return [sx, sy];
    sx = Number(item.pad_x);
    sy = Number(item.y);
    if (!isFinite(sx)) sx = 0;
    if (!isFinite(sy)) sy = 0;
    return [sx, sy];
  }
  function traceDist(item) {
    var d = Number(item.tdist);
    if (isFinite(d) && d >= 0) return d;
    var x = Number(item.x);
    var pad = Number(item.pad_x);
    if (isFinite(x) && isFinite(pad)) return Math.max(0, x - pad);
    return isFinite(x) ? x : 0;
  }

  function traceSVG(pts) {
    if (!pts || pts.length < 2) return '';
    var d = 'M' + pts[0][0] + ',' + pts[0][1];
    for (var i = 1; i < pts.length; i++) d += ' L' + pts[i][0] + ',' + pts[i][1];
    var a = pts[0];
    var b = pts[pts.length - 1];
    return '<path class="halo" d="' + d + '"></path>'
      + '<path class="line" d="' + d + '"></path>'
      + '<circle class="dot start" cx="' + a[0] + '" cy="' + a[1] + '" r="1.6"></circle>'
      + '<circle class="dot end" cx="' + b[0] + '" cy="' + b[1] + '" r="1.6"></circle>';
  }

  function drawTraceOn(img, pts) {
    var wrap = img && img.parentNode;
    var svg = wrap && wrap.querySelector('svg.trace');
    if (!svg || !pts || pts.length < 2) return;
    function apply() {
      var w = img.naturalWidth;
      var h = img.naturalHeight;
      if (!w || !h) return;
      svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
      svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
      svg.innerHTML = traceSVG(pts);
    }
    if (img.complete && img.naturalWidth) apply();
    else img.addEventListener('load', apply, { once: true });
  }

  function dropTrace(id) {
    delete traceCache[id];
  }

  function paintTraces() {
    var gen = ++traceGen;
    var need = [];
    disk.forEach(function (item) {
      if (!wantsTrace(item) || !item.boxed || item.x == null) return;
      var key = traceKey(item);
      var hit = traceCache[key];
      var dist = traceDist(item);
      if (!hit || hit.x !== dist) need.push(item);
    });
    function draw() {
      if (gen !== traceGen) return;
      disk.forEach(function (item) {
        if (!wantsTrace(item) || !item.boxed || item.x == null) return;
        var hit = traceCache[traceKey(item)];
        if (!hit || !hit.points) return;
        var card = document.querySelector(
          'article.card[data-id="' + attrSel(item.id) + '"]'
        );
        var img = card && card.querySelector('.cell[data-role="boxed"] img');
        if (img) drawTraceOn(img, hit.points);
      });
    }
    if (!need.length) {
      draw();
      return;
    }
    fetch('/api/trace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: need.map(function (it) {
          return {
            id: traceKey(it),
            distance: traceDist(it),
            start: traceStart(it)
          };
        })
      })
    }).then(function (res) { return res.json().then(function (data) {
      if (!res.ok) throw new Error(data.error || '轨迹生成失败');
      return data;
    }); }).then(function (data) {
      (data.traces || []).forEach(function (row) {
        var key = row && row.id;
        if (!key || !row.points) return;
        var it = null;
        for (var i = 0; i < need.length; i++) {
          if (traceKey(need[i]) === key) { it = need[i]; break; }
        }
        traceCache[key] = { x: it ? traceDist(it) : null, points: row.points };
      });
      draw();
    }).catch(function (err) { toast(err); });
  }

  async function loadDisk(opts) {
    var temps = disk.filter(function (r) { return r.temp; });
    var res = await fetch('/api/cases');
    if (!res.ok) throw new Error('读本地组失败');
    if (opts && opts.resetHidden) {
      dismissed = {};
      memRename = {};
    }
    disk = (await res.json()).filter(function (r) {
      return !dismissed[r.id] && !dismissed[r.diskId];
    });
    disk.forEach(function (r) { r.diskId = r.id; });
    if (!(opts && opts.resetHidden) && !syncLocal) {
      temps.forEach(function (t) {
        if (!isGone(t) && !disk.some(function (r) { return r.id === t.id; })) disk.push(t);
      });
      Object.keys(memRename).forEach(function (folder) {
        disk.forEach(function (r) {
          if (r.diskId === folder) r.id = r.group = memRename[folder];
        });
      });
    }
    render();
  }

  function findItem(id) {
    for (var i = 0; i < disk.length; i++) if (disk[i].id === id) return disk[i];
    return null;
  }

  async function deleteLocal(ids) {
    if (!syncLocal) return;
    for (var i = 0; i < ids.length; i++) {
      var res = await fetch('/api/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: (findItem(ids[i]) && folderId(findItem(ids[i]))) || ids[i] })
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(data.error || '删除本地失败');
    }
  }

  async function saveSlot(id, role, dataURL, filename) {
    var item = findItem(id);
    if (item && item.temp && !syncLocal) {
      item[role] = dataURL;
      if (role !== 'boxed') {
        item.boxed = null;
        item.error = null;
        item.x = item.y = item.conf = item.pad_x = item.ph = item.sx = item.sy = item.tdist = null;
        dropTrace(traceKey(item));
      }
      render();
      return item;
    }
    var body = { id: folderId(item) || id };
    body[role] = dataURL;
    if (role === 'bg') body.bg_name = filename || '';
    if (role === 'slider') body.slider_name = filename || '';
    var res = await fetch('/api/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || '保存失败');
    await loadDisk();
    return data;
  }

  async function runRecognize(id, opts) {
    opts = opts || {};
    if (!id || (busy && !opts.force)) return;
    if (!opts.force) busy = true;
    var btn = document.querySelector('[data-run="' + id + '"]');
    if (btn) { btn.disabled = true; btn.textContent = '识别中'; }
    try {
      var item = findItem(id);
      var payload = { id: folderId(item) || id, save: !!syncLocal };
      if (item && isInlineImg(item.bg) && isInlineImg(item.slider)) {
        var miss = [];
        if (!item.bg) miss.push('背景');
        if (!item.slider) miss.push('滑块');
        if (miss.length) throw new Error('缺少' + miss.join('和'));
        payload.bg = item.bg;
        payload.slider = item.slider;
      } else if (item && item.temp && !syncLocal) {
        var miss = [];
        if (!item.bg) miss.push('背景');
        if (!item.slider) miss.push('滑块');
        if (miss.length) throw new Error('缺少' + miss.join('和'));
        payload.bg = item.bg;
        payload.slider = item.slider;
        payload.save = false;
      }
      var res = await fetch('/api/recognize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) {
        var miss = (data.missing || []).map(function (k) {
          return k === 'bg' ? '背景' : (k === 'slider' ? '滑块' : k);
        });
        throw new Error(data.error || (miss.length ? ('缺少' + miss.join('和')) : '识别失败'));
      }
      applyRecognizeData(item, data);
      dropTrace(traceKey(item));
      if (folderId(item)) dropTrace(folderId(item));
      if (syncLocal && !opts.skipReload) {
        await loadDisk();
      } else if (!opts.skipReload) {
        render();
      }
    } catch (e) {
      if (!opts.quiet) toast(e);
      if (btn) { btn.disabled = false; btn.textContent = '重新识别'; }
      if (opts.force) throw e;
    } finally {
      if (!opts.force) busy = false;
    }
  }

  async function runAll() {
    if (busy) return;
    var ids = disk.filter(function (r) {
      return !isGone(r) && r.bg && r.slider;
    }).map(function (r) { return r.id; });
    if (!ids.length) { toast('没有可识别的卡片'); return; }
    busy = true;
    var btn = $('rerun-all');
    if (btn) { btn.disabled = true; btn.textContent = '识别中'; }
    var fail = 0;
    try {
      for (var i = 0; i < ids.length; i++) {
        if (btn) btn.textContent = '识别中 ' + (i + 1) + '/' + ids.length;
        try {
          await runRecognize(ids[i], { force: true, skipReload: true, quiet: true });
        } catch (e) {
          fail += 1;
        }
      }
      if (syncLocal) await loadDisk();
      else render();
      if (fail) toast(fail + ' 组未识别（缺图或失败）');
    } finally {
      busy = false;
      if (btn) { btn.disabled = false; btn.textContent = '全部重新识别'; }
    }
  }

  function attrSel(s) {
    return String(s || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  }

  function applyRenameLocal(oldName, newName) {
    disk.forEach(function (r) {
      if ((r.group || r.id) !== oldName) return;
      if (!r.temp) r.diskId = r.diskId || r.id;
      r.id = r.group = newName;
      if (r.diskId) memRename[r.diskId] = newName;
    });
    if (dismissed[oldName]) {
      dismissed[newName] = true;
      delete dismissed[oldName];
    }
    moveTracePref(oldName, newName);
  }

  async function renameGroup(oldName, newName) {
    if (!oldName || !newName || oldName === newName) return;
    if (nameTaken(newName, oldName)) throw new Error('组名已存在');
    if (syncLocal) {
      var item = disk.filter(function (r) { return (r.group || r.id) === oldName; })[0];
      var from = folderId(item) || oldName;
      var res = await fetch('/api/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: from, name: newName })
      });
      var data = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(data.error || '改名失败');
      if (item && item.diskId) delete memRename[item.diskId];
      moveTracePref(oldName, newName);
      await loadDisk();
    } else {
      applyRenameLocal(oldName, newName);
      render();
    }
  }

  function beginRename(name) {
    if (!name || busy) return;
    var h2 = document.querySelector('.group[data-group="' + attrSel(name) + '"] h2');
    if (!h2 || h2.querySelector('input.g-edit')) return;
    var span = h2.querySelector('.g-name');
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'g-edit';
    input.value = name;
    input.setAttribute('data-old', name);
    input.setAttribute('spellcheck', 'false');
    input.setAttribute('autocomplete', 'off');
    if (span) span.replaceWith(input);
    else h2.appendChild(input);
    input.focus();
    input.select();
  }

  function finishRename(input) {
    if (!input || input.getAttribute('data-done')) return;
    input.setAttribute('data-done', '1');
    var oldName = input.getAttribute('data-old') || '';
    if (input.getAttribute('data-cancel') === '1') {
      render();
      return;
    }
    var newName = safeName(input.value);
    if (!newName) {
      toast('组名无效');
      render();
      return;
    }
    if (newName === oldName) {
      render();
      return;
    }
    renameGroup(oldName, newName).catch(function (err) {
      toast(err);
      render();
    });
  }

  function dismissGroup(name) {
    if (!name || busy) return;
    var items = disk.filter(function (r) {
      return (r.group || r.id) === name;
    });
    if (!items.length) return;
    var persistIds = items.filter(function (it) { return it && !it.temp; })
      .map(function (it) { return folderId(it) || it.id; });
    items.forEach(function (it) {
      dismissed[it.id] = true;
      delete showTraceIds[it.id];
      delete traceOffIds[it.id];
      if (it.diskId) {
        dismissed[it.diskId] = true;
        delete showTraceIds[it.diskId];
        delete traceOffIds[it.diskId];
      }
    });
    persistTraceIds();
    disk = disk.filter(function (r) { return (r.group || r.id) !== name; });
    function finish() {
      render();
    }
    var groupEl = document.querySelector('.group[data-group="' + attrSel(name) + '"]');
    var tab = document.querySelector('#tabs button[data-g="' + attrSel(name) + '"]');
    if (!groupEl && !tab) {
      deleteLocal(persistIds).catch(function (err) { toast(err); });
      finish();
      return;
    }
    if (tab) tab.classList.add('leave');
    if (groupEl) {
      var h = groupEl.offsetHeight;
      groupEl.style.height = h + 'px';
      groupEl.getBoundingClientRect();
      groupEl.classList.add('leave');
      groupEl.style.height = '0px';
    }
    setTimeout(function () {
      deleteLocal(persistIds).catch(function (err) { toast(err); });
      finish();
    }, LEAVE_MS);
  }

  function dismissCard(id) {
    if (!id || dismissed[id] || busy) return;
    var item = findItem(id);
    var persist = !!(item && !item.temp);
    var persistId = persist ? (folderId(item) || id) : null;
    dismissed[id] = true;
    delete showTraceIds[id];
    delete traceOffIds[id];
    if (item && item.diskId) {
      dismissed[item.diskId] = true;
      delete showTraceIds[item.diskId];
      delete traceOffIds[item.diskId];
    }
    persistTraceIds();
    disk = disk.filter(function (r) { return r.id !== id; });
    var safe = id.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    var card = document.querySelector('article.card[data-id="' + safe + '"]');
    if (!card) {
      render();
      return;
    }
    var groupEl = card.closest('.group');
    var groupName = groupEl ? groupEl.getAttribute('data-group') : '';
    var remain = groupEl
      ? groupEl.querySelectorAll('article.card:not(.leave)')
      : [];
    var last = remain.length <= 1;
    var target = last && groupEl ? groupEl : card;
    var h = target.offsetHeight;
    target.style.height = h + 'px';
    target.getBoundingClientRect();
    target.classList.add('leave');
    target.style.height = '0px';
    if (last && groupName) {
      var tab = document.querySelector('#tabs button[data-g="' + groupName.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]');
      if (tab) tab.classList.add('leave');
    }
    setTimeout(function () {
      if (persistId) deleteLocal([persistId]).catch(function (err) { toast(err); });
      if (target.parentNode) target.remove();
      renderActs();
      renderTabs();
      $('empty').classList.toggle('hidden', disk.some(function (r) { return !isGone(r); }));
    }, LEAVE_MS);
  }

  async function addCard() {
    if (busy) return;
    busy = true;
    var addBtns = [$('add-card'), $('add-card-foot')];
    addBtns.forEach(function (b) { if (b) b.disabled = true; });
    try {
      var newId;
      if (!syncLocal) {
        newId = 'Card_' + Date.now();
        disk.push({
          id: newId, group: newId, temp: true,
          created: (function () {
            var d = new Date();
            var p = function (n) { return (n < 10 ? '0' : '') + n; };
            return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
              + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
          })(),
          x: null, y: null, conf: null,
          pad_x: null, ph: null, sx: null, sy: null, tdist: null,
          bg: null, slider: null, boxed: null, error: null
        });
        render();
      } else {
        var res = await fetch('/api/new', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}'
        });
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok) throw new Error(data.error || '添加失败');
        newId = data.id;
        await loadDisk();
      }
      var el = document.querySelector('article.card[data-id="' + (newId || '') + '"]');
      if (el) {
        el.classList.add('enter');
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    } catch (e) {
      toast(e);
    } finally {
      busy = false;
      [$('add-card'), $('add-card-foot')].forEach(function (b) {
        if (b) b.disabled = false;
      });
    }
  }

  var IMG_HINT = 'png / jpg / webp / gif / bmp';
  function fileExt(name) {
    var m = String(name || '').match(/(\.[^.\s\\/]+)$/);
    return m ? m[1].toLowerCase() : '';
  }
  function isImageFile(f) {
    if (!f) return false;
    return /^image\//.test(f.type || '') || /\.(png|jpe?g|webp|gif|bmp)$/i.test(f.name || '');
  }
  function rejectFileMsg(f) {
    if (!f) return '没有文件';
    if (isImageFile(f)) return '';
    var ext = fileExt(f.name);
    if (ext) return '不支持 ' + ext + '，请上传 ' + IMG_HINT;
    return '不是图片，请上传 ' + IMG_HINT;
  }

  async function applyToCell(id, role, dataURL, filename) {
    if (role === 'boxed') return;
    await saveSlot(id, role, dataURL, filename);
  }

  async function pickPair(files) {
    var assigned = { bg: null, slider: null };
    files.forEach(function (f) {
      var role = guessRole(f.name);
      if (role === 'boxed') return;
      if (role && !assigned[role]) assigned[role] = f;
    });
    var leftover = files.filter(function (f) {
      return f !== assigned.bg && f !== assigned.slider && guessRole(f.name) !== 'boxed';
    });
    leftover.sort(function (a, b) { return (b.size || 0) - (a.size || 0); });
    leftover.forEach(function (f) {
      if (!assigned.bg) assigned.bg = f;
      else if (!assigned.slider) assigned.slider = f;
    });
    return {
      bg: assigned.bg ? await fileToURL(assigned.bg) : null,
      slider: assigned.slider ? await fileToURL(assigned.slider) : null,
      bgName: assigned.bg ? assigned.bg.name : '',
      sliderName: assigned.slider ? assigned.slider.name : ''
    };
  }

  async function savePair(bg, slider, tag, bgName, sliderName) {
    var res = await fetch('/api/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bg: bg || '', slider: slider || '',
        tag: tag || '', bg_name: bgName || '', slider_name: sliderName || ''
      })
    });
    var data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.error || '保存失败');
    await loadDisk();
    return data;
  }

  async function takeFiles(fileList, onto) {
    var raw = Array.prototype.slice.call(fileList || []);
    if (!raw.length) {
      toast('没有文件');
      return;
    }
    var files = raw.filter(isImageFile);
    if (!files.length) {
      toast(rejectFileMsg(raw[0]));
      return;
    }
    if (onto && onto.id && onto.role && onto.role !== 'boxed') {
      var f = files[0];
      await applyToCell(onto.id, onto.role, await previewOk(await fileToURL(f)), f.name);
      return;
    }
    var pair = await pickPair(files);
    if (pair.bg || pair.slider) {
      await savePair(pair.bg, pair.slider, '', pair.bgName, pair.sliderName);
    }
  }

  function readEntries(dirEntry) {
    return new Promise(function (resolve, reject) {
      var reader = dirEntry.createReader();
      var all = [];
      function next() {
        reader.readEntries(function (batch) {
          if (!batch.length) { resolve(all); return; }
          all = all.concat(batch);
          next();
        }, reject);
      }
      next();
    });
  }
  function fileOf(entry) {
    return new Promise(function (resolve, reject) { entry.file(resolve, reject); });
  }
  async function ingestEntry(entry, tag) {
    if (!entry) return;
    if (entry.isFile) {
      await takeFiles([await fileOf(entry)]);
      return;
    }
    var children = await readEntries(entry);
    var files = [];
    var dirs = [];
    for (var i = 0; i < children.length; i++) {
      if (children[i].isFile) files.push(await fileOf(children[i]));
      else dirs.push(children[i]);
    }
    var imgs = files.filter(isImageFile);
    if (imgs.length) {
      var pair = await pickPair(imgs);
      if (pair.bg || pair.slider) {
        await savePair(pair.bg, pair.slider, tag || entry.name, pair.bgName, pair.sliderName);
      }
    }
    for (var j = 0; j < dirs.length; j++) await ingestEntry(dirs[j], dirs[j].name);
  }
  async function fromDrop(dt, onto) {
    if (onto && dt.files && dt.files.length) {
      await takeFiles(dt.files, onto);
      return;
    }
    var items = dt.items;
    if (items && items.length) {
      var used = false;
      for (var i = 0; i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
        if (entry) { used = true; await ingestEntry(entry, entry.name); }
      }
      if (used) return;
    }
    await takeFiles(dt.files);
  }

  function jumpToGroup(name) {
    if (!name) return;
    var el = document.querySelector('.group[data-group="' + attrSel(name) + '"]');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var tab = document.querySelector('#tabs button[data-g="' + attrSel(name) + '"]');
    if (tab && tab.blur) tab.blur();
  }

  function onActClick(e) {
    var gdel = e.target.closest('[data-g-del]');
    if (gdel) {
      e.preventDefault();
      e.stopPropagation();
      dismissGroup(gdel.getAttribute('data-g-del'));
      return;
    }
    var btn = e.target.closest('button');
    if (!btn) return;
    if (btn.id === 'reload') { loadDisk({ resetHidden: true }).catch(function (err) { toast(err); }); return; }
    if (btn.id === 'add-card') { addCard(); return; }
    if (btn.id === 'rerun-all') { runAll(); return; }
    var g = btn.getAttribute('data-g');
    if (g) jumpToGroup(g);
  }
  $('tabs').addEventListener('click', onActClick);
  $('acts').addEventListener('click', onActClick);
  $('acts').addEventListener('change', function (e) {
    if (e.target.id === 'sync-local') {
      syncLocal = !!e.target.checked;
      try { localStorage.setItem('sliderOcr.syncLocal', syncLocal ? '1' : '0'); } catch (err) {}
      if (syncLocal) {
        loadDisk({ resetHidden: true }).catch(function (err) { toast(err); });
      }
      return;
    }
    if (e.target.id !== 'show-trace') return;
    setTraceAll(!!e.target.checked);
  });
  $('tab-more').addEventListener('click', function () {
    tabsOpen = !tabsOpen;
    fitTabs();
  });
  window.addEventListener('resize', function () {
    if (!tabsOpen) fitTabs();
  });
  $('to-top').addEventListener('click', function () {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  $('board').addEventListener('click', function (e) {
    var rename = e.target.closest('[data-rename]');
    if (rename) {
      e.preventDefault();
      e.stopPropagation();
      beginRename(rename.getAttribute('data-rename'));
      return;
    }
    var run = e.target.closest('[data-run]');
    if (run) { runRecognize(run.getAttribute('data-run')); return; }
    var del = e.target.closest('[data-del]');
    if (del) { dismissCard(del.getAttribute('data-del')); return; }
    if (e.target.closest('#add-card-foot')) { addCard(); return; }
    var sw = e.target.closest('[data-switch]');
    if (sw) {
      e.preventDefault();
      e.stopPropagation();
      var cell = sw.closest('.cell');
      if (!cell) return;
      var way = cell.getAttribute('data-way') === 'link' ? 'file' : 'link';
      cell.setAttribute('data-way', way);
      sw.textContent = way === 'link' ? '改用上传' : '改用地址';
      return;
    }
    var pick = e.target.closest('[data-pick]');
    if (pick) {
      e.preventDefault();
      var cell = pick.closest('.cell');
      if (cell) cell.querySelector('input[type=file]').click();
      return;
    }
    if (e.target.closest('.cell.empty[data-way="file"] figure')
        && !e.target.closest('.way.link, [data-switch], input')) {
      var cell = e.target.closest('.cell');
      var file = cell && cell.querySelector('input[type=file]');
      if (file) file.click();
      return;
    }
    var apply = e.target.closest('[data-apply]');
    if (apply) {
      e.preventDefault();
      var cell = apply.closest('.cell');
      var src = cell && cell.querySelector('input.src');
      var raw = src ? src.value.trim() : '';
      if (!raw) { toast('请输入图片地址或 base64'); return; }
      resolveInput(raw).then(function (u) { return previewOk(u); }).then(function (u) {
        return applyToCell(apply.getAttribute('data-apply'), apply.getAttribute('data-role'), u,
          looksUrl(raw) ? nameFromUrl(raw) : '');
      }).catch(function (err) { toast(err); });
    }
  });

  $('board').addEventListener('change', function (e) {
    var input = e.target;
    if (input && input.getAttribute && input.getAttribute('data-trace')) {
      setTraceOn(input.getAttribute('data-trace'), input.checked);
      return;
    }
    if (!input || input.type !== 'file') return;
    var cell = input.closest('.cell');
    var f = input.files && input.files[0];
    input.value = '';
    if (!cell || !f) return;
    if (!isImageFile(f)) {
      toast(rejectFileMsg(f));
      return;
    }
    fileToURL(f).then(function (u) { return previewOk(u); }).then(function (u) {
      return applyToCell(cell.getAttribute('data-id'), cell.getAttribute('data-role'), u, f.name);
    }).catch(function (err) { toast(err); });
  });

  $('board').addEventListener('keydown', function (e) {
    var edit = e.target.closest('input.g-edit');
    if (edit) {
      if (e.key === 'Enter') {
        e.preventDefault();
        edit.blur();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        edit.setAttribute('data-cancel', '1');
        edit.blur();
      }
      return;
    }
    if (e.key !== 'Enter') return;
    var src = e.target.closest('input.src');
    if (!src) return;
    e.preventDefault();
    var cell = src.closest('.cell');
    var apply = cell && cell.querySelector('[data-apply]');
    if (apply) apply.click();
  });

  $('board').addEventListener('focusout', function (e) {
    var edit = e.target.closest && e.target.closest('input.g-edit');
    if (edit) finishRename(edit);
  });

  $('board').addEventListener('mouseover', function (e) {
    var cell = e.target.closest('.cell');
    if (cell) hoverCell = cell;
  });

  $('board').addEventListener('dragover', function (e) {
    var cell = e.target.closest('.cell.empty[data-role="bg"], .cell.empty[data-role="slider"]');
    if (!cell) return;
    e.preventDefault();
    e.stopPropagation();
    cell.classList.add('drop');
  });
  $('board').addEventListener('dragleave', function (e) {
    var cell = e.target.closest('.cell');
    if (cell) cell.classList.remove('drop');
  });
  $('board').addEventListener('drop', function (e) {
    var cell = e.target.closest('.cell.empty[data-role="bg"], .cell.empty[data-role="slider"]');
    if (!cell) return;
    e.preventDefault();
    e.stopPropagation();
    cell.classList.remove('drop');
    fromDrop(e.dataTransfer, {
      id: cell.getAttribute('data-id'),
      role: cell.getAttribute('data-role')
    }).catch(function (err) { toast(err); });
  });

  function isFileDrag(e) {
    var dt = e.dataTransfer;
    if (!dt) return false;
    if (dt.files && dt.files.length) return true;
    var types = dt.types;
    if (!types) return false;
    if (typeof types.contains === 'function') return types.contains('Files');
    for (var i = 0; i < types.length; i++) {
      var t = String(types[i] || '');
      if (t === 'Files' || t.toLowerCase() === 'application/x-moz-file') return true;
    }
    return false;
  }
  window.addEventListener('dragstart', function (e) {
    if (e.target && e.target.tagName === 'IMG') e.preventDefault();
  });
  window.addEventListener('dragover', function (e) {
    if (!isFileDrag(e)) return;
    e.preventDefault();
  });
  window.addEventListener('drop', function (e) {
    e.preventDefault();
    document.querySelectorAll('.cell.drop').forEach(function (el) {
      el.classList.remove('drop');
    });
  });

  window.addEventListener('paste', function (e) {
    if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) {
      return;
    }
    var cd = e.clipboardData;
    if (!cd) return;
    var files = [];
    if (cd.files && cd.files.length) files = cd.files;
    else {
      for (var i = 0; i < (cd.items || []).length; i++) {
        if (cd.items[i].type.indexOf('image/') === 0) files.push(cd.items[i].getAsFile());
      }
    }
    if (files.length) {
      e.preventDefault();
      var onto = hoverCell && hoverCell.classList.contains('empty')
        && hoverCell.getAttribute('data-role') !== 'boxed'
        ? { id: hoverCell.getAttribute('data-id'), role: hoverCell.getAttribute('data-role') }
        : null;
      if (!onto) {
        toast('请先把鼠标移到要贴的背景或滑块空位上');
        return;
      }
      takeFiles(files, onto).catch(function (err) { toast(err); });
    }
  });

  loadDisk().catch(function (err) {
    toast(err);
    render();
  });
})();
