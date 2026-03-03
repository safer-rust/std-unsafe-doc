import os
import subprocess
import sys


def get_nightly_version():
    """Return the output of ``rustc +nightly --version``, or ``'nightly'`` on failure."""
    try:
        output = subprocess.check_output(
            ['rustc', '+nightly', '--version'],
            stderr=subprocess.DEVNULL,
        )
        return output.decode('utf-8').strip()
    except Exception:
        return 'nightly'


def build_html(version):
    """Return the HTML page content for the given *version* string."""
    return (
        '<!DOCTYPE html>\n'
        '<html>\n'
        '<head>\n'
        f'<title>Public Unsafe APIs \u2014 nightly ({version})</title>\n'
        '</head>\n'
        '<body>\n'
        '<h1>Public Unsafe APIs</h1>\n'
        f'<p>Generated using {version}.</p>\n'
        '</body>\n'
        '</html>'
    )


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    if '--version-only' in argv:
        print(get_nightly_version())
        return

    out_path = argv[0] if argv else 'docs/index.html'
    version = get_nightly_version()
    html = build_html(version)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    with open(out_path, 'w') as f:
        f.write(html)

    print(f'Wrote {out_path} (version: {version})')


if __name__ == '__main__':
    main()
