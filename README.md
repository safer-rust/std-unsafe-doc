# audit-unsafe-doc

Extract all **public unsafe APIs** (unsafe functions and unsafe traits) from the
Rust standard library crates **core**, **alloc**, and **std**, and save the
results to a Markdown table.

## Goal

The script scans the local `nightly-2025-12-06` rust-src component via rustdoc
JSON, collects every item that is both `pub` and `unsafe`, and writes a
three-column Markdown table:

| Column | Content |
|--------|---------|
| Module | module path, e.g. `core::ptr` |
| API    | full item path linked to locally generated rustdoc HTML |
| Safety doc | text from the `# Safety` section of the item's docs |

## Prerequisites

1. **Rust nightly toolchain** for `nightly-2025-12-06`:
   ```sh
   rustup toolchain install nightly-2025-12-06
   ```
2. **Rust standard-library source** for that toolchain:
   ```sh
   rustup component add rust-src --toolchain nightly-2025-12-06
   ```
3. **Python 3** (3.8 or newer, no extra packages required).

## Usage

Run the script from the repository root:

```sh
python3 scripts/extract_public_unsafe_stdlib.py
```

This will:
1. Locate the `nightly-2025-12-06` sysroot with `rustc --print sysroot`.
2. Run `cargo rustdoc --output-format json` for `core`, `alloc`, and `std`.
3. Parse each JSON file and collect public unsafe items.
4. Write the results to **`public_unsafe_api_nightly-2025-12-06.md`** in the
   current directory.
5. Print the number of items written and the output path.

You can specify a custom output path:

```sh
python3 scripts/extract_public_unsafe_stdlib.py my_output.md
```

## Notes / Caveats

- **Nightly required**: rustdoc JSON (`--output-format json`) is a nightly-only
  unstable feature.
- **`file://` links**: the API column links to locally generated rustdoc HTML
  using `file://` URIs. These links are only clickable when the HTML has already
  been generated and opened in a browser or a markdown renderer that supports
  `file://` URLs. GitHub's web UI and most online renderers do not follow local
  file links.
- The first run is slower because cargo compiles the crates; subsequent runs
  reuse the build cache.
