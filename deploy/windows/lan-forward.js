// DeskClaw LAN 端口转发器（用户态，无需管理员）。
// 监听 0.0.0.0 并转发到 127.0.0.1（WSL2 mirrored 回环通道）。
// 适用场景：Hyper-V 防火墙未放行/被组策略回滚时的内网访问兜底（见部署文档坑 2）。
//
// 用法：node lan-forward.js
// 端口可用环境变量覆盖：LAN_FROM_PORT（默认 24517）/ LAN_TO_PORT（默认 14517）
const net = require('net');

const from = parseInt(process.env.LAN_FROM_PORT || '24517', 10);
const to = parseInt(process.env.LAN_TO_PORT || '14517', 10);

const server = net.createServer((clientSock) => {
  const upstream = net.connect({ host: '127.0.0.1', port: to });
  clientSock.pipe(upstream);
  upstream.pipe(clientSock);
  const clean = () => { clientSock.destroy(); upstream.destroy(); };
  clientSock.on('error', clean);
  upstream.on('error', clean);
  clientSock.on('close', () => upstream.destroy());
  upstream.on('close', () => clientSock.destroy());
});
server.on('error', (e) => console.error(`[fwd] port ${from}: ${e.message}`));
server.listen(from, '0.0.0.0', () => {
  console.log(`[fwd] 0.0.0.0:${from} -> 127.0.0.1:${to} OK`);
});

setInterval(() => {
  console.log(`[fwd] heartbeat ${new Date().toLocaleString()}`);
}, 60000);
