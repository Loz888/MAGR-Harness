const http = require('node:http');
const server = http.createServer((request, response) => response.end('ok'));
server.listen(0, '127.0.0.1', () => console.log('READY port=' + server.address().port));
process.on('SIGTERM', () => server.close(() => process.exit(0)));
