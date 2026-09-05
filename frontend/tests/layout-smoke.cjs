const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const baseURL = process.env.UI_BASE_URL || 'http://127.0.0.1:5173';
const output = path.resolve(__dirname, '../../.runtime/taste-ui-qa');
const accounts = Array.from({ length: 43 }, (_, i) => ({
  id: i + 1, email: `test.account.${String(i + 1).padStart(2, '0')}@example.com`, password: 'test-only',
  display_status: i % 7 === 0 ? 'invalid' : 'registered', lifecycle_status: 'registered',
  plan_state: i % 5 === 0 ? 'subscribed' : 'free', validity_status: i % 7 === 0 ? 'invalid' : 'valid',
  created_at: '2026-09-01T08:24:00', registered_at: '2026-09-01T08:24:00',
  provider_resources: [{ resource_type: 'mailbox', provider_name: 'Local Outlook', handle: `verify.${i + 1}@example.com` }],
}));
const fields = [{ key: 'endpoint', label: 'API URL', type: 'text' }, { key: 'token', label: 'API Key', type: 'text', secret: true }];
const catalog = [
  { value: 'local_ms_pool', label: 'Outlook', driver_type: 'local_ms_pool', category: 'selfhost', fields, description: 'Local Outlook mailbox pool' },
  { value: 'api_mailbox', label: 'API Mailbox', driver_type: 'api_mailbox', category: 'selfhost', fields, description: 'HTTP mailbox service' },
  { value: 'temp_mail', label: 'Temporary Mail', category: 'free', fields: [], description: 'Temporary mailbox provider' },
];
const settings = catalog.slice(0, 2).map((p, i) => ({
  id: i + 1, provider_type: 'mailbox', provider_key: p.value, display_name: p.label,
  enabled: true, is_default: i === 0, fields, config: { endpoint: 'https://mail.example.com', local_ms_pool_file: 'test-pool.txt' }, auth: {},
}));
const platform = { name: 'chatgpt', display_name: 'ChatGPT', supported_identity_modes: ['mailbox'], supported_executors: ['protocol', 'headless', 'headed'] };

async function mockAPI(page) {
  const state = { failure: false, empty: false, taskStatus: 'running', writes: [] };
  await page.route('**/api/**', async route => {
    const req = route.request();
    const url = new URL(req.url());
    const endpoint = url.pathname.slice(4);
    const method = req.method();
    if (method !== 'GET') state.writes.push({ endpoint, method });
    const send = (data, status = 200) => route.fulfill({ status, json: data });
    if (endpoint === '/auth/check') return send({ required: false });
    if (endpoint === '/system/version') return send({ has_update: false });
    if (endpoint === '/platforms') return send([platform]);
    if (endpoint === '/config/options') return send({ mailbox_providers: catalog, mailbox_settings: settings, captcha_providers: [], captcha_policy: {}, executor_options: [{ value: 'protocol', label: 'Protocol' }, { value: 'headless', label: 'Headless' }], identity_mode_options: [{ value: 'mailbox', label: 'Mailbox' }] });
    if (endpoint === '/config') return send({ default_identity_provider: 'mailbox', default_executor: 'headless' });
    if (endpoint === '/accounts/stats') return send({ total: 43, by_platform: { chatgpt: 43 }, by_plan_state: { free: 34, subscribed: 9 }, by_lifecycle_status: { registered: 43 }, by_validity_status: { valid: 36, invalid: 7 } });
    if (endpoint === '/accounts') {
      if (state.failure) return send({ error: 'Test connection failure' }, 503);
      let rows = state.empty ? [] : accounts.filter(a => a.email.includes(url.searchParams.get('email') || ''));
      if (url.searchParams.get('status')) rows = rows.filter(a => a.display_status === url.searchParams.get('status'));
      const size = Number(url.searchParams.get('page_size') || 20);
      const start = (Number(url.searchParams.get('page') || 1) - 1) * size;
      return send({ items: rows.slice(start, start + size), total: rows.length });
    }
    if (endpoint === '/actions/chatgpt') return send({ actions: [] });
    if (endpoint === '/tasks/register') return send({ task_id: 'layout-test' });
    if (endpoint === '/tasks/layout-test') return send({ status: state.taskStatus, cancellable: true, progress_detail: { total: 5, current: 2, label: '2 / 5' } });
    if (endpoint.endsWith('/cancel')) { state.taskStatus = 'cancelled'; return send({ status: 'cancelled' }); }
    if (endpoint.endsWith('/logs/stream')) return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"id":1,"line":"[18:30:42] Worker 1: mailbox ready","detail":{"subtask_id":"worker_1","subtask_label":"Worker 1"}}\n\ndata: {"id":2,"line":"[18:30:45] Worker 1: account saved","detail":{"subtask_id":"worker_1","subtask_label":"Worker 1"}}\n\n' });
    if (endpoint.endsWith('/events') || endpoint.endsWith('/otp-requests')) return send({ items: [] });
    if (endpoint.endsWith('/mailboxes')) return send({ items: [{ email: 'mailbox.test@example.com', key: 'test-mailbox', used: false, active: false }] });
    if (endpoint === '/provider-settings/test') return send({ ok: true, message: 'Test connection passed' });
    if (endpoint.startsWith('/provider-settings')) return send({ ok: true });
    throw new Error(`Unmocked request: ${method} ${endpoint}`);
  });
  return state;
}

async function noOverflow(page, label) {
  const result = await page.evaluate(() => {
    const main = document.querySelector('.workspace-main');
    const content = document.querySelector('.workspace-content');
    return { viewport: innerWidth, doc: document.documentElement.scrollWidth, main: main.scrollWidth - main.clientWidth, content: content.scrollWidth - content.clientWidth };
  });
  assert.ok(result.doc <= result.viewport + 1 && result.main <= 1 && result.content <= 1, `${label}: ${JSON.stringify(result)}`);
  const clipped = await page.locator('.accounts-header, .accounts-toolbar, .provider-row, .setting-group, .theme-options').evaluateAll(elements => elements
    .filter(el => el.scrollWidth > el.clientWidth + 2)
    .map(el => ({ className: el.className, width: el.clientWidth, content: el.scrollWidth })));
  assert.deepEqual(clipped, [], `${label}: clipped controls`);
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.UI_BROWSER_CHANNEL || 'msedge' });
  const errors = [];
  try {
    for (const width of [1440, 1024, 390, 360]) {
      for (const theme of ['light', 'dark']) {
        const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: 'reduce', isMobile: width < 768, hasTouch: width < 768 });
        const page = await context.newPage();
        page.on('pageerror', e => errors.push(e.message));
        await page.addInitScript(value => { localStorage.setItem('theme', value); localStorage.setItem('app-language', 'zh-CN'); }, theme);
        const state = await mockAPI(page);
        for (const [name, route] of [['dashboard', '/'], ['accounts', '/accounts/chatgpt'], ['mailbox', '/settings?tab=mailbox'], ['settings', '/settings?tab=general']]) {
          await page.goto(baseURL + route);
          await page.locator('h1').waitFor();
          if (name === 'accounts') await page.getByText('test.account.01@example.com', { exact: true }).waitFor();
          if (name === 'mailbox') await page.getByText('Outlook', { exact: true }).waitFor();
          await noOverflow(page, `${name}-${width}-${theme}`);
          await page.screenshot({ path: path.join(output, `${name}-${width}-${theme}.png`), fullPage: true });
        }
        await page.goto(baseURL + '/accounts/chatgpt?register=1');
        const dialog = page.getByRole('dialog');
        await dialog.getByRole('button', { name: '开始自动注册', exact: true }).waitFor();
        assert.equal(await dialog.getByRole('button', { name: '开始自动注册', exact: true }).isEnabled(), true);
        const bounds = await dialog.boundingBox();
        assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= width && bounds.y + bounds.height <= 901, `Dialog outside viewport: ${JSON.stringify(bounds)}`);
        assert.ok(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth + 1));
        await page.screenshot({ path: path.join(output, `register-${width}-${theme}.png`), fullPage: true });
        // Every API call is intercepted: this never creates a real registration task.
        await dialog.getByRole('button', { name: '开始自动注册', exact: true }).click();
        await page.getByText('2 / 5', { exact: true }).waitFor();
        await page.getByText('[18:30:42] Worker 1: mailbox ready', { exact: true }).waitFor();
        await page.screenshot({ path: path.join(output, `logs-${width}-${theme}.png`), fullPage: true });
        const worker = page.getByRole('button', { name: /Worker 1/ });
        await worker.click();
        assert.equal(await worker.getAttribute('aria-expanded'), 'false');
        await worker.click();
        await page.getByRole('button', { name: '取消任务', exact: true }).click();
        assert.equal(state.taskStatus, 'cancelled');
        await context.close();
        console.log(`PASS ${width}px ${theme}: four pages, registration and logs`);
      }
    }
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.on('pageerror', e => errors.push(e.message));
    const state = await mockAPI(page);
    await page.goto(baseURL + '/accounts/chatgpt');
    const search = page.getByRole('textbox', { name: /搜索/ });
    await search.fill('account.02');
    await page.getByText('test.account.02@example.com', { exact: true }).waitFor();
    await page.getByText('test.account.01@example.com', { exact: true }).waitFor({ state: 'hidden' });
    await page.locator('tbody input[type=checkbox]').first().check();
    assert.equal(await page.locator('tbody input:checked').count(), 1);
    await page.getByRole('button', { name: '收起侧栏', exact: true }).click();
    assert.ok(await page.locator('.app-sidebar').evaluate(el => el.classList.contains('is-collapsed')));
    await page.getByRole('button', { name: '展开侧栏', exact: true }).click();
    await page.getByRole('link', { name: '自动注册', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
    assert.ok(!new URL(page.url()).searchParams.has('register'));
    state.empty = true;
    await page.reload();
    await page.getByText('test.account.02@example.com', { exact: true }).waitFor({ state: 'hidden' });
    await page.screenshot({ path: path.join(output, 'accounts-empty.png') });
    state.failure = true;
    await page.reload();
    await page.getByRole('alert').waitFor();
    await page.screenshot({ path: path.join(output, 'accounts-error.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '展开侧栏', exact: true }).first().click();
    await page.locator('.app-sidebar.is-mobile-open').waitFor();
    await page.getByRole('link', { name: '邮箱服务', exact: true }).click();
    await page.getByText('Outlook', { exact: true }).waitFor();
    assert.equal(await page.locator('.app-sidebar.is-mobile-open').count(), 0);
    await page.locator('.provider-row').filter({ hasText: 'Outlook' }).getByRole('button', { name: '编辑', exact: true }).click();
    await page.getByRole('button', { name: '测试连接', exact: true }).click();
    await page.getByText('Test connection passed', { exact: true }).waitFor();
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByRole('button', { name: '邮箱管理', exact: true }).first().click();
    await page.getByText('mailbox.test@example.com', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, 'mailbox-manager-mobile.png') });
    await context.close();
    assert.deepEqual(errors, []);
    console.log('PASS search, selection, sidebar, registration shortcut, empty/error, mobile navigation; no page errors');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
