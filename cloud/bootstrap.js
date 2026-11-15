(() => {
  const status = document.getElementById('login-message');
  const retry = document.getElementById('startup-retry');
  const report = (text, localFile = false) => {
    if (status) status.textContent = text;
    if (retry) {
      retry.href = localFile ? 'http://127.0.0.1:8002/job-radar/' : location.pathname;
      retry.textContent = localFile ? '打开本地测试网页' : '重新加载登录页';
      retry.hidden = false;
    }
  };
  if (location.protocol === 'file:') {
    report('浏览器无法从文件地址加载登录模块。请先启动 JobRadar 本地预览，再打开 HTTP 测试网页。', true);
    return;
  }
  if (status) status.textContent = '正在加载登录组件…';
  import('./app.mjs').then(({start}) => start(report)).catch(() => {
    report('登录模块 app.mjs 或 data.mjs 加载失败。请检查本地预览服务并重新构建网页。');
  });
})();
