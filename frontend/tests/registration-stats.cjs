const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { mockAPI } = require('./layout-smoke.cjs');
const baseURL = process.env.UI_BASE_URL || 'http://127.0.0.1:5173';
const output = path.resolve(__dirname, '../../.runtime/registration-ui-qa');

async function noOverflow(page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth || [...document.querySelectorAll('.registration-analytics, .registration-charts, .analytics-dialog')].some(el => el.scrollWidth > el.clientWidth + 1));
  assert.equal(overflow, false, 'Statistics must fit the viewport');
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, channel: process.env.UI_BROWSER_CHANNEL || 'msedge' });
  const errors = [];
  try {
    for (const width of [1440, 1024, 768, 390, 360]) {
      for (const theme of ['dark', 'light']) {
        const context = await browser.newContext({ viewport: { width, height: 1000 }, reducedMotion: 'reduce', isMobile: width < 768, hasTouch: width < 768 });
        const page = await context.newPage();
        page.on('pageerror', error => errors.push(error.message));
        await page.addInitScript(theme => { localStorage.setItem('theme', theme); localStorage.setItem('app-language', 'zh-CN'); }, theme);
        const state = await mockAPI(page);
        await page.goto(baseURL + '/');
        await page.locator('[data-stat="success"]').waitFor();
        assert.equal(await page.locator('[data-stat="success"]').textContent(), '137');
        assert.equal(await page.locator('[data-stat="failed"]').textContent(), '19');
        assert.equal(await page.locator('[data-stage="verification"] strong').textContent(), '11');
        assert.equal(await page.locator('[data-stage="unknown"]').count(), 1);
        await page.evaluate(() => document.fonts.ready);
        await noOverflow(page);
        await page.screenshot({ path: path.join(output, `overview-${width}-${theme}.png`), fullPage: true });
        const periodRequest = page.waitForRequest(request => request.url().includes('/tasks/register/stats?') && request.url().includes('days=7'));
        await page.getByRole('combobox', { name: '统计时间范围' }).selectOption('7');
        await periodRequest;
        await page.locator('[data-stat="success"]').waitFor();
        await page.goto(baseURL + '/accounts/chatgpt');
        const trigger = page.getByRole('button', { name: '注册统计', exact: true });
        await trigger.click();
        const dialog = page.getByRole('dialog', { name: '注册表现' });
        await dialog.locator('[data-stat="failed"]').waitFor();
        await noOverflow(page);
        await page.screenshot({ path: path.join(output, `statistics-${width}-${theme}.png`), fullPage: true });
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'hidden' });
        assert.equal(await trigger.evaluate(el => el === document.activeElement), true, 'Closing statistics restores focus');
        await page.getByRole('button', { name: '自动注册', exact: true }).first().click();
        await page.getByRole('button', { name: '开始自动注册', exact: true }).click();
        await page.locator('.task-registration-statistics [data-stat="failed"]').waitFor();
        assert.equal(await page.locator('.task-registration-statistics [data-stat="failed"]').textContent(), '1');
        assert.equal(await page.locator('.task-registration-statistics [data-stage="verification"] strong').textContent(), '1');
        state.taskSuccess = 4;
        state.taskFailed = 1;
        state.taskStatus = 'succeeded';
        await page.waitForFunction(() => document.querySelector('.task-registration-statistics [data-stat="success"]')?.textContent === '4');
        await noOverflow(page);
        await page.screenshot({ path: path.join(output, `batch-${width}-${theme}.png`), fullPage: true });
        await context.close();
      }
    }

    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: { width: 900, height: 390 }, reducedMotion: 'reduce', isMobile: true, hasTouch: true });
      const page = await context.newPage();
      await page.addInitScript(theme => localStorage.setItem('theme', theme), theme);
      await mockAPI(page);
      await page.goto(baseURL + '/accounts/chatgpt');
      await page.getByRole('button', { name: '注册统计', exact: true }).click();
      await page.locator('[data-stat="success"]').waitFor();
      await noOverflow(page);
      await page.screenshot({ path: path.join(output, `statistics-landscape-${theme}.png`), fullPage: true });
      await context.close();
    }

    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    const state = await mockAPI(page);
    state.statsEmpty = true;
    await page.goto(baseURL + '/');
    await page.getByText('暂无注册结果', { exact: true }).waitFor();
    assert.equal(await page.locator('.outcome-ring-label strong').textContent(), '—');
    state.statsEmpty = false;
    state.statsFailure = true;
    await page.getByRole('button', { name: '刷新注册统计' }).click();
    await page.getByRole('alert').filter({ hasText: '注册统计加载失败' }).waitFor();
    state.statsFailure = false;
    await page.getByRole('button', { name: '重试', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[data-stat="success"]')?.textContent === '137');
    assert.equal(state.writes.length, 0, 'Inspecting/retrying statistics is read-only');

    // A final SSE event must fetch final counts even when an older GET resolves late.
    let taskReads = 0;
    await page.route('**/api/tasks/layout-test', async route => {
      const currentRead = ++taskReads;
      if (currentRead === 1) await new Promise(resolve => setTimeout(resolve, 400));
      return route.fulfill({ json: { type: 'register', status: currentRead === 1 ? 'running' : 'succeeded', success: currentRead === 1 ? 1 : 4, error_count: 1, progress_detail: { total: 5, current: currentRead === 1 ? 2 : 5 } } });
    });
    await page.route('**/api/tasks/layout-test/logs/stream', route => route.fulfill({ contentType: 'text/event-stream', body: 'data: {"done":true,"status":"succeeded"}\n\n' }));
    await page.goto(baseURL + '/accounts/chatgpt?register=1');
    await page.getByRole('button', { name: '开始自动注册', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.task-registration-statistics [data-stat="success"]')?.textContent === '4');
    await page.waitForTimeout(500);
    assert.equal(await page.locator('.task-registration-statistics [data-stat="success"]').textContent(), '4', 'Late running snapshots cannot overwrite final counts');
    assert.ok(taskReads >= 2, 'SSE completion must request the final task snapshot');
    assert.deepEqual(errors, []);
    await context.close();
    console.log('Registration statistics passed: 12 responsive/theme combinations, history, periods, focus, empty/error/retry and final SSE counts.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
