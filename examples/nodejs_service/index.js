const http = require('http');

const port = process.env.PORT || 3000;

const server = http.createServer((req, res) => {
  res.statusCode = 200;
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify({
    status: 'healthy',
    message: 'Heimdall NodeJS Node Alive!',
    time: new Date().toISOString(),
    env: process.env.NODE_ENV || 'development'
  }));
});

server.listen(port, () => {
  console.log(`NodeJS Server running on port ${port}`);
});
