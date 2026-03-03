#!/usr/bin/env python3
"""Unit tests for URL-generation logic in extract_public_unsafe.py."""

import sys
import unittest
from pathlib import Path

# Allow importing the module without its __main__ guard running.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_public_unsafe import _build_path_to_kind, rustdoc_stable_url  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
