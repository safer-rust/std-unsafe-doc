import os
import subprocess

# Get the Rust nightly version
nightly_version = subprocess.check_output(['rustc', '+nightly', '--version']).decode('utf-8').strip()

# HTML content
html_content = f'''<!DOCTYPE html>\n<html>\n<head>\n<title>Documentation - Rust Nightly {nightly_version}</title>\n</head>\n<body>\n<h1>Documentation for Rust Nightly {nightly_version}</h1>\n<p>This documentation is generated using Rust Nightly {nightly_version}.</p>\n</body>\n</html>'''  

# Write to the HTML file
docs_index_path = "docs/index.html"
with open(docs_index_path, 'w') as f:
    f.write(html_content)
