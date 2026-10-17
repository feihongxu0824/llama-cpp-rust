# Prebuilt llama.cpp bundles (zvec-ai fork)

This fork of [`utilityai/llama-cpp-rs`](https://github.com/utilityai/llama-cpp-rs)
adds an optional **prebuilt** linking path to `llama-cpp-sys-2` so that
downstream projects (notably `zvec-grep`) do not have to compile llama.cpp from
source in their CI. It is pinned to upstream tag `0.1.154`
(commit `bed81ad4ab1a6c904b11d425608e50f976d8ea62`, vendored llama.cpp
`5f55650a78f92aff4d48d671423e888fac0469ff`).

## What changed vs upstream

Only the sys crate is touched; the safe crate `llama-cpp-2` keeps its Rust
source byte-for-byte (a single `prebuilt` feature forward was added to its
`Cargo.toml`):

- `llama-cpp-sys-2/Cargo.toml`: new `prebuilt = []` feature.
- `llama-cpp-sys-2/build.rs`: a self-contained early-return branch (`link_prebuilt`)
  that runs when the `prebuilt` feature is on or `ZVEC_LLAMA_PREBUILT_DIR` is set.
  It skips bindgen, the `cc` wrapper compile and the CMake build entirely, copies a
  pre-generated `bindings.rs`, and links prebuilt static archives. The upstream
  from-source path below it is unchanged and remains the fallback.
- `llama-cpp-2/Cargo.toml`: `prebuilt = ["llama-cpp-sys-2/prebuilt"]`.
- `scripts/collect_prebuilt.py`: harvests a bundle from a from-source build.
- `.github/workflows/build-prebuilt.yml`: produces and publishes bundles.

## Bundle layout

A bundle is a per-platform, per-variant directory (shipped as `*.tar.gz`):

```
llama-prebuilt-<target>-<variant>-<version>/
  bindings.rs     # platform-specific bindgen output (matches the archives)
  manifest.json   # version, llama.cpp commit, target, variant, per-file sha256
  lib/            # static archives: libllama.a, libggml*.a, llama-common*, wrapper
```

`bindings.rs` is **platform-specific** (it embeds target struct layouts such as
`__sFILE` on Darwin), which is why it ships inside each per-platform bundle
rather than being committed once.

## Producing bundles (release CI)

`build-prebuilt.yml` runs a normal from-source build per target/variant and then
collects the artifacts, so the bundle is exactly what the from-source path would
link:

```bash
cargo build --release --target <triple> -p llama-cpp-sys-2 [--features metal|cuda]
python3 scripts/collect_prebuilt.py \
  --out-dir target/<triple>/release/build/llama-cpp-sys-2-*/out \
  --dest dist/<bundle> --target <triple> --variant <cpu|cuda> \
  --version 0.1.154 --llama-cpp-commit <sha> --profile release
```

The effective sys features must match what the consumer resolves. For `zvec-grep`
(`llama-cpp-2` with `default-features = false`):

| target | sys features | archives |
|---|---|---|
| `aarch64-apple-darwin` | `common` + `metal` | llama, ggml, ggml-base, ggml-cpu, ggml-metal, llama-common, llama-common-base, wrapper |
| `x86_64-apple-darwin` | `common` | as above minus ggml-metal |
| `x86_64-unknown-linux-gnu` | `common` (`+cuda`) | llama, ggml, ggml-base, ggml-cpu, llama-common(+base), wrapper (+ ggml-cuda, cudart/cublas) |
| `aarch64-unknown-linux-gnu` | `common` | as linux-x64 |
| `x86_64-pc-windows-msvc` | `common` (`+cuda`) | `.lib` equivalents |

Stage 1 publishes CPU bundles for `aarch64-apple-darwin`, `x86_64-unknown-linux-gnu`,
`x86_64-pc-windows-msvc`. Stage 2 adds `x86_64-apple-darwin`, `aarch64-unknown-linux-gnu`
and the CUDA variants.

## Consuming a bundle (downstream)

Point the dependency at this fork; `ZVEC_LLAMA_PREBUILT_DIR` selects the link path.
There are two modes:

**Env-var driven (recommended; automatic fallback).** Depend on the fork *without*
the `prebuilt` feature. When `ZVEC_LLAMA_PREBUILT_DIR` is set, `build.rs` links the
prebuilt archives; when it is unset, the crate builds from source. This is what a
phased rollout wants — targets that have a published bundle use it, the rest fall
back — with no per-platform Cargo changes:

```toml
llama-cpp-2 = { git = "https://github.com/zvec-ai/llama-cpp-rust", rev = "<pinned>", default-features = false }
```

**Feature enforced (hard-require prebuilt).** Once every shipped target has a bundle,
add the feature so a missing `ZVEC_LLAMA_PREBUILT_DIR` fails loudly instead of
silently compiling from source:

```toml
llama-cpp-2 = { git = "https://github.com/zvec-ai/llama-cpp-rust", rev = "<pinned>", default-features = false, features = ["prebuilt"] }
```

Either way, set the env var to the extracted bundle and build:

```bash
export ZVEC_LLAMA_PREBUILT_DIR=/path/to/llama-prebuilt-<target>-<variant>-<version>
cargo build --release   # no cmake / clang / bindgen required
```

The from-source path (env var unset **and** feature off) is upstream's unchanged
behavior: it requires cmake + clang + a C/C++ toolchain and the `llama.cpp` submodule.

### zvec-grep

`downstream/zvec-grep/` holds the ready-to-apply integration for `zvec-grep`: a
self-contained `rust/scripts/fetch-llama-prebuilt.mjs` (downloads + verifies the
bundle, exports `ZVEC_LLAMA_PREBUILT_DIR`, and is inert when no bundle is published)
and `APPLY.md` with the exact edits to `zg-engine/Cargo.toml`, both rust workflows,
and `Cargo.lock`. It uses the env-var-driven mode so stage-1 platforms without a
bundle keep building from source.

## Upgrading upstream

```bash
git fetch upstream --tags
git merge 0.1.<new>          # only build.rs / Cargo.toml features diverge, so conflicts are minimal
# re-run build-prebuilt.yml to regenerate bindings.rs + archives for every target,
# then bump the downstream `rev` and bundle version.
```
