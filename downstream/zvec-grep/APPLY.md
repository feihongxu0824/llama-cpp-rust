# Applying the prebuilt llama.cpp integration to `zvec-grep`

These are the stage-3 changes from the plan, staged here because `zvec-grep` is a
separate repository. Everything below is ready to apply mechanically; the only
values you must fill in are the **release tag** and the **pinned fork commit**
produced by the prerequisites.

> Repo name: scripts default to `zvec-ai/llama-cpp-rust`. The plan text used both
> `llama-cpp-rust` and `llama-cpp-rs` — confirm the actual org repo name once and,
> if it differs, pass `--repo <owner/name>` to the fetch script.

---

## Prerequisites (must be done first, or the build breaks)

1. **Push this fork** to `zvec-ai/llama-cpp-rust`, branch `zvec/prebuilt-0.1.154`
   (includes the vendored `llama.cpp` submodule, `build.rs` prebuilt branch,
   `scripts/collect_prebuilt.py`, `.github/workflows/build-prebuilt.yml`).
2. **Publish bundles**: in the fork repo, run Actions → *Build prebuilt llama.cpp*
   (`build-prebuilt.yml`) via `workflow_dispatch` for the stage-1 targets
   (`aarch64-apple-darwin`, `x86_64-unknown-linux-gnu`, `x86_64-pc-windows-msvc`,
   variant `cpu`). It creates a release tagged `prebuilt-0.1.154-<N>` with
   `llama-prebuilt-<target>-cpu-0.1.154.tar.gz` (+ `.sha256`) assets.
3. **Record two values**:
   - `TAG` = the release tag, e.g. `prebuilt-0.1.154-1`
   - `FORK_SHA` = `git rev-parse HEAD` on the pushed branch (the `rev` to pin).

Nothing below works until `FORK_SHA` resolves on GitHub and `TAG` is published.

---

## Change 1 — add the prefetch script

Copy this file into `zvec-grep`:

```
downstream/zvec-grep/rust/scripts/fetch-llama-prebuilt.mjs  ->  zvec-grep/rust/scripts/fetch-llama-prebuilt.mjs
```

It is self-contained (Node stdlib only; Node is already set up / preinstalled on
every runner used by these workflows). It resolves the host triple via `rustc -vV`
(the build uses no `--target`, so host == build target), downloads the matching
bundle, verifies sha256, extracts to `rust/target/llama-prebuilt/`, and exports
`ZVEC_LLAMA_PREBUILT_DIR` to `$GITHUB_ENV`. If no bundle is published for the
target/variant it exits 0 without exporting, so `llama-cpp-sys-2` transparently
falls back to a from-source build.

---

## Change 2 — `rust/crates/zg-engine/Cargo.toml`

Switch the `llama-cpp-2` dependency from crates.io to the pinned fork.

**Before** (line 30):

```toml
llama-cpp-2 = { version = "0.1.154", default-features = false }
```

**After**:

```toml
llama-cpp-2 = { git = "https://github.com/zvec-ai/llama-cpp-rust", rev = "<FORK_SHA>", default-features = false }
```

- The `cuda` / `vulkan` feature mappings (lines 15–16) are unchanged — they still
  forward to `llama-cpp-2/cuda` and `llama-cpp-2/vulkan`.
- **Do not add `features = ["prebuilt"]` in stage 1.** Prebuilt linking is driven
  by the `ZVEC_LLAMA_PREBUILT_DIR` env var that the prefetch script exports only
  when a bundle exists. Leaving the feature off is what preserves the automatic
  from-source fallback on platforms that don't have a bundle yet (`darwin-x64`,
  `linux-arm64-gnu`). In stage 2, once every shipped target has a bundle, you may
  add `features = ["prebuilt"]` to make a missing bundle a hard error instead.

---

## Change 3 — `.github/workflows/rust-package-build.yml`

**(3a)** Add the bundle tag to the top-level `env:` block (after line 19):

```yaml
env:
  CARGO_TERM_COLOR: always
  CARGO_BUILD_JOBS: 2
  CMAKE_BUILD_PARALLEL_LEVEL: 2
  # Tag of the prebuilt llama.cpp bundles published by zvec-ai/llama-cpp-rust.
  # Bump whenever the fork republishes bundles for a new llama.cpp revision.
  LLAMA_PREBUILT_TAG: <TAG>
```

**(3b)** Insert these three steps immediately **before** `- name: Build the native
target` (currently line 114). Indentation must match the surrounding steps (6 spaces
for `- name:`, 8 for `uses:`/`run:`/`with:`/`env:`, 10 for their children):

```yaml
      - name: Restore the prebuilt llama.cpp bundle
        id: llama-prebuilt-cache
        uses: actions/cache/restore@v5
        with:
          path: rust/target/llama-prebuilt/
          key: rust-llama-prebuilt-v1-${{ inputs.target }}-cpu-${{ env.LLAMA_PREBUILT_TAG }}
      - name: Fetch the prebuilt llama.cpp bundle
        id: llama-prebuilt-fetch
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: node scripts/fetch-llama-prebuilt.mjs --tag "${{ env.LLAMA_PREBUILT_TAG }}" --variant cpu
      - name: Save the prebuilt llama.cpp bundle
        if: steps.llama-prebuilt-fetch.outputs.prebuilt == 'true' && steps.llama-prebuilt-cache.outputs.cache-hit != 'true'
        uses: actions/cache/save@v5
        with:
          path: rust/target/llama-prebuilt/
          key: ${{ steps.llama-prebuilt-cache.outputs.cache-primary-key }}
      - name: Build the native target
        run: npm run build
```

Notes:
- The job's `working-directory` is `rust`, so `scripts/fetch-llama-prebuilt.mjs`
  and the default `--dest target/llama-prebuilt` resolve under `rust/`, matching
  the cache `path: rust/target/llama-prebuilt/` (cache paths are repo-root relative).
- **Keep** the "Install Linux build dependencies" step (clang/libclang-dev/cmake/
  ninja) for now — stage 1 still needs it for the from-source fallback on
  `linux-arm64-gnu`. Remove it only in stage 2 when all Linux targets have bundles.
- The `actions/cache/restore` + short-circuit in the script means a cache hit skips
  the download but still exports `ZVEC_LLAMA_PREBUILT_DIR` (the fetch step always runs).

---

## Change 4 — `.github/workflows/rust-ci.yml`

CI runners (`ubuntu-latest`, `macos-latest`, `windows-latest`) map to
`x86_64-unknown-linux-gnu`, `aarch64-apple-darwin`, `x86_64-pc-windows-msvc` —
all covered by stage-1 CPU bundles.

**(4a)** Add the tag to the top-level `env:` block (after line 37):

```yaml
env:
  CARGO_TERM_COLOR: always
  CARGO_INCREMENTAL: "0"
  CARGO_PROFILE_DEV_DEBUG: "1"
  CARGO_PROFILE_TEST_DEBUG: "1"
  RUST_BACKTRACE: 1
  LLAMA_PREBUILT_TAG: <TAG>
```

**(4b)** In the `quality` job, insert after the "Cache Rust dependencies" step
(after line 67) and before "Check formatting":

```yaml
      - name: Fetch the prebuilt llama.cpp bundle
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: node scripts/fetch-llama-prebuilt.mjs --tag "${{ env.LLAMA_PREBUILT_TAG }}" --variant cpu
```

**(4c)** In the `test` job, insert the same step after its "Cache Rust
dependencies" step (after line 102) and before "Run workspace tests".

Notes:
- No separate `actions/cache` is needed here: the bundle lands in `rust/target/`,
  which `Swatinem/rust-cache` (`workspaces: rust -> target`) already persists, and
  the script short-circuits on a warm cache. PRs (`save-if: event != pull_request`)
  simply re-download — a small, bounded cost.
- `permissions: {}` is set at the workflow top and `contents: read` per job, which
  is enough to download public release assets. If the fork repo is **private**, the
  default `GITHUB_TOKEN` cannot read its releases — either make the fork public or
  supply a token with `repo` scope to the fetch step via `--token-env`.

---

## Change 5 — `rust/Cargo.lock`

Switching the source rewrites two lock entries (`llama-cpp-2` and its path-dep
`llama-cpp-sys-2`, both now from the git source). Regenerate without compiling:

```bash
cd rust
cargo metadata --format-version 1 >/dev/null   # re-resolves and updates Cargo.lock
git --no-pager diff --stat Cargo.lock          # confirm only llama-cpp entries changed
```

CI runs with `--locked`, so the updated `Cargo.lock` **must** be committed.

---

## Apply order

1. Prerequisites (push fork, publish `TAG`, note `FORK_SHA`).
2. Change 1 (copy script).
3. Change 2 (Cargo.toml `rev = "<FORK_SHA>"`).
4. Change 5 (regenerate + verify `Cargo.lock`).
5. Changes 3 & 4 (workflows, `LLAMA_PREBUILT_TAG: <TAG>`).
6. Commit on a branch, open a PR, watch `rust-ci.yml` then a `rust-publish.yml` dry run.

---

## Verify

**Local (darwin-arm64, prebuilt):**

```bash
cd rust
export ZVEC_LLAMA_PREBUILT_DIR=$(node scripts/fetch-llama-prebuilt.mjs --tag <TAG>)
npm run build          # seconds; no cmake/clang/bindgen invoked
./target/release/zg --rg "zvec-grep Rust rewrite" README.md
```

**From-source fallback** (proves the escape hatch):

```bash
cd rust
unset ZVEC_LLAMA_PREBUILT_DIR
cargo build --release --package zg   # compiles llama.cpp via cmake (needs clang+cmake)
```

**Numeric parity:** run the `zg-engine` model-layer tests and compare an embedding
produced by the prebuilt build against the from-source build for the same input
(see `rust/RUST_MAIN_PARITY.md` and `benchmarks/model-layer`). Outputs must match —
the prebuilt archives are built from the same vendored `llama.cpp` commit, so any
difference indicates an ABI/feature mismatch to investigate before rollout.

---

## Rollback

Revert Change 2 to `llama-cpp-2 = { version = "0.1.154", default-features = false }`,
restore the previous `Cargo.lock`, and drop the workflow steps (Changes 3–4). The
prefetch script (Change 1) is inert when unreferenced and can stay.
