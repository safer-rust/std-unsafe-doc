# std-unsafe-doc

Extract all public unsafe APIs (unsafe functions and unsafe traits) from the
Rust standard library crates `core`, `alloc`, and `std`, and save the
results to a static HTML table.

## Goal

The script scans the local `nightly` rust-src component via rustdoc
JSON, collects every item that is both `pub` and `unsafe`, and writes a
five-column HTML table:

| Column | Content |
|--------|---------|
| (drag handle) | grab handle for reordering rows |
| Module | module path, e.g. `core::ptr` |
| API    | full item path linked to stable rustdoc |
| Safety doc | text from the `# Safety` section of the item's docs |
| Confirmed ✓ | checkbox to mark an API as reviewed |

## Prerequisites

1. **Rust nightly toolchain**:
   ```sh
   rustup toolchain install nightly
   ```
2. **Rust standard-library source** for that toolchain:
   ```sh
   rustup component add rust-src --toolchain nightly
   ```
3. **Python 3** (3.8 or newer, no extra packages required).

## Usage

Run the script from the repository root:

```sh
python3 scripts/extract_public_unsafe.py
```

This will:
1. Locate the `nightly` sysroot with `rustc --print sysroot`.
2. Run `cargo rustdoc --output-format json` for `core`, `alloc`, and `std`.
3. Parse each JSON file and collect public unsafe items.
4. Write the results to **`std-unsafe.html`** in the repository root.
5. Print the number of items written and the output path.

You can specify a custom output path:

```sh
python3 scripts/extract_public_unsafe.py my_output.html
```

### Generate docs/index.html (GitHub Pages source)

To generate or refresh the site's home page locally:

```sh
python3 scripts/extract_public_unsafe.py docs/index.html
```

This is the same command the CI workflow runs automatically on every push to
`main`. You can also trigger it manually from the **Actions** tab →
**Generate docs/index.html** → **Run workflow**.

## GitHub Pages

The site is served from the `docs/` folder on the `main` branch.

### Enabling Pages

1. Go to **Settings → Pages** in this repository.
2. Under **Source**, select **Deploy from a branch**.
3. Choose branch **`main`** and folder **`/docs`**, then click **Save**.

Once enabled, the site is available at:

> **<https://safer-rust.github.io/std-unsafe-doc/>**

The `docs/index.html` file is regenerated automatically by the
[Generate docs/index.html](.github/workflows/generate-docs.yml) workflow on
every push to `main`.

## Notes / Caveats

- **Nightly required**: rustdoc JSON (`--output-format json`) is a nightly-only
  unstable feature.
- The script always uses the **latest installed `nightly`** toolchain.  Results
  may change between nightly releases as the standard library evolves.  Run
  `rustup update nightly` to update to the current nightly before regenerating.
- The first run is slower because cargo compiles the crates; subsequent runs
  reuse the build cache.
