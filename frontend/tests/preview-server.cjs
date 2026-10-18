const path = require('node:path');
const { mockAPI } = require('./layout-smoke.cjs');

// Isolated UI preview: every API request is served from fixtures, never proxied.
(async () => {
  let handler;
  const state = await mockAPI({ route: async (_pattern, callback) => { handler = callback; } });
  const { createServer } = await import('vite');
  const server = await createServer({
    root: path.resolve(__dirname, '..'),
    server: { host: '127.0.0.1', port: Number(process.env.UI_PREVIEW_PORT || 5173), strictPort: true },
    plugins: [{
      name: 'isolated-ui-fixtures',
      configureServer(vite) {
        vite.middlewares.use(async (req, res, next) => {
          if (!req.url.startsWith('/api/')) return next();
          const requestUrl = new URL(req.url, 'http://127.0.0.1');
          if (requestUrl.pathname === '/api/__fixture/state') {
            for (const key of ['empty', 'failure', 'healthCases', 'statsFailure', 'statsEmpty']) if (requestUrl.searchParams.has(key)) state[key] = requestUrl.searchParams.get(key) === 'true';
            if (requestUrl.searchParams.has('taskStatus')) state.taskStatus = requestUrl.searchParams.get('taskStatus');
            res.setHeader('Content-Type', 'application/json');
            res.end(JSON.stringify(state));
            return;
          }
          try {
            await handler({
              request: () => ({ url: () => requestUrl.href, method: () => req.method }),
              fulfill: async ({ status = 200, json, body, contentType }) => {
                res.statusCode = status;
                res.setHeader('Content-Type', contentType || 'application/json');
                res.end(json === undefined ? body : JSON.stringify(json));
              },
            });
          } catch (error) {
            res.statusCode = 500;
            res.end(JSON.stringify({ detail: error.message }));
          }
        });
      },
    }],
  });
  await server.listen();
  server.printUrls();
})().catch(error => { console.error(error); process.exitCode = 1; });
