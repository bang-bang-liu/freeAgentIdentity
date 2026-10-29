const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { mockAPI } = require('./layout-smoke.cjs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const baseURL = process.env.UI_BASE_URL || 'http://127.0.0.1:5173';
const output = path.resolve(__dirname, '../../.runtime/login-refresh-qa');

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const errors = [];
  try {
    for (const theme of ['dark', 'light']) {
      for (const width of [1440, 1024]) {
        const context = await browser.newContext({ viewport: { width, height: 960 }, reducedMotion: 'reduce' });
        const page = await context.newPage();
        page.on('pageerror', err => errors.push(err.message));
        await page.addInitScript(value => {
          localStorage.setItem('theme', value);
          localStorage.setItem('app-language', 'zh-CN');
        }, theme);
        const state = await mockAPI(page);
        await page.goto(baseURL + '/accounts/chatgpt');
        const single = page.getByRole('button', { name: '重新登录 test.account.01@example.com', exact: true });
        await single.waitFor();
        await single.click();
        const dialog = page.getByRole('dialog', { name: '重新登录 ChatGPT', exact: true });
        await dialog.waitFor();
        await dialog.getByText('test.account.01@example.com', { exact: true }).waitFor();
        assert.match(await dialog.innerText(), /密码 \+ TOTP/);
        assert.equal(await dialog.getByLabel('登录代理', { exact: true }).inputValue(), 'http://127.0.0.1:7897');
        assert.equal(await page.evaluate(() => document.querySelector('[role="dialog"]').contains(document.activeElement)), true);
        for (let i = 0; i < 12; i++) {
          await page.keyboard.press('Tab');
          assert.equal(await page.evaluate(() => document.querySelector('[role="dialog"]').contains(document.activeElement)), true);
        }
        const box = await dialog.boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width && box.y >= 0 && box.y + box.height <= 960);
        await page.screenshot({ path: path.join(output, `single-${width}-${theme}.png`) });
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'hidden' });
        await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === '重新登录 test.account.01@example.com');

        await page.getByRole('button', { name: '表格视图', exact: true }).click();
        await page.getByRole('checkbox', { name: '选择 test.account.01@example.com', exact: true }).check();
        await page.getByRole('checkbox', { name: '选择 test.account.02@example.com', exact: true }).check();
        await page.getByRole('button', { name: '重新登录 (2)', exact: true }).click();
        await dialog.getByText('test.account.02@example.com', { exact: true }).waitFor();
        assert.match(await dialog.innerText(), /邮箱验证码/);
        await dialog.getByLabel('登录代理', { exact: true }).fill('http://127.0.0.1:7890');
        await dialog.getByLabel('登录并发数', { exact: true }).selectOption('2');
        await dialog.getByLabel('邮箱验证码来源', { exact: true }).selectOption('manual');
        await page.screenshot({ path: path.join(output, `batch-${width}-${theme}.png`) });
        await dialog.getByRole('button', { name: '开始登录 (2)', exact: true }).click();
        assert.deepEqual(state.loginBody.ids, [1, 2]);
        assert.equal(state.loginBody.proxy, 'http://127.0.0.1:7890');
        assert.equal(state.loginBody.email_otp_mode, 'manual');
        assert.equal(state.loginBody.setup_security, false);
        await page.getByRole('button', { name: '取消任务', exact: true }).click();
        await page.getByText('已取消 · 保留原凭据', { exact: true }).first().waitFor();
        assert.equal(state.taskStatus, 'cancelled');
        await page.screenshot({ path: path.join(output, `cancel-${width}-${theme}.png`) });
        await context.close();
        console.log(`PASS relogin desktop ${width}px ${theme}: single, batch, proxy, methods, focus, cancel`);
      }
    }

    const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
    const page = await context.newPage();
    page.on('pageerror', err => errors.push(err.message));
    await page.addInitScript(() => localStorage.setItem('app-language', 'zh-CN'));
    const state = await mockAPI(page);
    await page.goto(baseURL + '/accounts/chatgpt');
    await page.getByRole('button', { name: '重新登录 test.account.01@example.com', exact: true }).waitFor();
    state.loginPreviewFailure = true;
    await page.getByRole('button', { name: '重新登录', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '重新登录 ChatGPT', exact: true });
    await dialog.getByRole('alert').waitFor();
    assert.equal(await dialog.getByRole('button', { name: '开始登录', exact: true }).isDisabled(), true);
    state.loginPreviewFailure = false;
    state.loginBlocked = true;
    await dialog.getByRole('button', { name: '重试', exact: true }).click();
    await dialog.getByText('预检 43 个账号，可登录 0 个', { exact: true }).waitFor();
    assert.equal(await dialog.getByRole('button', { name: '开始登录', exact: true }).isDisabled(), true);
    await page.keyboard.press('Escape');
    state.loginBlocked = false;
    await page.getByRole('button', { name: '重新登录 test.account.01@example.com', exact: true }).click();
    await dialog.getByRole('button', { name: '开始登录 (1)', exact: true }).click();
    state.taskStatus = 'succeeded';
    await page.getByText('Token 验证通过 · 已保存', { exact: true }).waitFor();
    assert.match(await page.getByLabel('重新登录执行结果').innerText(), /密码 \+ TOTP.*7897/);
    await page.screenshot({ path: path.join(output, 'success-result.png') });
    assert.deepEqual(errors, []);
    assert.ok(state.writes.every(write => ['/login-refresh/preview', '/login-refresh/create'].includes(write.endpoint)), 'UI verification must use isolated fixtures only');
    await context.close();
    console.log('PASS relogin error/retry, blocked accounts, validated/saved results; no page errors or real account writes');
  } finally { await browser.close(); }
})().catch(err => { console.error(err); process.exitCode = 1; });
