# Site Cloner (Zig)

A single-file, dependency-free website cloning pipeline written in Zig 0.14.
It fetches a target URL, tokenizes the HTML into a compact arena, analyzes its
structure, and emits a scaffolded React + Vite + Tailwind project that mirrors
the source page's sections, assets, design tokens, and content statistics.

## Pipeline overview

1. **HTML tokenizer / hierarchy builder** — streaming tokenizer with raw-text
   handling (`script`, `style`, ...), void elements, malformed-nesting
   recovery (force-close / adoption / rebalancing), CDATA, comments, doctypes,
   and declarative shadow-DOM template detection.
2. **Tree analysis** — dead-subtree marking, redundant-wrapper elision,
   stacking-context (`z-index`) assignment, semantic weighting.
3. **Extraction** — CSS custom properties (`:root` tokens), image assets,
   internal links (with a Bloom filter), colors (hex/rgb) clustered with
   k-means, transitions/keyframes, form fields and table columns.
4. **Section modeling** — structural FNV hashing, MinHash signatures,
   text-entropy hashes, TF-IDF, Markov trigrams, prominence/LCP scoring,
   purity/state analysis, ARIA role inference and bipartite component labels.
5. **Asset pipeline** — concurrent-safe sequential download with relative/
   protocol-relative URL resolution and Shopify CDN upscaling, perceptual
   hashing with duplicate detection, sprite-slot packing, and blurhash-style
   placeholders derived from a DFT.
6. **Code generation (virtual file system)** — React components partitioned
   per 12 sections, design tokens, theme palette, SEO metadata, Markov
   placeholder generator, subset-font stub, Bloom-based client router,
   WebAssembly runtime stub, ECS world, XState machines, web workers,
   GraphQL stubs, a WebGL overlay, Tailwind/PostCSS config, a Playwright
   differential fuzzer, and build docs.

## Requirements

- Zig **0.14.x**. No system Zig is required: the compiler is available as a
  Python wheel (`ziglang`), which bundles the full toolchain:

  ```sh
  python3 -m venv .venv-zig
  . .venv-zig/bin/activate
  pip install ziglang==0.14.0
  ```

## Build and run

```sh
# with a system zig on PATH
zig build-exe site_cloner/main.zig -O ReleaseSafe -femit-bin=site_cloner/site_cloner

# or via the PyPI toolchain
python3 -m ziglang build-exe site_cloner/main.zig -O ReleaseSafe -femit-bin=site_cloner/site_cloner

# run inside a dedicated output directory (the tool writes clone-data/,
# public/, src/, and tests/ relative to the current working directory)
mkdir -p out && cd out
../site_cloner/site_cloner https://example.com
```

`build.sh` wraps both toolchain locations:

```sh
./site_cloner/build.sh            # builds the executable
./site_cloner/build.sh https://example.com   # build, then run in ./out
```

The generated React project is written under the working directory; inspect
`clone-data/raw.html` for the captured source and `src/components/` for the
partitioned section components.

## Notes / known limitations

- The HTTP client follows the stdlib default redirect behavior; non-200
  responses fail with `HttpRequestFailed`. Individual asset download failures
  are logged and skipped without aborting the run.
- `transcodeImages` and a few analysis hooks are intentionally placeholder
  passes (no image codec is linked); dimensions and sprite sheets are
  metadata-only stubs.
- The generated `subset.woff2` is a custom placeholder container, not a valid
  font binary.
- Generated TypeScript/TSX is emitted for scaffolding and is not type-checked
  by the Zig build.
