#!/usr/bin/env python3
"""Extract all public unsafe APIs from the Rust standard library crates.

Usage::

    python3 scripts/extract_public_unsafe.py [output_path]

Writes an HTML table to *output_path* (default: ``std-unsafe.html``).
"""

import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (also imported by tests)
# ─────────────────────────────────────────────────────────────────────────────

def _build_path_to_kind(paths):
    """Return ``{tuple(path_segments): kind}`` from a rustdoc JSON *paths* dict."""
    result = {}
    for _item_id, entry in paths.items():
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        kind = entry.get("kind")
        if path is None or kind is None:
            continue
        result[tuple(path)] = kind
    return result


def rustdoc_stable_url(crate, path_segments, kind, path_to_kind):
    """Build a stable rustdoc URL for the given item.

    Parameters
    ----------
    crate:
        Crate name (e.g. ``"alloc"``).
    path_segments:
        Full path as a list, e.g.
        ``["alloc", "string", "String", "as_mut_vec"]``.
    kind:
        ``"function"``, ``"trait"``, or other rustdoc kind string.
    path_to_kind:
        Mapping from ``tuple(path_segments)`` to rustdoc kind, built by
        :func:`_build_path_to_kind`.  May be ``None``.

    Returns
    -------
    str
        Absolute URL, or ``""`` for unsupported/invalid inputs.
    """
    BASE = "https://doc.rust-lang.org/stable/"

    if len(path_segments) < 2:
        return ""

    if kind == "function":
        # Detect method: second-to-last segment is a type (struct / enum / …)
        if len(path_segments) >= 3:
            type_segs = tuple(path_segments[:-1])
            type_name = path_segments[-2]
            method_name = path_segments[-1]

            # Check path_to_kind first; fall back to UpperCamelCase heuristic
            type_kind = None
            if path_to_kind is not None:
                type_kind = path_to_kind.get(type_segs)
            if type_kind is None and type_name[:1].isupper():
                type_kind = "struct"  # conservative default

            if type_kind in ("struct", "enum", "union", "type"):
                prefix = "/".join(path_segments[:-2])
                return (
                    f"{BASE}{prefix}/{type_kind}.{type_name}.html"
                    f"#method.{method_name}"
                )

        # Free function
        prefix = "/".join(path_segments[:-1])
        name = path_segments[-1]
        return f"{BASE}{prefix}/fn.{name}.html"

    if kind == "trait":
        prefix = "/".join(path_segments[:-1])
        name = path_segments[-1]
        return f"{BASE}{prefix}/trait.{name}.html"

    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Rustdoc JSON traversal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_impl_for_id(for_type):
    """Extract an item-ID string from an impl *for* type, trying common shapes.

    Returns ``None`` when the ID cannot be determined.
    """
    if not isinstance(for_type, dict):
        return None
    # resolved_path shape (most common in recent rustdoc JSON)
    rp = for_type.get("resolved_path")
    if isinstance(rp, dict):
        item_id = rp.get("id")
        if item_id is not None:
            return str(item_id)
    # Older / flat shape: {"id": …, "name": …, …}
    item_id = for_type.get("id")
    if item_id is not None:
        return str(item_id)
    return None


def _is_unsafe_function(item):
    """Return ``True`` if *item* is a public unsafe function item."""
    if item.get("visibility") != "public":
        return False
    inner = item.get("inner")
    if not isinstance(inner, dict):
        return False
    fn_inner = inner.get("function")
    if not isinstance(fn_inner, dict):
        return False
    header = fn_inner.get("header")
    if not isinstance(header, dict):
        return False
    # rustdoc JSON uses "unsafe_" (recent) or "is_unsafe" (older schemas)
    return bool(header.get("unsafe_") or header.get("is_unsafe"))


def _extract_safety_doc(docs):
    """Return text of every ``# Safety`` section in *docs*, joined by ``<br/>``.

    Returns ``""`` when none is found.
    """
    if not docs:
        return ""
    matches = re.findall(
        r"#\s+Safety\s*\n(.*?)(?=\n#\s|\Z)", docs, re.DOTALL | re.IGNORECASE
    )
    return "<br/>".join(m.strip() for m in matches if m.strip())


def _lookup_paths(paths, item_id):
    """Look up a *paths* entry by item-ID, trying both the original key and str()."""
    entry = paths.get(item_id)
    if entry is None:
        entry = paths.get(str(item_id))
    return entry


# ─────────────────────────────────────────────────────────────────────────────
# Main collection logic
# ─────────────────────────────────────────────────────────────────────────────

def collect_unsafe_items(crate_name, index, paths):
    """Collect all public unsafe items from a rustdoc JSON *index*.

    Parameters
    ----------
    crate_name:
        Crate name (``"core"``, ``"alloc"``, or ``"std"``).
    index:
        The ``index`` section of the rustdoc JSON output.
    paths:
        The ``paths`` section of the rustdoc JSON output.

    Returns
    -------
    list[dict]
        Sorted list of records with keys ``kind``, ``module_path``,
        ``full_path``, ``api_display``, ``url``, ``safety_doc``.
    """
    path_to_kind = _build_path_to_kind(paths)
    items = []
    seen = set()  # deduplicate by full_path

    # Gather item IDs that live inside impl blocks so we can skip them in the
    # free-function scan (they are picked up by the impl traversal below).
    impl_item_ids = set()
    for item in index.values():
        inner = item.get("inner")
        if isinstance(inner, dict):
            impl_inner = inner.get("impl")
            if isinstance(impl_inner, dict):
                for mid in impl_inner.get("items", []):
                    impl_item_ids.add(str(mid))

    # ── 1. Unsafe traits ─────────────────────────────────────────────────────
    for item_id, item in index.items():
        if item.get("visibility") != "public":
            continue
        inner = item.get("inner")
        if not isinstance(inner, dict):
            continue
        trait_inner = inner.get("trait")
        if not isinstance(trait_inner, dict):
            continue
        if not trait_inner.get("is_unsafe"):
            continue
        path_entry = _lookup_paths(paths, item_id)
        if not path_entry:
            continue
        full_segs = path_entry.get("path", [])
        if len(full_segs) < 2:
            continue
        full_path = "::".join(full_segs)
        if full_path in seen:
            continue
        seen.add(full_path)
        module_path = "::".join(full_segs[:-1])
        url = rustdoc_stable_url(crate_name, full_segs, "trait", path_to_kind)
        safety = _extract_safety_doc(item.get("docs", ""))
        items.append({
            "kind": "trait",
            "module_path": module_path,
            "full_path": full_path,
            "api_display": full_segs[-1],
            "url": url,
            "safety_doc": safety,
        })

    # ── 2. Free unsafe functions (not inside impl blocks) ────────────────────
    for item_id, item in index.items():
        if str(item_id) in impl_item_ids:
            continue
        if not _is_unsafe_function(item):
            continue
        path_entry = _lookup_paths(paths, item_id)
        if not path_entry:
            continue
        full_segs = path_entry.get("path", [])
        if len(full_segs) < 2:
            continue
        full_path = "::".join(full_segs)
        if full_path in seen:
            continue
        seen.add(full_path)
        module_path = "::".join(full_segs[:-1])
        url = rustdoc_stable_url(crate_name, full_segs, "function", path_to_kind)
        safety = _extract_safety_doc(item.get("docs", ""))
        items.append({
            "kind": "function",
            "module_path": module_path,
            "full_path": full_path,
            "api_display": full_segs[-1],
            "url": url,
            "safety_doc": safety,
        })

    # ── 3. Unsafe methods inside impl blocks ─────────────────────────────────
    for item in index.values():
        inner = item.get("inner")
        if not isinstance(inner, dict):
            continue
        impl_inner = inner.get("impl")
        if not isinstance(impl_inner, dict):
            continue

        # Resolve the type being implemented
        type_item_id = _resolve_impl_for_id(impl_inner.get("for"))
        if type_item_id is None:
            continue
        type_path_entry = _lookup_paths(paths, type_item_id)
        if not type_path_entry:
            continue
        type_segs = type_path_entry.get("path", [])
        if not type_segs:
            continue

        type_name = type_segs[-1]
        module_path = "::".join(type_segs[:-1])

        for method_id in impl_inner.get("items", []):
            method_item = index.get(method_id) or index.get(str(method_id))
            if not method_item:
                continue
            if not _is_unsafe_function(method_item):
                continue
            method_name = method_item.get("name", "")
            if not method_name:
                continue
            full_segs = list(type_segs) + [method_name]
            full_path = "::".join(full_segs)
            if full_path in seen:
                continue
            seen.add(full_path)
            api_display = f"{type_name}::{method_name}"
            url = rustdoc_stable_url(crate_name, full_segs, "function", path_to_kind)
            safety = _extract_safety_doc(method_item.get("docs", ""))
            items.append({
                "kind": "method",
                "module_path": module_path,
                "full_path": full_path,
                "api_display": api_display,
                "url": url,
                "safety_doc": safety,
            })

    items.sort(key=lambda x: (x["module_path"], x["full_path"]))
    return items


# ─────────────────────────────────────────────────────────────────────────────
# HTML generation
# ─────────────────────────────────────────────────────────────────────────────

_HTML_HEAD = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Public Unsafe APIs — nightly</title>
<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; }
.page-wrap { width: 100%; padding: 16px 24px; }
.unsafe-table-wrap { width: 100%; overflow-x: auto; }
.unsafe-table-wrap table { width: 100%; table-layout: fixed; border-collapse: collapse; min-width: 600px; }
.unsafe-table-wrap th, .unsafe-table-wrap td { padding: 4px 8px; word-break: break-word; vertical-align: top; border: 1px solid #ddd; }
.unsafe-table-wrap th { position: relative; white-space: nowrap; user-select: none; -webkit-user-select: none; }
.col-resize-handle { position: absolute; right: 0; top: 0; bottom: 0; width: 5px; cursor: col-resize; }
.col-resize-handle:hover { background: rgba(0,0,0,.15); }
/* Checkbox column */
.confirm-cell { text-align: center; }
.confirm-cb { cursor: pointer; width: 16px; height: 16px; }
/* Confirmed row highlight */
.row-confirmed td { background-color: #f0fff4; }
/* Nightly version info */
.version-info { font-size: 0.875rem; color: #555; }
</style>
</head>
"""

_HTML_SCRIPT = """\
<script>
(function () {
  // localStorage key includes the page path to avoid cross-page conflicts.
  // Data structure:
  //   STORAGE_CHECKED_KEY -> JSON object { data-id: boolean } (checkbox state)
  var STORAGE_CHECKED_KEY = 'unsafe-doc-checked:' + location.pathname;
  document.addEventListener('DOMContentLoaded', function () {
    var table = document.querySelector('.unsafe-table-wrap table');
    if (!table) return;
    var tbody = table.querySelector('tbody');
    var cols = table.querySelectorAll('col');
    var ths  = table.querySelectorAll('thead th');

    // ── Column resize ──────────────────────────────────────────────────
    ths.forEach(function (th, i) {
      var handle = document.createElement('div');
      handle.className = 'col-resize-handle';
      th.appendChild(handle);
      var startX = 0, startW = 0;
      handle.addEventListener('mousedown', function (e) {
        startX = e.clientX;
        startW = th.getBoundingClientRect().width;
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
        e.preventDefault();
      });
      function onMove(e) {
        var w = startW + (e.clientX - startX);
        if (w > 40) { cols[i].style.width = w + 'px'; }
      }
      function onUp() {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
    });

    // ── Helpers ────────────────────────────────────────────────────────
    function getRows() {
      return Array.from(tbody.querySelectorAll('tr'));
    }
    function saveChecked() {
      var state = {};
      getRows().forEach(function (r) {
        var cb = r.querySelector('.confirm-cb');
        if (cb) state[r.dataset.id] = cb.checked;
      });
      try { localStorage.setItem(STORAGE_CHECKED_KEY, JSON.stringify(state)); }
      catch (e) {}
    }
    function loadChecked() {
      try {
        var saved = localStorage.getItem(STORAGE_CHECKED_KEY);
        if (!saved) return;
        var state = JSON.parse(saved);
        getRows().forEach(function (r) {
          var cb = r.querySelector('.confirm-cb');
          if (cb && r.dataset.id in state) {
            cb.checked = state[r.dataset.id];
            r.classList.toggle('row-confirmed', cb.checked);
          }
        });
      } catch (e) {}
    }

    // ── Checkbox ──────────────────────────────────────────────────────
    getRows().forEach(function (row) {
      var cb = row.querySelector('.confirm-cb');
      if (cb) {
        cb.addEventListener('change', function () {
          row.classList.toggle('row-confirmed', cb.checked);
          saveChecked();
        });
      }
    });

    // Restore persisted checked state
    loadChecked();
  });
}());
</script>
"""

_TABLE_HEADER = """\
<div class="unsafe-table-wrap">
<table>
<colgroup>
<col style="width:4%">
<col style="width:15%">
<col style="width:18%">
<col style="width:7%">
<col style="width:49%">
<col style="width:7%">
</colgroup>
<thead>
<tr><th>序号</th><th>module 路径</th><th>API 名称</th><th>属性</th><th>Safety doc</th><th>Confirmed ✓</th></tr>
</thead>
<tbody>
"""


def write_html(out_path, all_items, version_str, crates):
    """Write the full HTML page to *out_path*."""
    lines = [_HTML_HEAD, "<body>\n<div class=\"page-wrap\">\n"]
    lines.append("<h1>Public Unsafe APIs — nightly</h1>\n")
    crate_list = ", ".join(f"<code>{c}</code>" for c in crates)
    lines.append(f"<p>Generated from crates: {crate_list}.</p>\n")
    escaped_ver = html.escape(version_str)
    lines.append(
        f'<p class="version-info"><strong>Nightly version:</strong>'
        f" <code>{escaped_ver}</code></p>\n"
    )
    lines.append("\n")
    lines.append(_HTML_SCRIPT)
    lines.append("\n")
    lines.append(_TABLE_HEADER)

    for row_num, item in enumerate(all_items, 1):
        data_id = html.escape(item["full_path"])
        module_cell = html.escape(item["module_path"])
        api_display = html.escape(item["api_display"])
        url = html.escape(item["url"])
        kind = html.escape(item["kind"])
        # Safety doc: _extract_safety_doc may include "<br/>" sentinels;
        # HTML-escape each plain-text piece individually.
        safety_parts = item["safety_doc"].split("<br/>")
        safety = "<br/>".join(html.escape(part) for part in safety_parts)
        lines.append(
            f'<tr data-id="{data_id}">'
            f"<td>{row_num}</td>"
            f"<td><code>{module_cell}</code></td>"
            f'<td><a href="{url}"><code>{api_display}</code></a></td>'
            f"<td>{kind}</td>"
            f"<td>{safety}</td>"
            f'<td class="confirm-cell">'
            f'<input type="checkbox" class="confirm-cb" aria-label="Confirmed">'
            f"</td>"
            f"</tr>\n"
        )

    lines.append("</tbody>\n</table>\n</div>\n")
    lines.append("</div>\n</body>\n</html>\n")

    Path(out_path).write_text("".join(lines), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Rustdoc JSON generation
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _nightly_version():
    try:
        result = _run(["rustc", "+nightly", "--version", "--verbose"])
        return result.stdout.strip()
    except Exception:
        return ""


def _generate_rustdoc_json(crate_name, tmp_dir):
    """Generate rustdoc JSON for *crate_name* and return the parsed dict."""
    sysroot = _run(["rustc", "+nightly", "--print", "sysroot"]).stdout.strip()
    lib_src = Path(sysroot) / "lib" / "rustlib" / "src" / "rust" / "library"
    src_lib = lib_src / crate_name / "src" / "lib.rs"
    if not src_lib.exists():
        src_lib = lib_src / crate_name / "lib.rs"
    if not src_lib.exists():
        raise FileNotFoundError(
            f"Could not find source for crate {crate_name!r} under {lib_src}"
        )

    out_dir = Path(tmp_dir) / crate_name
    out_dir.mkdir(parents=True, exist_ok=True)

    _run([
        "rustdoc", "+nightly",
        str(src_lib),
        "--edition", "2021",
        "-Z", "unstable-options",
        "--output-format", "json",
        "--output", str(out_dir),
        "--crate-name", crate_name,
        "--crate-type", "lib",
    ])

    candidates = list(out_dir.glob("*.json"))
    if not candidates:
        raise FileNotFoundError(f"No JSON output found for crate {crate_name!r}")
    with open(candidates[0], encoding="utf-8") as fh:
        return json.load(fh)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "std-unsafe.html"
    crates = ["core", "alloc", "std"]

    version_str = _nightly_version()
    all_items = []

    with tempfile.TemporaryDirectory() as tmp:
        for crate_name in crates:
            print(f"Processing {crate_name}...", flush=True)
            try:
                doc = _generate_rustdoc_json(crate_name, tmp)
            except Exception as exc:
                print(f"  Warning: {exc}", file=sys.stderr)
                continue
            index = doc.get("index", {})
            paths = doc.get("paths", {})
            try:
                items = collect_unsafe_items(crate_name, index, paths)
            except Exception as exc:
                print(
                    f"  Warning: failed to collect unsafe items for {crate_name!r}: {exc}",
                    file=sys.stderr,
                )
                continue
            all_items.extend(items)
            print(f"  {len(items)} unsafe items found")

    if not all_items:
        print(
            "Error: no unsafe items found across all crates. "
            "Check that the nightly toolchain and rust-src component are installed "
            "(rustup toolchain install nightly && rustup component add rust-src --toolchain nightly), "
            "and review the warnings printed above.",
            file=sys.stderr,
        )
        sys.exit(1)

    write_html(out_path, all_items, version_str, crates)
    print(f"\nWrote {len(all_items)} items to {out_path}")


if __name__ == "__main__":
    main()
