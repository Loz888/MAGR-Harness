'use strict';
// A real TLS/ALPN/HTTP2 endpoint for public protocol preconditions. No proxying,
// arbitrary host selection, project source, hidden test or application code.
const fs = require('node:fs');
const http2 = require('node:http2');
const options = {
  key: fs.readFileSync('/opt/harness/server.key'),
  cert: fs.readFileSync('/opt/harness/server.crt'),
  allowHTTP1: true,
  settings: {maxConcurrentStreams: 32},
};
let ready = 0;
for (const address of ['127.0.0.2', '127.0.0.3']) {
  const server = http2.createSecureServer(options, (request, response) => {
    const host = request.headers[':authority'] || request.headers.host;
    const allowedHost = address === '127.0.0.2' ? 'example.com' : 'www.google.com';
    const allowed = host === allowedHost && request.method === 'GET' && request.url === '/';
    const status = host !== allowedHost ? 421 : request.method !== 'GET' ? 405 : 404;
    setTimeout(() => {
      if (response.destroyed) return;
      response.writeHead(allowed ? 200 : status, {
        'content-type': 'text/html; charset=utf-8',
        'cache-control': 'public, max-age=60',
      });
      response.end(allowed ? '<!doctype html><title>Offline protocol fixture</title>\n' : 'Unavailable\n');
    }, 15);
  });
  server.maxConnections = 64;
  server.on('sessionError', () => {});
  server.on('tlsClientError', () => {});
  server.on('error', () => process.exit(1));
  server.listen(443, address, () => {
    if (++ready !== 2) return;
    process.setgroups([]);
    process.setgid(65532);
    process.setuid(65532);
    const status = fs.readFileSync('/proc/self/status', 'utf8');
    if (!/^CapEff:\s+0+$/m.test(status)) process.exit(1);
    console.log('HARNESS_OFFLINE_TLS_READY');
  });
}
