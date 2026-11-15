import contextlib
from datetime import datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('jobradar_pages', ROOT / 'tools/pages.py')
pages = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pages)
from tools import daily_candidates as daily
from collect_jobs import payload, sources


class PagesTest(unittest.TestCase):
    def collection(self, directory, now, codes=(1, 1), warnings=None):
        jobs = [payload(dict(company='测试单位',title='法务专员 ' + str(code),city='上海',source=sources.JNJ_NAME,
                             url=sources.JNJ + f'/en/jobs/r-{code}/legal?token=not-in-feed',
                             source_posted_at='2026-11-10', check_status='在招待确认'),
                        now.isoformat(timespec='seconds')) for code in codes]
        for job in jobs:
            job['qualifications']['source_details']['internal_blob'] = 'private-note fixture'
        report = dict(mode='scheduled', collected_at=now.isoformat(), warnings=warnings or [], extracted=len(jobs), rows=jobs,
                      sources=[dict(source=source, returned=len(jobs) if source == sources.JNJ_NAME else 0,
                                    accepted=len(jobs) if source == sources.JNJ_NAME else 0) for source in daily.SCHEDULED_SOURCES])
        path = directory / 'collection.json'
        path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        return path

    def test_daily_feed_sanitizes_deduplicates_and_preserves_earlier_discovery(self):
        now = datetime(2026, 11, 15, 9, tzinfo=sources.CST)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = daily.generate(self.collection(root, now), None, now)
            self.assertEqual(len(first['jobs']), 1)
            self.assertNotIn('not-in-feed', str(first))
            self.assertNotIn('private-note', str(first))
            self.assertEqual(first['jobs'][0]['qualifications']['source_details']['source_posted_at'], '2026-11-10')
            self.assertEqual(first['jobs'][0]['first_seen'], '2026-11-15')
            next_day = now + timedelta(days=1)
            second = daily.generate(self.collection(root, next_day, (2,)), first, next_day)
            self.assertEqual(second['jobs'][0]['first_seen'], '2026-11-16')
            third_day = next_day + timedelta(days=1)
            third = daily.generate(self.collection(root, third_day, (1,)), second, third_day)
            self.assertEqual(third['jobs'][0]['first_seen'], '2026-11-15')
            with self.assertRaises(ValueError):
                daily.generate(self.collection(root, third_day, (3,)), third, third_day)

    def test_daily_generation_stops_without_replacing_existing_data_and_manual_reuses_it(self):
        now = datetime.now(sources.CST)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'daily-jobs.json'
            existing = daily.generate(self.collection(root, now - timedelta(days=1)), None, now - timedelta(days=1))
            encoded = (json.dumps(existing, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
            path.write_bytes(encoded)
            for issue in ('empty', 'source', 'malformed'):
                warnings = [dict(source=sources.JNJ_NAME, error='HTTP 503', blocked=False)] if issue == 'source' else []
                report = self.collection(root, now, () if issue == 'empty' else (1,), warnings)
                if issue == 'malformed':
                    report.write_text('{', encoding='utf-8')
                args = ['daily_candidates.py','--output',str(path),'--previous',str(path),'--report',str(report)]
                with self.subTest(issue=issue), patch.object(sys,'argv',args), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        daily.main()
                    self.assertEqual(stopped.exception.code, 2)
                    self.assertEqual(path.read_bytes(), encoded)
            target = root / 'manual.json'
            with patch.object(sys,'argv',['daily_candidates.py','--output',str(target),'--previous',str(path)]), contextlib.redirect_stdout(io.StringIO()):
                daily.main()
            self.assertEqual(target.read_bytes(), encoded)

    def test_pages_only_include_the_audited_feed_and_keep_csp_same_origin(self):
        now = datetime(2026, 11, 15, 9, tzinfo=sources.CST)
        config = pages.public_config('https://test-project.supabase.co', 'sb_publishable_fixture')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / 'sdk.js'
            sdk.write_text('/* SDK fixture */', encoding='utf-8')
            assets = dict(pages.ASSET_FILES, **{'sdk.js': (sdk, 'text/javascript')})
            feed = daily.generate(self.collection(root, now), None, now)
            path = root / 'feed.json'
            path.write_text(json.dumps(feed, ensure_ascii=False), encoding='utf-8')
            out = root / 'site'
            with patch.object(pages, 'ASSET_FILES', assets):
                pages.build(out, config, manual_applications=True, daily_feed=path)
                self.assertEqual({file.name for file in out.iterdir()}, pages.BUILD_NAMES | {daily.FEED_NAME})
                self.assertIn("connect-src 'self' https://test-project.supabase.co", (out/'index.html').read_text(encoding='utf-8'))
                self.assertIn('每日新发现', (out/'index.html').read_text(encoding='utf-8'))
                pages.build(out, config, manual_applications=True)
                self.assertEqual(daily.read_feed((out/daily.FEED_NAME).read_bytes()), feed)
                feed['jobs'][0]['owner_id'] = 'unexpected-private-field'
                path.write_text(json.dumps(feed, ensure_ascii=False), encoding='utf-8')
                with self.assertRaises(ValueError):
                    pages.build(out, config, daily_feed=path)
                self.assertNotIn('owner_id', (out/daily.FEED_NAME).read_text(encoding='utf-8'))

    def test_pages_workflow_uses_only_successful_scheduled_artifacts_and_retains_manual_build(self):
        workflow = (ROOT / '.github/workflows/pages.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', workflow)
        self.assertIn('workflow_run:', workflow)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", workflow)
        self.assertIn("github.event.workflow_run.event == 'schedule'", workflow)
        self.assertIn('github.event.workflow_run.head_repository.full_name == github.repository', workflow)
        self.assertIn('github.event.workflow_run.head_branch == github.event.repository.default_branch', workflow)
        self.assertIn('name: production-scheduled-reference', workflow)
        self.assertIn('--previous-url', workflow)
        self.assertNotIn('JOBRADAR_PROD_JOBS_DSN', workflow)

    def test_build_uses_only_allowlisted_assets_and_public_config(self):
        with tempfile.TemporaryDirectory(prefix='jobradar-build-') as directory:
            root = Path(directory)
            sdk = root / 'sdk.js'
            sdk.write_text('/* SDK fixture */', encoding='utf-8')
            (root / '.env').write_text('private fixture', encoding='utf-8')
            out = root / 'dist'
            assets = dict(pages.ASSET_FILES)
            assets['sdk.js'] = (sdk, 'text/javascript')
            config = pages.public_config('https://test-project.supabase.co/', 'sb_publishable_fixture')
            with patch.object(pages, 'ASSET_FILES', assets):
                pages.build(out, config)
                self.assertEqual({entry.name for entry in out.iterdir()}, pages.BUILD_NAMES)
                self.assertEqual(json.loads((out / 'config.json').read_bytes()), config)
                html = (out / 'index.html').read_text(encoding='utf-8')
                self.assertIn('https://test-project.supabase.co', html)
                self.assertIn('src="bootstrap.js"', html)
                self.assertIn("import('./app.mjs')", (out / 'bootstrap.js').read_text(encoding='utf-8'))
                self.assertIn('src="sdk.js"', html)
                self.assertIn('href="style.css"', html)
                self.assertTrue(all(b'private fixture' not in file.read_bytes() for file in out.iterdir()))
                (out / '.env').write_text('unwanted fixture', encoding='utf-8')
                with self.assertRaises(ValueError):
                    pages.build(out, config)

    def test_rejects_privileged_keys(self):
        for key in ('sb_secret_dummy_test', 'eyJhbGciOiJIUzI1NiJ9.fixture', 'sb_publishable_REPLACE_ME'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                pages.public_config('https://test-project.supabase.co', key)

    def test_staging_preview_requires_distinct_config_and_matching_ref(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def write(file, ref, key):
                (root / file).write_text(f'SUPABASE_URL=https://{ref}.supabase.co\nSUPABASE_PUBLISHABLE_KEY={key}\n')

            write('.env', 'production', 'sb_publishable_prod_fixture')
            with self.assertRaises(ValueError):
                pages.staging_preview_config('staging', root)
            write('.env.staging', 'staging', 'sb_publishable_stage_fixture')
            self.assertEqual(pages.staging_preview_config('staging', root)['url'], 'https://staging.supabase.co')
            with self.assertRaises(ValueError):
                pages.staging_preview_config('wrong', root)
            write('.env.staging', 'staging', 'sb_publishable_prod_fixture')
            with self.assertRaises(ValueError):
                pages.staging_preview_config('staging', root)
            write('.env.staging', 'production', 'sb_publishable_stage_fixture')
            with self.assertRaises(ValueError):
                pages.staging_preview_config('production', root)

    @unittest.skipUnless(shutil.which('git'), 'Git executable required')
    def test_audit_finds_env_even_after_deletion_from_current_branch(self):
        with tempfile.TemporaryDirectory(prefix='jobradar-git-') as directory:
            root = Path(directory)
            def git(*args):
                subprocess.run(['git', '-C', directory, '-c', 'user.name=Fixture',
                                '-c', 'user.email=fixture@example.test', *args], check=True, capture_output=True)

            git('init', '--quiet')
            (root / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
            git('add', '.gitignore')
            git('commit', '-m', 'Ignore fixtures')
            with contextlib.redirect_stdout(io.StringIO()):
                pages.audit_git(root)
            (root / '.env').write_text('synthetic fixture', encoding='utf-8')
            git('add', '-f', '.env')
            git('commit', '-m', 'Synthetic leak')
            git('rm', '.env')
            git('commit', '-m', 'Remove fixture')
            with self.assertRaises(ValueError):
                pages.audit_git(root)
