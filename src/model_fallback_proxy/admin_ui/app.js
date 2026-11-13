/*
 * 继续工程师 · 模型降级路由管理台（前端逻辑）
 * 纯原生 JS，无框架、无构建；只调用 model_fallback_proxy 挂载的 /_fallback/* 接口。
 *
 * 页面：概览 / 降级链路 / 重试策略 / 供应商 / 特殊规则 / 试运行（hash 路由）。
 *
 * 方向约定（与用户口径一致）：
 *   顶部 = 首选（叶子，最先尝试，通常是免费公司渠道）
 *   底部 = 兜底（根/地基，最后尝试，通常是自付费渠道，例如 glm-5.3）
 *   一个节点可有多个 children，因此整体是 DAG；被多条路径指向的节点标「汇聚点」。
 *
 * 安全：任何情况下都不读取、不展示 api_key 值，只处理 api_key_env 变量名。
 */
(function () {
  'use strict';

  var API = {
    config: '/_fallback/admin/config',
    health: '/_fallback/admin/health_check',
    status: '/_fallback/status'
  };

  // 关键词类别：后两个是确定性错误，默认关闭
  var KEYWORD_META = [
    { key: 'capacity', title: '容量/过载', desc: '上游返回 at capacity、overloaded、no available accounts 等。' },
    { key: 'network_error', title: '网络错误', desc: '连接失败、DNS/TLS 错误、连接超时等传输层异常。' },
    { key: 'stream_break', title: '流中断', desc: 'SSE 已开始输出但中途断开或未收到 response.completed。' },
    { key: 'model_not_found', title: '模型不存在', desc: '该渠道不支持此模型名（404 / model not found），换渠道通常有效。' },
    { key: 'auth_error', title: '鉴权错误', desc: '401/403 等密钥失效。确定性错误，重试无意义，开启会浪费额度。', deterministic: true },
    { key: 'corrupted_history', title: '历史损坏', desc: '历史密文 hydration 失败。确定性错误，换模型也无法恢复，开启会浪费额度。', deterministic: true }
  ];

  // 节点策略字段：留空 = 继承 defaults
  var PARAM_META = [
    { key: 'retries', label: '重试次数', step: '1', min: '0', hint: '同一节点额外重试次数' },
    { key: 'retry_interval_ms', label: '首次间隔(ms)', step: '50', min: '0', hint: '第一次重试前等待' },
    { key: 'backoff_multiplier', label: '退避倍数', step: '0.1', min: '1', hint: '每次重试间隔放大倍数' },
    { key: 'max_retry_interval_ms', label: '间隔上限(ms)', step: '100', min: '0', hint: '退避封顶' },
    { key: 'first_byte_timeout_s', label: '首字节超时(s)', step: '5', min: '1', hint: '超过则判为可重试失败' },
    { key: 'cooldown_s', label: '渠道冷却(s)', step: '10', min: '0', hint: '该渠道失败后暂停使用时长' }
  ];

  // 全局 defaults 字段
  var DEFAULT_META = PARAM_META.concat([
    { key: 'connect_timeout_s', label: '连接超时(s)', step: '1', min: '1', hint: '建立连接超时' }
  ]);

  // 特殊规则动作：后端固定集合，非法值前端拦
  var ACTION_META = [
    { key: 'switch_next', title: '降级换下一节点', desc: '失败后按 children 顺序换到下一个模型。' },
    { key: 'retry_same', title: '只同节点重试', desc: '不换模型，先在本节点按 retries 重试。' },
    { key: 'cooldown_node', title: '冷却该节点', desc: '失败后把该 供应商:模型 冷却一段时间。' },
    { key: 'signal_compact', title: '提示压缩上下文', desc: '标记该会话应压缩上下文（如超长报错）。' },
    { key: 'signal_new_session', title: '提示开新会话', desc: '标记历史已损坏，建议开新会话。' }
  ];

  var QUICK_STATUS = [429, 502, 503, 529, 408, 400, 401, 403, 404];

  var ROUTES = {
    overview: { title: '概览', desc: '运行状态、供应商健康与最近的降级事件。' },
    dag: { title: '降级链路', desc: '编辑模型降级 DAG：上方首选，下方最后兜底。' },
    retry: { title: '重试策略', desc: '选择哪些异常触发重试，并设置全局默认参数。' },
    providers: { title: '供应商', desc: '维护各渠道的地址、鉴权方式与启用状态。' },
    rules: { title: '特殊规则', desc: '按错误文案命中，优先于普通状态码/关键词判定。' },
    dryrun: { title: '试运行', desc: '按当前配置推演一次请求会经历的全部尝试。' }
  };

  var state = {
    config: null,      // 当前编辑中的配置
    saved: null,       // 后端已保存版本，用于 diff
    status: null,
    version: '',
    route: 'overview'
  };

  /* ---------- 小工具 ---------- */
  function $(id) { return document.getElementById(id); }

  function el(tag, className, textValue) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (textValue !== undefined && textValue !== null) { node.textContent = String(textValue); }
    return node;
  }

  function deepCopy(value) { return JSON.parse(JSON.stringify(value)); }

  function showToast(message, isError) {
    var toast = $('toast');
    toast.textContent = message;
    toast.className = 'toast' + (isError ? ' error' : '');
    toast.classList.remove('hidden');
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(function () { toast.classList.add('hidden'); }, isError ? 6000 : 3200);
  }

  function showBanner(message, kind) {
    var banner = $('banner');
    if (!message) { banner.classList.add('hidden'); return; }
    banner.textContent = message;
    banner.className = 'banner' + (kind === 'info' ? ' info' : '');
  }

  function requestJson(url, options) {
    var timeoutMs = (options && options.timeoutMs) || 15000;
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, timeoutMs);
    var init = Object.assign({ signal: controller.signal, headers: {} }, options || {});
    delete init.timeoutMs;
    if (init.body) { init.headers['Content-Type'] = 'application/json'; }
    return fetch(url, init).then(function (resp) {
      clearTimeout(timer);
      return resp.text().then(function (textValue) {
        var payload = null;
        try { payload = textValue ? JSON.parse(textValue) : null; } catch (err) { payload = null; }
        if (!resp.ok) {
          var detail = (payload && (payload.error || payload.message)) || textValue || ('HTTP ' + resp.status);
          throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
        }
        return payload;
      });
    }).catch(function (err) {
      clearTimeout(timer);
      if (err && err.name === 'AbortError') { throw new Error('请求超时（' + Math.round(timeoutMs / 1000) + 's）'); }
      throw err;
    });
  }

  function fmtNum(value) {
    if (value === undefined || value === null || value === '') { return '-'; }
    return String(value);
  }

  function inheritOf(node, key) {
    if (node && node[key] !== undefined && node[key] !== null && node[key] !== '') { return node[key]; }
    return state.config.defaults[key];
  }

  function fmtTime(ts) {
    if (!ts) { return '-'; }
    var seconds = ts > 1e11 ? Math.round(ts / 1000) : ts;
    var date = new Date(seconds * 1000);
    if (isNaN(date.getTime())) { return String(ts); }
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    return pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds());
  }

  /* ---------- DAG 计算 ---------- */
  function routerOf(config) { return (config || state.config).router || {}; }

  function allModels() { return Object.keys(routerOf()).sort(); }

  // 反向邻接：model -> 哪些节点的 children 指向它
  function parentMap(config) {
    var router = routerOf(config);
    var map = {};
    Object.keys(router).forEach(function (name) { map[name] = []; });
    Object.keys(router).forEach(function (name) {
      (router[name].children || []).forEach(function (child) {
        if (map[child]) { map[child].push(name); }
      });
    });
    return map;
  }

  function childSet(config) {
    var used = {};
    var router = routerOf(config);
    Object.keys(router).forEach(function (name) {
      (router[name].children || []).forEach(function (child) { used[child] = true; });
    });
    return used;
  }

  // 入口（首选/叶子）：显式 entry:true 优先；否则按「没有任何节点指向它」推断
  function entryModels(config) {
    var router = routerOf(config);
    var marked = Object.keys(router).filter(function (name) { return router[name].entry === true; });
    if (marked.length) { return marked.sort(); }
    var used = childSet(config);
    var inferred = Object.keys(router).filter(function (name) { return !used[name]; });
    return (inferred.length ? inferred : Object.keys(router)).sort();
  }

  // 到 sink 的最长距离（边数）：sink=0 是兜底根，数值越大越靠上（越优先）
  function depthToSink(config) {
    var router = routerOf(config);
    var memo = {};
    function walk(name, stack) {
      if (memo[name] !== undefined) { return memo[name]; }
      if (stack.indexOf(name) >= 0) { return 0; }
      var kids = (router[name] && router[name].children) || [];
      var nextStack = stack.concat([name]);
      var best = 0;
      kids.forEach(function (child) {
        if (!router[child]) { return; }
        best = Math.max(best, 1 + walk(child, nextStack));
      });
      memo[name] = best;
      return best;
    }
    Object.keys(router).forEach(function (name) { walk(name, []); });
    return memo;
  }

  // 环检测：DFS 三色
  function findCycle(config) {
    var router = routerOf(config);
    var color = {};
    var stack = [];
    var found = null;
    function visit(name) {
      if (found) { return; }
      if (color[name] === 1) {
        var start = stack.indexOf(name);
        found = stack.slice(start >= 0 ? start : 0).concat([name]);
        return;
      }
      if (color[name] === 2) { return; }
      color[name] = 1;
      stack.push(name);
      ((router[name] && router[name].children) || []).forEach(function (child) {
        if (router[child]) { visit(child); }
      });
      stack.pop();
      color[name] = 2;
    }
    Object.keys(router).forEach(visit);
    return found;
  }

  function maxDepth(config) {
    var memo = depthToSink(config);
    var maximum = 0;
    Object.keys(memo).forEach(function (name) { maximum = Math.max(maximum, memo[name]); });
    return maximum;
  }

  // 本地校验：与后端 validateConfig 对齐（provider 不存在 / children 环 / 自引用 / remote_model 空串 / special_rules 动作）
  function validateConfig(config) {
    var errors = [];
    var router = config.router || {};
    var providers = config.providers || {};

    if (!Object.keys(providers).length) { errors.push('至少需要一个供应商。'); }
    if (!Object.keys(router).length) { errors.push('至少需要一个模型节点。'); }

    Object.keys(providers).forEach(function (name) {
      var provider = providers[name] || {};
      if (!provider.base_url) { errors.push('供应商「' + name + '」缺少 base_url。'); }
      if (provider.auth_mode === 'bearer' && !provider.api_key_env) {
        errors.push('供应商「' + name + '」使用 bearer 鉴权，必须填写 api_key_env 变量名。');
      }
    });

    Object.keys(router).forEach(function (name) {
      var node = router[name] || {};
      if (node.remote_model === '') {
        errors.push('节点「' + name + '」的 remote_model 是空串；留空请直接删除该字段。');
      }
      if (!node.provider) { errors.push('节点「' + name + '」未指定供应商。'); }
      else if (!providers[node.provider]) { errors.push('节点「' + name + '」的供应商「' + node.provider + '」不存在。'); }
      var seen = {};
      (node.children || []).forEach(function (child) {
        if (child === name) { errors.push('节点「' + name + '」不能把自己列为子节点。'); }
        else if (!router[child]) { errors.push('节点「' + name + '」的子节点「' + child + '」不存在。'); }
        else if (seen[child]) { errors.push('节点「' + name + '」的子节点「' + child + '」重复。'); }
        seen[child] = true;
      });
    });

    var cycle = findCycle(config);
    if (cycle) { errors.push('降级链路存在环：' + cycle.join(' → ')); }

    var statusList = (config.retry_on && config.retry_on.http_status) || [];
    statusList.forEach(function (code) {
      var value = Number(code);
      if (!(value >= 100 && value <= 599)) { errors.push('HTTP 状态码「' + code + '」不合法。'); }
    });

    var allowedActions = {};
    ACTION_META.forEach(function (meta) { allowedActions[meta.key] = true; });
    var rules = config.special_rules || [];
    if (!Array.isArray(rules)) {
      errors.push('special_rules 必须是数组。');
    } else {
      var names = {};
      rules.forEach(function (rule, index) {
        var label = (rule && rule.name) ? rule.name : ('第 ' + (index + 1) + ' 条');
        if (!rule || typeof rule !== 'object') { errors.push('特殊规则 ' + label + ' 必须是对象。'); return; }
        if (!rule.name || !String(rule.name).trim()) { errors.push('特殊规则第 ' + (index + 1) + ' 条缺少名称。'); }
        else if (names[rule.name]) { errors.push('特殊规则名称「' + rule.name + '」重复。'); }
        names[rule.name] = true;
        if (!Array.isArray(rule.patterns) || !rule.patterns.length) {
          errors.push('特殊规则「' + label + '」至少需要一个 pattern。');
        } else {
          rule.patterns.forEach(function (pattern) {
            if (typeof pattern !== 'string' || !pattern.trim()) {
              errors.push('特殊规则「' + label + '」存在空 pattern。');
            }
          });
        }
        (rule.actions || []).forEach(function (action) {
          if (!allowedActions[action]) { errors.push('特殊规则「' + label + '」含未知动作「' + action + '」。'); }
        });
      });
    }

    return errors;
  }

  // 非阻断提醒：不改配置也能保存，只提示风险
  function configWarnings(config) {
    var warnings = [];
    var edges = maxDepth(config);
    if (edges > 5) {
      warnings.push('最长降级路径为 ' + (edges + 1) + ' 层节点，建议不超过 6 层，否则单次失败会等待很久。');
    }
    if (!entryModels(config).length) { warnings.push('没有首选模型，客户端请求会找不到路由。'); }
    var disabled = Object.keys(config.providers || {}).filter(function (name) {
      return config.providers[name].enabled === false;
    });
    if (disabled.length) { warnings.push('已停用供应商：' + disabled.join('、') + '（相关节点会被跳过）。'); }
    Object.keys(config.router || {}).forEach(function (name) {
      var node = config.router[name];
      var retries = Number(inheritOf(node, 'retries')) || 0;
      var interval = Number(inheritOf(node, 'retry_interval_ms')) || 0;
      if (retries >= 3 && interval >= 2000) {
        warnings.push('节点 ' + name + ' 重试 ' + retries + ' 次、首次间隔 ' + interval + 'ms，最坏等待偏长。');
      }
    });
    (config.special_rules || []).forEach(function (rule) {
      if (rule && rule.cooldown && (rule.actions || []).indexOf('cooldown_node') < 0) {
        warnings.push('特殊规则「' + rule.name + '」开了 cooldown，但没勾选 cooldown_node，冷却不会生效。');
      }
      if (rule && rule.cooldown && (rule.actions || []).indexOf('switch_next') >= 0 && (rule.actions || []).indexOf('signal_compact') >= 0) {
        warnings.push('特殊规则「' + rule.name + '」同时开了 switch_next 与 signal_compact，注意「压缩上下文」提示可能和换模型叠加。');
      }
    });
    return warnings;
  }

  /* ---------- 渲染：状态码 / 关键词 ---------- */
  function renderStatusTags() {
    var quick = $('statusQuick');
    quick.textContent = '';
    var list = state.config.retry_on.http_status || [];
    QUICK_STATUS.forEach(function (code) {
      var active = list.indexOf(code) >= 0;
      var button = el('button', 'quick-tag' + (active ? ' on' : ''), String(code));
      button.type = 'button';
      button.title = active ? ('点击移除 ' + code) : ('点击添加 ' + code);
      button.addEventListener('click', function () {
        var current = state.config.retry_on.http_status || [];
        if (current.indexOf(code) >= 0) {
          state.config.retry_on.http_status = current.filter(function (item) { return item !== code; });
        } else {
          state.config.retry_on.http_status = current.concat([code]);
        }
        markDirty(); renderStatusTags();
      });
      quick.appendChild(button);
    });

    var box = $('statusTags');
    box.textContent = '';
    var sorted = (state.config.retry_on.http_status || []).slice().sort(function (a, b) { return a - b; });
    if (!sorted.length) {
      box.appendChild(el('span', 'muted small', '未选择任何状态码触发重试。'));
      return;
    }
    sorted.forEach(function (code) {
      var tag = el('span', 'tag', String(code));
      var remove = el('button', '', '×');
      remove.type = 'button';
      remove.title = '移除 ' + code;
      remove.setAttribute('aria-label', '移除状态码 ' + code);
      remove.addEventListener('click', function () {
        state.config.retry_on.http_status = state.config.retry_on.http_status.filter(function (item) { return item !== code; });
        markDirty(); renderStatusTags();
      });
      tag.appendChild(remove);
      box.appendChild(tag);
    });
  }

  function renderKeywordList() {
    var box = $('keywordList');
    box.textContent = '';
    KEYWORD_META.forEach(function (meta) {
      var label = el('label', 'kw' + (meta.deterministic ? ' det' : ''));
      var input = el('input');
      input.type = 'checkbox';
      input.checked = !!state.config.retry_on.keywords[meta.key];
      input.setAttribute('aria-label', meta.title);
      input.addEventListener('change', function () {
        state.config.retry_on.keywords[meta.key] = input.checked;
        markDirty();
      });
      var textBox = el('div');
      var titleRow = el('div', 'kw-title');
      titleRow.appendChild(document.createTextNode(meta.title));
      if (meta.deterministic) { titleRow.appendChild(el('span', 'chip warn', '确定性错误')); }
      textBox.appendChild(titleRow);
      textBox.appendChild(el('div', 'kw-desc', meta.desc));
      label.appendChild(input);
      label.appendChild(textBox);
      box.appendChild(label);
    });
  }

  /* ---------- 渲染：全局 defaults ---------- */
  function renderDefaults() {
    var box = $('defaultsGrid');
    box.textContent = '';
    DEFAULT_META.forEach(function (meta) {
      var item = el('div', 'form-item');
      item.appendChild(el('label', '', meta.label));
      var input = el('input');
      input.type = 'number';
      input.step = meta.step;
      input.min = meta.min;
      input.value = fmtNum(state.config.defaults[meta.key]) === '-' ? '' : state.config.defaults[meta.key];
      input.title = meta.hint;
      input.setAttribute('aria-label', '默认 ' + meta.label);
      input.addEventListener('change', function () {
        var raw = input.value.trim();
        if (raw === '') { return; }
        var parsed = Number(raw);
        if (isNaN(parsed)) { input.value = state.config.defaults[meta.key]; showToast('「' + meta.label + '」必须是数字', true); return; }
        state.config.defaults[meta.key] = parsed;
        markDirty(); renderDag();
      });
      item.appendChild(input);
      item.appendChild(el('div', 'desc', meta.hint));
      box.appendChild(item);
    });
  }

  /* ---------- 渲染：供应商 ---------- */
  function renderProviders() {
    var box = $('providerList');
    box.textContent = '';
    var providers = state.config.providers || {};

    Object.keys(providers).sort().forEach(function (name) {
      var provider = providers[name];
      var card = el('div', 'provider-card' + (provider.enabled === false ? ' off' : ''));

      var head = el('div', 'prow');
      head.appendChild(el('span', 'pname', name));

      var enabledLabel = el('label', 'check');
      var enabled = el('input');
      enabled.type = 'checkbox';
      enabled.checked = provider.enabled !== false;
      enabled.addEventListener('change', function () {
        provider.enabled = enabled.checked;
        markDirty(); renderProviders(); renderDag();
      });
      enabledLabel.appendChild(enabled);
      enabledLabel.appendChild(document.createTextNode('启用'));
      head.appendChild(enabledLabel);

      var modeSelect = el('select');
      ['passthrough', 'bearer'].forEach(function (mode) {
        var option = el('option', '', mode);
        option.value = mode;
        if ((provider.auth_mode || 'passthrough') === mode) { option.selected = true; }
        modeSelect.appendChild(option);
      });
      modeSelect.setAttribute('aria-label', name + ' auth_mode');
      modeSelect.addEventListener('change', function () {
        provider.auth_mode = modeSelect.value;
        markDirty(); renderProviders();
      });
      head.appendChild(modeSelect);

      var removeBtn = el('button', 'btn icon danger', '删除');
      removeBtn.type = 'button';
      removeBtn.title = '删除供应商 ' + name;
      removeBtn.addEventListener('click', function () { removeProvider(name); });
      head.appendChild(removeBtn);
      card.appendChild(head);

      card.appendChild(fieldRow(name, 'base_url', '地址', provider.base_url || '', 'https://…', 'text', function (value) {
        provider.base_url = value;
      }));
      card.appendChild(fieldRow(name, 'api_key_env', 'api_key_env', provider.api_key_env || '', '只填变量名，不填密钥', 'text', function (value) {
        if (value) { provider.api_key_env = value; } else { delete provider.api_key_env; }
      }));
      card.appendChild(fieldRow(name, 'provider_failure_threshold', '熔断阈值', provider.provider_failure_threshold === undefined ? '' : provider.provider_failure_threshold, '默认 3', 'number', function (value) {
        if (value === '') { delete provider.provider_failure_threshold; return; }
        provider.provider_failure_threshold = Number(value);
      }));

      box.appendChild(card);
    });

    if (!Object.keys(providers).length) { box.appendChild(el('span', 'muted small', '还没有供应商。')); }

    var selectBox = $('newNodeProvider');
    var previous = selectBox.value;
    selectBox.textContent = '';
    Object.keys(providers).sort().forEach(function (name) {
      var option = el('option', '', name + (providers[name].enabled === false ? '（已停用）' : ''));
      option.value = name;
      selectBox.appendChild(option);
    });
    if (previous && providers[previous]) { selectBox.value = previous; }
  }

  function fieldRow(providerName, key, label, value, placeholder, type, consume) {
    var row = el('div', 'pmeta-row');
    row.appendChild(el('span', '', label));
    var input = el('input');
    input.type = type;
    input.value = value;
    input.placeholder = placeholder;
    input.setAttribute('aria-label', providerName + ' ' + key);
    input.addEventListener('change', function () {
      consume(input.value.trim());
      markDirty();
    });
    row.appendChild(input);
    return row;
  }

  function removeProvider(name) {
    var models = Object.keys(state.config.router || {}).filter(function (model) {
      return state.config.router[model].provider === name;
    });
    var message = '确定删除供应商「' + name + '」？';
    if (models.length) { message += '\n仍在使用它的模型：' + models.join('、') + '\n删除后这些节点会失去供应商，需要先改到别的供应商。'; }
    if (!window.confirm(message)) { return; }
    delete state.config.providers[name];
    markDirty(); renderProviders(); renderDag();
  }

  function addProvider() {
    var name = $('newProviderName').value.trim();
    var baseUrl = $('newProviderUrl').value.trim();
    var envName = $('newProviderEnv').value.trim();
    if (!name) { showToast('请填写供应商名', true); return; }
    if (state.config.providers[name]) { showToast('供应商「' + name + '」已存在', true); return; }
    state.config.providers[name] = {
      base_url: baseUrl,
      auth_mode: envName ? 'bearer' : 'passthrough',
      api_key_env: envName,
      enabled: true
    };
    $('newProviderName').value = '';
    $('newProviderUrl').value = '';
    $('newProviderEnv').value = '';
    markDirty(); renderProviders(); renderDag();
    showToast('已新增供应商：' + name);
  }

  /* ---------- 渲染：分层 DAG（上=首选，下=兜底） ---------- */
  var cardIndex = {};   // model -> 卡片元素（本轮渲染）
  var edgePairs = [];   // [{from, to}]，from 在上层，to 在下层

  function buildNodeCard(modelName, depth, parents) {
    var router = state.config.router;
    var node = router[modelName];
    var kids = node.children || [];
    var card = el('div', 'node-card');
    if (!kids.length) { card.classList.add('sink'); }
    if (parents.length > 1) { card.classList.add('hub'); }
    card.setAttribute('data-model', modelName);

    var head = el('div', 'node-head');
    var nameBox = el('div', 'node-name');
    var nameInput = el('input');
    nameInput.type = 'text';
    nameInput.value = modelName;
    nameInput.setAttribute('aria-label', '模型名');
    nameInput.title = '改名会同步更新所有指向它的降级链路';
    nameInput.addEventListener('change', function () {
      var next = nameInput.value.trim();
      if (!next || next === modelName) { nameInput.value = modelName; return; }
      renameModel(modelName, next);
    });
    nameBox.appendChild(nameInput);
    head.appendChild(nameBox);

    var badgeRow = el('div', 'node-meta');
    badgeRow.appendChild(el('span', 'chip prov', node.provider || '未指定供应商'));
    badgeRow.appendChild(el('span', 'chip', '第 ' + (depth + 1) + ' 层'));
    if (!kids.length) { badgeRow.appendChild(el('span', 'chip depth', '根 · 最后兜底')); }
    if (parents.length > 1) { badgeRow.appendChild(el('span', 'chip hubchip', '汇聚点 · ' + parents.length + ' 条入边')); }
    if (node.remote_model) { badgeRow.appendChild(el('span', 'chip', '上游 ' + node.remote_model)); }
    nameBox.appendChild(badgeRow);

    var actions = el('div', 'node-actions');
    var entryBtn = el('button', 'btn icon' + (node.entry ? ' on' : ''), node.entry ? '首选 ✓' : '设为首选');
    entryBtn.type = 'button';
    entryBtn.title = '标记为可被客户端直接请求的入口模型';
    entryBtn.addEventListener('click', function () {
      if (node.entry) { delete node.entry; } else { node.entry = true; }
      markDirty(); renderDag();
    });
    actions.appendChild(entryBtn);

    var delBtn = el('button', 'btn icon danger', '删除');
    delBtn.type = 'button';
    delBtn.addEventListener('click', function () { removeModel(modelName); });
    actions.appendChild(delBtn);
    head.appendChild(actions);
    card.appendChild(head);

    // 上游真实模型 ID：留空表示与节点名同名
    var remote = el('div', 'node-remote');
    remote.appendChild(el('span', 'muted', 'remote_model'));
    var remoteInput = el('input');
    remoteInput.type = 'text';
    remoteInput.value = node.remote_model || '';
    remoteInput.placeholder = '留空＝与模型名同名';
    remoteInput.setAttribute('aria-label', modelName + ' remote_model');
    remoteInput.addEventListener('change', function () {
      var value = remoteInput.value.trim();
      if (value) { node.remote_model = value; } else { delete node.remote_model; }
      markDirty(); renderDag();
    });
    remote.appendChild(remoteInput);
    card.appendChild(remote);

    // 每节点策略参数：留空 = 继承 defaults
    var params = el('div', 'node-params');
    PARAM_META.forEach(function (meta) {
      var wrapper = el('div', 'param');
      wrapper.appendChild(el('label', '', meta.label));
      var input = el('input');
      input.type = 'number';
      input.step = meta.step;
      input.min = meta.min;
      input.value = (node[meta.key] === undefined || node[meta.key] === null) ? '' : node[meta.key];
      input.placeholder = '继承 ' + fmtNum(state.config.defaults[meta.key]);
      input.title = meta.hint;
      input.setAttribute('aria-label', modelName + ' ' + meta.label);
      input.addEventListener('change', function () {
        var raw = input.value.trim();
        if (raw === '') { delete node[meta.key]; }
        else {
          var parsed = Number(raw);
          if (isNaN(parsed)) {
            input.value = '';
            delete node[meta.key];
            showToast('「' + meta.label + '」必须是数字', true);
          } else { node[meta.key] = parsed; }
        }
        markDirty();
      });
      wrapper.appendChild(input);
      params.appendChild(wrapper);
    });
    card.appendChild(params);

    // 降级去向（children，按优先级排列）
    var outBar = el('div', 'edge-bar out');
    outBar.appendChild(el('span', 'edge-label', '降级去向 →'));
    if (!kids.length) { outBar.appendChild(el('span', 'chip', '无（到此为止）')); }
    kids.forEach(function (child) {
      var chip = el('span', 'chip edge-chip', child);
      var remove = el('button', 'btn icon danger', '×');
      remove.type = 'button';
      remove.title = '移除降级目标 ' + child;
      remove.addEventListener('click', function () {
        node.children = node.children.filter(function (item) { return item !== child; });
        markDirty(); renderDag();
      });
      chip.appendChild(remove);
      outBar.appendChild(chip);
    });
    var select = el('select');
    var emptyOption = el('option', '', '＋ 添加降级目标');
    emptyOption.value = '';
    select.appendChild(emptyOption);
    allModels().forEach(function (candidate) {
      if (candidate === modelName) { return; }
      if (kids.indexOf(candidate) >= 0) { return; }
      if (wouldCreateCycle(modelName, candidate)) { return; }
      var option = el('option', '', candidate + '（' + (router[candidate].provider || '?') + '）');
      option.value = candidate;
      select.appendChild(option);
    });
    select.addEventListener('change', function () {
      if (!select.value) { return; }
      node.children = kids.concat([select.value]);
      markDirty(); renderDag();
    });
    outBar.appendChild(select);
    card.appendChild(outBar);

    // 被谁降级进来（反向邻接，只读）
    var inBar = el('div', 'edge-bar in');
    inBar.appendChild(el('span', 'edge-label', '被谁降级进来 ←'));
    if (!parents.length) {
      inBar.appendChild(el('span', 'chip', '无（顶层，可直接作为首选）'));
    } else {
      parents.forEach(function (parent) { inBar.appendChild(el('span', 'chip in-chip', parent)); });
    }
    card.appendChild(inBar);

    cardIndex[modelName] = card;
    return card;
  }

  function renderDag() {
    var forest = $('forest');
    if (!forest) { return; }
    forest.textContent = '';
    cardIndex = {};
    edgePairs = [];

    var router = state.config.router || {};
    var models = Object.keys(router);
    if (!models.length) {
      $('forestEmpty').classList.remove('hidden');
      renderSimEntries();
      return;
    }
    $('forestEmpty').classList.add('hidden');

    var depthMap = depthToSink(state.config);
    var parents = parentMap(state.config);
    var maximum = 0;
    models.forEach(function (name) { maximum = Math.max(maximum, depthMap[name] || 0); });

    // 分层：depth=max 在最上（首选），depth=0 在最下（兜底根）
    var layers = [];
    for (var i = 0; i <= maximum; i += 1) { layers.push([]); }
    models.forEach(function (name) { layers[maximum - (depthMap[name] || 0)].push(name); });

    var entries = entryModels(state.config);
    layers.forEach(function (row) {
      row.sort(function (a, b) {
        var aEntry = entries.indexOf(a) >= 0 ? 0 : 1;
        var bEntry = entries.indexOf(b) >= 0 ? 0 : 1;
        if (aEntry !== bEntry) { return aEntry - bEntry; }
        return a < b ? -1 : (a > b ? 1 : 0);
      });
    });

    var canvas = el('div', 'dag');
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'dag-edges');
    canvas.appendChild(svg);

    var rowsBox = el('div', 'dag-rows');
    layers.forEach(function (row, rowIndex) {
      var level = maximum - rowIndex;
      var rowBox = el('div', 'dag-row');
      rowBox.setAttribute('data-level', String(level));
      var rowHead = el('div', 'dag-row-label');
      if (level === 0) { rowHead.textContent = '兜底层（根 · 最后尝试）'; }
      else if (level === maximum) { rowHead.textContent = '首选层（顶层 · 最先尝试）'; }
      else { rowHead.textContent = '第 ' + (level + 1) + ' 层'; }
      rowBox.appendChild(rowHead);
      var rowCards = el('div', 'dag-cards');
      row.forEach(function (name) { rowCards.appendChild(buildNodeCard(name, level, parents[name] || [])); });
      rowBox.appendChild(rowCards);
      rowsBox.appendChild(rowBox);
      if (rowIndex < layers.length - 1) {
        var arrowRow = el('div', 'dag-arrow');
        arrowRow.textContent = '↓ 失败后降级到此层';
        rowsBox.appendChild(arrowRow);
      }
    });
    canvas.appendChild(rowsBox);
    forest.appendChild(canvas);

    models.forEach(function (name) {
      ((router[name].children) || []).forEach(function (child) {
        if (router[child]) { edgePairs.push({ from: name, to: child }); }
      });
    });

    renderSimEntries();
    requestAnimationFrame(drawEdges);
  }

  // 用 SVG 贝塞尔曲线画 DAG 边（禁止字符画）
  function drawEdges() {
    var canvas = document.querySelector('#forest .dag');
    if (!canvas) { return; }
    var svg = canvas.querySelector('svg.dag-edges');
    if (!svg) { return; }
    var box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) { return; }
    svg.setAttribute('viewBox', '0 0 ' + box.width + ' ' + box.height);
    svg.setAttribute('width', String(box.width));
    svg.setAttribute('height', String(box.height));
    while (svg.firstChild) { svg.removeChild(svg.firstChild); }

    edgePairs.forEach(function (pair) {
      var fromCard = cardIndex[pair.from];
      var toCard = cardIndex[pair.to];
      if (!fromCard || !toCard) { return; }
      var a = fromCard.getBoundingClientRect();
      var b = toCard.getBoundingClientRect();
      var x1 = a.left - box.left + a.width / 2;
      var y1 = a.bottom - box.top;
      var x2 = b.left - box.left + b.width / 2;
      var y2 = b.top - box.top;
      var midY = (y1 + y2) / 2;
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M ' + x1 + ' ' + y1 + ' C ' + x1 + ' ' + midY + ', ' + x2 + ' ' + midY + ', ' + x2 + ' ' + y2);
      path.setAttribute('class', 'edge-line');
      svg.appendChild(path);
      var dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      dot.setAttribute('cx', String(x2));
      dot.setAttribute('cy', String(y2));
      dot.setAttribute('r', '3');
      dot.setAttribute('class', 'edge-dot');
      svg.appendChild(dot);
    });
  }

  // 首选项下拉（试运行用）
  function renderSimEntries() {
    var simEntry = $('simEntry');
    if (!simEntry) { return; }
    var previous = simEntry.value;
    simEntry.textContent = '';
    var entries = entryModels(state.config);
    entries.forEach(function (entry) {
      var option = el('option', '', entry);
      option.value = entry;
      simEntry.appendChild(option);
    });
    if (previous && entries.indexOf(previous) >= 0) { simEntry.value = previous; }
  }

  /* ---------- 渲染：特殊规则 ---------- */
  function renderRules() {
    var box = $('rulesBox');
    box.textContent = '';
    var rules = state.config.special_rules || [];
    if (!rules.length) {
      box.appendChild(el('div', 'empty', '还没有特殊规则。特殊规则用于「超长上下文」「历史损坏」这类需要换模型或开新会话的确定性错误。'));
      return;
    }
    rules.forEach(function (rule, index) {
      box.appendChild(buildRuleCard(rule, index));
    });
  }

  function buildRuleCard(rule, index) {
    var card = el('div', 'rule-card');

    var head = el('div', 'rule-head');
    var nameInput = el('input');
    nameInput.type = 'text';
    nameInput.value = rule.name || '';
    nameInput.placeholder = '规则名，如 context_overflow';
    nameInput.setAttribute('aria-label', '第 ' + (index + 1) + ' 条规则名称');
    nameInput.addEventListener('change', function () {
      var next = nameInput.value.trim();
      if (!next) { nameInput.value = rule.name || ''; showToast('规则名不能为空', true); return; }
      rule.name = next;
      markDirty();
    });
    head.appendChild(nameInput);

    var enabledLabel = el('label', 'check');
    var enabled = el('input');
    enabled.type = 'checkbox';
    enabled.checked = rule.enabled !== false;
    enabled.addEventListener('change', function () {
      rule.enabled = enabled.checked;
      markDirty();
    });
    enabledLabel.appendChild(enabled);
    enabledLabel.appendChild(document.createTextNode('启用'));
    head.appendChild(enabledLabel);

    var cooldownLabel = el('label', 'check');
    var cooldown = el('input');
    cooldown.type = 'checkbox';
    cooldown.checked = rule.cooldown === true;
    cooldown.addEventListener('change', function () {
      rule.cooldown = cooldown.checked;
      markDirty();
    });
    cooldownLabel.appendChild(cooldown);
    cooldownLabel.appendChild(document.createTextNode('命中后冷却节点'));
    head.appendChild(cooldownLabel);

    var delBtn = el('button', 'btn icon danger', '删除');
    delBtn.type = 'button';
    delBtn.addEventListener('click', function () {
      if (!window.confirm('确定删除特殊规则「' + (rule.name || ('第 ' + (index + 1) + ' 条')) + '」？')) { return; }
      state.config.special_rules.splice(index, 1);
      markDirty(); renderRules();
    });
    head.appendChild(delBtn);
    card.appendChild(head);

    // patterns
    var patternField = el('div', 'rule-field');
    patternField.appendChild(el('label', '', '匹配文案（小写包含即命中，可多个）'));
    var patternTags = el('div', 'tags');
    (rule.patterns || []).forEach(function (pattern, patternIndex) {
      var tag = el('span', 'tag', pattern);
      var remove = el('button', '', '×');
      remove.type = 'button';
      remove.title = '移除 ' + pattern;
      remove.addEventListener('click', function () {
        rule.patterns.splice(patternIndex, 1);
        markDirty(); renderRules();
      });
      tag.appendChild(remove);
      patternTags.appendChild(tag);
    });
    if (!(rule.patterns || []).length) { patternTags.appendChild(el('span', 'muted small', '还没有 pattern。')); }
    patternField.appendChild(patternTags);

    var addRow = el('div', 'inline-form');
    var patternInput = el('input');
    patternInput.type = 'text';
    patternInput.placeholder = '如 context_length_exceeded';
    patternInput.setAttribute('aria-label', '新增 pattern');
    var addBtn = el('button', 'btn small', '添加 pattern');
    addBtn.type = 'button';
    addBtn.addEventListener('click', function () {
      var value = patternInput.value.trim();
      if (!value) { showToast('请填写 pattern', true); return; }
      if ((rule.patterns || []).indexOf(value) >= 0) { showToast('pattern 已存在', true); return; }
      rule.patterns = (rule.patterns || []).concat([value]);
      markDirty(); renderRules();
    });
    patternInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); addBtn.click(); }
    });
    addRow.appendChild(patternInput);
    addRow.appendChild(addBtn);
    patternField.appendChild(addRow);
    card.appendChild(patternField);

    // actions 多选
    var actionField = el('div', 'rule-field');
    actionField.appendChild(el('label', '', '命中后的动作（可多选）'));
    var actionGrid = el('div', 'action-grid');
    ACTION_META.forEach(function (meta) {
      var label = el('label', 'check');
      var checkbox = el('input');
      checkbox.type = 'checkbox';
      checkbox.checked = (rule.actions || []).indexOf(meta.key) >= 0;
      checkbox.title = meta.desc;
      checkbox.addEventListener('change', function () {
        var actions = (rule.actions || []).slice();
        if (checkbox.checked) { if (actions.indexOf(meta.key) < 0) { actions.push(meta.key); } }
        else { actions = actions.filter(function (item) { return item !== meta.key; }); }
        rule.actions = actions;
        markDirty();
      });
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(meta.title));
      label.title = meta.desc;
      actionGrid.appendChild(label);
    });
    actionField.appendChild(actionGrid);
    card.appendChild(actionField);

    return card;
  }

  function addRule() {
    state.config.special_rules = state.config.special_rules || [];
    var base = 'rule_' + (state.config.special_rules.length + 1);
    var name = base;
    var counter = 1;
    while (state.config.special_rules.some(function (rule) { return rule.name === name; })) {
      counter += 1;
      name = base + '_' + counter;
    }
    state.config.special_rules.push({
      name: name,
      patterns: [],
      actions: ['switch_next'],
      cooldown: false,
      enabled: true
    });
    markDirty(); renderRules();
  }

  /* ---------- 图操作 ---------- */
  function wouldCreateCycle(parent, child) {
    var router = state.config.router;
    var seen = {};
    function reach(name) {
      if (name === parent) { return true; }
      if (seen[name]) { return false; }
      seen[name] = true;
      var kids = (router[name] && router[name].children) || [];
      for (var i = 0; i < kids.length; i += 1) {
        if (reach(kids[i])) { return true; }
      }
      return false;
    }
    return reach(child);
  }

  function removeModel(modelName) {
    var router = state.config.router;
    var parents = Object.keys(router).filter(function (name) {
      return (router[name].children || []).indexOf(modelName) >= 0;
    });
    var message = '确定删除模型「' + modelName + '」？';
    if (parents.length) { message += '\n它会被从以下节点移除：' + parents.join('、'); }
    if (!window.confirm(message)) { return; }
    parents.forEach(function (name) {
      router[name].children = router[name].children.filter(function (item) { return item !== modelName; });
    });
    delete router[modelName];
    markDirty(); renderDag();
  }

  function renameModel(oldName, newName) {
    if (state.config.router[newName]) { showToast('模型名「' + newName + '」已存在', true); renderDag(); return; }
    var router = state.config.router;
    var rebuilt = {};
    Object.keys(router).forEach(function (name) {
      var key = (name === oldName) ? newName : name;
      var node = deepCopy(router[name]);
      node.children = (node.children || []).map(function (child) {
        return child === oldName ? newName : child;
      });
      rebuilt[key] = node;
    });
    state.config.router = rebuilt;
    markDirty(); renderDag();
    showToast('已改名：' + oldName + ' → ' + newName);
  }

  function addModel() {
    var name = $('newNodeName').value.trim();
    var provider = $('newNodeProvider').value;
    if (!name) { showToast('请填写模型名', true); return; }
    if (state.config.router[name]) { showToast('模型「' + name + '」已存在', true); return; }
    if (!provider) { showToast('请先添加供应商', true); return; }
    state.config.router[name] = { provider: provider, children: [], entry: true };
    $('newNodeName').value = '';
    markDirty(); renderDag();
    showToast('已新增模型：' + name);
  }

  /* ---------- 试运行 ---------- */
  function simulate() {
    var entry = $('simEntry').value;
    if (!entry || !state.config.router[entry]) { showToast('请选择首选模型', true); return; }
    var ignoreCooldown = $('simIgnoreCooldown').checked;
    var cooldowns = {};
    if (!ignoreCooldown && state.status && state.status.cooldowns_seconds) {
      cooldowns = state.status.cooldowns_seconds;
    }

    var rows = [];
    var totalMs = 0;
    var visited = {};

    function walk(model, depth, path) {
      if (depth > 16) { return; }
      var node = state.config.router[model];
      if (!node) { return; }
      if (path.indexOf(model) >= 0) { return; }
      var nextPath = path.concat([model]);
      var provider = node.provider;
      var providerInfo = state.config.providers[provider] || {};
      var retries = Number(inheritOf(node, 'retries')) || 0;
      var interval = Number(inheritOf(node, 'retry_interval_ms')) || 0;
      var multiplier = Number(inheritOf(node, 'backoff_multiplier')) || 1;
      var capRaw = inheritOf(node, 'max_retry_interval_ms');
      var maxInterval = Number(capRaw === undefined ? interval : capRaw) || interval;
      var current = interval;
      var cooling = cooldowns[provider + ':' + model];

      if (visited[model]) {
        rows.push({ model: model, provider: provider, attempt: '-', waitMs: 0, note: 'DAG 汇聚点，本轮已尝试过，不重复发起' });
      } else if (providerInfo.enabled === false) {
        rows.push({ model: model, provider: provider, attempt: '-', waitMs: 0, note: '供应商已停用，跳过' });
      } else if (cooling && cooling > 0) {
        rows.push({ model: model, provider: provider, attempt: '-', waitMs: 0, note: '渠道冷却中，跳过（剩余 ' + Math.round(cooling) + 's）' });
      } else {
        for (var attempt = 0; attempt <= retries; attempt += 1) {
          var waitMs = attempt === 0 ? 0 : Math.round(current);
          totalMs += waitMs;
          rows.push({
            model: model, provider: provider, attempt: attempt + 1, waitMs: waitMs,
            note: attempt === 0 ? '首选尝试' : ('同节点第 ' + attempt + ' 次重试')
          });
          if (attempt < retries) { current = Math.min(current * multiplier, maxInterval); }
        }
      }
      visited[model] = true;
      (node.children || []).forEach(function (child) { walk(child, depth + 1, nextPath); });
    }

    walk(entry, 0, []);

    var tbody = $('simTable').querySelector('tbody');
    tbody.textContent = '';
    if (!rows.length) {
      var emptyTr = el('tr');
      var emptyTd = el('td', 'muted', '没有可执行的尝试。');
      emptyTd.colSpan = 7;
      emptyTr.appendChild(emptyTd);
      tbody.appendChild(emptyTr);
    }
    var cumulative = 0;
    rows.forEach(function (row, index) {
      cumulative += row.waitMs || 0;
      var tr = el('tr');
      tr.appendChild(el('td', '', String(index + 1)));
      tr.appendChild(el('td', '', row.model));
      tr.appendChild(el('td', '', row.provider || '-'));
      tr.appendChild(el('td', '', row.attempt));
      tr.appendChild(el('td', '', row.waitMs ? ('+ ' + row.waitMs + ' ms') : '—'));
      tr.appendChild(el('td', '', (Math.round(cumulative / 100) / 10) + ' s'));
      tr.appendChild(el('td', 'reason', row.note));
      tbody.appendChild(tr);
    });

    var attempts = rows.filter(function (row) { return row.attempt !== '-'; }).length;
    $('simSummary').textContent = '首选 ' + entry + '：' + attempts + ' 次尝试，'
      + rows.length + ' 行，预计等待 ' + (Math.round(totalMs / 100) / 10) + ' s'
      + (ignoreCooldown ? '（已忽略冷却）' : '');
  }

  /* ---------- 状态轮询 ---------- */
  function renderHealth(health) {
    var box = $('healthLights');
    box.textContent = '';
    if (!health || !Object.keys(health).length) {
      box.appendChild(el('span', 'muted small', '还没有健康数据，可点「一键供应商体检」。'));
      return;
    }
    Object.keys(health).sort().forEach(function (name) {
      var item = health[name] || {};
      var pill = el('span', 'pill ' + (item.ok ? 'ok' : 'bad'));
      pill.appendChild(el('span', 'dot'));
      pill.appendChild(document.createTextNode(name));
      var reason = item.ok ? '正常' : (item.reason || '失败');
      pill.title = name + '：' + reason + (item.checked_at ? ('（' + fmtTime(item.checked_at) + '）') : '');
      if (!item.ok) { pill.appendChild(el('span', 'health-reason', ' ' + reason)); }
      box.appendChild(pill);
    });
  }

  function renderStatus(status) {
    if (!status) { return; }
    $('pillRequest').textContent = fmtNum(status.request_count);
    $('pillRetry').textContent = fmtNum(status.retry_count);
    $('pillFallback').textContent = fmtNum(status.fallback_count);
    $('pillCapacity').textContent = fmtNum(status.capacity_count);

    renderHealth(status.provider_health);

    var cooling = status.cooldowns_seconds || {};
    var providerCooling = status.provider_cooldowns_seconds || {};
    var coolBox = $('cooldownBox');
    coolBox.textContent = '';
    var keys = Object.keys(cooling);
    if (!keys.length) {
      coolBox.appendChild(el('span', 'muted small', '当前没有渠道处于冷却状态。'));
    } else {
      keys.sort().forEach(function (key) {
        coolBox.appendChild(el('span', 'pill', key + ' 剩余 ' + Math.round(cooling[key]) + 's'));
      });
    }
    Object.keys(providerCooling).sort().forEach(function (name) {
      coolBox.appendChild(el('span', 'pill', '供应商 ' + name + ' 熔断 ' + Math.round(providerCooling[name]) + 's'));
    });

    var tbody = $('eventTable').querySelector('tbody');
    tbody.textContent = '';
    var events = status.events || [];
    if (!events.length) {
      var tr = el('tr');
      var td = el('td', 'muted', '暂无降级事件。');
      td.colSpan = 5;
      tr.appendChild(td);
      tbody.appendChild(tr);
    } else {
      events.slice(0, 50).forEach(function (event) {
        var row = el('tr');
        row.appendChild(el('td', '', event.ts ? fmtTime(event.ts) : '-'));
        row.appendChild(el('td', '', event.from || '-'));
        row.appendChild(el('td', '', event.to || '-'));
        row.appendChild(el('td', '', event.kind || '-'));
        row.appendChild(el('td', 'reason', event.reason || ''));
        tbody.appendChild(row);
      });
    }
  }

  function pollStatus() {
    return requestJson(API.status, { timeoutMs: 8000 })
      .then(function (status) { state.status = status; renderStatus(status); })
      .catch(function () { /* 状态拉取失败不打扰用户 */ });
  }

  /* ---------- 路由 ---------- */
  function setRoute(route) {
    if (!ROUTES[route]) { route = 'overview'; }
    state.route = route;
    Object.keys(ROUTES).forEach(function (name) {
      var page = $('page-' + name);
      if (page) { page.classList.toggle('hidden', name !== route); }
    });
    Array.prototype.forEach.call(document.querySelectorAll('#nav a'), function (link) {
      link.classList.toggle('on', link.getAttribute('data-route') === route);
    });
    $('pageTitle').textContent = ROUTES[route].title;
    $('pageDesc').textContent = ROUTES[route].desc;
    document.title = ROUTES[route].title + ' · 模型降级路由管理台';
    if (route === 'dag') { requestAnimationFrame(drawEdges); }
  }

  function currentRoute() {
    var hash = (location.hash || '').replace(/^#\/?/, '');
    if (!hash) { return 'overview'; }
    return ROUTES[hash] ? hash : 'overview';
  }

  /* ---------- 配置加载 / 保存 ---------- */
  function markDirty() {
    $('btnSave').disabled = false;
    $('saveHint').textContent = '有未保存修改';
    showBanner('');
  }

  function normalizeConfig(config) {
    var result = deepCopy(config || {});
    result.policy_version = result.policy_version || 1;
    result.defaults = result.defaults || {};
    result.retry_on = result.retry_on || {};
    result.retry_on.http_status = result.retry_on.http_status || [];
    result.retry_on.keywords = result.retry_on.keywords || {};
    KEYWORD_META.forEach(function (meta) {
      if (result.retry_on.keywords[meta.key] === undefined) { result.retry_on.keywords[meta.key] = false; }
    });
    result.providers = result.providers || {};
    Object.keys(result.providers).forEach(function (name) {
      var provider = result.providers[name];
      if (provider.enabled === undefined) { provider.enabled = true; }
    });
    result.router = result.router || {};
    Object.keys(result.router).forEach(function (name) {
      var node = result.router[name];
      node.children = node.children || [];
      // 兼容旧字段名 provider_cooldown_s
      if (node.cooldown_s === undefined && node.provider_cooldown_s !== undefined) {
        node.cooldown_s = node.provider_cooldown_s;
      }
      delete node.provider_cooldown_s;
    });
    if (!Array.isArray(result.special_rules)) {
      result.special_rules = result.special_rules ? [] : [];
    }
    result.special_rules.forEach(function (rule) {
      if (!Array.isArray(rule.patterns)) { rule.patterns = []; }
      if (!Array.isArray(rule.actions)) { rule.actions = ['switch_next']; }
      if (rule.enabled === undefined) { rule.enabled = true; }
      if (rule.cooldown === undefined) { rule.cooldown = false; }
    });
    return result;
  }

  function loadConfig() {
    showBanner('');
    return requestJson(API.config, { timeoutMs: 10000 })
      .then(function (payload) {
        var config = normalizeConfig(payload && payload.config);
        state.config = config;
        state.saved = deepCopy(config);
        state.version = (payload && payload.version) || '';
        $('versionLine').textContent = '后端版本 ' + (state.version || '未知')
          + ' · 配置版本 ' + config.policy_version;
        renderAll();
        $('btnSave').disabled = true;
        $('saveHint').textContent = '无未保存修改';
        showToast('配置已加载');
      })
      .catch(function (err) {
        showBanner('无法读取后端配置：' + err.message + '（确认代理服务在运行，且页面由 /_fallback/admin 提供）');
      });
  }

  // 生成可读 diff 摘要，用于保存前二次确认
  function diffSummary(before, after) {
    var lines = [];
    var beforeRouter = before.router || {};
    var afterRouter = after.router || {};

    Object.keys(afterRouter).forEach(function (name) {
      if (!beforeRouter[name]) {
        lines.push('新增模型 ' + name + ' → ' + afterRouter[name].provider);
        return;
      }
      var b = beforeRouter[name];
      var a = afterRouter[name];
      if (b.provider !== a.provider) { lines.push(name + ' 供应商 ' + b.provider + ' → ' + a.provider); }
      var bKids = (b.children || []).join(',');
      var aKids = (a.children || []).join(',');
      if (bKids !== aKids) { lines.push(name + ' 降级链 [' + (bKids || '无') + '] → [' + (aKids || '无') + ']'); }
      if (!!b.entry !== !!a.entry) { lines.push(name + ' 首选项标记：' + (a.entry ? '开启' : '关闭')); }
      if ((b.remote_model || '') !== (a.remote_model || '')) {
        lines.push(name + ' remote_model ' + (b.remote_model || '（同名）') + ' → ' + (a.remote_model || '（同名）'));
      }
      PARAM_META.forEach(function (meta) {
        var bv = b[meta.key] === undefined ? '继承' : b[meta.key];
        var av = a[meta.key] === undefined ? '继承' : a[meta.key];
        if (String(bv) !== String(av)) { lines.push(name + ' ' + meta.label + ' ' + bv + ' → ' + av); }
      });
    });
    Object.keys(beforeRouter).forEach(function (name) {
      if (!afterRouter[name]) { lines.push('删除模型 ' + name); }
    });

    DEFAULT_META.forEach(function (meta) {
      var bv = (before.defaults || {})[meta.key];
      var av = (after.defaults || {})[meta.key];
      if (String(bv === undefined ? '' : bv) !== String(av === undefined ? '' : av)) {
        lines.push('默认 ' + meta.label + ' ' + (bv === undefined ? '空' : bv) + ' → ' + (av === undefined ? '空' : av));
      }
    });

    var bStatus = ((before.retry_on && before.retry_on.http_status) || []).slice().sort().join(',');
    var aStatus = ((after.retry_on && after.retry_on.http_status) || []).slice().sort().join(',');
    if (bStatus !== aStatus) { lines.push('HTTP 状态码 [' + bStatus + '] → [' + aStatus + ']'); }

    var bKw = (before.retry_on && before.retry_on.keywords) || {};
    var aKw = (after.retry_on && after.retry_on.keywords) || {};
    KEYWORD_META.forEach(function (meta) {
      if (!!bKw[meta.key] !== !!aKw[meta.key]) {
        lines.push('类别 ' + meta.title + '：' + (aKw[meta.key] ? '开启' : '关闭'));
      }
    });

    var bProv = Object.keys(before.providers || {}).sort();
    var aProv = Object.keys(after.providers || {}).sort();
    if (bProv.join(',') !== aProv.join(',')) {
      lines.push('供应商集合 [' + bProv.join(',') + '] → [' + aProv.join(',') + ']');
    }
    aProv.forEach(function (name) {
      var b = (before.providers || {})[name];
      var a = (after.providers || {})[name];
      if (!b || !a) { return; }
      ['base_url', 'auth_mode', 'api_key_env', 'provider_failure_threshold'].forEach(function (key) {
        if (String(b[key] === undefined ? '' : b[key]) !== String(a[key] === undefined ? '' : a[key])) {
          lines.push('供应商 ' + name + ' ' + key + '：' + (b[key] || '空') + ' → ' + (a[key] || '空'));
        }
      });
      if ((b.enabled !== false) !== (a.enabled !== false)) {
        lines.push('供应商 ' + name + '：' + (a.enabled === false ? '停用' : '启用'));
      }
    });

    // 特殊规则：按名字（或位置）对比
    var bRules = before.special_rules || [];
    var aRules = after.special_rules || [];
    var bMap = {};
    bRules.forEach(function (rule, index) { bMap[(rule && rule.name) || ('#' + index)] = rule || {}; });
    var seenRules = {};
    aRules.forEach(function (rule, index) {
      var key = (rule && rule.name) || ('#' + index);
      seenRules[key] = true;
      var b = bMap[key];
      if (!b) { lines.push('新增特殊规则 ' + key); return; }
      if ((b.patterns || []).join('|') !== (rule.patterns || []).join('|')) {
        lines.push('规则 ' + key + ' patterns [' + (b.patterns || []).join(',') + '] → [' + (rule.patterns || []).join(',') + ']');
      }
      if ((b.actions || []).join('|') !== (rule.actions || []).join('|')) {
        lines.push('规则 ' + key + ' 动作 [' + (b.actions || []).join(',') + '] → [' + (rule.actions || []).join(',') + ']');
      }
      if (!!b.enabled !== !!rule.enabled) { lines.push('规则 ' + key + '：' + (rule.enabled !== false ? '启用' : '停用')); }
      if (!!b.cooldown !== !!rule.cooldown) { lines.push('规则 ' + key + ' 冷却：' + (rule.cooldown ? '开启' : '关闭')); }
    });
    Object.keys(bMap).forEach(function (key) {
      if (!seenRules[key]) { lines.push('删除特殊规则 ' + key); }
    });

    return lines;
  }

  function saveConfig() {
    if (!state.config) { return; }
    var errors = validateConfig(state.config);
    if (errors.length) {
      showBanner('配置校验未通过：' + errors.join('；'));
      showToast('配置校验未通过，共 ' + errors.length + ' 处问题', true);
      return;
    }
    var changes = diffSummary(state.saved, state.config);
    if (!changes.length) { showToast('配置没有变化'); return; }
    var warnings = configWarnings(state.config);
    var preview = changes.slice(0, 12).join('\n');
    var more = changes.length > 12 ? ('\n… 还有 ' + (changes.length - 12) + ' 项') : '';
    var warningText = warnings.length ? ('\n\n注意：\n- ' + warnings.join('\n- ')) : '';
    var confirmText = '即将保存以下变更（' + changes.length + ' 项）：\n\n'
      + preview + more + warningText + '\n\n确认保存？';
    if (!window.confirm(confirmText)) { return; }
    $('btnSave').disabled = true;
    requestJson(API.config, { method: 'PUT', body: JSON.stringify(state.config), timeoutMs: 15000 })
      .then(function (payload) {
        if (payload && payload.ok === false) { throw new Error(payload.error || '后端拒绝保存'); }
        state.saved = deepCopy(state.config);
        showBanner('');
        showToast('配置已保存并生效');
        $('btnSave').disabled = true;
        $('saveHint').textContent = '无未保存修改';
        pollStatus();
      })
      .catch(function (err) {
        $('btnSave').disabled = false;
        showBanner('保存失败：' + err.message);
        showToast('保存失败：' + err.message, true);
      });
  }

  function runHealthCheck() {
    var button = $('btnHealth');
    button.disabled = true;
    button.textContent = '体检中…';
    requestJson(API.health, { method: 'POST', body: '{}', timeoutMs: 30000 })
      .then(function (payload) {
        var results = (payload && payload.results) || [];
        var okCount = results.filter(function (item) { return item.ok; }).length;
        var detail = results.map(function (item) {
          return (item.ok ? '✓ ' : '✗ ') + item.provider
            + (item.latency_ms ? (' ' + item.latency_ms + 'ms') : '')
            + (item.ok ? '' : ('（' + (item.detail || '失败') + '）'));
        }).join('；');
        showBanner('供应商体检：' + okCount + '/' + results.length + ' 正常。' + detail, okCount === results.length ? 'info' : '');
        showToast('体检完成：' + okCount + '/' + results.length + ' 正常', okCount !== results.length);
        pollStatus();
      })
      .catch(function (err) {
        showBanner('体检请求失败：' + err.message);
        showToast('体检请求失败', true);
      })
      .then(function () {
        button.disabled = false;
        button.textContent = '一键供应商体检';
      });
  }

  /* ---------- 事件绑定 ---------- */
  function bindEvents() {
    $('btnReload').addEventListener('click', function () {
      if (state.config && diffSummary(state.saved, state.config).length) {
        if (!window.confirm('有未保存的修改，确定放弃并重新加载？')) { return; }
      }
      loadConfig();
    });
    $('btnSave').addEventListener('click', saveConfig);
    $('btnHealth').addEventListener('click', runHealthCheck);
    $('btnRefreshStatus').addEventListener('click', function () {
      pollStatus().then(function () { showToast('状态已刷新'); });
    });
    $('btnAddStatus').addEventListener('click', function () {
      var input = $('newStatus');
      var code = Number(input.value);
      if (!code || code < 100 || code > 599) { showToast('请输入 100-599 之间的状态码', true); return; }
      if (state.config.retry_on.http_status.indexOf(code) >= 0) { showToast('状态码 ' + code + ' 已存在', true); return; }
      state.config.retry_on.http_status.push(code);
      input.value = '';
      markDirty(); renderStatusTags();
    });
    $('newStatus').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); $('btnAddStatus').click(); }
    });
    $('btnAddNode').addEventListener('click', addModel);
    $('btnAddProvider').addEventListener('click', addProvider);
    $('btnAddRule').addEventListener('click', addRule);
    $('btnSimulate').addEventListener('click', simulate);
    $('simEntry').addEventListener('change', function () { $('simSummary').textContent = ''; });

    var resizeTimer = null;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(drawEdges, 150);
    });

    window.addEventListener('hashchange', function () { setRoute(currentRoute()); });

    window.addEventListener('beforeunload', function (event) {
      if (state.config && state.saved && diffSummary(state.saved, state.config).length) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
  }

  function renderAll() {
    renderStatusTags();
    renderKeywordList();
    renderDefaults();
    renderProviders();
    renderDag();
    renderRules();
  }

  function init() {
    bindEvents();
    setRoute(currentRoute());
    loadConfig().then(function () {
      pollStatus();
      setInterval(pollStatus, 10000);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
