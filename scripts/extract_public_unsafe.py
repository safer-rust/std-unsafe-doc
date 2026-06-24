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
RUSTDOC_NIGHTLY_BASE = "https://doc.rust-lang.org/nightly"

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


def get_rustc_version():
    """Return rustc version string for the selected toolchain."""
    result = run(["rustc", f"+{TOOLCHAIN}", "--version"])
    return result.stdout.strip()


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
    """Return raw markdown text under the first '# Safety' heading, or ''."""
    if not docs:
        return ""
    pattern = re.compile(
        r"^#+\s+Safety\b.*?$\n(.*?)(?=^#+\s|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(docs)
    if not match:
        return ""
    return match.group(1).strip()


def _inline_formatting(text):
    r"""Convert inline markdown in *text* to HTML.

    Handles `` `code` ``, `` **bold** ``, `` *italic* `` and
    `` [text](url) ``.  Reference-style `` [`Foo`] `` links are styled
    as plain `` <code>Foo</code> ``.  Input must already be
    HTML-escaped for safety (`` < `` → `` &lt; `` etc.).
    """
    text = re.sub(r"\[`([^`]+)`\]", r"<code>\1</code>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown_to_html(text):
    """Convert rustdoc-flavoured markdown Safety section text to HTML.

    Block-level constructs handled:
    * paragraphs (blank-line separated)
    * unordered lists (`` * item `` / `` - item ``) with continuation lines
    * ordered lists (`` 1. item ``) with continuation lines
    * reference definitions: `` [`N`]: url `` lines are silently dropped

    Inline formatting is forwarded to `` _inline_formatting() ``.
    """
    if not text:
        return ""

    lines = text.splitlines()
    out = []
    i = 0

    def _flush(paras):
        if paras:
            escaped = html.escape(" ".join(paras))
            out.append("<p>" + _inline_formatting(escaped) + "</p>")

    def _finish_list(items, list_tag):
        li_html = "".join(
            "<li>"
            + _inline_formatting(html.escape(" ".join(item_lines)))
            + "</li>"
            for item_lines in items
        )
        out.append("<" + list_tag + ">" + li_html + "</" + list_tag + ">")

    while i < len(lines):
        stripped = lines[i].strip()

        if not stripped:
            i += 1
            continue

        if re.match(r"^\[.+\]:\s*\S", stripped):
            i += 1
            continue

        ul_match = re.match(r"^(\s*)[\-\*]\s+(.*)", lines[i])
        if ul_match:
            items = []
            while i < len(lines):
                li_m = re.match(r"^(\s*)[\-\*]\s+(.*)", lines[i])
                if li_m:
                    items.append([li_m.group(2).strip()])
                elif items and lines[i].strip():
                    items[-1].append(lines[i].strip())
                else:
                    break
                i += 1
            _finish_list(items, "ul")
            continue

        ol_match = re.match(r"^(\s*)(\d+)\.\s+(.*)", lines[i])
        if ol_match:
            items = []
            while i < len(lines):
                li_m = re.match(r"^(\s*)\d+\.\s+(.*)", lines[i])
                if li_m:
                    items.append([li_m.group(2).strip()])
                elif items and lines[i].strip():
                    items[-1].append(lines[i].strip())
                else:
                    break
                i += 1
            _finish_list(items, "ol")
            continue

        para_lines = []
        while i < len(lines) and lines[i].strip():
            para_lines.append(lines[i].strip())
            i += 1
        _flush(para_lines)

    return "\n".join(out) if out else ""


def rustdoc_nightly_url(
    crate,
    path_segments,
    kind,
    *,
    path_kind="",
    parent_kind="",
):
    """Return a URL to the Rust nightly documentation page, or ''.

    Generates a URL of the form:
        https://doc.rust-lang.org/nightly/{crate}/{module.../}{prefix}.{name}.html

    Returns '' if path information is insufficient.
    """
    if len(path_segments) < 2:
        return ""

    # Method items are rendered on their parent type page:
    # .../struct.Type.html#method.method_name
    if path_kind == "method" and len(path_segments) >= 3:
        parent_segments = path_segments[:-1]
        parent_name = parent_segments[-1]
        method_name = path_segments[-1]
        module_parts = parent_segments[1:-1]  # strip crate and parent type name
        page_prefix = {
            "struct": "struct",
            "enum": "enum",
            "trait": "trait",
            "primitive": "primitive",
            "union": "union",
            "type": "type",
        }.get(parent_kind, "")
        if not page_prefix:
            return ""
        parts = [
            RUSTDOC_NIGHTLY_BASE,
            crate,
            *module_parts,
            f"{page_prefix}.{parent_name}.html#method.{method_name}",
        ]
        return "/".join(parts)

    module_parts = path_segments[1:-1]  # strip crate prefix
    item_name = path_segments[-1]
    prefix = {"function": "fn", "trait": "trait"}.get(kind, "")
    if not prefix:
        return ""
    parts = [RUSTDOC_NIGHTLY_BASE, crate] + list(module_parts) + [f"{prefix}.{item_name}.html"]
    return "/".join(parts)


def _find_resolved_path(node):
    """Best-effort search for a resolved_path {id,name} inside a type node."""
    if isinstance(node, dict):
        resolved = node.get("resolved_path")
        if isinstance(resolved, dict):
            type_id = resolved.get("id")
            type_name = resolved.get("name") or ""
            if type_id:
                return type_id, type_name
        for value in node.values():
            result = _find_resolved_path(value)
            if result is not None:
                return result
    elif isinstance(node, list):
        for value in node:
            result = _find_resolved_path(value)
            if result is not None:
                return result
    return None


def _method_parent_map(crate, index, paths):
    """Return item_id -> (parent_path_segments, parent_kind) for impl methods."""
    parent_by_item_id = {}

    for impl_item in index.values():
        impl_data = (impl_item.get("inner") or {}).get("impl")
        if not impl_data:
            continue

        impl_items = impl_data.get("items") or []
        if not impl_items:
            continue

        parent_path_segments = []
        parent_kind = ""

        impl_for = impl_data.get("for") or {}

        if isinstance(impl_for, dict) and impl_for.get("primitive"):
            primitive_name = impl_for.get("primitive")
            parent_path_segments = [crate, primitive_name]
            parent_kind = "primitive"
        elif isinstance(impl_for, dict) and impl_for.get("raw_pointer"):
            parent_path_segments = [crate, "pointer"]
            parent_kind = "primitive"
        elif isinstance(impl_for, dict) and impl_for.get("slice"):
            parent_path_segments = [crate, "slice"]
            parent_kind = "primitive"
        elif isinstance(impl_for, dict) and impl_for.get("array"):
            parent_path_segments = [crate, "array"]
            parent_kind = "primitive"
        else:
            resolved = _find_resolved_path(impl_for)
            if resolved is not None:
                parent_type_id, _parent_name = resolved
                parent_path_entry = paths.get(str(parent_type_id)) or {}
                parent_path_segments = parent_path_entry.get("path") or []
                parent_kind = parent_path_entry.get("kind") or ""

        if not parent_path_segments:
            continue

        for method_item_id in impl_items:
            parent_by_item_id[str(method_item_id)] = (parent_path_segments, parent_kind)

    return parent_by_item_id


def _infer_pathless_method_parent(crate, item_name, docs):
    """Infer parent type for pathless method-like items.

    Some rustdoc JSON entries (notably alloc Rc/Arc strong-count APIs) are
    public unsafe functions in ``index`` with no ``paths`` and no impl linkage,
    even though they are documented as associated methods.
    """
    if crate != "alloc":
        return None

    if "Rc::" in docs or "Rc<T>" in docs:
        return ["alloc", "rc", "Rc", item_name], "struct"
    if "Arc::" in docs or "Arc<T>" in docs:
        return ["alloc", "sync", "Arc", item_name], "struct"

    return None


def _normalize_json_id(raw):
    """Normalize rustdoc JSON ``Id`` (serialized as int or str) for map lookups."""
    if raw is None:
        return None
    return str(raw)


def _parent_module_path_by_item(index, paths, root_raw):
    """Map item id -> parent module path segments (from crate root walk).

    Rustdoc often omits ``paths[import_item_id]`` for ``pub use`` rows, but those
    ids still appear under a parent module's ``inner.module.items``. The visible
    path is then ``parent_path + [use.name]`` (e.g. ``core::str`` + ``from_raw_parts``).
    """
    parent = {}
    if root_raw is None:
        return parent
    root = str(root_raw)
    root_path = (paths.get(root) or {}).get("path")
    if not root_path:
        return parent

    def walk(mod_id_str, mod_path):
        item = index.get(mod_id_str)
        if not item:
            return
        mod_inner = (item.get("inner") or {}).get("module")
        if not mod_inner:
            return
        for cid in mod_inner.get("items") or []:
            cid_s = str(cid)
            parent[cid_s] = list(mod_path)
            child = index.get(cid_s)
            if not child:
                continue
            if "module" in (child.get("inner") or {}):
                child_path = (paths.get(cid_s) or {}).get("path")
                if child_path:
                    walk(cid_s, child_path)

    walk(root, root_path)
    return parent


def _reexport_paths_by_target(index, paths, parent_by_item):
    """Map definition item id -> paths where that item appears via ``pub use``.

    Each ``pub use path::item`` becomes its own index entry with ``inner.use``:
    ``use.id`` is the *definition* item's id (same id you get for the ``function``
    / ``trait`` entry). Prefer ``paths[import_item_id]`` when present; otherwise
    derive ``parent_module_path + use.name`` via *parent_by_item*.

    See rustdoc_json_types::Use and ItemSummary path semantics in the compiler
    sources (paths for definitions are not guaranteed to be the public path).
    """
    by_target = {}
    for import_item_id, item in index.items():
        if item.get("visibility") != "public":
            continue
        inner = item.get("inner") or {}
        use_data = inner.get("use")
        if not isinstance(use_data, dict):
            continue
        if use_data.get("is_glob"):
            # Glob imports usually reference a module id, not each re-exported item.
            continue
        target_id = _normalize_json_id(use_data.get("id"))
        if not target_id:
            continue
        path_entry = paths.get(import_item_id) or {}
        segs = list(path_entry.get("path") or [])
        if len(segs) < 2:
            pub_name = use_data.get("name") or ""
            parent_path = parent_by_item.get(import_item_id)
            if parent_path and pub_name:
                segs = list(parent_path) + [pub_name]
        if len(segs) < 2:
            continue
        by_target.setdefault(target_id, []).append(segs)
    return by_target


def _shortest_reexport_path(alternatives):
    """Pick a stable shortest path among re-export aliases (then lexicographic)."""
    return min(alternatives, key=lambda s: (len(s), s))


def _container_parent_map(index, paths):
    """Map child item_id -> (parent_path_segments, parent_kind) for all
    container types (traits, modules, impls).

    Used as fallback when an item has no entry in the JSON ``paths`` map.
    """
    parent_map = {}
    for parent_id, parent_item in index.items():
        p_inner = (parent_item.get("inner") or {}).copy()
        for container_key in ("trait", "module", "impl"):
            container = p_inner.get(container_key)
            if not isinstance(container, dict):
                continue
            child_ids = container.get("items") or []
            if not child_ids:
                continue
            parent_path_entry = paths.get(parent_id) or {}
            parent_segs = list(parent_path_entry.get("path") or [])
            if not parent_segs:
                continue
            parent_kind = parent_path_entry.get("kind") or ""
            if container_key == "trait":
                parent_kind = "trait"
            elif container_key == "impl":
                parent_kind = parent_kind or "impl"
            elif container_key == "module":
                parent_kind = "module"
            for cid in child_ids:
                cid_s = str(cid)
                if cid_s not in parent_map:
                    parent_map[cid_s] = (list(parent_segs), parent_kind)
    return parent_map


def _impl_trait_map(index, paths):
    """Return {impl_method_id_str: (trait_path_tuple, trait_id_str)}
    for impl methods that implement a trait."""
    mapping = {}
    for _impl_id, impl_item in index.items():
        impl_data = (impl_item.get("inner") or {}).get("impl")
        if not impl_data:
            continue
        trait_ref = impl_data.get("trait")
        if not trait_ref:
            continue
        resolved = _find_resolved_path(trait_ref)
        if resolved is None:
            continue
        trait_id, _trait_name = resolved
        trait_id_str = str(trait_id)
        trait_path_entry = paths.get(trait_id_str) or {}
        trait_path = tuple(trait_path_entry.get("path") or [])
        if not trait_path:
            continue
        impl_method_ids = impl_data.get("items") or []
        for im_id in impl_method_ids:
            mapping[str(im_id)] = (trait_path, trait_id_str)
    return mapping


def _is_public_unsafe_fn(item):
    """Return True if *item* is a public unsafe function."""
    visibility = item.get("visibility")
    if visibility not in ("public", "default"):
        return False
    inner = item.get("inner", {})
    fn_data = inner.get("function")
    if fn_data is None:
        return False
    header = fn_data.get("header", {})
    return bool(header.get("is_unsafe"))


def collect_unsafe_items(json_path, *, trait_safety_registry=None):
    """Parse rustdoc JSON and return list of (module_path, full_path, kind, docs).

    If *trait_safety_registry* is provided it is mutated in-place to record
    trait-method safety docs discovered in this crate.  When an impl method
    lacks its own Safety section the registry (built from earlier crates) is
    consulted as a fallback.
    """
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
    method_parents = _method_parent_map(crate, index, paths)
    parent_by_item = _parent_module_path_by_item(index, paths, data.get("root"))
    reexport_by_target = _reexport_paths_by_target(index, paths, parent_by_item)
    container_parents = _container_parent_map(index, paths)
    impl_trait_map = _impl_trait_map(index, paths)

    # Reverse lookup to resolve parent kind for method URLs.
    path_kind_by_segments = {}
    for _item_id, path_info in paths.items():
        segs = path_info.get("path") or []
        if segs:
            path_kind_by_segments[tuple(segs)] = path_info.get("kind") or ""

    items = []

    # ── Pass 1: populate trait safety registry ──────────────────────
    # Collect trait-method safety docs before processing any items, so
    # that same-crate impl methods processed earlier in the index can
    # still find their trait-method safety doc.
    for item_id, item in index.items():
        if not _is_public_unsafe_fn(item):
            continue
        path_entry = paths.get(item_id)
        if path_entry is None:
            continue
        segs = path_entry.get("path") or []
        parent_kind = path_kind_by_segments.get(tuple(segs[:-1]), "")
        if parent_kind != "trait":
            continue
        docs = item.get("docs") or ""
        safety_doc = extract_safety_section(docs)
        if not safety_doc:
            continue
        if trait_safety_registry is not None:
            trait_path_tuple = tuple(segs[:-1])
            method_name = item.get("name") or ""
            if method_name and trait_path_tuple:
                trait_safety_registry.setdefault(trait_path_tuple, {})[method_name] = safety_doc

    # ── Pass 2: collect all unsafe items ────────────────────────────
    for item_id, item in index.items():
        visibility = item.get("visibility")
        if visibility not in ("public", "default"):
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
        path_kind = ""
        parent_kind = ""

        # Prefer impl-derived parent path for methods to avoid flattened
        # crate-level paths like alloc::decrement_strong_count.
        if item_id in method_parents and item.get("name"):
            parent_segments, parent_kind = method_parents[item_id]
            full_path_segments = list(parent_segments) + [item.get("name")]
            path_kind = "method"
        elif path_entry is None:
            name = item.get("name") or ""
            container_info = container_parents.get(item_id)
            if container_info is not None:
                parent_segs, parent_pkind = container_info
                full_path_segments = list(parent_segs) + [name]
                path_kind = "method" if parent_pkind in (
                    "trait", "impl", "struct", "enum", "primitive", "union",
                ) else ""
                parent_kind = parent_pkind
            else:
                inferred = _infer_pathless_method_parent(crate, name, item.get("docs") or "")
                if inferred is not None:
                    full_path_segments, parent_kind = inferred
                    path_kind = "method"
                else:
                    full_path_segments = [crate, name] if name else [crate]
        else:
            full_path_segments = path_entry.get("path") or []
            path_kind = path_entry.get("kind") or ""

        # Fallback: if the path is only [crate, name], try to find the
        # actual container parent (e.g. trait methods).
        if len(full_path_segments) <= 2:
            name = item.get("name") or ""
            container_info = container_parents.get(item_id)
            if container_info is not None:
                parent_segs, parent_pkind = container_info
                full_path_segments = list(parent_segs) + [name]
                path_kind = "method" if parent_pkind in (
                    "trait", "impl", "struct", "enum", "primitive", "union",
                ) else ""
                parent_kind = parent_pkind

        reexport_alts = reexport_by_target.get(item_id)
        if reexport_alts:
            full_path_segments = list(_shortest_reexport_path(reexport_alts))

        if not full_path_segments:
            continue

        full_path = "::".join(full_path_segments)
        module_path = "::".join(full_path_segments[:-1]) if len(full_path_segments) > 1 else crate

        docs = item.get("docs") or ""
        safety_doc = extract_safety_section(docs)

        # If no safety doc and this is an impl method implementing a trait,
        # look up the trait method's safety doc from the registry.
        if not safety_doc and trait_safety_registry is not None:
            method_name = item.get("name") or ""
            if method_name:
                # Try exact trait-path lookup via impl_trait_map.
                impl_info = impl_trait_map.get(item_id)
                if impl_info is not None:
                    trait_path, _trait_id = impl_info
                    trait_methods = trait_safety_registry.get(trait_path)
                    if trait_methods is None:
                        # Path mismatch across crates: try matching by
                        # last two segments (module + trait name).
                        for rp, rm in trait_safety_registry.items():
                            if len(rp) >= 2 and len(trait_path) >= 2 and rp[-2:] == trait_path[-2:]:
                                trait_methods = rm
                                break
                    if trait_methods is not None:
                        safety_doc = trait_methods.get(method_name, "")
                # Broad fallback: search all traits by method name.
                if not safety_doc:
                    for trait_methods in trait_safety_registry.values():
                        sd = trait_methods.get(method_name, "")
                        if sd:
                            safety_doc = sd
                            break

        if path_kind == "method" and len(full_path_segments) >= 3:
            parent_kind = parent_kind or path_kind_by_segments.get(
                tuple(full_path_segments[:-1]), ""
            )

        # ── Refine *kind* for filter display ──────────────────────────
        display_kind = kind
        if kind == "function":
            if path_kind == "method":
                parent_seg_tuple = tuple(full_path_segments[:-1])
                parent_pkind = path_kind_by_segments.get(parent_seg_tuple, "")
                if parent_pkind == "trait":
                    display_kind = "trait_method"
                else:
                    display_kind = "method"
            else:
                parent_seg_tuple = tuple(full_path_segments[:-1])
                parent_pkind = path_kind_by_segments.get(parent_seg_tuple, "")
                if parent_pkind == "trait":
                    display_kind = "trait_method"

        # Drop unresolvable items: bare [crate, name] functions that
        # couldn't be associated with any container parent.  These are
        # typically trait-method definitions whose parent trait is not
        # accessible in the JSON paths map — their proper entries
        # already appear as trait_method items.
        if display_kind == "function" and len(full_path_segments) == 2:
            continue

        url = rustdoc_nightly_url(
            crate,
            full_path_segments,
            kind,
            path_kind=path_kind,
            parent_kind=parent_kind,
        )

        items.append((module_path, full_path, display_kind, url, safety_doc))

    return items


def write_html(all_items, output_path, rustc_version):
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
        f"<title>Public Unsafe APIs \u2014 {TOOLCHAIN} ({html.escape(rustc_version)})</title>",
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
        "/* Filter controls */",
        ".controls { display: grid; grid-template-columns: minmax(0, 320px) minmax(0, 320px); gap: 12px; margin-bottom: 14px; }",
        ".control-box label { display: block; font-weight: 600; margin-bottom: 6px; font-size: 13px; }",
        ".control-box input { width: 100%; box-sizing: border-box; border: 1px solid #d0d7de; border-radius: 8px; padding: 8px 10px; font-size: 14px; }",
        ".types { margin-bottom: 12px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }",
        ".type-list { display: flex; flex-wrap: wrap; gap: 8px 12px; }",
        ".type-item { font-size: 13px; color: #24292f; background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 999px; padding: 5px 10px; cursor: pointer; }",
        ".safety-item { font-size: 13px; color: #24292f; background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 999px; padding: 5px 10px; cursor: pointer; }",
        ".summary { margin: 8px 0 12px; color: #57606a; font-size: 13px; }",
        ".muted { color: #6e7781; }",
        "</style>",
        "</head>",
        "<body>",
        '<div class="page-wrap">',
        f"<h1>Public Unsafe APIs \u2014 {TOOLCHAIN} ({html.escape(rustc_version)})</h1>",
        f"<p>Generated from crates: {crates_html}.</p>",
        "",
        '<div class="types">',
        '  <div class="muted" style="font-weight:600;">Filter</div>',
        '  <div id="typeFilters" class="type-list"></div>',
        '  <label class="safety-item">',
        '    <input type="checkbox" id="safetyFilter" style="margin-right:6px;" />',
        "    Only without Safety Doc",
        "  </label>",
        "</div>",
        '<div id="summary" class="summary"></div>',
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
        "",
        "    // ── Filter ──────────────────────────────────────────────────────────",
        "    var rows = getRows();",
        "    var safetyOnly = false;",
        "    var typeCounts = rows.reduce(function (acc, r) {",
        "      var t = r.dataset.type || 'unknown';",
        "      acc[t] = (acc[t] || 0) + 1; return acc;",
        "    }, {});",
        "    var typeFilters = document.getElementById('typeFilters');",
        "    var selectedTypes = new Set(Object.keys(typeCounts));",
        "    Object.keys(typeCounts).sort().forEach(function (type) {",
        "      var label = document.createElement('label');",
        "      label.className = 'type-item';",
        "      label.innerHTML = '<input type=\"checkbox\" checked data-type=\"' + type + '\" style=\"margin-right:6px;\">' + type + ' (' + typeCounts[type] + ')';",
        "      typeFilters.appendChild(label);",
        "    });",
        "",
        "    function applyFilters() {",
        "      var visible = 0;",
        "      for (var r = 0; r < rows.length; r++) {",
        "        var row = rows[r];",
        "        var type = row.dataset.type || '';",
        "        var typeOk = selectedTypes.has(type);",
        "        var safetyOk = !safetyOnly || row.dataset.safety === '0';",
        "        var show = typeOk && safetyOk;",
        "        row.style.display = show ? '' : 'none';",
        "        if (show) visible += 1;",
        "      }",
        "      document.getElementById('summary').textContent = 'Showing ' + visible + ' / ' + rows.length + ' items';",
        "    }",
        "",
        "    typeFilters.addEventListener('change', function (event) {",
        "      var tgt = event.target;",
        "      if (tgt.tagName !== 'INPUT' || tgt.type !== 'checkbox') return;",
        "      var t = tgt.dataset.type; if (!t) return;",
        "      if (tgt.checked) selectedTypes.add(t); else selectedTypes.delete(t);",
        "      applyFilters();",
        "    });",
        "",
        "    document.getElementById('safetyFilter').addEventListener('change', function () {",
        "      safetyOnly = this.checked;",
        "      applyFilters();",
        "    });",
        "    applyFilters();",
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
        '<tr><th>Index</th><th>Module Path</th><th>API Name</th>'
        '<th>Kind</th><th>Safety Doc</th><th> Mark </th></tr>',
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
        safety_cell = "<br/>".join(markdown_to_html(d) for d in docs)
        has_safety = "1" if any(d for d in docs) else "0"
        data_attrs = (
            f' data-type="{html.escape(kind, quote=True)}"'
            f' data-module="{html.escape(module_path, quote=True)}"'
            f' data-api="{html.escape(api_name, quote=True)}"'
            f' data-safety="{has_safety}"'
        )
        lines.append(
            f'<tr data-id="{html.escape(full_path, quote=True)}"{data_attrs}>'
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
    rustc_version = get_rustc_version()
    print(f"Rustc:     {rustc_version}")
    sysroot = get_sysroot()
    print(f"Sysroot:   {sysroot}")
    lib_dir = library_dir(sysroot)
    print(f"Library:   {lib_dir}")
    print()

    all_items = []
    trait_safety_registry = {}
    for crate in CRATES:
        print(f"[{crate}]")
        json_path = generate_rustdoc_json(crate, lib_dir)
        print(f"  Parsing {json_path}")
        items = collect_unsafe_items(json_path, trait_safety_registry=trait_safety_registry)
        print(f"  Found {len(items)} public unsafe items")
        all_items.extend(items)
        print()

    write_html(all_items, output_path, rustc_version)
    print(f"Wrote {len(all_items)} items to {output_path.resolve()}")


if __name__ == "__main__":
    main()
