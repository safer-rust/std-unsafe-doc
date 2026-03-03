#!/usr/bin/env python3
"""Unit tests for URL-generation logic in extract_public_unsafe.py."""

import sys
import unittest
from pathlib import Path

# Allow importing the module without its __main__ guard running.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_public_unsafe import (  # noqa: E402
    _build_path_to_kind,
    collect_unsafe_items,
    rustdoc_stable_url,
)


SAMPLE_PATHS = {
    "1": {"path": ["alloc", "string", "String"], "kind": "struct"},
    "2": {"path": ["alloc", "boxed", "Box"], "kind": "struct"},
    "3": {"path": ["alloc", "collections", "TryReserveError"], "kind": "enum"},
    "4": {"path": ["alloc", "alloc", "alloc_zeroed"], "kind": "function"},
    "5": {"path": ["core", "slice", "from_raw_parts"], "kind": "function"},
    "6": {"path": ["std", "io", "Write"], "kind": "trait"},
}


class TestBuildPathToKind(unittest.TestCase):
    def test_basic(self):
        result = _build_path_to_kind(SAMPLE_PATHS)
        self.assertEqual(result[("alloc", "string", "String")], "struct")
        self.assertEqual(result[("alloc", "boxed", "Box")], "struct")
        self.assertEqual(result[("alloc", "collections", "TryReserveError")], "enum")

    def test_missing_keys_skipped(self):
        bad = {"x": {"only_path": ["a", "b"]}}
        result = _build_path_to_kind(bad)
        self.assertEqual(result, {})

    def test_non_dict_skipped(self):
        result = _build_path_to_kind({"x": None, "y": "string"})
        self.assertEqual(result, {})


class TestRustdocStableUrl(unittest.TestCase):
    def setUp(self):
        self.ptk = _build_path_to_kind(SAMPLE_PATHS)

    # ── Free functions ──────────────────────────────────────────────────────

    def test_free_function_alloc(self):
        url = rustdoc_stable_url(
            "alloc", ["alloc", "alloc", "alloc_zeroed"], "function", self.ptk
        )
        self.assertEqual(
            url,
            "https://doc.rust-lang.org/stable/alloc/alloc/fn.alloc_zeroed.html",
        )

    def test_free_function_core(self):
        url = rustdoc_stable_url(
            "core", ["core", "slice", "from_raw_parts"], "function", self.ptk
        )
        self.assertEqual(
            url,
            "https://doc.rust-lang.org/stable/core/slice/fn.from_raw_parts.html",
        )

    # ── Unsafe traits ───────────────────────────────────────────────────────

    def test_trait(self):
        url = rustdoc_stable_url(
            "std", ["std", "io", "Write"], "trait", self.ptk
        )
        self.assertEqual(
            url,
            "https://doc.rust-lang.org/stable/std/io/trait.Write.html",
        )

    # ── Methods on structs ──────────────────────────────────────────────────

    def test_method_on_struct_string(self):
        url = rustdoc_stable_url(
            "alloc",
            ["alloc", "string", "String", "as_mut_vec"],
            "function",
            self.ptk,
        )
        self.assertEqual(
            url,
            "https://doc.rust-lang.org/stable/alloc/string/struct.String.html#method.as_mut_vec",
        )

    def test_method_on_struct_box(self):
        url = rustdoc_stable_url(
            "alloc",
            ["alloc", "boxed", "Box", "assume_init"],
            "function",
            self.ptk,
        )
        self.assertEqual(
            url,
            "https://doc.rust-lang.org/stable/alloc/boxed/struct.Box.html#method.assume_init",
        )

    # ── Methods on enums ───────────────────────────────────────────────────

    def test_method_on_enum(self):
        url = rustdoc_stable_url(
            "alloc",
            ["alloc", "collections", "TryReserveError", "some_method"],
            "function",
            self.ptk,
        )
        self.assertEqual(
            url,
            "https://doc.rust-lang.org/stable/alloc/collections/enum.TryReserveError.html#method.some_method",
        )

    # ── Fallback / edge cases ──────────────────────────────────────────────

    def test_method_without_path_to_kind_defaults_to_struct(self):
        url = rustdoc_stable_url(
            "alloc",
            ["alloc", "string", "String", "as_mut_vec"],
            "function",
            None,
        )
        self.assertEqual(
            url,
            "https://doc.rust-lang.org/stable/alloc/string/struct.String.html#method.as_mut_vec",
        )

    def test_too_short_returns_empty(self):
        self.assertEqual(rustdoc_stable_url("alloc", ["alloc"], "function", self.ptk), "")
        self.assertEqual(rustdoc_stable_url("alloc", [], "function", self.ptk), "")

    def test_unknown_kind_free_item_returns_empty(self):
        url = rustdoc_stable_url(
            "alloc", ["alloc", "alloc", "something"], "module", self.ptk
        )
        self.assertEqual(url, "")


# ── Minimal synthetic rustdoc JSON helpers ─────────────────────────────────

def _make_fn_item(item_id, name, is_unsafe=True, visibility="public"):
    """Build a minimal rustdoc JSON function item."""
    return {
        "id": item_id,
        "name": name,
        "visibility": visibility,
        "docs": "# Safety\nCaller must ensure safety." if is_unsafe else "",
        "inner": {
            "function": {
                "decl": {},
                "generics": {},
                "header": {"unsafe_": is_unsafe, "const_": False, "async_": False},
                "has_body": True,
            }
        },
    }


def _make_impl_item(impl_id, for_type_id, method_ids):
    """Build a minimal rustdoc JSON impl item."""
    return {
        "id": impl_id,
        "name": None,
        "visibility": "public",
        "docs": "",
        "inner": {
            "impl": {
                "is_unsafe": False,
                "generics": {},
                "trait": None,
                "for": {"resolved_path": {"id": for_type_id, "name": "T", "args": {}}},
                "items": list(method_ids),
            }
        },
    }


class TestCollectUnsafeItems(unittest.TestCase):
    """Tests for collect_unsafe_items() using synthetic rustdoc JSON."""

    def _make_index_paths(self):
        """Return (index, paths) for a minimal alloc crate with:
        - String::as_mut_vec  (unsafe method on struct String)
        - Box::assume_init    (unsafe method on struct Box)
        - alloc_zeroed        (free unsafe function)
        - unsafe_trait        (unsafe trait GlobalAlloc)
        """
        # paths: item_id -> {path, kind}
        paths = {
            "10": {"path": ["alloc", "string", "String"], "kind": "struct"},
            "20": {"path": ["alloc", "boxed", "Box"], "kind": "struct"},
            "30": {"path": ["alloc", "alloc", "alloc_zeroed"], "kind": "function"},
            "40": {"path": ["alloc", "alloc", "GlobalAlloc"], "kind": "trait"},
        }
        # index: item_id -> item
        method_as_mut_vec = _make_fn_item("11", "as_mut_vec")
        method_assume_init = _make_fn_item("21", "assume_init")
        fn_alloc_zeroed = _make_fn_item("30", "alloc_zeroed")
        impl_string = _make_impl_item("50", "10", ["11"])
        impl_box = _make_impl_item("51", "20", ["21"])
        trait_global_alloc = {
            "id": "40",
            "name": "GlobalAlloc",
            "visibility": "public",
            "docs": "# Safety\nImplementors must be correct.",
            "inner": {"trait": {"is_unsafe": True, "items": [], "generics": {}}},
        }
        index = {
            "11": method_as_mut_vec,
            "21": method_assume_init,
            "30": fn_alloc_zeroed,
            "40": trait_global_alloc,
            "50": impl_string,
            "51": impl_box,
        }
        return index, paths

    def test_method_kind_and_api_display(self):
        index, paths = self._make_index_paths()
        items = collect_unsafe_items("alloc", index, paths)
        methods = [i for i in items if i["kind"] == "method"]
        method_names = {i["api_display"] for i in methods}
        self.assertIn("String::as_mut_vec", method_names)
        self.assertIn("Box::assume_init", method_names)

    def test_method_module_path(self):
        index, paths = self._make_index_paths()
        items = collect_unsafe_items("alloc", index, paths)
        by_display = {i["api_display"]: i for i in items if i["kind"] == "method"}
        self.assertEqual(by_display["String::as_mut_vec"]["module_path"], "alloc::string")
        self.assertEqual(by_display["Box::assume_init"]["module_path"], "alloc::boxed")

    def test_method_url(self):
        index, paths = self._make_index_paths()
        items = collect_unsafe_items("alloc", index, paths)
        by_display = {i["api_display"]: i for i in items if i["kind"] == "method"}
        self.assertEqual(
            by_display["String::as_mut_vec"]["url"],
            "https://doc.rust-lang.org/stable/alloc/string/struct.String.html#method.as_mut_vec",
        )
        self.assertEqual(
            by_display["Box::assume_init"]["url"],
            "https://doc.rust-lang.org/stable/alloc/boxed/struct.Box.html#method.assume_init",
        )

    def test_free_function_not_mistaken_for_method(self):
        index, paths = self._make_index_paths()
        items = collect_unsafe_items("alloc", index, paths)
        fns = [i for i in items if i["kind"] == "function"]
        fn_names = {i["api_display"] for i in fns}
        self.assertIn("alloc_zeroed", fn_names)
        # free function URL must use fn. prefix
        fn_item = next(i for i in fns if i["api_display"] == "alloc_zeroed")
        self.assertIn("/fn.alloc_zeroed.html", fn_item["url"])

    def test_unsafe_trait_collected(self):
        index, paths = self._make_index_paths()
        items = collect_unsafe_items("alloc", index, paths)
        traits = [i for i in items if i["kind"] == "trait"]
        self.assertTrue(any(i["api_display"] == "GlobalAlloc" for i in traits))

    def test_method_full_path(self):
        index, paths = self._make_index_paths()
        items = collect_unsafe_items("alloc", index, paths)
        full_paths = {i["full_path"] for i in items}
        self.assertIn("alloc::string::String::as_mut_vec", full_paths)
        self.assertIn("alloc::boxed::Box::assume_init", full_paths)

    def test_safe_method_not_collected(self):
        index, paths = self._make_index_paths()
        # Add a safe method to String's impl
        index["11_safe"] = _make_fn_item("11_safe", "len", is_unsafe=False)
        impl_string = index["50"]
        impl_string["inner"]["impl"]["items"].append("11_safe")
        items = collect_unsafe_items("alloc", index, paths)
        api_displays = {i["api_display"] for i in items}
        self.assertNotIn("String::len", api_displays)

    def test_private_method_not_collected(self):
        index, paths = self._make_index_paths()
        # Add a private unsafe method
        priv = _make_fn_item("11_priv", "secret", is_unsafe=True, visibility="")
        index["11_priv"] = priv
        impl_string = index["50"]
        impl_string["inner"]["impl"]["items"].append("11_priv")
        items = collect_unsafe_items("alloc", index, paths)
        api_displays = {i["api_display"] for i in items}
        self.assertNotIn("String::secret", api_displays)

    def test_unresolvable_impl_for_gracefully_skipped(self):
        """impl blocks whose 'for' type cannot be resolved are silently ignored."""
        index, paths = self._make_index_paths()
        # Add an impl whose 'for' points to an unknown ID
        bad_impl = {
            "id": "99",
            "name": None,
            "visibility": "public",
            "docs": "",
            "inner": {
                "impl": {
                    "is_unsafe": False,
                    "generics": {},
                    "trait": None,
                    "for": {"resolved_path": {"id": "9999", "name": "Unknown", "args": {}}},
                    "items": ["11"],
                }
            },
        }
        index["99"] = bad_impl
        # Should not raise; as_mut_vec may be deduplicated but no crash
        items = collect_unsafe_items("alloc", index, paths)
        self.assertIsInstance(items, list)


if __name__ == "__main__":
    unittest.main()
