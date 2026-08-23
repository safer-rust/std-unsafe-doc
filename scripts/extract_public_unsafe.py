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
import urllib.request
from pathlib import Path

TOOLCHAIN = "nightly"
CRATES = ["core", "alloc", "std"]
DEFAULT_OUTPUT = "std-unsafe.html"
RUSTDOC_NIGHTLY_BASE = "https://doc.rust-lang.org/nightly"
CONTRACTS_URL = "https://raw.githubusercontent.com/safer-rust/RAPx/main/rapx/src/verify/contract/assets/std-public-contracts.json"
CONTRACTS_CACHE_PATH = Path(__file__).resolve().parent / "std-public-contracts.cache.json"

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
    if parent_kind in ("struct", "enum", "trait", "primitive", "union", "type") and len(path_segments) >= 3:
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


def _inner_dict(item):
    """Return item['inner'] as a dict, or {} if missing or a bare variant name.

    rustdoc JSON format v61+ serializes payload-less variants (e.g. an
    ``extern_type``) as a bare string instead of ``{"extern_type": {}}``.
    """
    inner = item.get("inner")
    return inner if isinstance(inner, dict) else {}


def _method_parent_map(crate, index, paths):
    """Return item_id -> (parent_path_segments, parent_kind) for impl methods."""
    parent_by_item_id = {}

    for impl_item in index.values():
        impl_data = _inner_dict(impl_item).get("impl")
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
        mod_inner = _inner_dict(item).get("module")
        if not mod_inner:
            return
        for cid in mod_inner.get("items") or []:
            cid_s = str(cid)
            parent[cid_s] = list(mod_path)
            child = index.get(cid_s)
            if not child:
                continue
            if "module" in _inner_dict(child):
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
        inner = _inner_dict(item)
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


def _module_rename_map(index, paths, parent_by_item):
    """Return {internal_module_path_tuple: public_module_path_tuple} for
    module-level ``pub use`` renames including glob uses:
    ``pub use core_arch as arch`` or ``pub use core_arch::*``.

    When a module is publicly re-exported under a different name, items
    inside it keep their definition paths (e.g. ``core::core_arch::foo``).
    This map lets us rewrite the prefix to the public name (``core::arch::foo``).
    """
    renames = {}
    for import_item_id, item in index.items():
        if item.get("visibility") != "public":
            continue
        inner = _inner_dict(item)
        use_data = inner.get("use")
        if not isinstance(use_data, dict):
            continue

        target_id = _normalize_json_id(use_data.get("id"))
        if not target_id:
            continue

        target_item = index.get(target_id)
        if target_item is None:
            continue
        if "module" not in _inner_dict(target_item):
            continue

        target_entry = paths.get(target_id) or {}
        target_path = tuple(target_entry.get("path") or [])
        if not target_path:
            continue

        if use_data.get("is_glob"):
            import_path = tuple(parent_by_item.get(import_item_id) or [])
        else:
            import_entry = paths.get(import_item_id) or {}
            import_path = tuple(import_entry.get("path") or [])
            if not import_path:
                parent_path = parent_by_item.get(import_item_id)
                pub_name = use_data.get("name") or ""
                if parent_path and pub_name:
                    import_path = tuple(parent_path + [pub_name])

        if import_path and import_path != target_path:
            renames[target_path] = import_path

    # When core::core_arch::arch → core::arch, the entire core_arch
    # module is publicly core::arch.  Items directly under
    # core::core_arch::x86::* should use core::arch::x86::*.
    if ("core", "core_arch", "arch") in renames:
        renames[("core", "core_arch")] = ("core", "arch")

    return renames


def _apply_module_renames(path_str, module_renames):
    """Apply *module_renames* to a ``::``-separated path string."""
    if not module_renames or not path_str:
        return path_str
    segs = path_str.split("::")
    sorted_renames = sorted(module_renames.items(), key=lambda x: -len(x[0]))
    changed = True
    while changed:
        changed = False
        for internal, public in sorted_renames:
            ilist = list(internal)
            if segs[:len(ilist)] == ilist:
                segs = list(public) + segs[len(ilist):]
                changed = True
                break
    return "::".join(segs)


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
        p_inner = _inner_dict(parent_item).copy()
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
        impl_data = _inner_dict(impl_item).get("impl")
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
    inner = _inner_dict(item)
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
    module_renames = _module_rename_map(index, paths, parent_by_item)

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
        # Resolve parent path: prefer paths map, fall back to container_parents.
        path_entry = paths.get(item_id)
        segs = path_entry.get("path") or [] if path_entry is not None else []
        parent_kind = ""
        container_info = container_parents.get(item_id)
        if len(segs) <= 2 and container_info is not None:
            parent_segs, parent_pkind = container_info
            if parent_pkind == "trait":
                segs = list(parent_segs) + [item.get("name", "")]
                parent_kind = "trait"
        if len(segs) <= 2:
            continue
        if not parent_kind:
            parent_kind = path_kind_by_segments.get(tuple(segs[:-1]), "")
        if parent_kind != "trait":
            continue
        docs = item.get("docs") or ""
        safety_doc = extract_safety_section(docs)
        if trait_safety_registry is not None:
            trait_path_tuple = tuple(segs[:-1])
            method_name = item.get("name") or ""
            if method_name and trait_path_tuple:
                trait_safety_registry.setdefault(trait_path_tuple, {})[method_name] = safety_doc or ""

    # ── Pass 2: collect all unsafe items ────────────────────────────
    for item_id, item in index.items():
        visibility = item.get("visibility")
        if visibility not in ("public", "default"):
            continue

        inner = _inner_dict(item)
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

        # Build display path with public module names (e.g. core_arch -> arch)
        display_path_segments = list(full_path_segments)
        if module_renames:
            sorted_renames = sorted(module_renames.items(), key=lambda x: -len(x[0]))
            changed = True
            while changed:
                changed = False
                for internal, public in sorted_renames:
                    ilist = list(internal)
                    if display_path_segments[:len(ilist)] == ilist:
                        display_path_segments = list(public) + display_path_segments[len(ilist):]
                        changed = True
                        break

        full_path = "::".join(display_path_segments)
        module_path = "::".join(display_path_segments[:-1]) if len(display_path_segments) > 1 else crate

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

        if len(full_path_segments) >= 3:
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

        trait_origin = full_path
        if display_kind == "trait_method":
            trait_origin = full_path
        elif impl_info := impl_trait_map.get(item_id):
            trait_path, _ = impl_info
            method_name = item.get("name") or ""
            if trait_path and method_name:
                trait_origin = _apply_module_renames(
                    "::".join(trait_path) + "::" + method_name,
                    module_renames,
                )
        elif display_kind == "method" and trait_safety_registry is not None:
            method_name = item.get("name") or ""
            if method_name:
                matches = []
                for tpath, tmethods in trait_safety_registry.items():
                    if method_name in tmethods:
                        matches.append(tpath)
                if len(matches) == 1:
                    trait_origin = _apply_module_renames(
                        "::".join(matches[0]) + "::" + method_name,
                        module_renames,
                    )

        items.append((module_path, full_path, display_kind, url, safety_doc, trait_origin))

    return items


RUST_SAFETY_TAGS = [
    ("NonNull",            r"non[-\s]?null|not\s+null|must\s+not\s+be\s+null|must\s+not\s+be\s+0\b|must\s+be\s+non[-\s]?null"),
    ("Align",              r"must\s+be\s+(?:page\s+)?aligned|aligned\s+pointer|page\s+aligned|alignment\s+requirements|properly\s+aligned|well\s+aligned|must\s+satisfy.*alignment|alignment\s+of.*\bT\b|aligned\s+to"),
    ("Allocated",          r"allocated\s+by|allocated\s+with|have\s+been\s+allocated|memory\s+allocation|been\s+created\s+by\s+.*allocator|allocated\s+object|valid\s+allocation|currently\s+allocated|must\s+denote\s+a\s+block\s+of\s+memory|must\s+be\s+an\s+allocation|was\s+allocated"),
    ("InBound",            r"in\s+bounds|within\s+the\s+bounds|within.*\bthe\s+bound(?!s\s+of)|single\s+allocated\s+object|without\s+(?:bounds\s+)?check(?:ing)?|does\s+not\s+exceed|must\s+not\s+exceed|must\s+fall\s+within|must\s+be\s+within\b|less\s+than\s+or\s+equal\s+to"),
    ("NonOverlap",         r"not\s+overlap|must\s+not\s+overlap|must\s+be\s+disjoint|no\s+overlap|do\s+not\s+overlap"),
    ("ValidPtr",           r"valid\s+pointer|dereferenceable|must\s+be\s+dereferenceable"),
    ("ValidRead",          r"valid\s+for\s+read(?:ing|s?)|must\s+be\s+readable|safe\s+to\s+read|valid\s+and\s+remains?\s+valid\s+for\s+reading"),
    ("ValidWrite",         r"valid\s+for\s+writ(?:ing|es?)|must\s+be\s+writable|safe\s+to\s+write|valid\s+and\s+remains?\s+valid\s+for\s+writing"),
    ("Valid",              r"must\s+be\s+(?:a\s+)?valid\s+(?:(?:for|pointer|memory|instance|I/O|allocation|CPU|ID|entry|struct|VMA|file|struct\s+of\s+type|page\s+table|region|mapping|device|address|base\s+address|physical\s+address|VM\s+area|file\s+descriptor|handle|resource|context|one|mapping|CPU\s+ID|page|frame|segment|pte|entry))|valid\s+pointer|must\s+point\s+to\s+a\s+valid|must\s+point\s+at\s+a\s+valid|valid\s+entry|must\s+remain\s+valid|must\s+be\s+correct|must\s+be\s+configured\s+correctly|must\s+be\s+properly\s+configured|correctly\s+represent|maps\s+to\s+a\s+valid|dma\s+direction\s+correspond\s+correctly|violate\s+memory\s+safety"),
    ("ValidMemory",        r"valid\s+(?:I/O|MMIO|memory\s+region|memory\s+mapped|physical\s+address|base\s+address|io\s*mapped|memory\s+range|region\s+of\s+memory|I/O\s+APIC|IOMMU)"),
    ("ValidNum",           r"valid\s+range|must\s+be\s+less\s+than\s+or\s+equal|must\s+be\s+greater\s+than|does\s+not\s+overflow|must\s+not\s+overflow|within\s+(?:the\s+)?range|must\s+be\s+within\b.*(?:MAX|MIN|max|min|isize|usize)|must\s+not\s+be\s+greater\s+than|must\s+be\s+a\s+power\s+of\s+2|must\s+be\s+less\s+than\s+.*cardinality|must\s+not\s+exceed\s+.*\bMAX|be\s+less\s+than.*\bSelf"),
    ("ValidString",        r"valid\s+(?:utf[-\s]?8|utf8)|utf[-\s]?8\s+encoded|must\s+be\s+(?:utf|valid\s+unicode)"),
    ("ValidCStr",           r"null\s+(?:terminator|byte)|nul[-\s]?terminated|must\s+be\s+null[-\s]?terminated|with_nul|\\0"),
    ("Init",               r"must\s+be\s+initialized|fully\s+initialized|initialized\s+state|in\s+an\s+initialized|properly\s+initialized|must\s+be\s+in\s+an?\s+init|already\s+initialized|been\s+initialized|remain\s+initialized"),
    ("Typed",              r"type\s+of\s+the\s+.*\s+must\s+be|correct\s+type|type\s+\bT\b\s+must\b|must\s+match\s+the\s+type|must\s+be\s+of\s+type|must\s+be\s+layout[-\s]?compatible|must\s+be\s+the\s+same\s+type|\brepr\s*\(\s*C\s*\)|transmute\s+to|untyped|must\s+point\s+to\s+a\s+virtual\s+memory\s+region\s+of\s+type"),
    ("Unwrap",             r"must\s+be\s+(?:Some|Ok|Err)|unwrap"),
    ("NoPadding",          r"no\s+padding|padding.*must\s+be\s+0|must\s+not\s+have\s+padding|zero\s+padding"),
    ("Owning",             r"sole\s+ownership|exclusive\s+ownership|unique\s+ownership|ownership.*of.*pointer|no\s+other\s+.*\s+owns?\b|must\s+not\s+have\s+other\s+.*\s+own|single\s+owner|only\s+owner|must\s+own|exclusive\s+(?:access|control)|must\s+have\s+ownership\s+of"),
    ("Alias",              r"must\s+not\s+alias|no\s+alias|must\s+not\s+have\s+.*\s+alias|must\s+not\s+overlap\s+in\s+.*\s+alias|\balias\b|aliasing"),
    ("Alive",              r"remains?\s+valid|remain\s+valid|must\s+not\s+be\s+freed|must\s+not\s+be\s+dropped|valid\s+for\s+the\s+duration|valid\s+for\s+the\s+lifetime|must\s+outlive|not\s+been\s+deallocated|not\s+be\s+deallocated|be\s+alive|must\s+not\s+be\s+deallocated|must\s+not\s+outlive|drop\s+before|must\s+outlast|outlives?\s+the\s+(?:created|lifetime|reference)"),
    ("Pinned",             r"remain\s+pinned|must\s+remain\s+at\s+the\s+same|must\s+not\s+move|not\s+moved|must\s+not\s+be\s+moved|must\s+stay\s+at\s+the\s+same|pinned\s+at|remain\s+at\s+the\s+same\s+address"),
    ("NonMutRef",          r"no\s+mutable\s+reference|must\s+not\s+create\s+(?:a\s+)?mutable\s+reference|must\s+not\s+have\s+(?:a\s+)?mutable\s+(?:reference|alias)|exclusive\s+mutable|must\s+not\s+be\s+referenced\s+by\s+a\s+living"),
    ("NonData_race",       r"data\s+race|must\s+not\s+cause\s+a\s+data\s+race|no\s+data\s+race|must\s+not\s+introduce\s+(?:a\s+)?data\s+race"),
    ("NonConcurrent",      r"concurrent\s+access|concurrently\s+access|must\s+not\s+be\s+(?:accessed|modified)\s+concurrently|no\s+concurrent|must\s+not\s+race|must\s+not\s+cause.*race|race\s+condition|must\s+not\s+be\s+used\s+concurrently|must\s+ensure\s+that\s+this\s+call\s+does\s+not\s+race|must\s+not\s+be\s+accessed\s+(?:by|from)\s+other|not\s+accessed\s+concurrently|no\s+other\s+.*\s+can\s+access|not\s+\[?Sync\]?"),
    ("NonMutate",          r"must\s+not\s+be\s+modified|must\s+not\s+be\s+mutated|not\s+be\s+mutated|must\s+not\s+mutate|no\s+other.*\bwrites?\b|must\s+not\s+be\s+changed|must\s+not\s+be\s+altered|must\s+be\s+read[-\s]?only|must\s+remain\s+unchanged|immutable\s+after"),
    ("LockHold",           r"(?:lock|mutex)\s+(?:is|must\s+be|should\s+be|being)\s+(?:held|acquired|locked)|holding\s+the\s+(?:lock|mutex)|must\s+hold\s+the\s+lock|lock\s+must\s+be|caller\s+must\s+hold|locked\s+before"),
    ("NonVolatile",        r"volatile|read_volatile|write_volatile|must\s+not\s+be\s+volatile|non[-\s]?tearing|volatile\s+memory|single\s+memory\s+(?:load|store)\s+instruction"),
    ("ContainerOf",        r"container\s*_?\s*of|container\s+of|embedded\s+in.*at\s+(?:byte\s+)?offset|is\s+embedded\s+in|must\s+embed\b|must\s+point\s+to\s+the\s+.*\s+that\s+is\s+embedded|at\s+byte\s+offset|at\s+offset"),
    ("Invariant",          r"type\s+invariant|must\s+remain\s+a\s+(?:max[-\s]?heap|valid\s+utf|heap)|uphold\s+(?:the\s+)?invariant|uphold\s+.*\bguarantee"),
    ("RefTransfer",        r"reference\s+count|refcount|ref\s+count|non[-\s]?zero\s+reference\s+count|increment\s+(?:the\s+)?ref|bump\s+(?:the\s+)?ref|holds?\s+a\s+reference|transfer\s+(?:of\s+)?ownership\s+of\s+the\s+ref|own\s+a\s+ref"),
    ("NonZero",            r"non[-\s]?zero|not\s+zero|not\s+be\s+zero|greater\s+than\s+zero|must\s+not\s+be\s+empty"),
    ("NonDropped",         r"not\s+(?:be\s+)?dropped|must\s+not\s+drop|while.*is\s+not\s+dropped|must\s+be\s+dropped\s+before|must\s+not\s+be\s+freed|not\s+been\s+freed|must\s+not\s+destroy|must\s+not\s+forget"),
    ("NonAccessable",      r"never\s+again\s+be\s+(?:read|accessed|written)|must\s+not\s+be\s+(?:accessed|used|read|written)\s+after|no\s+longer\s+be\s+used|must\s+not\s+be\s+read\s+from\s+or\s+written\s+to|must\s+not\s+be\s+accessed"),
    ("NonInstance",        r"must\s+not\s+exist\b.*\binstance|must\s+not\s+be\s+any\s+instance|no.*instances?\s+(?:may|can|should)\s+exist|must\s+not\s+have\s+(?:an\s+)?instance|single\s+instance|only\s+one\s+instance|must\s+not\s+be\s+used\s+to\s+create.*\s+twice|must\s+not\s+be\s+referenced\s+by\s+a\s+living|must\s+not\s+be\s+created\s+again|have\s+been\s+removed"),
    ("Assoc",              r"associated\s+with\b|must\s+be\s+associated\s+with|correspond\s+to\b.*\bof\s+type|must\s+match\s+the\s+type\s+of"),
    ("FlagSet",            r"must\s+already\s+(?:have\s+been|be)\s+set|flag.*must\s+be|bit.*must\s+be\s+set|must\s+have\s+been\s+set|feature\s+flag|must\s+be\s+enabled|must\s+be\s+present|is\s+present|is\s+true\)|ensure\s+that\s+.*\s+is\s+present|can_sync_dma\b|is\s+enabled"),
    ("CanFail",            r"may\s+(?:fail|return|not\s+succeed)|can\s+fail|fallible|must\s+handle\s+(?:the\s+)?failure|error\s+must\s+be\s+handled|must\s+check\s+the\s+result"),
    ("MayInvalid",         r"may\s+become\s+invalid|might\s+become\s+invalid|could\s+become\s+invalid|may\s+be\s+invalidated|may\s+not\s+be\s+valid\s+after|may\s+cause\s+.*undefined|may\s+lead\s+to\s+a\s+kernel|may\s+lead\s+to\s+memory"),
    ("CallOnce",           r"only\s+once|called\s+once|called\s+at\s+most\s+once|must\s+be\s+called\s+once|boot\s+context|in\s+the\s+boot\s+context|this\s+function\s+must\s+be\s+called\s+once|must\s+be\s+called\s+exactly\s+once|must\s+be\s+called\s+on\s+an?\s+.*\s+that\s+hasn|called\s+on\s+the\s+bootstrapping|must\s+not\s+have\s+been\s+called|hasn'?t\s+called\s+this|must\s+be\s+called\s+once.*\s+for\s+each"),
    ("PostToFunc",         r"after\b.*\bhas\s+been\s+called|after\b.*\bbefore\b|called\s+before\b.*\bcalled\b|called\s+after\b.*\bis\s+called|must\s+not\s+be\s+used\s+before|called\s+on\s+the\s+bsp|called\s+on\s+the\s+ap|must\s+have\s+been\s+invoked|preceding\s+call\s+to|must\s+be\s+called\s+after|must\s+first\s+call|subsequent\s+to|prior\s+call\s+to|must\s+precede\b|can\s+only\s+be\s+called\s+before|must\s+be\s+called\s+before|can\s+only\s+be\s+called\s+after"),
    ("CalledBy",           r"called\s+(?:from|by)\s+(?:the\s+)?(?:kernel|driver|interrupt|irq|softirq|tasklet|workqueue|timer|callback|notifier|probe|remove|suspend|resume|ioctl|syscall|exception|trap|NMI|atomic|process|user|hardware|firmware|bootloader|an?\s+irq|an?\s+interrupt)"),
    ("CurThread",          r"current\s+thread|calling\s+thread|same\s+thread|on\s+the\s+current\s+CPU|only\s+this\s+thread|this\s+cpu|current\s+cpu|current\s+processor|current\s+task\s+is\s+pinned|no\s+preemption\s+can\s+occur|must\s+not\s+be\s+preempted|cpu[-\s]?local|only\s+available\s+on\s+the\s+current|per[-\s]?cpu"),
    ("OriginateFrom",      r"returned\s+by\b.*\bcall\b|must\s+(?:come|be|originate)\s+from\b.*\bcall|must\s+be\s+a\s+preceding\s+call|must\s+be\s+the\s+result\s+of|must\s+have\s+been\s+obtained\s+(?:from|by)|must\s+have\s+been\s+created\s+(?:by|with|using)|previously\s+(?:forgotten|prepared|created|allocated|obtained)|must\s+be.*\s+that\s+was\s+(?:previously|earlier)\s+(?:forgotten|prepared|created)"),    ("NotPostToFunc",      r"should\s+not\s+be\s+called\s+(?:post|after)|must\s+not\s+be\s+called\s+(?:post|after)|cannot\s+be\s+called\s+(?:after|post)"),
    ("NotPriorToFunc",     r"should\s+not\s+be\s+called\s+(?:prior|before)|must\s+not\s+be\s+called\s+(?:prior|before)|cannot\s+be\s+called\s+(?:before|prior)"),
    ("UserSpace",          r"in\s+user\s+space|user\s+space\s+memory|must\s+be\s+in\s+user\s+space|from_user_space|user\s+space"),
    ("XorAccess",          r"mutual\s+exclusive|mutually\s+exclusive|exclusively\s+(?:access|write)|exclusive\s+(?:write|access)\s+to|must\s+be\s+(?:the\s+)?exclusive|not\s+be\s+(?:accessed|written)\s+by\s+others"),
    ("Forgotten",          r"forgotten|mem::forget|ManuallyDrop|into_raw|must\s+be\s+forgotten|previously\s+forgotten|was\s+forgotten|had\s+been\s+forgotten"),


]


def extract_tags(full_doc: str) -> str:
    if not full_doc:
        return ""
    lower = full_doc.lower()
    matched = []
    for tag, pattern in RUST_SAFETY_TAGS:
        if re.search(pattern, lower):
            matched.append(tag)
    return ", ".join(matched)


def _build_module_tree(sorted_items):
    """Build and render the module hierarchy tree as HTML sidebar content."""
    from collections import defaultdict

    module_counts = defaultdict(int)
    for (module_path, full_path, kind), (_url_docs) in sorted_items:
        module_counts[module_path] += 1

    all_path_nodes = set()
    for mp in module_counts:
        parts = mp.split("::")
        for i in range(len(parts)):
            all_path_nodes.add("::".join(parts[:i + 1]))

    root = {}
    for p in sorted(all_path_nodes):
        parts = p.split("::")
        node = root
        for part in parts:
            node = node.setdefault(part, {})

    html_parts = []

    total = sum(module_counts.values())
    html_parts.append(
        '<li><span class="tree-node tree-node-all selected" data-module="">'
        f'Show All <span class="tree-count">({total})</span></span></li>'
    )

    def _subtree_total(children_dict, path_parts):
        total = 0
        for cname, cdict in children_dict.items():
            sub_path = "::".join(path_parts + [cname])
            total += module_counts.get(sub_path, 0)
            if cdict:
                total += _subtree_total(cdict, path_parts + [cname])
        return total

    def render(name, children, path_parts):
        full_path = "::".join(path_parts)
        count = module_counts.get(full_path, 0)

        has_children = bool(children)

        if not has_children:
            if count > 0:
                return (
                    f'<li><span class="tree-node" data-module="{html.escape(full_path)}">'
                    f'{html.escape(name)} <span class="tree-count">({count})</span>'
                    '</span></li>'
                )
            return ""

        subtree_total = count + _subtree_total(children, path_parts)

        if subtree_total == 0:
            return ""

        lines = ['<li>']
        lines.append(
            f'<span class="tree-toggle expanded" data-toggle="{html.escape(full_path)}">'
            '&#9662;</span>'
        )
        lines.append(
            f'<span class="tree-node" data-module="{html.escape(full_path)}">'
            f'{html.escape(name)} <span class="tree-count">({subtree_total})</span>'
            '</span>'
        )
        lines.append('<ul>')
        sorted_children = sorted(
            children.items(), key=lambda x: (not x[1], x[0].lower())
        )
        for cname, cdict in sorted_children:
            child_html = render(cname, cdict, path_parts + [cname])
            if child_html:
                lines.append(child_html)
        lines.append('</ul>')
        lines.append('</li>')
        return "\n".join(lines)

    for crate in CRATES:
        if crate in root:
            html_parts.append(render(crate, root[crate], [crate]))

    return "\n".join(html_parts)


def _build_auto_tags(contracts):
    """Build {api_path: "tag, tag"} lookup from the RAPx contracts JSON."""
    lookup = {}
    for api_path, entries in contracts.items():
        tags = sorted(set(e["tag"] for e in entries if e.get("tag") and e["tag"] != "any"))
        if tags:
            lookup[api_path] = ", ".join(tags)
    return lookup


def _resolve_auto_tags(full_path, kind, lookup):
    """Return the auto-tags for *full_path*, tolerating RAPx path conventions.

    RAPx contracts record inherent methods without their impl type segment
    (e.g. ``core::mem::maybe_uninit::assume_init``) whereas the docs table uses
    the full path (``core::mem::maybe_uninit::MaybeUninit::assume_init``). For
    method rows we fall back to the type-stripped path when the exact path is
    absent from *lookup*.
    """
    if full_path in lookup:
        return lookup[full_path]
    if kind == "method":
        segs = full_path.split("::")
        if len(segs) >= 3:
            candidate = "::".join(segs[:-2] + segs[-1:])
            if candidate in lookup:
                return lookup[candidate]
    return ""


def _load_auto_tags():
    """Load RAPx contract tags, falling back to a cached copy on failure.

    On a successful fetch the raw response is written to
    ``scripts/std-public-contracts.cache.json`` so a future run can still
    populate tags even when the RAPx repo (or network) is unavailable.
    """
    try:
        with urllib.request.urlopen(CONTRACTS_URL, timeout=15) as resp:
            raw = resp.read()
        auto_tags = _build_auto_tags(json.loads(raw))
        print(f"  Loaded {len(auto_tags)} API tags from contracts JSON")
        try:
            existing = (
                CONTRACTS_CACHE_PATH.read_bytes()
                if CONTRACTS_CACHE_PATH.exists()
                else b""
            )
            if existing != raw:
                CONTRACTS_CACHE_PATH.write_bytes(raw)
                print(f"  Cached contracts JSON to {CONTRACTS_CACHE_PATH}")
        except OSError as e:
            print(f"  Warning: could not write contracts cache ({e})")
        return auto_tags
    except Exception as e:
        print(f"  Warning: could not load contracts JSON ({e})")
        if CONTRACTS_CACHE_PATH.exists():
            try:
                cached = json.loads(CONTRACTS_CACHE_PATH.read_text(encoding="utf-8"))
                auto_tags = _build_auto_tags(cached)
                print(
                    f"  Using cached {len(auto_tags)} API tags from "
                    f"{CONTRACTS_CACHE_PATH}"
                )
                return auto_tags
            except (OSError, json.JSONDecodeError) as e2:
                print(f"  Warning: could not load contracts cache ({e2})")
        return {}


def write_html(all_items, output_path, rustc_version):
    """Write the collected items to a static HTML file.

    Rows are deduplicated by (module_path, full_path, kind).  When duplicate
    rows have different Safety docs they are merged with ``<br/>`` as separator.
    The table is responsive (full-width, horizontally scrollable) and all
    column headers support drag-to-resize via inline CSS + JavaScript.
    Safety doc content is HTML-escaped to prevent injection.
    Rows are sorted ascending by module path then API name.
    """
    # Deduplicate: key = (module_path, full_path, kind), value = (url, [safety_docs], trait_origin)
    seen: dict[tuple[str, str, str], tuple[str, list[str], str]] = {}
    for module_path, full_path, kind, url, safety_doc, trait_origin in all_items:
        key = (module_path, full_path, kind)
        if key not in seen:
            seen[key] = (url, [safety_doc] if safety_doc else [], trait_origin)
        else:
            existing_url, docs, _to = seen[key]
            merged_url = existing_url or url
            if safety_doc and safety_doc not in docs:
                docs.append(safety_doc)
            seen[key] = (merged_url, docs, trait_origin)

    # Sort by (module_path, api_name) ascending
    def _sort_key(entry):
        (module_path, full_path, kind), _val = entry
        api_name = full_path.split("::")[-1]
        return (module_path, api_name)

    sorted_items = sorted(seen.items(), key=_sort_key)

    tree_html = _build_module_tree(sorted_items)

    # Fetch auto-detected tags from RAPx contracts
    auto_tags_lookup = _load_auto_tags()

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
        ".layout { display: flex; height: 100vh; }",
        ".sidebar {"
        " width: 280px; flex-shrink: 0;"
        " border-right: 1px solid #d0d7de;"
        " overflow-y: auto; padding: 12px;"
        " background: #f6f8fa;"
        "}",
        ".sidebar-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }",
        ".sidebar h3 { margin: 0; font-size: 14px; color: #57606a; }",
        ".sidebar-toggle { cursor: pointer; border: 1px solid #d0d7de; background: #fff; border-radius: 4px; padding: 1px 7px; font-size: 12px; line-height: 1.4; color: #57606a; }",
        ".sidebar-toggle:hover { background: #eaeef2; }",
        ".sidebar-resizer { width: 5px; flex-shrink: 0; cursor: col-resize; background: transparent; }",
        ".sidebar-resizer:hover, .sidebar-resizer.dragging { background: rgba(9, 105, 218, 0.25); }",
        ".layout.sidebar-hidden .sidebar, .layout.sidebar-hidden .sidebar-resizer { display: none; }",
        ".sidebar-fab { display: none; position: fixed; top: 12px; left: 12px; z-index: 20; cursor: pointer; border: 1px solid #d0d7de; background: #fff; border-radius: 6px; padding: 4px 9px; font-size: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.12); }",
        ".sidebar-fab:hover { background: #eaeef2; }",
        ".layout.sidebar-hidden .sidebar-fab { display: block; }",
        ".tree { list-style: none; padding: 0; margin: 0; font-size: 13px; }",
        ".tree ul { list-style: none; padding-left: 16px; }",
        ".tree li { margin: 1px 0; }",
        ".tree-toggle {"
        " cursor: pointer; display: inline-block; width: 16px;"
        " text-align: center; color: #6e7781; user-select: none;"
        " vertical-align: middle; font-size: 11px;"
        "}",
        ".tree-toggle:hover { color: #24292f; }",
        ".tree-toggle.collapsed {"
        " transform: rotate(-90deg); display: inline-block;"
        "}",
        ".tree-node {"
        " cursor: pointer; padding: 2px 6px; border-radius: 4px;"
        " display: inline-block; color: #24292f;"
        " vertical-align: middle;"
        "}",
        ".tree-node:hover { background: #eaeef2; }",
        ".tree-node.selected { background: #ddf4ff; font-weight: 600; }",
        ".tree-node-all { font-weight: 600; }",
        ".tree-count { color: #6e7781; font-size: 0.85em; }",
        ".main { flex: 1; min-width: 0; overflow-y: auto; padding: 16px 24px; }",
        "@media (max-width: 768px) {"
        " .layout { flex-direction: column; }"
        " .sidebar { width: 100%; max-height: 35vh;"
        "  border-right: none; border-bottom: 1px solid #d0d7de; }"
        " .sidebar-resizer { display: none; }"
        " .main { overflow-y: visible; }"
        "}",
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
        ".tags-input, .notes-input {"
        " width: 100%; border: 1px solid #d0d7de; border-radius: 4px;"
        " padding: 2px 4px; font-size: 12px; box-sizing: border-box;"
        " resize: none; overflow: hidden; font-family: inherit;"
        "}",
        ".tags-input:focus, .notes-input:focus {"
        " outline: 2px solid #0969da; outline-offset: -1px;"
        "}",
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
        '<div class="layout">',
        '<div class="sidebar">',
        '<div class="sidebar-header">',
        '<h3>Module Tree</h3>',
        '<button class="sidebar-toggle" id="sidebarToggle" title="Hide sidebar">\u00ab</button>',
        '</div>',
        '<ul class="tree">',
        tree_html,
        '</ul>',
        '</div>',
        '<div class="sidebar-resizer"></div>',
        '<div class="main">',
        '<button class="sidebar-fab" id="sidebarFab" title="Show sidebar">\u2630</button>',
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
        "  var STORAGE_DATA_KEY = 'unsafe-doc-data:' + location.pathname;",
        "  document.addEventListener('DOMContentLoaded', function () {",
        "    var table = document.querySelector('.unsafe-table-wrap table');",
        "    if (!table) return;",
        "    var tbody = table.querySelector('tbody');",
        "    var cols = table.querySelectorAll('col');",
        "    var ths  = table.querySelectorAll('thead th');",
        "",
        "    // ── Column resize ────────────────────────────────────────────────",
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
        "    // ── Sidebar resize & toggle ───────────────────────────────────────",
        "    var layout = document.querySelector('.layout');",
        "    var sidebar = document.querySelector('.sidebar');",
        "    var resizer = document.querySelector('.sidebar-resizer');",
        "    var sidebarToggle = document.getElementById('sidebarToggle');",
        "    var sidebarFab = document.getElementById('sidebarFab');",
        "    var sidebarWidth = 280;",
        "    var SIDEBAR_KEY = 'unsafe-doc-sidebar:' + location.pathname;",
        "",
        "    function saveSidebar() {",
        "      var state = { w: sidebarWidth, hidden: layout.classList.contains('sidebar-hidden') };",
        "      try { localStorage.setItem(SIDEBAR_KEY, JSON.stringify(state)); } catch (e) {}",
        "    }",
        "    function setSidebarHidden(hidden) {",
        "      layout.classList.toggle('sidebar-hidden', hidden);",
        "      saveSidebar();",
        "    }",
        "",
        "    resizer.addEventListener('mousedown', function (e) {",
        "      e.preventDefault();",
        "      var startX = e.clientX;",
        "      var startW = sidebarWidth;",
        "      resizer.classList.add('dragging');",
        "      function onMove(ev) {",
        "        var w = startW + (ev.clientX - startX);",
        "        if (w < 160) w = 160;",
        "        if (w > 800) w = 800;",
        "        sidebarWidth = w;",
        "        sidebar.style.width = w + 'px';",
        "      }",
        "      function onUp() {",
        "        resizer.classList.remove('dragging');",
        "        document.removeEventListener('mousemove', onMove);",
        "        document.removeEventListener('mouseup', onUp);",
        "        saveSidebar();",
        "      }",
        "      document.addEventListener('mousemove', onMove);",
        "      document.addEventListener('mouseup', onUp);",
        "    });",
        "",
        "    sidebarToggle.addEventListener('click', function () { setSidebarHidden(true); });",
        "    sidebarFab.addEventListener('click', function () { setSidebarHidden(false); });",
        "",
        "    (function restoreSidebar() {",
        "      try {",
        "        var s = JSON.parse(localStorage.getItem(SIDEBAR_KEY) || 'null');",
        "        if (s) {",
        "          if (s.w && s.w >= 160) { sidebarWidth = s.w; sidebar.style.width = s.w + 'px'; }",
        "          if (s.hidden) layout.classList.add('sidebar-hidden');",
        "        }",
        "      } catch (e) {}",
        "    })();",
        "",
        "    // ── Helpers ──────────────────────────────────────────────────────",
        "    function getRows() {",
        "      return Array.from(tbody.querySelectorAll('tr'));",
        "    }",
        "",
        "    // ── URL state ────────────────────────────────────────────────────",
        "    function updateURL() { /* replaced below */ }",
        "    function loadFromURL() { /* replaced below */ }",
        "",
        "    function buildURL() {",
        "      var params = new URLSearchParams();",
        "      if (safetyOnly) params.set('s', '1');",
        "      var types = [];",
        "      typeCheckboxes.forEach(function (cb) { if (cb.checked) types.push(cb.dataset.type); });",
        "      if (types.length < typeCheckboxes.length) params.set('t', types.join(','));",
        "      if (selectedModule) params.set('m', selectedModule);",
        "      var url = location.protocol + '//' + location.host + location.pathname;",
        "      var qs = params.toString();",
        "      return qs ? url + '?' + qs : url;",
        "    }",
        "",
        "    updateURL = function () {",
        "      var newUrl = buildURL();",
        "      if (location.search !== '?' + new URL(newUrl).searchParams.toString()) {",
        "        try { history.replaceState(null, '', newUrl); } catch (e) {}",
        "      }",
        "    };",
        "",
        "    loadFromURL = function () {",
        "      var params = new URLSearchParams(location.search);",
        "      var types = params.get('t');",
        "      if (types) {",
        "        selectedTypes.clear();",
        "        types.split(',').forEach(function (t) { selectedTypes.add(t); });",
        "        typeCheckboxes.forEach(function (cb) {",
        "          cb.checked = selectedTypes.has(cb.dataset.type);",
        "        });",
        "      }",
        "      if (params.get('s') === '1') {",
        "        safetyOnly = true;",
        "        document.getElementById('safetyFilter').checked = true;",
        "      }",
        "      var mod = params.get('m');",
        "      if (mod) {",
        "        selectedModule = mod;",
        "        var allNodes = document.querySelectorAll('.tree-node');",
        "        allNodes.forEach(function (n) {",
        "          n.classList.toggle('selected', n.dataset.module === mod);",
        "        });",
        "      }",
        "    };",
        "",
        "    // ── localStorage for tags & notes ────────────────────────────────",
        "    function saveData() {",
        "      var data = {};",
        "      getRows().forEach(function (r) {",
        "        var tags = r.querySelector('.tags-input');",
        "        var notes = r.querySelector('.notes-input');",
        "        if (tags && tags.value.trim()) data[r.dataset.id + ':t'] = tags.value;",
        "        if (notes && notes.value.trim()) data[r.dataset.id + ':n'] = notes.value;",
        "      });",
        "      try { localStorage.setItem(STORAGE_DATA_KEY, JSON.stringify(data)); }",
        "      catch (e) {}",
        "    }",
        "    function loadData() {",
        "      var data = {};",
        "      try {",
        "        var saved = localStorage.getItem(STORAGE_DATA_KEY);",
        "        if (saved) data = JSON.parse(saved);",
        "      } catch (e) {}",
        "      getRows().forEach(function (r) {",
        "        var tags = r.querySelector('.tags-input');",
        "        var notes = r.querySelector('.notes-input');",
        "        if (tags && data[r.dataset.id + ':t']) {",
        "          tags.value = data[r.dataset.id + ':t'];",
        "          autoResize(tags);",
        "        } else if (tags && r.dataset.autoTags) {",
        "          tags.value = r.dataset.autoTags;",
        "          autoResize(tags);",
        "        }",
        "        if (notes && data[r.dataset.id + ':n']) {",
        "          notes.value = data[r.dataset.id + ':n'];",
        "          r.classList.add('row-confirmed');",
        "          autoResize(notes);",
        "        }",
        "      });",
        "    }",
        "",
        "    // ── Tags & Notes inputs ─────────────────────────────────────────────",
        "    getRows().forEach(function (row) {",
        "      var tags = row.querySelector('.tags-input');",
        "      var notes = row.querySelector('.notes-input');",
        "      if (tags) {",
        "        tags.addEventListener('input', function () {",
        "          autoResize(this);",
        "          saveData();",
        "        });",
        "      }",
        "      if (notes) {",
        "        notes.addEventListener('input', function () {",
        "          autoResize(this);",
        "          row.classList.toggle('row-confirmed', notes.value.trim() !== '');",
        "          saveData();",
        "        });",
        "      }",
        "    });",
        "    function autoResize(ta) {",
        "      ta.style.height = 'auto';",
        "      ta.style.height = ta.scrollHeight + 'px';",
        "    }",
        "",
        "    // ── Filter ────────────────────────────────────────────────────────",
        "    var rows = getRows();",
        "    var safetyOnly = false;",
        "    var selectedModule = '';",
        "    var typeCounts = rows.reduce(function (acc, r) {",
        "      var t = r.dataset.type || 'unknown';",
        "      acc[t] = (acc[t] || 0) + 1; return acc;",
        "    }, {});",
        "    var typeFilters = document.getElementById('typeFilters');",
        "    var selectedTypes = new Set(Object.keys(typeCounts));",
        "    var typeCheckboxes = [];",
        "    Object.keys(typeCounts).sort().forEach(function (type) {",
        "      var label = document.createElement('label');",
        "      label.className = 'type-item';",
        "      label.innerHTML = '<input type=\"checkbox\" checked data-type=\"' + type + '\" style=\"margin-right:6px;\">' + type;",
        "      typeCheckboxes.push(label.querySelector('input'));",
        "      typeFilters.appendChild(label);",
        "    });",
        "",
        "    function applyFilters() {",
        "      var visible = 0;",
        "      var grouped = {};",
        "      for (var r = 0; r < rows.length; r++) {",
        "        var row = rows[r];",
        "        var type = row.dataset.type || '';",
        "        var typeOk = selectedTypes.has(type);",
        "        var safetyOk = !safetyOnly || row.dataset.safety === '0';",
        "        var moduleOk = !selectedModule || row.dataset.module === selectedModule || row.dataset.module.indexOf(selectedModule + '::') === 0;",
        "        var show = typeOk && safetyOk && moduleOk;",
        "        row.style.display = show ? '' : 'none';",
        "        if (show) {",
        "          visible += 1;",
        "          var origin = row.dataset.traitOrigin || row.dataset.id;",
        "          if (grouped[origin] === undefined) grouped[origin] = true;",
        "          if (row.dataset.safety === '1') grouped[origin] = false;",
        "        }",
        "      }",
        "      var needDocs = 0;",
        "      for (var k in grouped) { if (grouped[k]) needDocs++; }",
        "      document.getElementById('summary').textContent = 'Showing ' + visible + ' / ' + rows.length + ' items; ' + needDocs + ' APIs need safety docs after grouping trait methods';",
        "    }",
        "",
        "    typeFilters.addEventListener('change', function (event) {",
        "      var tgt = event.target;",
        "      if (tgt.tagName !== 'INPUT' || tgt.type !== 'checkbox') return;",
        "      var t = tgt.dataset.type; if (!t) return;",
        "      if (tgt.checked) selectedTypes.add(t); else selectedTypes.delete(t);",
        "      applyFilters();",
        "      updateURL();",
        "    });",
        "",
        "    document.getElementById('safetyFilter').addEventListener('change', function () {",
        "      safetyOnly = this.checked;",
        "      applyFilters();",
        "      updateURL();",
        "    });",
        "",
        "    // ── Module tree ───────────────────────────────────────────────────",
        "    var treeNodes = document.querySelectorAll('.tree-node');",
        "    treeNodes.forEach(function (node) {",
        "      node.addEventListener('click', function (e) {",
        "        e.stopPropagation();",
        "        selectedModule = this.dataset.module;",
        "        treeNodes.forEach(function (n) { n.classList.remove('selected'); });",
        "        this.classList.add('selected');",
        "        applyFilters();",
        "        updateURL();",
        "      });",
        "    });",
        "",
        "    var treeToggles = document.querySelectorAll('.tree-toggle');",
        "    treeToggles.forEach(function (toggle) {",
        "      toggle.addEventListener('click', function (e) {",
        "        e.stopPropagation();",
        "        var ul = this.parentElement.querySelector(':scope > ul');",
        "        if (ul) {",
        "          if (ul.style.display === 'none') {",
        "            ul.style.display = '';",
        "            this.classList.remove('collapsed');",
        "            this.classList.add('expanded');",
        "          } else {",
        "            ul.style.display = 'none';",
        "            this.classList.remove('expanded');",
        "            this.classList.add('collapsed');",
        "          }",
        "        }",
        "      });",
        "    });",
        "",
        "    // ── Init: URL > localStorage, then apply ───────────────────────",
        "    safetyOnly = false;",
        "    document.getElementById('safetyFilter').checked = false;",
        "    loadFromURL();",
        "    loadData();",
        "    applyFilters();",
        "  });",
        "}());",
        "</script>",
        "",
        '<div class="unsafe-table-wrap">',
        '<table>',
        '<colgroup>',
        '<col style="width:3%">',
        '<col style="width:13%">',
        '<col style="width:15%">',
        '<col style="width:5%">',
        '<col style="width:34%">',
        '<col style="width:15%">',
        '<col style="width:15%">',
        '</colgroup>',
        '<thead>',
        '<tr><th>Index</th><th>Module Path</th><th>API Name</th>'
        '<th>Kind</th><th>Safety Doc</th><th>Tags</th><th>Notes</th></tr>',
        '</thead>',
        '<tbody>',
    ]

    for idx, ((module_path, full_path, kind), (url, docs, trait_origin)) in enumerate(sorted_items, 1):
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
        auto_tags = _resolve_auto_tags(full_path, kind, auto_tags_lookup)
        data_attrs = (
            f' data-type="{html.escape(kind, quote=True)}"'
            f' data-module="{html.escape(module_path, quote=True)}"'
            f' data-api="{html.escape(api_name, quote=True)}"'
            f' data-safety="{has_safety}"'
            f' data-trait-origin="{html.escape(trait_origin, quote=True)}"'
            f' data-auto-tags="{html.escape(auto_tags, quote=True)}"'
        )
        lines.append(
            f'<tr data-id="{html.escape(full_path, quote=True)}"{data_attrs}>'
            f'<td>{idx}</td>'
            f'<td>{module_cell}</td>'
            f'<td>{api_cell}</td>'
            f'<td>{kind_cell}</td>'
            f'<td>{safety_cell}</td>'
            f'<td><textarea class="tags-input" placeholder="tags" rows="1"></textarea></td>'
            f'<td><textarea class="notes-input" placeholder="notes" rows="1"></textarea></td>'
            f'</tr>'
        )

    lines += ["</tbody>", "</table>", "</div>", "</div>", "</div>", "</body>", "</html>", ""]
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
