const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

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
  const state = { failure: false, empty: false, healthCases: false, statsFailure: false, statsEmpty: false, taskType: 'register', taskSuccess: 1, taskFailed: 1, taskStatus: 'running', writes: [] };
  const taskResponse = () => ({ type: state.taskType, status: state.taskStatus, success: state.taskSuccess, error_count: state.taskFailed, cancellable: state.taskStatus === 'running', progress_detail: { total: 5, current: state.taskSuccess + state.taskFailed, label: `${state.taskSuccess + state.taskFailed} / 5` }, registration_stats: { success: state.taskSuccess, failed: state.taskFailed, completed: state.taskSuccess + state.taskFailed, success_rate: state.taskSuccess + state.taskFailed ? Math.round(state.taskSuccess / (state.taskSuccess + state.taskFailed) * 1000) / 10 : null, failure_stages: state.taskFailed ? [{ stage: 'verification', count: state.taskFailed }] : [], setup_failures: 0, setup_failure_stages: [] } });
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
    if (endpoint === '/tasks/register/stats') {
      if (state.statsFailure) return send({ error: 'Fixture statistics unavailable' }, 503);
      return send({ success: state.statsEmpty ? 0 : 137, failed: state.statsEmpty ? 0 : 19, completed: state.statsEmpty ? 0 : 156, success_rate: state.statsEmpty ? null : 87.8, total_tasks: state.statsEmpty ? 0 : 24, active_tasks: 0, cancelled_tasks: state.statsEmpty ? 0 : 2, interrupted_tasks: 0, setup_failures: 0, setup_failure_stages: [], failure_stages: state.statsEmpty ? [] : [{ stage: 'verification', count: 11 }, { stage: 'authorization', count: 4 }, { stage: 'credentials', count: 3 }, { stage: 'unknown', count: 1 }], updated_at: '2026-10-18T05:40:32Z' });
    }
    if (endpoint === '/accounts') {
      if (state.failure) return send({ error: 'Test connection failure' }, 503);
      const source = state.healthCases ? [
        { ...accounts[0], email: 'alpha@example.com', display_status: 'registered', validity_status: 'unknown' },
        { ...accounts[1], email: 'beta@example.com', validity_status: 'invalid', display_summary: { status: { validity: 'valid' } } },
        { ...accounts[2], email: 'gamma@example.com', validity_status: 'valid', display_summary: { status: { validity: 'invalid' } } },
      ] : accounts;
      let rows = state.empty ? [] : source.filter(a => a.email.includes(url.searchParams.get('email') || ''));
      if (url.searchParams.get('status')) rows = rows.filter(a => [a.display_status, a.lifecycle_status, a.plan_state, a.validity_status].includes(url.searchParams.get('status')));
      const size = Number(url.searchParams.get('page_size') || 20);
      const start = (Number(url.searchParams.get('page') || 1) - 1) * size;
      return send({ items: rows.slice(start, start + size), total: rows.length });
    }
    if (endpoint === '/actions/chatgpt') return send({ actions: [] });
    if (/^\/accounts\/\d+$/.test(endpoint)) return send({ ok: true });
    if (endpoint.startsWith('/accounts/export/')) return send({ items: accounts });
    if (endpoint.startsWith('/accounts/check-') || endpoint === '/accounts/login-refresh') { state.taskType = 'account_check_all'; return send({ task_id: 'layout-test' }); }
    if (endpoint === '/tasks/register') { state.taskType = 'register'; state.taskStatus = 'running'; return send({ task_id: 'layout-test' }); }
    if (endpoint === '/tasks/layout-test') return send(taskResponse());
    if (endpoint.endsWith('/cancel')) { state.taskStatus = 'cancelled'; return send(taskResponse()); }
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

module.exports = { mockAPI };

if (require.main === module) {
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
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
          if (name === 'accounts') {
            await page.getByText('test.account.01@example.com', { exact: true }).waitFor();
            assert.equal(await page.locator('.account-tile').count(), 20);
            assert.equal(await page.locator('.account-tile').first().locator('.account-avatar').getAttribute('data-health'), 'invalid');
            assert.equal(await page.locator('.account-tile').nth(1).locator('.account-avatar').getAttribute('data-health'), 'valid');
            const images = await page.locator('.account-avatar').evaluateAll(elements => elements.map(el => { const r = el.getBoundingClientRect(); return { width: r.width, height: r.height }; }));
            assert.ok(images.every(image => image.width === image.height && image.width === 54), 'Avatars must remain circular');
            await page.screenshot({ path: path.join(output, `avatars-${width}-${theme}.png`), fullPage: true });
            await page.getByRole('checkbox', { name: '选择 test.account.02@example.com', exact: true }).check();
            await page.getByRole('button', { name: '表格视图', exact: true }).click();
            await page.locator('tbody .account-avatar').first().waitFor();
            assert.equal(await page.getByRole('checkbox', { name: '选择 test.account.02@example.com', exact: true }).isChecked(), true);
            await noOverflow(page, `table-${width}-${theme}`);
            await page.screenshot({ path: path.join(output, `table-${width}-${theme}.png`), fullPage: true });
            await page.getByRole('button', { name: '头像视图', exact: true }).click();
          }
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
    await page.addInitScript(() => localStorage.setItem('accounts-view', 'table'));
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
    await page.getByRole('button', { name: '清除', exact: true }).click();
    await page.getByRole('button', { name: '有效 36', exact: true }).click();
    await page.getByRole('button', { name: 'test.account.01@example.com', exact: true }).waitFor({ state: 'hidden' });
    assert.equal(await page.getByRole('combobox', { name: '状态', exact: true }).inputValue(), 'valid');
    assert.equal(await page.getByRole('button', { name: '全部账号 43', exact: true }).count(), 1);
    await page.getByRole('button', { name: 'test.account.02@example.com', exact: true }).click();
    await page.getByRole('dialog', { name: '账号详情', exact: true }).waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    assert.equal(await page.evaluate(() => document.activeElement.textContent), 'test.account.02@example.com');
    await page.getByRole('button', { name: '紧凑行高', exact: true }).click();
    assert.equal(await page.locator('.accounts-panel').getAttribute('data-density'), 'compact');
    await page.getByRole('button', { name: '全部账号 43', exact: true }).click();
    await page.getByRole('button', { name: '自动注册', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: '开始自动注册', exact: true }).click();
    await page.getByRole('dialog').getByText('2 / 5', { exact: true }).waitFor();
    await page.getByRole('dialog').getByRole('button', { name: '关闭', exact: true }).click();
    const dock = page.getByRole('region', { name: '近期任务', exact: true });
    await dock.getByText('执行中', { exact: true }).first().waitFor();
    await dock.getByRole('button', { name: '查看日志', exact: true }).click();
    await dock.getByText('[18:30:42] Worker 1: mailbox ready', { exact: true }).waitFor();
    await dock.getByRole('button', { name: '取消任务', exact: true }).click();
    await dock.getByRole('button', { name: '收起日志', exact: true }).click();
    await dock.getByText('已取消', { exact: true }).first().waitFor();
    state.empty = true;
    await page.reload();
    await page.getByText('test.account.02@example.com', { exact: true }).waitFor({ state: 'hidden' });
    await page.screenshot({ path: path.join(output, 'accounts-empty.png') });
    state.failure = true;
    await page.reload();
    await page.getByRole('alert').first().waitFor();
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
    const healthContext = await browser.newContext({ viewport: { width: 1280, height: 720 } });
    const healthPage = await healthContext.newPage();
    healthPage.on('pageerror', error => errors.push(error.message));
    const healthState = await mockAPI(healthPage);
    healthState.healthCases = true;
    await healthPage.goto(baseURL + '/accounts/chatgpt');
    await healthPage.getByRole('button', { name: 'alpha@example.com', exact: true }).waitFor();
    const healthValues = await healthPage.locator('.account-tile .account-avatar').evaluateAll(elements => elements.map(el => el.dataset.health));
    assert.deepEqual(healthValues, ['unknown', 'valid', 'invalid']);
    assert.deepEqual(await healthPage.locator('.account-avatar-initial').allTextContents(), ['A', 'B', 'G']);
    assert.equal(await healthPage.locator('.account-tile[data-health=unknown]').count(), 1, 'Registered must not imply available');
    await healthPage.getByRole('button', { name: 'beta@example.com', exact: true }).click();
    await healthPage.getByRole('dialog', { name: '账号详情', exact: true }).waitFor();
    await healthPage.keyboard.press('Escape');
    await healthPage.getByRole('dialog').waitFor({ state: 'hidden' });
    // Radix restores focus in a scheduled cleanup after the dialog unmounts.
    await healthPage.waitForFunction(() => document.activeElement.getAttribute('aria-label') === 'beta@example.com', null, { timeout: 2000 });
    assert.equal(await healthPage.evaluate(() => document.activeElement.getAttribute('aria-label')), 'beta@example.com');
    assert.deepEqual(healthState.writes, [], 'Avatar inspection must not mutate accounts');
    await healthContext.close();
    assert.deepEqual(errors, []);
    console.log('PASS search, selection, sidebar, registration shortcut, empty/error, mobile navigation; no page errors');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
}
