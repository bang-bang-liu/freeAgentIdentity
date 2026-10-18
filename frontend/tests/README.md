# Frontend Checks

Run from `frontend`:

```sh
npm run build
node tests/preview-server.cjs
```

The preview binds to `127.0.0.1:5173`. Set `UI_PREVIEW_PORT` to use another
port. It serves the real UI with isolated fixture responses. No API request
reaches the backend, and no real account, mailbox, or task is changed.
This server is for visual testing only. Normal development still uses
`npm run dev` and the backend on port 8000.

With Playwright and Microsoft Edge installed:

```sh
node tests/layout-smoke.cjs
node tests/registration-stats.cjs
```

`UI_BASE_URL` overrides the frontend URL. `PLAYWRIGHT_MODULE` can point to an
existing Playwright installation, and `UI_BROWSER_CHANNEL` selects another
installed browser. The suite intercepts every API request independently.

Coverage includes light/dark layouts, desktop/mobile overflow, registration,
search, selection, platform-scoped summary totals, status filters, account
drawer focus restoration, compact rows, task log collapse/cancellation,
empty/error states, and settings navigation. Screenshots are generated under
`.runtime/taste-ui-qa` at the repository root.

The account checks also cover avatar/table switching, selection retention,
circular sizing, accessible account initials and validity precedence. A
registered account with unknown validity must never be shown as available.

Registration checks cover historical/per-batch counts, failure stage labels,
date range requests, read-only inspection, empty/error/retry states, dialog
focus restoration and final statistics after an SSE completion race. The
fixtures never contact a registration, mailbox or login provider.
