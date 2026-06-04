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
  }
};

// 等待 Bridge SDK 就绪
(async function init() {
  try {
    await bridge.ready();
    console.log('[BugCatcher] Bridge SDK 就绪');
    bindEvents();
    await loadStats();
    await loadBugs();
  } catch (e) {
    console.error('[BugCatcher] 初始化失败:', e);
    showError('初始化失败: ' + e.message);
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
}

// ------------------------------------------------------------------
// 数据加载
// ------------------------------------------------------------------

async function loadStats() {
  try {
    const res = await bridge.apiGet('stats');
    if (res.code !== 0) {
      console.warn('获取统计失败:', res.message);
      return;
    }
    const data = res.data || {};
    document.getElementById('statConfirmed').textContent = data.total_confirmed || 0;
    document.getElementById('statSuspected').textContent = data.total_suspected || 0;
    document.getElementById('statTotal').textContent = (data.total_confirmed || 0) + (data.total_suspected || 0);

    // 今日新增
    const todayRes = await bridge.apiGet('bugs', {
      page: 1,
      page_size: 1,
      status: ''
    });
    // 由于后端没有按日期筛选，这里简单显示总记录数
    document.getElementById('statToday').textContent = '-';
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

    const res = await bridge.apiGet('bugs', params);
    if (res.code !== 0) {
      showError('加载失败: ' + res.message);
      return;
    }

    const data = res.data || {};
    state.bugs = data.bugs || [];
    state.total = data.total || 0;

    renderBugs();
    renderPagination();
  } catch (e) {
    showError('加载失败: ' + e.message);
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
        <div class="empty-state-icon">🛡️</div>
        <div>暂无 Bug 记录</div>
      </div>
    `;
    return;
  }

  listEl.innerHTML = state.bugs.map(bug => `
    <div class="bug-card" data-id="${bug.id}">
      <div class="bug-card-header">
        <span class="bug-severity severity-${bug.severity}">${bug.severity}</span>
        <span class="bug-result result-${bug.result}">${bug.result}</span>
        <span class="bug-status status-${bug.status}">${bug.status}</span>
        <span class="bug-time">${formatTime(bug.created_at)}</span>
      </div>
      <div class="bug-summary">${escapeHtml(bug.summary)}</div>
      <div class="bug-umo">${escapeHtml(bug.umo_display || bug.umo)}</div>
      <div class="bug-actions">
        <button class="btn btn-primary btn-small" onclick="showDetail('${bug.id}')">详情</button>
        ${bug.status === 'open' ? `
          <button class="btn btn-success btn-small" onclick="resolveBug('${bug.id}')">标记已解决</button>
          <button class="btn btn-success btn-small" onclick="ignoreBug('${bug.id}')">忽略</button>
        ` : ''}
        <button class="btn btn-danger btn-small" onclick="deleteBug('${bug.id}')">删除</button>
      </div>
    </div>
  `).join('');
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

  const rawMsgs = (bug.raw_messages || []).map(m => `
    <div class="msg-line">
      <span class="msg-time">${formatTime(m.timestamp, true)}</span>
      <span class="msg-sender">${escapeHtml(m.sender_name)}</span>
      <span>${escapeHtml(m.content)}</span>
    </div>
  `).join('');

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
      <h3>来源</h3>
      <div class="detail-text">
平台: ${escapeHtml(bug.platform || '-')}
UMO: ${escapeHtml(bug.umo)}
时间: ${escapeHtml(bug.created_at)}
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

async function resolveBug(id) {
  if (!confirm('确定标记为已解决？')) return;
  await updateStatus(id, 'resolved');
}

async function ignoreBug(id) {
  if (!confirm('确定忽略此记录？')) return;
  await updateStatus(id, 'ignored');
}

async function updateStatus(id, status) {
  try {
    const res = await bridge.apiPost(`bugs/${id}/status`, { status });
    if (res.code !== 0) {
      alert('更新失败: ' + res.message);
      return;
    }
    await loadBugs();
  } catch (e) {
    alert('更新失败: ' + e.message);
  }
}

async function deleteBug(id) {
  if (!confirm('确定删除此记录？删除后不可恢复。')) return;
  try {
    const res = await bridge.apiPost(`bugs/${id}/delete`, {});
    if (res.code !== 0) {
      alert('删除失败: ' + res.message);
      return;
    }
    await loadBugs();
    await loadStats();
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
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

function showError(msg) {
  document.getElementById('bugList').innerHTML = `
    <div class="empty-state">
      <div class="empty-state-icon">⚠️</div>
      <div>${escapeHtml(msg)}</div>
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
