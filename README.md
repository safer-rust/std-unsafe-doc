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
python3 scripts/extract_public_unsafe.py
```

This will:
1. Locate the `nightly-2025-12-06` sysroot with `rustc --print sysroot`.
2. Run `cargo rustdoc --output-format json` for `core`, `alloc`, and `std`.
3. Parse each JSON file and collect public unsafe items.
4. Write the results to **`std-unsafe.md`** in the repository root.
5. Print the number of items written and the output path.

You can specify a custom output path:

```sh
python3 scripts/extract_public_unsafe.py my_output.md
```

### Generate docs/index.md (GitHub Pages source)

To generate or refresh the site's home page locally:

```sh
python3 scripts/extract_public_unsafe.py docs/index.md
```

This is the same command the CI workflow runs automatically on every push to
`main`. You can also trigger it manually from the **Actions** tab →
**Generate docs/index.md** → **Run workflow**.

## GitHub Pages

The site is served from the `docs/` folder on the `main` branch.

### Enabling Pages

1. Go to **Settings → Pages** in this repository.
2. Under **Source**, select **Deploy from a branch**.
3. Choose branch **`main`** and folder **`/docs`**, then click **Save**.

Once enabled, the site is available at:

> **<https://safer-rust.github.io/std-unsafe-doc/>**

The `docs/index.md` file is regenerated automatically by the
[Generate docs/index.md](.github/workflows/generate-docs.yml) workflow on
every push to `main`.

## Interactive Table Features

The generated page (`docs/index.md`) includes two interactive front-end
enhancements that work entirely in the browser with no backend required.

### Drag-and-Drop Row Sorting

Each table row has a **drag handle** (☰) in the leftmost column.  Grab the
handle and drag a row to a new position to reorder the entries.  The new order
is automatically saved to `localStorage` and restored the next time you open
the page.

### Confirmed Checkbox

The rightmost **Confirmed ✓** column contains a checkbox for each row.  Check
it once you have manually reviewed an API and are satisfied it is safe.
The checked state is saved to `localStorage` and is restored on page load.
Confirmed rows are highlighted with a subtle green background.

### localStorage Details

Both the row order and the checked state are stored per-page-path so that
different pages do not interfere with each other:

| Key | Value |
|-----|-------|
| `unsafe-doc-order:<pathname>` | JSON array of item paths (row order) |
| `unsafe-doc-checked:<pathname>` | JSON object mapping item path → `true`/`false` |

To **reset** all customizations, open your browser's DevTools → Application →
Local Storage, and delete the two keys that start with `unsafe-doc-`.
Alternatively, run the following in the browser console:

```js
Object.keys(localStorage)
  .filter(k => k.startsWith('unsafe-doc-'))
  .forEach(k => localStorage.removeItem(k));
location.reload();
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
