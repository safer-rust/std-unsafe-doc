#!/usr/bin/env python3
"""Extract all public unsafe APIs from Rust stdlib crates core, alloc, and std.

Usage:
    python3 scripts/extract_public_unsafe_stdlib.py [OUTPUT_FILE]

Output location:
    The default output file is written to the repository root directory
    (the parent of the ``scripts/`` folder), not the current working directory.

    If OUTPUT_FILE is provided:
      - A relative path is resolved relative to the repository root.
      - An absolute path is used as-is.

Prerequisites:
    rustup toolchain install nightly
    rustup component add rust-src --toolchain nightly
"""

import argparse
import html
import json
import re
import subprocess
import sys
from pathlib import Path

TOOLCHAIN = "nightly"
CRATES = ["core", "alloc", "std"]
DEFAULT_OUTPUT = "std-unsafe.html"
RUSTDOC_STABLE_BASE = "https://doc.rust-lang.org/stable"

# Repo root is one level above this script (scripts/../)
REPO_ROOT = Path(__file__).resolve().parent.parent


def run(cmd, *, cwd=None, check=True):
    """Run a subprocess command and return its output."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        print(f"ERROR: command {' '.join(cmd)} failed (exit {result.returncode})",
              file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return result


def get_sysroot():
    """Return the sysroot path for the target toolchain."""
    # Verify the toolchain is installed before using it.
    probe = subprocess.run(
        ["rustup", "toolchain", "list"],
        capture_output=True,
        text=True,
    )
    toolchain_names = probe.stdout if probe.returncode == 0 else ""
    # Accept bare "nightly" or any dated nightly when TOOLCHAIN == "nightly"
    if not any(
        (parts := line.split()) and parts[0].startswith(TOOLCHAIN)
        for line in toolchain_names.splitlines()
    ):
        print(
            f"ERROR: Rust toolchain '{TOOLCHAIN}' is not installed.\n"
            f"Run: rustup toolchain install {TOOLCHAIN}",
            file=sys.stderr,
        )
        sys.exit(1)
    result = run(["rustc", f"+{TOOLCHAIN}", "--print", "sysroot"])
    return Path(result.stdout.strip())


def library_dir(sysroot):
    """Return the path to the stdlib library workspace."""
    path = sysroot / "lib" / "rustlib" / "src" / "rust" / "library"
    if not path.is_dir():
        print(
            f"ERROR: rust-src not found at {path}\n"
            f"Run: rustup component add rust-src --toolchain {TOOLCHAIN}",
            file=sys.stderr,
        )
        sys.exit(1)
    return path


def generate_rustdoc_json(crate, lib_dir):
    """Run cargo rustdoc to produce rustdoc JSON for *crate*.

    Returns the path to the generated JSON file.
    """
    crate_dir = lib_dir / crate
    if not crate_dir.is_dir():
        print(f"ERROR: crate directory not found: {crate_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"  Generating rustdoc JSON for {crate}...")
    run(
        [
            "cargo",
            f"+{TOOLCHAIN}",
            "rustdoc",
            "--lib",
            "-Z",
            "unstable-options",
            "--output-format",
            "json",
        ],
        cwd=str(crate_dir),
    )

    # The workspace-level target dir is one level up from crate_dir.
    # cargo uses the workspace root's target/ directory.
    candidates = [
        crate_dir / "target" / "doc" / f"{crate}.json",
        lib_dir / "target" / "doc" / f"{crate}.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    print(
        f"ERROR: rustdoc JSON for {crate} not found. Tried:\n"
        + "\n".join(f"  {p}" for p in candidates),
        file=sys.stderr,
    )
    sys.exit(1)


def extract_safety_section(docs):
    """Return text under the first '# Safety' heading in *docs*, or ''."""
    if not docs:
        return ""
    # Match any heading level: #+ Safety (case-insensitive)
    pattern = re.compile(
        r"^#+\s+Safety\b.*?$\n(.*?)(?=^#+\s|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(docs)
    if not match:
        return ""
    # Collapse whitespace for a compact table cell
    text = match.group(1).strip()
    # Replace newlines with spaces (HTML table cells handle wrapping)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text


def rustdoc_stable_url(crate, path_segments, kind):
    """Return a URL to the Rust stable documentation page, or ''.

    Generates a URL of the form:
        https://doc.rust-lang.org/stable/{crate}/{module.../}{prefix}.{name}.html

    Returns '' if *kind* is unsupported or path information is insufficient.
    """
    if len(path_segments) < 2:
        return ""
    module_parts = path_segments[1:-1]  # strip crate prefix
    item_name = path_segments[-1]
    prefix = {"function": "fn", "trait": "trait"}.get(kind, "")
    if not prefix:
        return ""
    parts = [RUSTDOC_STABLE_BASE, crate] + list(module_parts) + [f"{prefix}.{item_name}.html"]
    return "/".join(parts)


def collect_unsafe_items(json_path):
    """Parse rustdoc JSON and return list of (module_path, full_path, kind, docs)."""
    try:
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read {json_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    for required_key in ("index", "paths", "format_version"):
        if required_key not in data:
            print(
                f"ERROR: expected key '{required_key}' missing in {json_path}. "
                f"Known keys: {list(data.keys())}",
                file=sys.stderr,
            )
            sys.exit(1)

    index = data["index"]
    paths = data["paths"]
    crate = json_path.stem  # filename without .json

    items = []
    for item_id, item in index.items():
        if item.get("visibility") != "public":
            continue

        inner = item.get("inner", {})
        kind = None

        if "function" in inner:
            header = inner["function"].get("header", {})
            if header.get("is_unsafe"):
                kind = "function"
        elif "trait" in inner:
            if inner["trait"].get("is_unsafe"):
                kind = "trait"

        if kind is None:
            continue

        # Resolve full path from the paths map
        path_entry = paths.get(item_id)
        if path_entry is None:
            # Fall back to item name with crate prefix
            name = item.get("name") or ""
            full_path_segments = [crate, name] if name else [crate]
        else:
            full_path_segments = path_entry.get("path") or []

        if not full_path_segments:
            continue

        full_path = "::".join(full_path_segments)
        module_path = "::".join(full_path_segments[:-1]) if len(full_path_segments) > 1 else crate

        docs = item.get("docs") or ""
        safety_doc = extract_safety_section(docs)
        url = rustdoc_stable_url(crate, full_path_segments, kind)

        items.append((module_path, full_path, kind, url, safety_doc))

    return items


def write_html(all_items, output_path):
    """Write the collected items to a static HTML file.

    Rows are deduplicated by (module_path, full_path, kind).  When duplicate
    rows have different Safety docs they are merged with ``<br/>`` as separator.
    The table is responsive (full-width, horizontally scrollable) and all
    column headers support drag-to-resize via inline CSS + JavaScript.
    Safety doc content is HTML-escaped to prevent injection.
    Rows are sorted ascending by module path then API name.
    """
    # Deduplicate: key = (module_path, full_path, kind), value = (url, [safety_docs])
    seen: dict[tuple[str, str, str], tuple[str, list[str]]] = {}
    for module_path, full_path, kind, url, safety_doc in all_items:
        key = (module_path, full_path, kind)
        if key not in seen:
            seen[key] = (url, [safety_doc] if safety_doc else [])
        else:
            existing_url, docs = seen[key]
            # Keep first non-empty URL
            merged_url = existing_url or url
            if safety_doc and safety_doc not in docs:
                docs.append(safety_doc)
            seen[key] = (merged_url, docs)

    # Sort by (module_path, api_name) ascending
    def _sort_key(entry):
        (module_path, full_path, kind), _val = entry
        api_name = full_path.split("::")[-1]
        return (module_path, api_name)

    sorted_items = sorted(seen.items(), key=_sort_key)

    crates_html = ", ".join(f"<code>{c}</code>" for c in CRATES)

    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Public Unsafe APIs \u2014 {TOOLCHAIN}</title>",
        "<style>",
        "* { box-sizing: border-box; }",
        "body { margin: 0; font-family: system-ui, sans-serif; }",
        ".page-wrap { width: 100%; padding: 16px 24px; }",
        ".unsafe-table-wrap { width: 100%; overflow-x: auto; }",
        ".unsafe-table-wrap table { width: 100%; table-layout: fixed;"
        " border-collapse: collapse; min-width: 600px; }",
        ".unsafe-table-wrap th, .unsafe-table-wrap td"
        " { padding: 4px 8px; word-break: break-word; vertical-align: top;"
        " border: 1px solid #ddd; }",
        ".unsafe-table-wrap th { position: relative; white-space: nowrap;"
        " user-select: none; -webkit-user-select: none; }",
        ".col-resize-handle { position: absolute; right: 0; top: 0; bottom: 0;"
        " width: 5px; cursor: col-resize; }",
        ".col-resize-handle:hover { background: rgba(0,0,0,.15); }",
        "/* Checkbox column */",
        ".confirm-cell { text-align: center; }",
        ".confirm-cb { cursor: pointer; width: 16px; height: 16px; }",
        "/* Confirmed row highlight */",
        ".row-confirmed td { background-color: #f0fff4; }",
        "</style>",
        "</head>",
        "<body>",
        '<div class="page-wrap">',
        f"<h1>Public Unsafe APIs \u2014 {TOOLCHAIN}</h1>",
        f"<p>Generated from crates: {crates_html}.</p>",
        "",
        "<script>",
        "(function () {",
        "  // localStorage key includes the page path to avoid cross-page conflicts.",
        "  // Data structure:",
        "  //   STORAGE_CHECKED_KEY -> JSON object { data-id: boolean } (checkbox state)",
        "  var STORAGE_CHECKED_KEY = 'unsafe-doc-checked:' + location.pathname;",
        "  document.addEventListener('DOMContentLoaded', function () {",
        "    var table = document.querySelector('.unsafe-table-wrap table');",
        "    if (!table) return;",
        "    var tbody = table.querySelector('tbody');",
        "    var cols = table.querySelectorAll('col');",
        "    var ths  = table.querySelectorAll('thead th');",
        "",
        "    // ── Column resize ──────────────────────────────────────────────────",
        "    ths.forEach(function (th, i) {",
        "      var handle = document.createElement('div');",
        "      handle.className = 'col-resize-handle';",
        "      th.appendChild(handle);",
        "      var startX = 0, startW = 0;",
        "      handle.addEventListener('mousedown', function (e) {",
        "        startX = e.clientX;",
        "        startW = th.getBoundingClientRect().width;",
        "        document.addEventListener('mousemove', onMove);",
        "        document.addEventListener('mouseup', onUp);",
        "        e.preventDefault();",
        "      });",
        "      function onMove(e) {",
        "        var w = startW + (e.clientX - startX);",
        "        if (w > 40) { cols[i].style.width = w + 'px'; }",
        "      }",
        "      function onUp() {",
        "        document.removeEventListener('mousemove', onMove);",
        "        document.removeEventListener('mouseup', onUp);",
        "      }",
        "    });",
        "",
        "    // ── Helpers ────────────────────────────────────────────────────────",
        "    function getRows() {",
        "      return Array.from(tbody.querySelectorAll('tr'));",
        "    }",
        "    function saveChecked() {",
        "      var state = {};",
        "      getRows().forEach(function (r) {",
        "        var cb = r.querySelector('.confirm-cb');",
        "        if (cb) state[r.dataset.id] = cb.checked;",
        "      });",
        "      try { localStorage.setItem(STORAGE_CHECKED_KEY, JSON.stringify(state)); }",
        "      catch (e) {}",
        "    }",
        "    function loadChecked() {",
        "      try {",
        "        var saved = localStorage.getItem(STORAGE_CHECKED_KEY);",
        "        if (!saved) return;",
        "        var state = JSON.parse(saved);",
        "        getRows().forEach(function (r) {",
        "          var cb = r.querySelector('.confirm-cb');",
        "          if (cb && r.dataset.id in state) {",
        "            cb.checked = state[r.dataset.id];",
        "            r.classList.toggle('row-confirmed', cb.checked);",
        "          }",
        "        });",
        "      } catch (e) {}",
        "    }",
        "",
        "    // ── Checkbox ──────────────────────────────────────────────────────",
        "    getRows().forEach(function (row) {",
        "      var cb = row.querySelector('.confirm-cb');",
        "      if (cb) {",
        "        cb.addEventListener('change', function () {",
        "          row.classList.toggle('row-confirmed', cb.checked);",
        "          saveChecked();",
        "        });",
        "      }",
        "    });",
        "",
        "    // Restore persisted checked state",
        "    loadChecked();",
        "  });",
        "}());",
        "</script>",
        "",
        '<div class="unsafe-table-wrap">',
        '<table>',
        '<colgroup>',
        '<col style="width:4%">',
        '<col style="width:15%">',
        '<col style="width:18%">',
        '<col style="width:7%">',
        '<col style="width:49%">',
        '<col style="width:7%">',
        '</colgroup>',
        '<thead>',
        '<tr><th>序号</th><th>module 路径</th><th>API 名称</th>'
        '<th>属性</th><th>Safety doc</th><th>Confirmed ✓</th></tr>',
        '</thead>',
        '<tbody>',
    ]

    for idx, ((module_path, full_path, kind), (url, docs)) in enumerate(sorted_items, 1):
        api_name = full_path.split("::")[-1]
        module_cell = f"<code>{html.escape(module_path)}</code>"
        if url:
            api_cell = (
                f'<a href="{html.escape(url)}">'
                f'<code>{html.escape(api_name)}</code>'
                f'</a>'
            )
        else:
            api_cell = f"<code>{html.escape(api_name)}</code>"
        kind_cell = html.escape(kind)
        safety_cell = "<br/>".join(html.escape(d) for d in docs)
        lines.append(
            f'<tr data-id="{html.escape(full_path, quote=True)}">'
            f'<td>{idx}</td>'
            f'<td>{module_cell}</td>'
            f'<td>{api_cell}</td>'
            f'<td>{kind_cell}</td>'
            f'<td>{safety_cell}</td>'
            f'<td class="confirm-cell">'
            f'<input type="checkbox" class="confirm-cb" aria-label="Confirmed">'
            f'</td>'
            f'</tr>'
        )

    lines += ["</tbody>", "</table>", "</div>", "</div>", "</body>", "</html>", ""]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Extract public unsafe APIs from Rust stdlib (core/alloc/std)."
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help=(
            f"Output HTML file (default: {DEFAULT_OUTPUT} in the repo root). "
            "A relative path is resolved relative to the repo root; "
            "an absolute path is used as-is."
        ),
    )
    args = parser.parse_args()

    if args.output is None:
        output_path = REPO_ROOT / DEFAULT_OUTPUT
    else:
        p = Path(args.output)
        output_path = p if p.is_absolute() else REPO_ROOT / p

    print(f"Toolchain: {TOOLCHAIN}")
    sysroot = get_sysroot()
    print(f"Sysroot:   {sysroot}")
    lib_dir = library_dir(sysroot)
    print(f"Library:   {lib_dir}")
    print()

    all_items = []
    for crate in CRATES:
        print(f"[{crate}]")
        json_path = generate_rustdoc_json(crate, lib_dir)
        print(f"  Parsing {json_path}")
        items = collect_unsafe_items(json_path)
        print(f"  Found {len(items)} public unsafe items")
        all_items.extend(items)
        print()

    write_html(all_items, output_path)
    print(f"Wrote {len(all_items)} items to {output_path.resolve()}")


if __name__ == "__main__":
    main()
