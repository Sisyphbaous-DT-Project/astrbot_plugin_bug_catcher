/**
 * Bug Catcher Dashboard 前端逻辑
 */

// 全局状态
const state = {
  page: 1,
  pageSize: 20,
  total: 0,
  bugs: [],
  filters: {
    severity: '',
    status: '',
    result: ''
  },
  pending: new Set()
};

// Bridge SDK（AstrBot 注入的全局对象）
// app.js 在 Bridge SDK 之前加载，需要等待其就绪
let bridge;

async function getBridge() {
  return new Promise((resolve, reject) => {
    const maxWait = 5000; // 最多等 5 秒
    const start = Date.now();
    const check = () => {
      if (window.AstrBotPluginPage) {
        resolve(window.AstrBotPluginPage);
        return;
      }
      if (Date.now() - start > maxWait) {
        reject(new Error('Bridge SDK 加载超时'));
        return;
      }
      setTimeout(check, 50);
    };
    check();
  });
}

// 等待 Bridge SDK 就绪
(async function init() {
  try {
    bridge = await getBridge();
    await bridge.ready();
    console.log('[BugCatcher] Bridge SDK 就绪');
    bindEvents();
    bindButtonRipple();
    await loadStats();
    await loadBugs();
  } catch (e) {
    console.error('[BugCatcher] 初始化失败:', e);
    showError('初始化失败: ' + (e?.message || '未知错误'));
  }
})();

// ------------------------------------------------------------------
// 事件绑定
// ------------------------------------------------------------------

function bindEvents() {
  document.getElementById('btnRefresh').addEventListener('click', () => {
    state.page = 1;
    loadStats();
    loadBugs();
  });

  document.getElementById('filterSeverity').addEventListener('change', (e) => {
    state.filters.severity = e.target.value;
    state.page = 1;
    loadBugs();
  });

  document.getElementById('filterStatus').addEventListener('change', (e) => {
    state.filters.status = e.target.value;
    state.page = 1;
    loadBugs();
  });

  document.getElementById('filterResult').addEventListener('change', (e) => {
    state.filters.result = e.target.value;
    state.page = 1;
    loadBugs();
  });

  document.getElementById('modalClose').addEventListener('click', closeModal);
  document.getElementById('detailModal').addEventListener('click', (e) => {
    if (e.target.id === 'detailModal') closeModal();
  });

  // 确认弹窗事件
  document.getElementById('confirmModalClose').addEventListener('click', closeConfirmModal);
  document.getElementById('confirmNo').addEventListener('click', closeConfirmModal);
  document.getElementById('confirmModal').addEventListener('click', (e) => {
    if (e.target.id === 'confirmModal') closeConfirmModal();
  });
  document.getElementById('confirmYes').addEventListener('click', () => {
    if (confirmCallback) confirmCallback();
    closeConfirmModal();
  });
}

function bindButtonRipple() {
  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest('.btn, .page-btn, .modal-close');
    if (!button || button.disabled) return;

    const rect = button.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height);
    const ripple = document.createElement('span');
    ripple.className = 'btn-ripple';
    ripple.style.width = `${size}px`;
    ripple.style.height = `${size}px`;
    ripple.style.left = `${event.clientX - rect.left - size / 2}px`;
    ripple.style.top = `${event.clientY - rect.top - size / 2}px`;
    button.appendChild(ripple);
    setTimeout(() => ripple.remove(), 520);
  });
}

// ------------------------------------------------------------------
// 数据加载
// ------------------------------------------------------------------

async function loadStats() {
  try {
    const data = unwrapRes(await bridge.apiGet('stats')) || {};
    document.getElementById('statConfirmed').textContent = data.total_confirmed || 0;
    document.getElementById('statSuspected').textContent = data.total_suspected || 0;
    document.getElementById('statTotal').textContent = (data.total_confirmed || 0) + (data.total_suspected || 0);
    document.getElementById('statToday').textContent = data.today_count || 0;
  } catch (e) {
    console.error('加载统计失败:', e);
  }
}

async function loadBugs() {
  const listEl = document.getElementById('bugList');
  listEl.innerHTML = '<div class="loading">加载中...</div>';

  try {
    const params = {
      page: state.page,
      page_size: state.pageSize
    };
    if (state.filters.severity) params.severity = state.filters.severity;
    if (state.filters.status) params.status = state.filters.status;
    if (state.filters.result) params.result = state.filters.result;

    const data = unwrapRes(await bridge.apiGet('bugs', params)) || {};
    state.bugs = data.bugs || [];
    state.total = data.total || 0;

    renderBugs();
    renderPagination();
  } catch (e) {
    showError('加载失败: ' + (e?.message || '未知错误'));
  }
}

// ------------------------------------------------------------------
// 渲染
// ------------------------------------------------------------------

function renderBugs() {
  const listEl = document.getElementById('bugList');

  if (state.bugs.length === 0) {
    listEl.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-title">暂无 Bug 记录</div>
        <div class="empty-state-hint">群聊消息将被自动分析，发现的 bug 会显示在这里</div>
      </div>
    `;
    return;
  }

  listEl.innerHTML = state.bugs.map(bug => {
    const reportHistory = Array.isArray(bug.report_history) ? bug.report_history : [];
    const reportCount = reportHistory.length;
    const reportBadge = reportCount > 1 ? `<span class="report-count">+${reportCount - 1}</span>` : '';
    const firstReporter = reportHistory[0]
      ? reportHistory[0].reporter_name : '未知';
    const safeId = escapeHtml(bug.id);
    const isPending = state.pending.has(safeId);

    return `
    <div class="bug-card" data-id="${safeId}">
      <div class="bug-card-header">
        <span class="bug-severity severity-${escapeHtml(bug.severity)}">${escapeHtml(bug.severity)}</span>
        <span class="bug-result result-${escapeHtml(bug.result)}">${escapeHtml(bug.result)}</span>
        <span class="bug-status status-${escapeHtml(bug.status)}">${escapeHtml(bug.status)}</span>
        ${reportBadge}
        <span class="bug-time">${formatTime(bug.created_at)}</span>
      </div>
      <div class="bug-summary">${escapeHtml(bug.summary)}</div>
      <div class="bug-meta">
        <span class="meta-item"><span class="meta-label">群聊</span>${escapeHtml(bug.umo_display || bug.umo)}</span>
        <span class="meta-item"><span class="meta-label">报告者</span>${escapeHtml(firstReporter)}</span>
        <span class="meta-item"><span class="meta-label">时间</span>${formatTime(bug.created_at)}</span>
      </div>
      <div class="bug-actions">
        <button class="btn btn-primary btn-small" onclick="showDetail('${safeId}')">详情</button>
        ${bug.status === 'open' ? `
          <button class="btn btn-success btn-small" onclick="resolveBug('${safeId}')" ${isPending ? 'disabled' : ''}>${isPending ? '处理中...' : '标记已解决'}</button>
          <button class="btn btn-success btn-small" onclick="ignoreBug('${safeId}')" ${isPending ? 'disabled' : ''}>${isPending ? '处理中...' : '忽略'}</button>
        ` : ''}
        <button class="btn btn-danger btn-small" onclick="deleteBug('${safeId}')" ${isPending ? 'disabled' : ''}>${isPending ? '处理中...' : '删除'}</button>
      </div>
    </div>
  `}).join('');
}

function renderPagination() {
  const totalPages = Math.ceil(state.total / state.pageSize) || 1;
  const el = document.getElementById('pagination');

  let html = '';

  // 上一页
  html += `<button class="page-btn" ${state.page <= 1 ? 'disabled' : ''} onclick="goPage(${state.page - 1})">上一页</button>`;

  // 页码
  const maxButtons = 5;
  let start = Math.max(1, state.page - Math.floor(maxButtons / 2));
  let end = Math.min(totalPages, start + maxButtons - 1);
  if (end - start < maxButtons - 1) {
    start = Math.max(1, end - maxButtons + 1);
  }

  for (let i = start; i <= end; i++) {
    html += `<button class="page-btn ${i === state.page ? 'active' : ''}" onclick="goPage(${i})">${i}</button>`;
  }

  // 下一页
  html += `<button class="page-btn" ${state.page >= totalPages ? 'disabled' : ''} onclick="goPage(${state.page + 1})">下一页</button>`;

  html += `<span class="page-info">共 ${state.total} 条</span>`;

  el.innerHTML = html;
}

// ------------------------------------------------------------------
// 操作
// ------------------------------------------------------------------

async function showDetail(id) {
  const bug = state.bugs.find(b => b.id === id);
  if (!bug) return;

  const pmi = bug.primary_message_index;
  const rawMessages = Array.isArray(bug.raw_messages) ? bug.raw_messages : [];
  const rawMsgs = rawMessages.map((raw, idx) => {
    const m = raw || {};
    const isPrimary = idx === pmi;
    return `
    <div class="msg-line${isPrimary ? ' msg-primary' : ''}">
      ${isPrimary ? '<span class="msg-primary-badge">关键消息</span>' : ''}
      <span class="msg-time">${formatTime(m.timestamp, true)}</span>
      <span class="msg-sender">${escapeHtml(m.sender_name)}</span>
      <span>${escapeHtml(m.content)}</span>
    </div>
  `;
  }).join('');

  // 汇报历史
  const reportHistory = Array.isArray(bug.report_history) ? bug.report_history : [];
  const reportHistoryHtml = reportHistory.length > 0
    ? reportHistory.map(raw => {
      const r = raw || {};
      return `
      <div class="report-entry">
        <span class="report-time">${formatTime(r.reported_at)}</span>
        <span class="report-who">${escapeHtml(r.reporter_name || '未知')}</span>
        <span class="report-where">${escapeHtml(r.umo_display || r.umo || '')}</span>
      </div>
    `}).join('')
    : '<div style="color:var(--text-muted)">无汇报历史</div>';

  // 首次报告者信息
  const firstReport = reportHistory[0] || {};
  const reporterInfo = firstReport.reporter_name
    ? `${escapeHtml(firstReport.reporter_name)} (${escapeHtml(firstReport.reporter_id || '')})`
    : '未知';

  document.getElementById('modalBody').innerHTML = `
    <div class="detail-section">
      <h3>摘要</h3>
      <div class="detail-text">${escapeHtml(bug.summary)}</div>
    </div>
    <div class="detail-section">
      <h3>AI 分析</h3>
      <div class="detail-text">${escapeHtml(bug.analysis)}</div>
    </div>
    <div class="detail-section">
      <h3>来源信息</h3>
      <div class="detail-text">
平台: ${escapeHtml(bug.platform || '-')}
群聊: ${escapeHtml(bug.umo_display || bug.umo || '-')}
首次发现: ${formatTime(bug.created_at)}
首次报告者: ${reporterInfo}
状态: ${escapeHtml(bug.status)}
${bug.note ? '备注: ' + escapeHtml(bug.note) : ''}
      </div>
    </div>
    <div class="detail-section">
      <h3>汇报历史 (${reportHistory.length} 次)</h3>
      <div class="report-history">
        ${reportHistoryHtml}
      </div>
    </div>
    <div class="detail-section">
      <h3>原始消息</h3>
      <div class="detail-messages">
        ${rawMsgs || '<div style="color:var(--text-muted)">无原始消息</div>'}
      </div>
    </div>
  `;

  document.getElementById('detailModal').classList.add('show');
}

function closeModal() {
  document.getElementById('detailModal').classList.remove('show');
}

let confirmCallback = null;

function showConfirmModal(message, onConfirm) {
  document.getElementById('confirmMessage').textContent = message;
  document.getElementById('confirmModal').classList.add('show');
  confirmCallback = onConfirm;
}

function closeConfirmModal() {
  document.getElementById('confirmModal').classList.remove('show');
  confirmCallback = null;
}

let toastTimeout;
function showToast(message, type = 'info') {
  const toast = document.getElementById('toast');
  const msg = document.getElementById('toastMsg');
  msg.textContent = message;
  toast.className = `toast ${type}`;
  toast.classList.add('show');
  clearTimeout(toastTimeout);
  toastTimeout = setTimeout(() => toast.classList.remove('show'), 3000);
}

async function resolveBug(id) {
  await updateStatus(id, 'resolved');
}

async function ignoreBug(id) {
  await updateStatus(id, 'ignored');
}

async function updateStatus(id, status) {
  if (state.pending.has(id)) return;
  state.pending.add(id);
  renderBugs();

  try {
    unwrapRes(await bridge.apiPost(`bugs/${id}/status`, { status }));
    showToast('状态更新成功', 'success');
    await loadBugs();
    await loadStats();
  } catch (e) {
    showToast('更新失败: ' + (e?.message || '未知错误'), 'error');
  } finally {
    state.pending.delete(id);
    renderBugs();
  }
}

async function deleteBug(id) {
  showConfirmModal('确定删除此记录？删除后不可恢复。', async () => {
    if (state.pending.has(id)) return;
    state.pending.add(id);
    renderBugs();

    try {
      unwrapRes(await bridge.apiPost(`bugs/${id}/delete`, {}));
      showToast('删除成功', 'success');
      await loadBugs();
      await loadStats();
    } catch (e) {
      showToast('删除失败: ' + (e?.message || '未知错误'), 'error');
    } finally {
      state.pending.delete(id);
      renderBugs();
    }
  });
}

function goPage(page) {
  const totalPages = Math.ceil(state.total / state.pageSize) || 1;
  if (page < 1 || page > totalPages) return;
  state.page = page;
  loadBugs();
}

// ------------------------------------------------------------------
// 工具函数
// ------------------------------------------------------------------

// Dashboard 父窗口会自动剥离后端 {code, message, data} 的外层包装
// 有 data 字段的响应 → 前端收到 data 内容（无 code/message）
// 无 data 字段的响应 → 前端收到完整包装 {code, message}
// 统一处理这两种格式
function unwrapRes(res) {
  if (res && typeof res.code === 'number') {
    if (res.code !== 0) {
      throw new Error(res.message || '请求失败');
    }
    return res.data ?? null;
  }
  return res;
}

function showError(msg) {
  document.getElementById('bugList').innerHTML = `
    <div class="empty-state error">
      <div class="empty-state-title">加载失败</div>
      <div class="empty-state-hint">${escapeHtml(msg)}</div>
    </div>
  `;
}

function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatTime(val, isTimestamp) {
  if (!val) return '-';
  let date;
  if (isTimestamp && typeof val === 'number') {
    date = new Date(val * 1000);
  } else {
    date = new Date(val);
  }
  if (isNaN(date.getTime())) return '-';
  const pad = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
