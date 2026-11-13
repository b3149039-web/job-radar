"""Serve the cloud client on the current computer only."""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SDK = ROOT / 'node_modules/@supabase/supabase-js/dist/umd/supabase.js'
ASSETS = {
    '/': (HERE / 'index.html', 'text/html'),
    '/style.css': (HERE / 'style.css', 'text/css'),
    '/app.mjs': (HERE / 'app.mjs', 'text/javascript'),
    '/data.mjs': (HERE / 'data.mjs', 'text/javascript'),
    '/sdk.js': (SDK, 'text/javascript'),
}


def load_config(path=ROOT / '.env'):
    if not path.is_file():
        return None
    env = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, value = line.partition('=')
        name, value = name.strip(), value.strip()
        if not separator or name not in ('SUPABASE_URL', 'SUPABASE_PUBLISHABLE_KEY') or name in env:
            raise ValueError('.env 仅接受 SUPABASE_URL 与 SUPABASE_PUBLISHABLE_KEY，每项填一次。')
        env[name] = value
    url = env.get('SUPABASE_URL', '').rstrip('/')
    key = env.get('SUPABASE_PUBLISHABLE_KEY', '')
    if not re.fullmatch(r'https://[a-z0-9-]+\.supabase\.co', url):
        raise ValueError('请填写 Supabase HTTPS Project URL。')
    if not re.fullmatch(r'sb_publishable_[A-Za-z0-9_-]+', key) or key == 'sb_publishable_REPLACE_ME':
        raise ValueError('请填写 sb_publishable_ 开头的公开密钥。')
    return {'url': url, 'publishableKey': key}


def make_handler(config, port):
    class LocalHandler(BaseHTTPRequestHandler):
        def log_request(self, code='-', size='-'):
            self.log_message('%s %s %s', self.command, self.path.partition('?')[0], code)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{port}':
                self.send_error(403)
                return
            path = self.path.partition('?')[0]
            if path == '/config.json':
                body, media = json.dumps(config).encode('utf-8'), 'application/json'
            elif path in ASSETS:
                file, media = ASSETS[path]
                if not file.is_file():
                    self.send_error(404)
                    return
                body = file.read_bytes()
            else:
                self.send_error(404)
                return
            endpoint = config['url'] if config else ''
            self.send_response(200)
            self.send_header('Content-Type', media + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; "
                             f"style-src 'self'; connect-src 'self' {endpoint}; "
                             "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)
    return LocalHandler


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    try:
        config = load_config()
    except ValueError as exc:
        parser.error(str(exc))
    if not SDK.is_file():
        print('Supabase SDK 尚未安装。请先按 PHASE2.md 安装项目依赖。')
    if not config:
        print('尚未配置 .env，登录不可用。')
    with ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(config, args.port)) as server:
        print(f'JobRadar http://127.0.0.1:{args.port}/', flush=True)
        server.serve_forever()
