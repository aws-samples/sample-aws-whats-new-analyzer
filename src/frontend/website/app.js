// ─────────────────────────────────────────────────────────────────────────────
// SAMPLE CODE — NOT INTENDED FOR PRODUCTION USE.
// This code is provided as a reference implementation only.
// ─────────────────────────────────────────────────────────────────────────────

// ─── API helpers ───
async function api(method, body) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getIdToken()}`,
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch('/api/preferences', opts);
  return resp.json();
}

async function accountsApi(method, body) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getIdToken()}`,
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch('/api/accounts', opts);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ message: resp.statusText }));
    throw new Error(err.message || `Request failed (${resp.status})`);
  }
  return resp.json();
}

async function feedbackApi(method, body) {
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getIdToken()}`,
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch('/api/feedback', opts);
  return resp.json();
}

async function fetchResults(params = {}) {
  const qs = new URLSearchParams(params).toString();
  const resp = await fetch(`/api/results${qs ? '?' + qs : ''}`, {
    headers: { 'Authorization': `Bearer ${getIdToken()}` },
  });
  const body = await resp.json();
  // Server returns { items: [...], cursor: <string|null> }.
  // Tolerate older shape (bare array) for safety during deploy transitions.
  if (Array.isArray(body)) return { items: body, cursor: null };
  return {
    items: Array.isArray(body.items) ? body.items : [],
    cursor: body.cursor || null,
  };
}

// ─── Toast notifications ───
function toast(msg, type = 'success') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ─── Shared accounts cache ───
// Loaded once and reused by accounts tab, results account filter, and preferences scope selector.
let cachedAccounts = null;

async function getAccounts() {
  if (cachedAccounts) return cachedAccounts;
  cachedAccounts = await accountsApi('GET');
  return cachedAccounts;
}

function invalidateAccountsCache() {
  cachedAccounts = null;
}

// Populate a <select> element with account options from the registry.
// Keeps the first <option> (e.g. "All Accounts" or "Global") intact.
async function populateAccountSelector(selectEl) {
  try {
    const accounts = await getAccounts();
    // Remove all options after the first (the default option)
    while (selectEl.options.length > 1) selectEl.remove(1);
    accounts.forEach(acct => {
      const opt = document.createElement('option');
      opt.value = acct.account_id;
      opt.textContent = acct.display_name
        ? `${acct.display_name} (${acct.account_id})`
        : acct.account_id;
      selectEl.appendChild(opt);
    });
  } catch (e) {
    console.error('Failed to populate account selector:', e);
  }
}

// ─── Tab navigation ───
let preferencesLoaded = false;
let accountsLoaded = false;

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add('active');

    // Lazy-load preferences on first visit to the tab
    if (btn.dataset.tab === 'preferences' && !preferencesLoaded) {
      preferencesLoaded = true;
      loadPreferences();
      populateAccountSelector(document.getElementById('prefAccountScope'));
    }

    // Lazy-load accounts on first visit to the tab
    if (btn.dataset.tab === 'accounts' && !accountsLoaded) {
      accountsLoaded = true;
      loadAccounts();
    }
  });
});

// ─── HTML helpers ───
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function sanitizeHtml(str) {
  const allowedTags = ['a', 'b', 'i', 'em', 'strong', 'br', 'p', 'ul', 'ol', 'li', 'code'];
  const parser = new DOMParser();
  const doc = parser.parseFromString(str, 'text/html');

  function clean(node) {
    const children = Array.from(node.childNodes);
    children.forEach(child => {
      if (child.nodeType === Node.TEXT_NODE) return;
      if (child.nodeType === Node.ELEMENT_NODE) {
        const tag = child.tagName.toLowerCase();
        if (!allowedTags.includes(tag)) {
          child.replaceWith(document.createTextNode(child.textContent));
        } else {
          Array.from(child.attributes).forEach(attr => {
            if (tag === 'a' && attr.name === 'href') return;
            child.removeAttribute(attr.name);
          });
          if (tag === 'a') {
            child.setAttribute('target', '_blank');
            child.setAttribute('rel', 'noopener noreferrer');
          }
          clean(child);
        }
      } else {
        child.remove();
      }
    });
  }

  clean(doc.body);
  return doc.body.innerHTML;
}

// ═══════════════════════════════════════════════════════════════════
// ─── Accounts Tab (Task 15.2) ───
// ═══════════════════════════════════════════════════════════════════

function renderAccounts(items) {
  const list = document.getElementById('accountsList');
  list.textContent = '';
  if (!items.length) {
    const p = document.createElement('p');
    p.className = 'empty-state';
    p.textContent = 'No accounts registered.';
    list.appendChild(p);
    return;
  }
  items.forEach(item => {
    const div = document.createElement('div');
    div.className = 'account-item';
    div.dataset.id = item.account_id;

    const info = document.createElement('div');
    info.className = 'account-item-info';

    const nameDiv = document.createElement('div');
    nameDiv.className = 'account-item-name';
    nameDiv.textContent = item.display_name || item.account_id;
    if (item.is_central) {
      nameDiv.append(' ');
      const badge = document.createElement('span');
      badge.className = 'badge-central';
      badge.textContent = 'Central';
      nameDiv.appendChild(badge);
    }

    const metaDiv = document.createElement('div');
    metaDiv.className = 'account-item-meta';
    const idSpan = document.createElement('span');
    idSpan.textContent = item.account_id;
    metaDiv.appendChild(idSpan);

    info.appendChild(nameDiv);
    info.appendChild(metaDiv);

    const actions = document.createElement('div');
    actions.className = 'account-item-actions';

    const toggleLabel = document.createElement('label');
    toggleLabel.className = 'toggle-switch';
    toggleLabel.title = (item.enabled ? 'Disable' : 'Enable') + ' account';
    const toggleInput = document.createElement('input');
    toggleInput.type = 'checkbox';
    toggleInput.className = 'toggle-enabled';
    toggleInput.checked = !!item.enabled;
    toggleInput.setAttribute('aria-label', 'Toggle account enabled');
    const toggleSlider = document.createElement('span');
    toggleSlider.className = 'toggle-slider';
    toggleLabel.appendChild(toggleInput);
    toggleLabel.appendChild(toggleSlider);

    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'edit-account-btn btn-ghost';
    editBtn.setAttribute('aria-label', 'Edit account');
    editBtn.textContent = 'Edit';

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'remove-account-btn btn-danger';
    removeBtn.disabled = !!item.is_central;
    removeBtn.title = item.is_central ? 'Central account cannot be removed' : 'Remove account';
    removeBtn.setAttribute('aria-label', 'Remove account');
    removeBtn.textContent = 'Remove';

    actions.appendChild(toggleLabel);
    actions.appendChild(editBtn);
    actions.appendChild(removeBtn);

    div.appendChild(info);
    div.appendChild(actions);
    list.appendChild(div);
  });
}

async function loadAccounts() {
  const list = document.getElementById('accountsList');
  list.textContent = '';
  const loading = document.createElement('p');
  loading.className = 'empty-state';
  loading.textContent = 'Loading accounts…';
  list.appendChild(loading);
  try {
    await authReady;
    invalidateAccountsCache();
    const items = await getAccounts();
    renderAccounts(items);
  } catch (e) {
    list.textContent = '';
    const err = document.createElement('p');
    err.className = 'empty-state';
    err.textContent = 'Failed to load accounts.';
    list.appendChild(err);
    console.error(e);
  }
}

async function addAccount() {
  if (!selectedOrgAccount) {
    toast('Select an account from the list first', 'error');
    return;
  }

  const accountId = selectedOrgAccount.account_id;
  const displayName = document.getElementById('accountNameInput').value.trim()
    || selectedOrgAccount.name;

  try {
    await accountsApi('POST', {
      account_id: accountId,
      display_name: displayName,
    });
    document.getElementById('addAccountForm').classList.add('hidden');
    selectedOrgAccount = null;
    invalidateOrgAccountsCache();
    toast('Account added');
    loadAccounts();
    refreshAccountSelectors();
  } catch (e) {
    toast(e.message || 'Failed to add account', 'error');
  }
}

async function toggleAccount(accountId, enabled) {
  try {
    await accountsApi('PUT', { account_id: accountId, enabled });
    invalidateAccountsCache();
  } catch (e) {
    toast(e.message || 'Failed to update account', 'error');
    loadAccounts(); // revert toggle visually
  }
}

async function removeAccount(accountId) {
  if (!confirm('Remove this account? This cannot be undone.')) return;
  try {
    await accountsApi('DELETE', { account_id: accountId });
    toast('Account removed');
    loadAccounts();
    refreshAccountSelectors();
  } catch (e) {
    toast(e.message || 'Failed to remove account', 'error');
  }
}

// Refresh account selectors in results and preferences views after account changes
async function refreshAccountSelectors() {
  invalidateAccountsCache();
  await Promise.all([
    populateAccountSelector(document.getElementById('accountFilter')),
    populateAccountSelector(document.getElementById('prefAccountScope')),
  ]);
}

// ─── Add Account form toggle ───
let orgAccountsCache = null;
let selectedOrgAccount = null;

async function fetchOrgAccounts() {
  if (orgAccountsCache) return orgAccountsCache;
  await authReady;
  const resp = await fetch('/api/accounts?list_org=true', {
    headers: { 'Authorization': `Bearer ${getIdToken()}` },
  });
  if (!resp.ok) {
    console.error('Failed to fetch org accounts:', resp.status);
    return [];
  }
  orgAccountsCache = await resp.json();
  return orgAccountsCache;
}

function invalidateOrgAccountsCache() {
  orgAccountsCache = null;
}

function renderAccountSearchResults(items) {
  const container = document.getElementById('accountSearchResults');
  const emptyMsg = document.getElementById('accountSearchEmpty');
  container.textContent = '';

  if (!items.length) {
    container.classList.add('hidden');
    emptyMsg.classList.remove('hidden');
    return;
  }
  emptyMsg.classList.add('hidden');
  container.classList.remove('hidden');

  items.forEach(acct => {
    const opt = document.createElement('div');
    opt.className = 'account-search-option';
    opt.setAttribute('role', 'option');
    opt.dataset.accountId = acct.account_id;
    opt.dataset.name = acct.name;

    const nameSpan = document.createElement('span');
    nameSpan.className = 'account-search-option-name';
    nameSpan.textContent = acct.name || acct.account_id;

    const idSpan = document.createElement('span');
    idSpan.className = 'account-search-option-id';
    idSpan.textContent = acct.account_id;

    opt.appendChild(nameSpan);
    opt.appendChild(idSpan);
    container.appendChild(opt);
  });
}

function selectOrgAccount(accountId, name) {
  selectedOrgAccount = { account_id: accountId, name: name };
  document.getElementById('selectedAccountId').textContent = accountId;
  document.getElementById('accountNameInput').value = name;
  document.getElementById('accountSelectedInfo').classList.remove('hidden');
  document.getElementById('accountSaveBtn').disabled = false;
  document.getElementById('accountSearchResults').classList.add('hidden');
  document.getElementById('accountSearchInput').value = name
    ? `${name} (${accountId})`
    : accountId;
}

document.getElementById('addAccountBtn').addEventListener('click', async () => {
  const form = document.getElementById('addAccountForm');
  form.classList.remove('hidden');
  selectedOrgAccount = null;
  document.getElementById('accountSearchInput').value = '';
  document.getElementById('accountNameInput').value = '';
  document.getElementById('accountSelectedInfo').classList.add('hidden');
  document.getElementById('accountSaveBtn').disabled = true;
  document.getElementById('accountSearchResults').classList.add('hidden');
  document.getElementById('accountSearchEmpty').classList.add('hidden');
  document.getElementById('accountSearchNoOrg').classList.add('hidden');

  // Show loading state and fetch org accounts
  const loadingMsg = document.getElementById('accountSearchLoading');
  loadingMsg.classList.remove('hidden');
  const accounts = await fetchOrgAccounts();
  loadingMsg.classList.add('hidden');

  if (!accounts.length) {
    document.getElementById('accountSearchNoOrg').classList.remove('hidden');
  }

  document.getElementById('accountSearchInput').focus();
});

// ─── Search input filtering ───
document.getElementById('accountSearchInput').addEventListener('input', () => {
  const query = document.getElementById('accountSearchInput').value.trim().toLowerCase();
  // If user edits after selecting, clear selection
  if (selectedOrgAccount) {
    selectedOrgAccount = null;
    document.getElementById('accountSelectedInfo').classList.add('hidden');
    document.getElementById('accountSaveBtn').disabled = true;
  }

  if (!orgAccountsCache || !orgAccountsCache.length) return;

  if (!query) {
    renderAccountSearchResults(orgAccountsCache);
    return;
  }

  const filtered = orgAccountsCache.filter(acct =>
    (acct.name && acct.name.toLowerCase().includes(query))
    || acct.account_id.includes(query)
  );
  renderAccountSearchResults(filtered);
});

// Show full list on focus if we have data
document.getElementById('accountSearchInput').addEventListener('focus', () => {
  if (!selectedOrgAccount && orgAccountsCache && orgAccountsCache.length) {
    const query = document.getElementById('accountSearchInput').value.trim().toLowerCase();
    const items = query
      ? orgAccountsCache.filter(a =>
          (a.name && a.name.toLowerCase().includes(query)) || a.account_id.includes(query))
      : orgAccountsCache;
    renderAccountSearchResults(items);
  }
});

// ─── Click handler for search results ───
document.getElementById('accountSearchResults').addEventListener('click', (e) => {
  const opt = e.target.closest('.account-search-option');
  if (!opt) return;
  selectOrgAccount(opt.dataset.accountId, opt.dataset.name);
});

// ─── Keyboard navigation for search results ───
document.getElementById('accountSearchInput').addEventListener('keydown', (e) => {
  const container = document.getElementById('accountSearchResults');
  if (container.classList.contains('hidden')) return;
  const options = Array.from(container.querySelectorAll('.account-search-option'));
  if (!options.length) return;

  const current = container.querySelector('.highlighted');
  let idx = current ? options.indexOf(current) : -1;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (current) current.classList.remove('highlighted');
    idx = (idx + 1) % options.length;
    options[idx].classList.add('highlighted');
    options[idx].scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (current) current.classList.remove('highlighted');
    idx = idx <= 0 ? options.length - 1 : idx - 1;
    options[idx].classList.add('highlighted');
    options[idx].scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (current) {
      selectOrgAccount(current.dataset.accountId, current.dataset.name);
    }
  }
});

document.getElementById('accountCancelBtn').addEventListener('click', () => {
  document.getElementById('addAccountForm').classList.add('hidden');
  selectedOrgAccount = null;
});

document.getElementById('accountSaveBtn').addEventListener('click', addAccount);

// ─── Edit Account modal ───
const editAccountModal = document.getElementById('editAccountModal');
const editAccountName = document.getElementById('editAccountName');
const editAccountSaveBtn = document.getElementById('editAccountSaveBtn');
const editAccountCancelBtn = document.getElementById('editAccountCancelBtn');
let editingAccountId = null;

editAccountCancelBtn.addEventListener('click', () => editAccountModal.classList.add('hidden'));
editAccountModal.addEventListener('click', (e) => {
  if (e.target === editAccountModal) editAccountModal.classList.add('hidden');
});

editAccountSaveBtn.addEventListener('click', async () => {
  const displayName = editAccountName.value.trim();
  if (!displayName || !editingAccountId) return;
  try {
    await accountsApi('PUT', {
      account_id: editingAccountId,
      display_name: displayName,
    });
    editAccountModal.classList.add('hidden');
    toast('Account updated');
    loadAccounts();
    refreshAccountSelectors();
  } catch (e) {
    toast(e.message || 'Failed to update account', 'error');
  }
});

// ─── Close account search dropdown on outside click ───
document.addEventListener('click', (e) => {
  const form = document.getElementById('addAccountForm');
  const results = document.getElementById('accountSearchResults');
  if (!form.contains(e.target)) {
    results.classList.add('hidden');
  }
});

// ─── Delegated click handlers for accounts list ───
document.getElementById('accountsList').addEventListener('click', (e) => {
  const item = e.target.closest('.account-item');
  if (!item) return;
  const accountId = item.dataset.id;

  // Toggle enabled/disabled
  const toggle = e.target.closest('.toggle-enabled');
  if (toggle) {
    toggleAccount(accountId, toggle.checked);
    return;
  }

  // Edit button
  if (e.target.closest('.edit-account-btn')) {
    editingAccountId = accountId;
    const nameEl = item.querySelector('.account-item-name');
    // Extract display name (text content minus the central badge)
    const nameText = nameEl.childNodes[0].textContent.trim();
    editAccountName.value = nameText;
    editAccountModal.classList.remove('hidden');
    editAccountName.focus();
    return;
  }

  // Remove button
  if (e.target.closest('.remove-account-btn')) {
    if (e.target.closest('.remove-account-btn').disabled) return;
    removeAccount(accountId);
  }
});

// ═══════════════════════════════════════════════════════════════════
// ─── Preferences (updated for account scope — Task 15.4) ───
// ═══════════════════════════════════════════════════════════════════

function renderPreferences(items) {
  const list = document.getElementById('prefList');
  list.textContent = '';
  if (!items.length) {
    const p = document.createElement('p');
    p.className = 'empty-state';
    p.textContent = 'No preferences yet. Add one to get started.';
    list.appendChild(p);
    return;
  }
  items.forEach(item => {
    const div = document.createElement('div');
    div.className = 'pref-item';
    div.dataset.id = item.id;

    const content = document.createElement('div');
    content.className = 'pref-item-content';

    const statement = document.createElement('div');
    statement.className = 'pref-item-statement';
    statement.textContent = item.statement;

    const date = document.createElement('div');
    date.className = 'pref-item-date';
    date.textContent = item.created_at ? new Date(item.created_at).toLocaleDateString() : '';

    content.appendChild(statement);
    content.appendChild(date);

    const actions = document.createElement('div');
    actions.className = 'pref-item-actions';

    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'edit-btn btn-ghost';
    editBtn.setAttribute('aria-label', 'Edit preference');
    editBtn.textContent = 'Edit';

    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.className = 'delete-btn btn-danger';
    deleteBtn.setAttribute('aria-label', 'Delete preference');
    deleteBtn.textContent = 'Delete';

    actions.appendChild(editBtn);
    actions.appendChild(deleteBtn);

    div.appendChild(content);
    div.appendChild(actions);
    list.appendChild(div);
  });
}

async function loadPreferences() {
  try {
    await authReady;
    const scope = document.getElementById('prefAccountScope').value;
    const params = scope ? { account_id: scope } : {};
    const qs = new URLSearchParams(params).toString();
    const opts = {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${getIdToken()}`,
      },
    };
    const resp = await fetch(`/api/preferences${qs ? '?' + qs : ''}`, opts);
    const items = await resp.json();
    renderPreferences(items);
  } catch (e) {
    const prefList = document.getElementById('prefList');
    prefList.textContent = '';
    const err = document.createElement('p');
    err.className = 'empty-state';
    err.textContent = 'Failed to load preferences.';
    prefList.appendChild(err);
    console.error(e);
  }
}

// Reload preferences when account scope changes
document.getElementById('prefAccountScope').addEventListener('change', () => {
  loadPreferences();
});

// ─── Add preference ───
const addBtn = document.getElementById('addPrefBtn');
const prefForm = document.getElementById('prefForm');
const prefInput = document.getElementById('prefInput');
const prefSaveBtn = document.getElementById('prefSaveBtn');
const prefCancelBtn = document.getElementById('prefCancelBtn');

addBtn.addEventListener('click', () => {
  prefForm.classList.remove('hidden');
  prefInput.value = '';
  prefInput.focus();
});

prefCancelBtn.addEventListener('click', () => {
  prefForm.classList.add('hidden');
});

prefSaveBtn.addEventListener('click', async () => {
  const statement = prefInput.value.trim();
  if (!statement) return;
  try {
    const scope = document.getElementById('prefAccountScope').value;
    const body = { statement };
    if (scope) body.account_id = scope;
    await api('POST', body);
    prefForm.classList.add('hidden');
    toast('Preference added');
    loadPreferences();
  } catch (e) {
    toast('Failed to add preference', 'error');
  }
});

// ─── Edit preference modal ───
const editModal = document.getElementById('editModal');
const editInput = document.getElementById('editInput');
const editSaveBtn = document.getElementById('editSaveBtn');
const editCancelBtn = document.getElementById('editCancelBtn');
let editingId = null;

editCancelBtn.addEventListener('click', () => editModal.classList.add('hidden'));
editModal.addEventListener('click', (e) => {
  if (e.target === editModal) editModal.classList.add('hidden');
});

editSaveBtn.addEventListener('click', async () => {
  const statement = editInput.value.trim();
  if (!statement || !editingId) return;
  try {
    await api('PUT', { id: editingId, statement });
    editModal.classList.add('hidden');
    toast('Preference updated');
    loadPreferences();
  } catch (e) {
    toast('Failed to update preference', 'error');
  }
});

// ─── Delegated click handlers for edit/delete preferences ───
document.getElementById('prefList').addEventListener('click', async (e) => {
  const item = e.target.closest('.pref-item');
  if (!item) return;
  const id = item.dataset.id;

  if (e.target.closest('.edit-btn')) {
    editingId = id;
    editInput.value = item.querySelector('.pref-item-statement').textContent;
    editModal.classList.remove('hidden');
    editInput.focus();
  }

  if (e.target.closest('.delete-btn')) {
    if (!confirm('Delete this preference?')) return;
    try {
      await api('DELETE', { id });
      toast('Preference deleted');
      loadPreferences();
    } catch (e) {
      toast('Failed to delete preference', 'error');
    }
  }
});

// ═══════════════════════════════════════════════════════════════════
// ─── Feedback ───
// ═══════════════════════════════════════════════════════════════════

let feedbackState = {};

async function loadFeedback() {
  try {
    const accountId = document.getElementById('accountFilter').value;
    const qs = accountId ? `?account_id=${encodeURIComponent(accountId)}` : '';
    const resp = await fetch(`/api/feedback${qs}`, {
      headers: { 'Authorization': `Bearer ${getIdToken()}` },
    });
    const items = await resp.json();
    feedbackState = {};
    if (Array.isArray(items)) {
      items.forEach(item => {
        feedbackState[item.announcement_id] = item.rating;
      });
    }
    renderResults(getFilteredResults());
  } catch (e) {
    console.error('Failed to load feedback:', e);
  }
}

async function submitFeedback(id, rating) {
  try {
    const body = { announcement_id: id, rating };
    const accountId = document.getElementById('accountFilter').value;
    if (accountId) body.account_id = accountId;
    await feedbackApi('POST', body);
    feedbackState[id] = rating;
    renderResults(getFilteredResults());
    toast('Feedback submitted');
  } catch (e) {
    toast('Failed to submit feedback', 'error');
  }
}

async function deleteFeedback(id) {
  try {
    await feedbackApi('DELETE', { announcement_id: id });
    delete feedbackState[id];
    renderResults(getFilteredResults());
    toast('Feedback removed');
  } catch (e) {
    toast('Failed to remove feedback', 'error');
  }
}

// ═══════════════════════════════════════════════════════════════════
// ─── Results (server-side filter + cursor pagination) ───
// ═══════════════════════════════════════════════════════════════════

// `allResults` is append-only across pagination within the current
// (filter, account) combination. Filter and account changes reset it.
let allResults = [];
let currentFilter = 'relevant';
let resultsCursor = null;
let isLoadingResults = false;
let resultsScrollObserver = null;

// Map the UI toggle to the API's is_relevant query param.
// 'all' -> no filter, 'relevant' -> true, 'not-relevant' -> false.
function isRelevantParam() {
  if (currentFilter === 'relevant') return 'true';
  if (currentFilter === 'not-relevant') return 'false';
  return null;
}

// Kept as a passthrough so feedback handlers that call
// renderResults(getFilteredResults()) stay correct. Filtering is now
// applied server-side via is_relevant.
function getFilteredResults() {
  return allResults;
}

// ─── Toggle filter buttons ───
document.querySelector('.toggle-group').addEventListener('click', (e) => {
  const btn = e.target.closest('.toggle-btn');
  if (!btn) return;
  if (btn.classList.contains('active')) return; // no-op when same filter clicked
  document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  currentFilter = btn.dataset.filter;
  loadResults({ reset: true });
});

// Build a lookup map from account_id → display_name using cached accounts
function buildAccountNameMap() {
  const map = {};
  if (cachedAccounts) {
    cachedAccounts.forEach(a => { map[a.account_id] = a.display_name || a.account_id; });
  }
  return map;
}

function renderResults(items) {
  const list = document.getElementById('resultsList');
  list.textContent = '';
  if (!items.length) {
    const p = document.createElement('p');
    p.className = 'empty-state';
    p.textContent = 'No results yet. Recommendations will appear once announcements are processed.';
    list.appendChild(p);
    return;
  }
  const nameMap = buildAccountNameMap();
  let lastDateKey = null;
  items.forEach(item => {
    const dateKey = getItemDateKey(item);
    if (dateKey && dateKey !== lastDateKey) {
      list.appendChild(buildDateDivider(dateKey));
      lastDateKey = dateKey;
    }
    list.appendChild(buildResultCard(item, nameMap));
  });
}

// Append rendered cards for a freshly-loaded page without rebuilding the list.
function appendResultCards(items) {
  if (!items.length) return;
  const list = document.getElementById('resultsList');
  // Remove the empty-state placeholder if present (first append after a reset
  // that returned zero items previously).
  const empty = list.querySelector('.empty-state');
  if (empty) empty.remove();
  const nameMap = buildAccountNameMap();
  // Determine the last date divider already in the list
  const existingDividers = list.querySelectorAll('.date-divider');
  let lastDateKey = existingDividers.length
    ? existingDividers[existingDividers.length - 1].dataset.dateKey
    : null;
  items.forEach(item => {
    const dateKey = getItemDateKey(item);
    if (dateKey && dateKey !== lastDateKey) {
      list.appendChild(buildDateDivider(dateKey));
      lastDateKey = dateKey;
    }
    list.appendChild(buildResultCard(item, nameMap));
  });
}

/** Extract a date string (YYYY-MM-DD) from an item for grouping. */
function getItemDateKey(item) {
  const ann = item.announcement || {};
  let dateSource = ann.pubDate || '';
  if (!dateSource) {
    let rawTs = item.timestamp || '';
    if (rawTs.includes('#')) rawTs = rawTs.split('#').slice(1).join('#');
    dateSource = rawTs;
  }
  if (!dateSource) return '';
  const d = new Date(dateSource);
  if (isNaN(d.getTime())) return '';
  return d.toISOString().slice(0, 10);
}

/** Build a lightweight date section divider element. */
function buildDateDivider(dateKey) {
  const divider = document.createElement('div');
  divider.className = 'date-divider';
  divider.dataset.dateKey = dateKey;
  const label = new Date(dateKey + 'T00:00:00').toLocaleDateString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric'
  });
  divider.textContent = label;
  return divider;
}

function buildResultCard(item, nameMap) {
    const ann = item.announcement || {};
    const title = ann.title || item.id || 'Untitled';
    const link = ann.link || '#';
    const announcementId = ann.link || '';
    const relevant = item.is_relevant;
    const description = ann.description ? sanitizeHtml(ann.description) : '';
    const reasoning = item.reasoning ? escapeHtml(item.reasoning) : '';
    const hasFlip = description && reasoning;
    const currentRating = feedbackState[announcementId] || '';

    const accountId = item.account_id || '';
    const accountLabel = accountId ? (nameMap[accountId] || accountId) : '';

    const card = document.createElement('div');
    card.className = 'result-item';

    // Header
    const header = document.createElement('div');
    header.className = 'result-header';

    const titleLink = document.createElement('a');
    titleLink.href = link;
    titleLink.target = '_blank';
    titleLink.rel = 'noopener';
    titleLink.textContent = title;
    header.appendChild(titleLink);

    if (accountLabel) {
      const accountBadge = document.createElement('span');
      accountBadge.className = 'account-badge';
      accountBadge.textContent = accountLabel;
      header.appendChild(accountBadge);
    }

    const badge = document.createElement('span');
    badge.className = relevant ? 'badge relevant' : 'badge not-relevant';
    badge.textContent = relevant ? 'Relevant' : 'Not relevant';
    header.appendChild(badge);

    card.appendChild(header);

    // Body (description / reasoning flip) — collapsible
    if (description || reasoning) {
      const bodyWrapper = document.createElement('div');
      bodyWrapper.className = 'result-body-wrapper';

      const bodyToggle = document.createElement('button');
      bodyToggle.type = 'button';
      bodyToggle.className = 'result-body-toggle';
      bodyToggle.setAttribute('aria-expanded', 'false');
      bodyToggle.setAttribute('aria-label', 'Show announcement details');
      bodyToggle.innerHTML = '<span class="toggle-chevron">&#9654;</span> Show details';
      bodyWrapper.appendChild(bodyToggle);

      const body = document.createElement('div');
      body.className = 'result-body hidden' + (hasFlip ? ' flippable' : '');
      if (hasFlip) {
        body.setAttribute('role', 'button');
        body.setAttribute('tabindex', '0');
        body.setAttribute('aria-label', 'Click to toggle between release details and AI reasoning');
      }

      const front = document.createElement('div');
      front.className = 'result-body-front';
      // description is already sanitized HTML; reasoning is escaped plain text
      // Parse sanitized HTML safely via DOMParser and adopt the nodes
      if (description) {
        const parsed = new DOMParser().parseFromString(description, 'text/html');
        Array.from(parsed.body.childNodes).forEach(n => front.appendChild(document.adoptNode(n)));
      } else {
        front.textContent = item.reasoning || '';
      }
      body.appendChild(front);

      if (hasFlip) {
        const back = document.createElement('div');
        back.className = 'result-body-back hidden';
        back.textContent = item.reasoning || '';
        body.appendChild(back);

        const hint = document.createElement('span');
        hint.className = 'flip-hint';
        hint.textContent = 'Click for AI reasoning';
        body.appendChild(hint);
      }

      bodyWrapper.appendChild(body);
      card.appendChild(bodyWrapper);
    }

    // Footer
    const footer = document.createElement('div');
    footer.className = 'result-footer';

    const fbButtons = document.createElement('div');
    fbButtons.className = 'feedback-buttons';

    const upBtn = document.createElement('button');
    upBtn.type = 'button';
    upBtn.className = 'feedback-btn' + (currentRating === 'up' ? ' active' : '');
    upBtn.dataset.id = announcementId;
    upBtn.dataset.rating = 'up';
    upBtn.setAttribute('aria-label', 'Upvote');
    upBtn.textContent = '↑';

    const downBtn = document.createElement('button');
    downBtn.type = 'button';
    downBtn.className = 'feedback-btn' + (currentRating === 'down' ? ' active' : '');
    downBtn.dataset.id = announcementId;
    downBtn.dataset.rating = 'down';
    downBtn.setAttribute('aria-label', 'Downvote');
    downBtn.textContent = '↓';

    fbButtons.appendChild(upBtn);
    fbButtons.appendChild(downBtn);
    footer.appendChild(fbButtons);

    card.appendChild(footer);
    return card;
}

// Monotonic token to detect stale loadResults responses.
// Incremented on every reset (filter change, account change, refresh).
// Pages that come back after a newer reset are dropped.
let resultsLoadToken = 0;

async function loadResults({ reset = true } = {}) {
  if (isLoadingResults) return;
  isLoadingResults = true;

  const list = document.getElementById('resultsList');
  const loadMoreWrapper = document.getElementById('resultsLoadMoreWrapper');
  const loadMoreBtn = document.getElementById('loadMoreResultsBtn');
  let myToken = resultsLoadToken;

  try {
    if (reset) {
      resultsLoadToken += 1;
      myToken = resultsLoadToken;
      allResults = [];
      resultsCursor = null;
      list.textContent = '';
      const loading = document.createElement('p');
      loading.className = 'empty-state';
      loading.textContent = 'Loading results…';
      list.appendChild(loading);
      loadMoreWrapper.classList.add('hidden');
    } else {
      // "Load more" — visual hint while fetching the next page
      if (loadMoreBtn) {
        loadMoreBtn.disabled = true;
        loadMoreBtn.textContent = 'Loading…';
      }
    }

    await authReady;
    const params = { limit: '30' };
    const accountId = document.getElementById('accountFilter').value;
    if (accountId) params.account_id = accountId;
    const isRel = isRelevantParam();
    if (isRel !== null) params.is_relevant = isRel;
    if (!reset && resultsCursor) params.cursor = resultsCursor;

    const { items, cursor } = await fetchResults(params);

    // Stale response — a newer reset happened while we were fetching
    if (myToken !== resultsLoadToken) return;

    if (reset) {
      allResults = items;
      resultsCursor = cursor;
      // Refresh feedback overlay for the new dataset. loadFeedback() ends
      // with renderResults(getFilteredResults()) on success — which renders
      // the new allResults, replacing the loading placeholder. If feedback
      // loading fails it doesn't render, so we render here as a fallback.
      await loadFeedback();
      if (myToken !== resultsLoadToken) return;
      // Idempotent re-render — guarantees the loading placeholder is gone
      // even if loadFeedback errored, and is cheap when it didn't.
      renderResults(allResults);
    } else {
      allResults = allResults.concat(items);
      resultsCursor = cursor;
      appendResultCards(items);
    }

    // Show/hide the Load more button based on whether more pages exist
    loadMoreWrapper.classList.toggle('hidden', !resultsCursor);
    if (loadMoreBtn) {
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = 'Load more';
    }
  } catch (e) {
    if (myToken !== resultsLoadToken) return;
    if (reset) {
      list.textContent = '';
      const err = document.createElement('p');
      err.className = 'empty-state';
      err.textContent = 'Failed to load results.';
      list.appendChild(err);
    } else if (loadMoreBtn) {
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = 'Load more';
      toast('Failed to load more results', 'error');
    }
    console.error(e);
  } finally {
    isLoadingResults = false;
  }
}

// IntersectionObserver triggers `loadResults({ reset: false })` whenever the
// sentinel below the list scrolls into view, giving infinite-scroll behavior.
// The "Load more" button is the keyboard/screen-reader accessible fallback.
function setupResultsScrollObserver() {
  if (resultsScrollObserver) return;
  const sentinel = document.getElementById('resultsSentinel');
  if (!sentinel || typeof IntersectionObserver !== 'function') return;
  resultsScrollObserver = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (entry.isIntersecting && resultsCursor && !isLoadingResults) {
        loadResults({ reset: false });
      }
    }
  }, { rootMargin: '200px' });
  resultsScrollObserver.observe(sentinel);
}

// ─── Account filter for results ───
document.getElementById('accountFilter').addEventListener('change', () => {
  loadResults({ reset: true });
});

// ─── Load more button (a11y fallback for infinite scroll) ───
document.getElementById('loadMoreResultsBtn').addEventListener('click', () => {
  if (resultsCursor && !isLoadingResults) loadResults({ reset: false });
});

// ─── Collapse/expand body toggle ───
document.getElementById('resultsList').addEventListener('click', (e) => {
  const toggleBtn = e.target.closest('.result-body-toggle');
  if (toggleBtn) {
    const wrapper = toggleBtn.closest('.result-body-wrapper');
    const body = wrapper.querySelector('.result-body');
    const isExpanded = !body.classList.contains('hidden');
    body.classList.toggle('hidden');
    toggleBtn.setAttribute('aria-expanded', String(!isExpanded));
    if (isExpanded) {
      toggleBtn.innerHTML = '<span class="toggle-chevron">&#9654;</span> Show details';
    } else {
      toggleBtn.innerHTML = '<span class="toggle-chevron rotated">&#9654;</span> Hide details';
    }
    return;
  }

  // ─── Flip body (release message ↔ reasoning) ───
  const body = e.target.closest('.result-body.flippable');
  if (body && !e.target.closest('a') && !e.target.closest('.feedback-btn')) {
    flipBody(body);
    return;
  }

  // ─── Feedback button click handler ───
  const btn = e.target.closest('.feedback-btn');
  if (!btn) return;
  const id = btn.dataset.id;
  const rating = btn.dataset.rating;
  if (feedbackState[id] === rating) {
    deleteFeedback(id);
  } else {
    submitFeedback(id, rating);
  }
});

// Keyboard support for flippable bodies
document.getElementById('resultsList').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    const body = e.target.closest('.result-body.flippable');
    if (body) {
      e.preventDefault();
      flipBody(body);
    }
  }
});

function flipBody(body) {
  const front = body.querySelector('.result-body-front');
  const back = body.querySelector('.result-body-back');
  const hint = body.querySelector('.flip-hint');
  const showingFront = !front.classList.contains('hidden');
  front.classList.toggle('hidden');
  back.classList.toggle('hidden');
  if (hint) hint.textContent = showingFront ? 'Click for release details' : 'Click for AI reasoning';
}

document.getElementById('refreshResultsBtn').addEventListener('click', () => loadResults({ reset: true }));

// ─── Initial load ───
// Populate the results account filter and load results on page load (default active tab)
(async function init() {
  await authReady;
  await populateAccountSelector(document.getElementById('accountFilter'));

  // If ?account=<id> is in the URL, pre-select that account in the filter
  const urlParams = new URLSearchParams(window.location.search);
  const accountParam = urlParams.get('account');
  if (accountParam) {
    const accountFilter = document.getElementById('accountFilter');
    // Ensure the option exists (it may have been populated above)
    const optionExists = Array.from(accountFilter.options).some(o => o.value === accountParam);
    if (optionExists) {
      accountFilter.value = accountParam;
    } else {
      // Add a temporary option so the filter still works for valid account IDs
      const opt = document.createElement('option');
      opt.value = accountParam;
      opt.textContent = accountParam;
      accountFilter.appendChild(opt);
      accountFilter.value = accountParam;
    }
  }

  setupResultsScrollObserver();
  loadResults({ reset: true });
})();
