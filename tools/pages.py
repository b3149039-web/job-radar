"""Build, preview and audit the GitHub Pages files without publishing them."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloud.serve_local import ASSETS, load_config

BUILD_DIR = ROOT / 'dist' / 'job-radar'
STAGING_BUILD_DIR = ROOT / 'dist' / 'staging-preview'
PUBLIC_REPO = ROOT / 'pages-repo'
ASSET_FILES = {'index.html': ASSETS['/'], **{route[1:]: asset for route, asset in ASSETS.items() if route != '/'}}
BUILD_NAMES = set(ASSET_FILES) | {'config.json', '.nojekyll'}
PUBLISH_SOURCES = (
    '.gitignore', '.github/workflows/pages.yml', 'cloud/index.html', 'cloud/style.css',
    'cloud/app.mjs', 'cloud/data.mjs', 'cloud/serve_local.py', 'pnpm-lock.yaml', 'tools/pages.py',
)


def public_config(url, key):
    url = (url or '').strip().rstrip('/')
    key = (key or '').strip()
    if not re.fullmatch(r'https://[a-z0-9-]+\.supabase\.co', url):
        raise ValueError('SUPABASE_URL must be a Supabase HTTPS project origin.')
    if not re.fullmatch(r'sb_publishable_[A-Za-z0-9_-]+', key) or key == 'sb_publishable_REPLACE_ME':
        raise ValueError('SUPABASE_PUBLISHABLE_KEY must be a publishable key.')
    return {'url': url, 'publishableKey': key}


def check_public_bytes(data):
    if re.search(rb'sb_secret_[a-zA-Z0-9_-]{8,}|postgres(?:ql)?://[^/\s]+:[^@\s]+@', data):
        raise ValueError('Sensitive value detected in a publishing file. Build stopped.')


def staging_preview_config(expected_ref, directory=ROOT):
    staging = load_config(directory / '.env.staging')
    production = load_config(directory / '.env')
    if not staging or not production:
        raise ValueError('需要独立的 .env.staging 和现有的 .env 来比对项目。')
    if (not re.fullmatch(r'[a-z0-9]+', expected_ref) or
            staging['url'] != f'https://{expected_ref}.supabase.co' or
            staging['url'] == production['url'] or staging['publishableKey'] == production['publishableKey']):
        raise ValueError('测试项目与指定的 Project Ref 不匹配，或配置和正式环境重复。')
    return staging


def build(out, config, *, manual_applications=False):
    config = public_config(config['url'], config['publishableKey'])
    out.mkdir(parents=True, exist_ok=True)
    if out.is_symlink() or any(
        file.name not in BUILD_NAMES or file.is_symlink() or not file.is_file() for file in out.iterdir()
    ):
        raise ValueError('The build directory contains unexpected entries. Use a clean directory.')
    files = {name: asset.read_bytes() for name, (asset, _) in ASSET_FILES.items()}
    csp = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        f"connect-src 'self' {config['url']}; base-uri 'none'; form-action 'none'"
    )
    head = ('<head>\n  <meta http-equiv="Content-Security-Policy" content="' + csp +
            '">\n  <meta name="referrer" content="no-referrer">').encode()
    if files['index.html'].count(b'<head>') != 1:
        raise ValueError('Cannot install the static site CSP.')
    files['index.html'] = files['index.html'].replace(b'<head>', head)
    if manual_applications:
        files['index.html'] = files['index.html'].replace(b'<head>', b'<head>\n  <meta name="jobradar-manual-applications" content="enabled">')
    files['config.json'] = (json.dumps(config, indent=2) + '\n').encode()
    files['.nojekyll'] = b''
    for data in files.values():
        check_public_bytes(data)
    for name, data in files.items():
        (out / name).write_bytes(data)
    audit_build(out)


def audit_build(out):
    if out.is_symlink() or {entry.name for entry in out.iterdir()} != BUILD_NAMES:
        raise ValueError('The Pages artifact does not match the allowlist.')
    for entry in out.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise ValueError('The Pages artifact contains a link or non-file.')
        check_public_bytes(entry.read_bytes())
    config = json.loads((out / 'config.json').read_text(encoding='utf-8'))
    if set(config) != {'url', 'publishableKey'} or config != public_config(config['url'], config['publishableKey']):
        raise ValueError('The Pages artifact contains unexpected configuration.')


def export_source():
    files = {name: (ROOT / name).read_bytes() for name in PUBLISH_SOURCES}
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    package.pop('scripts', None)
    files['package.json'] = (json.dumps(package, indent=2) + '\n').encode()
    files['README.md'] = (
        '# JobRadar Pages\n\n'
        'Static Supabase client. Enable GitHub Actions in Settings → Pages, set '
        'SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY as repository Actions variables, '
        'then run the Deploy JobRadar Pages workflow manually.\n'
    ).encode('utf-8')
    for data in files.values():
        check_public_bytes(data)
    PUBLIC_REPO.mkdir()
    for name, data in files.items():
        destination = PUBLIC_REPO / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def private_path(name):
    parts = PurePosixPath(name.casefold()).parts
    return any(part in ('node_modules', '.jobradar-backups', 'dist') or
               (part == '.env' or part.startswith('.env.') and part != '.env.example') or
               (part.startswith(('jobradar-legacy-backup', 'jobradar-private-backup')) and part.endswith('.json'))
               for part in parts)


def audit_git(directory):
    def git(*args, check=True):
        return subprocess.run(['git', '-C', str(directory), *args], capture_output=True, check=check)

    repo = git('rev-parse', '--show-toplevel', check=False)
    if repo.returncode:
        if b'not a git repository' in repo.stderr.lower():
            print('No local Git repository; there is no local Git history to audit.')
            return
        raise ValueError('Git repository lookup failed; no history verdict is available.')
    if Path(os.fsdecode(repo.stdout).strip()).resolve() != directory.resolve():
        raise ValueError('Select the repository root. The selected directory belongs to a parent repository.')
    indexed = git('ls-files', '-z').stdout.decode('utf-8', errors='replace').split('\0')
    refs = ['HEAD'] if git('rev-parse', '--verify', 'HEAD', check=False).returncode == 0 else []
    history = git('log', '--all', '--reflog', *refs, '--format=', '--name-only', '--no-renames', '-z')
    names = (name.strip('\n') for name in history.stdout.decode('utf-8', errors='replace').split('\0'))
    if any(map(private_path, indexed)) or any(map(private_path, names)):
        raise ValueError('A private path is tracked, staged or present in locally available Git history. Stop before publishing.')
    if git('check-ignore', '--quiet', '.env').returncode != 0:
        raise ValueError('The .env ignore rule did not apply.')
    print('Git index, local refs/reflogs and .env ignore rule passed.')


def preview(out, port):
    audit_build(out)
    media = {name: asset[1] for name, asset in ASSET_FILES.items()} | {'config.json': 'application/json'}
    prefix = '/job-radar/'

    class Handler(BaseHTTPRequestHandler):
        def log_request(self, code='-', size='-'):
            self.log_message('%s %s %s', self.command, self.path.partition('?')[0], code)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{port}':
                self.send_error(403)
                return
            url_path = self.path.partition('?')[0]
            name = (url_path[len(prefix):] or 'index.html') if url_path.startswith(prefix) else ''
            if name not in media:
                self.send_error(404)
                return
            data = (out / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', media[name] + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(('127.0.0.1', port), Handler) as server:
        print(f'Pages preview at http://127.0.0.1:{port}{prefix}', flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build_command = commands.add_parser('build')
    build_command.add_argument('--local-env', action='store_true')
    build_command.add_argument('--manual-applications', action='store_true',
                               help='Enable the applications UI after migration 006 is verified')
    build_command.add_argument('--expected-project-ref', help='Require the build URL to match the approved project')
    commands.add_parser('audit-build')
    commands.add_parser('export-source')
    commands.add_parser('audit-git').add_argument('directory', type=Path, nargs='?', default=ROOT)
    commands.add_parser('preview').add_argument('--port', type=int, default=8001)
    stage = commands.add_parser('preview-staging', help='构建并预览独立的 jobradar-staging 前端')
    stage.add_argument('--project-ref', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'build':
            if not BUILD_DIR.resolve().is_relative_to(ROOT):
                raise ValueError('The build directory resolves outside the project.')
            config = load_config() if args.local_env else public_config(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_PUBLISHABLE_KEY'))
            if not config:
                raise ValueError('No local .env configuration.')
            if args.expected_project_ref is not None and (
                    not re.fullmatch(r'[a-z0-9]{20}', args.expected_project_ref) or
                    config['url'] != f'https://{args.expected_project_ref}.supabase.co'):
                raise ValueError('Supabase URL does not match the approved Pages project reference.')
            build(BUILD_DIR, config, manual_applications=args.manual_applications)
            print('Built and audited dist/job-radar.')
        elif args.command == 'audit-build':
            audit_build(BUILD_DIR)
            print('Artifact allowlist passed.')
        elif args.command == 'export-source':
            export_source()
            print('Created pages-repo from the public source allowlist.')
        elif args.command == 'audit-git':
            audit_git(args.directory)
        elif args.command == 'preview-staging':
            if STAGING_BUILD_DIR.resolve() != STAGING_BUILD_DIR:
                raise ValueError('测试构建目录不能重定向到其他路径。')
            build(STAGING_BUILD_DIR, staging_preview_config(args.project_ref), manual_applications=True)
            print('jobradar-staging preview only.', flush=True)
            preview(STAGING_BUILD_DIR, 8002)
        else:
            preview(BUILD_DIR, args.port)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
