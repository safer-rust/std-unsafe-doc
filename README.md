# std-unsafe-doc

Extract all public unsafe APIs (unsafe functions and unsafe traits) from the
Rust standard library crates `core`, `alloc`, and `std`, and save the
results to a static HTML table.

## Goal

The script scans the local `nightly` rust-src component via rustdoc
JSON, collects every item that is both `pub` and `unsafe`, and writes an
HTML table:

| Column | Content |
|--------|---------|
| Index | generated row number |
| Module Path | module path, e.g. `core::ptr` |
| API Name | item name linked to nightly rustdoc |
| Kind | function, method, trait method, or trait |
| Safety doc | text from the `# Safety` section of the item's docs |
| LLM Review Comment | `gpt-5.6-sol` comment generated from the current Rust snapshot with RAPx full context |
| Diff | independent LLM semantic correctness judgment against the original `# Safety` section |
| Tags | RAPx-derived and manually editable contract tags |
| Notes | locally persisted audit notes |

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
RUST_UNSAFE_DOC_TOOLCHAIN=nightly-2026-08-27 \
  python3 scripts/extract_public_unsafe.py docs/index.html
```

This is the same command the CI workflow runs automatically on every push to
`main`. You can also trigger it manually from the **Actions** tab →
**Generate docs/index.html** → **Run workflow**.

The generator loads `data/core_current_review_data.json` when present. This
artifact contains 427 unique `core` APIs with Safety documentation, excluding
`core::arch` and its descendants, from `rustc 1.100.0-nightly
(bff8e12ff 2026-08-26)`. Stage 2 uses the `full` context preset: source and
signature plus RAPx unsafe callees, call graph, unsafe operations, related
types/helpers, and trait/macro context. Records use exact rustdoc public API paths, so comments
are never assigned by API name alone. The page provides filters for generated
comments and Diff entries. The Diff column reports an independent LLM judgment
based only on the original and generated Safety text. It provides an editable
classification with three values: `Correct`, `Missing`, and `Incorrect`.
`Missing` is used only when the generated comment is a semantically correct
strict subset of the original safety requirements; omitted original
requirements are listed in the editable text area. Any other semantic mismatch,
including an added, weakened, strengthened, contradictory, or otherwise wrong
condition, is `Incorrect`. Each Incorrect item is a concise sentence stating
the generated condition and its semantic difference from the original Safety
Doc, without a long explanation or reasoning trace. `Correct` hides the detail editor. These are automatic judgments,
not human-review labels. Changes made in the page are stored locally with the
existing audit data and do not modify the generated artifact.

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
- The script uses `nightly` by default. Set `RUST_UNSAFE_DOC_TOOLCHAIN` to pin a
  dated nightly when the page must match an experiment snapshot.
- The first run is slower because cargo compiles the crates; subsequent runs
  reuse the build cache.
