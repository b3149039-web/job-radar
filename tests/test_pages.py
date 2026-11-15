import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('jobradar_pages', ROOT / 'tools/pages.py')
pages = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pages)


class PagesTest(unittest.TestCase):
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
