#!/usr/bin/env python3
"""Extract all public unsafe APIs from Rust stdlib crates core, alloc, and std.

Usage:
    python3 scripts/extract_public_unsafe_stdlib.py [OUTPUT_FILE]

Prerequisites:
    rustup toolchain install nightly-2025-12-06
    rustup component add rust-src --toolchain nightly-2025-12-06
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TOOLCHAIN = "nightly-2025-12-06"
CRATES = ["core", "alloc", "std"]
DEFAULT_OUTPUT = f"public_unsafe_api_{TOOLCHAIN}.md"


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
    # Replace newlines / pipes to keep Markdown table valid
    text = re.sub(r"\s*\n\s*", " ", text)
    text = text.replace("|", "\\|")
    return text


def html_url(lib_dir, crate, path_segments, kind):
    """Return a file:// URL to the locally generated rustdoc HTML, or ''."""
    if len(path_segments) < 2:
        return ""
    # module segments are everything except the last (item name)
    module_parts = path_segments[1:-1]  # strip crate prefix
    item_name = path_segments[-1]
    prefix = {"function": "fn", "trait": "trait"}.get(kind, "")
    if not prefix:
        return ""
    # HTML is in the workspace-level target/doc directory
    doc_dir = lib_dir / "target" / "doc" / crate
    html_path = doc_dir.joinpath(*module_parts) / f"{prefix}.{item_name}.html"
    return html_path.as_uri()


def collect_unsafe_items(json_path, lib_dir):
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
        url = html_url(lib_dir, crate, full_path_segments, kind)

        items.append((module_path, full_path, url, safety_doc))

    return items


def write_markdown(all_items, output_path):
    """Write the collected items to a Markdown file."""
    lines = [
        f"# Public Unsafe APIs — {TOOLCHAIN}",
        "",
        f"Generated from crates: {', '.join(f'`{c}`' for c in CRATES)}.",
        "",
        "| Module | API | Safety doc |",
        "| ------ | --- | ---------- |",
    ]
    for module_path, full_path, url, safety_doc in sorted(all_items):
        if url:
            api_cell = f"[`{full_path}`]({url})"
        else:
            api_cell = f"`{full_path}`"
        lines.append(f"| `{module_path}` | {api_cell} | {safety_doc} |")

    lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Extract public unsafe APIs from Rust stdlib (core/alloc/std)."
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=DEFAULT_OUTPUT,
        help=f"Output markdown file (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()
    output_path = Path(args.output)

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
        items = collect_unsafe_items(json_path, lib_dir)
        print(f"  Found {len(items)} public unsafe items")
        all_items.extend(items)
        print()

    write_markdown(all_items, output_path)
    print(f"Wrote {len(all_items)} items to {output_path.resolve()}")


if __name__ == "__main__":
    main()
