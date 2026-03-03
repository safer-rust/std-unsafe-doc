"""Unit tests for scripts/extract_public_unsafe.py."""

import importlib.util
import os
import sys
import tempfile
import unittest

# Load the script as a module without executing __main__.
_SCRIPT = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'extract_public_unsafe.py')
spec = importlib.util.spec_from_file_location('extract_public_unsafe', _SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class TestBuildHtml(unittest.TestCase):
    """Tests for the build_html helper."""

    def test_title_contains_version(self):
        html = mod.build_html('rustc 1.80.0-nightly (abc1234 2024-01-01)')
        self.assertIn(
            '<title>Public Unsafe APIs \u2014 nightly (rustc 1.80.0-nightly (abc1234 2024-01-01))</title>',
            html,
        )

    def test_body_contains_version(self):
        version = 'rustc 1.80.0-nightly (abc1234 2024-01-01)'
        html = mod.build_html(version)
        self.assertIn(version, html)

    def test_h1_present(self):
        html = mod.build_html('nightly')
        self.assertIn('<h1>Public Unsafe APIs</h1>', html)

    def test_fallback_version(self):
        html = mod.build_html('nightly')
        self.assertIn('nightly', html)
        self.assertIn('<title>', html)


class TestGetNightlyVersionFallback(unittest.TestCase):
    """Tests for get_nightly_version failure handling."""

    def test_returns_nightly_on_failure(self):
        """When rustc is not available the function should return 'nightly'."""
        original_path = os.environ.get('PATH', '')
        try:
            # Make rustc unavailable by clearing PATH.
            os.environ['PATH'] = '/nonexistent'
            version = mod.get_nightly_version()
            self.assertEqual(version, 'nightly')
        finally:
            os.environ['PATH'] = original_path


class TestMainVersionOnly(unittest.TestCase):
    """Tests for the --version-only CLI flag."""

    def test_version_only_prints_and_returns(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.main(['--version-only'])

        output = buf.getvalue().strip()
        # Should print something non-empty (either a real version or 'nightly').
        self.assertTrue(len(output) > 0)
        self.assertIn('nightly', output)


class TestMainWritesFile(unittest.TestCase):
    """Tests for main() writing the HTML file."""

    def test_writes_html_to_custom_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = os.path.join(tmpdir, 'out.html')
            mod.main([out])
            self.assertTrue(os.path.exists(out))
            with open(out) as fh:
                content = fh.read()
            self.assertIn('<title>Public Unsafe APIs', content)
            self.assertIn('nightly', content)


if __name__ == '__main__':
    unittest.main()
