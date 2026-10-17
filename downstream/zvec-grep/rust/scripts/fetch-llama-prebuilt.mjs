#!/usr/bin/env node
// Fetch a prebuilt llama.cpp bundle for the current host target before `cargo build`.
//
// This mirrors the protocol documented in zvec-ai/llama-cpp-rust PREBUILT.md and its
// scripts/fetch_prebuilt.py reference implementation. It exists so that zg-engine's
// `llama-cpp-sys-2` dependency links prebuilt static archives instead of compiling
// llama.cpp/ggml from source (CMake + bindgen + cc) on every CI run.
//
// Phased rollout (stage 1 publishes CPU bundles for aarch64-apple-darwin,
// x86_64-unknown-linux-gnu, x86_64-pc-windows-msvc). Because coverage is partial, the
// script is intentionally non-fatal when a bundle is missing:
//
//   bundle found      -> verify sha256, extract, export ZVEC_LLAMA_PREBUILT_DIR
//                        (written to $GITHUB_ENV so the later `npm run build` step sees it).
//                        build.rs then links the prebuilt archives. exit 0.
//   bundle absent     -> export nothing and exit 0; build.rs falls back to a from-source
//                        build (requires clang/cmake, which the workflow still installs).
//   checksum/IO error -> exit 1. A corrupt/partial bundle must never silently degrade to a
//                        from-source build, so this is a hard failure.
//
// The build in this repo is `cargo build --release --package zg` with no --target, i.e. it
// always targets the host. We therefore resolve the triple from `rustc -vV` (authoritative
// for what cargo will build); --target overrides it for cross-compiles.
//
// Usage:
//   node scripts/fetch-llama-prebuilt.mjs --tag prebuilt-0.1.154-1 [--variant cpu]
//       [--repo zvec-ai/llama-cpp-rust] [--version 0.1.154] [--target <triple>]
//       [--dest target/llama-prebuilt] [--base-url <url>] [--token-env GITHUB_TOKEN]

import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const DEFAULTS = {
  repo: "zvec-ai/llama-cpp-rust",
  variant: "cpu",
  version: "0.1.154",
  dest: "target/llama-prebuilt",
  tokenEnv: "GITHUB_TOKEN",
};

const HARD_FAILURE = 1;

function log(msg) {
  process.stderr.write(`${msg}\n`);
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) continue;
    const eq = token.indexOf("=");
    if (eq !== -1) {
      args[token.slice(2, eq)] = token.slice(eq + 1);
    } else {
      const key = token.slice(2);
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("--")) {
        args[key] = next;
        i += 1;
      } else {
        args[key] = true;
      }
    }
  }
  return args;
}

// Resolve the rust target triple that `cargo build` will actually produce. The build has no
// --target flag, so it is the host triple; `rustc -vV` is the source of truth.
function hostTriple() {
  const res = spawnSync("rustc", ["-vV"], { encoding: "utf8" });
  if (res.status !== 0 || !res.stdout) {
    throw new Error(`rustc -vV failed (status ${res.status}); pass --target explicitly`);
  }
  const line = res.stdout.split("\n").find((l) => l.startsWith("host:"));
  if (!line) throw new Error("could not parse host triple from `rustc -vV`");
  return line.slice("host:".length).trim();
}

async function download(url, token) {
  const headers = { "User-Agent": "zvec-grep-llama-prebuilt-fetcher" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let resp;
  try {
    resp = await fetch(url, { headers });
  } catch (err) {
    // Network unreachable / DNS / TLS: treat like "not published" so CI can fall back.
    log(`network error reaching ${url}: ${err.message}`);
    return null;
  }
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`download failed for ${url}: HTTP ${resp.status}`);
  return Buffer.from(await resp.arrayBuffer());
}

function sha256(buf) {
  return createHash("sha256").update(buf).digest("hex");
}

function exportEnv(name, value) {
  process.env[name] = value;
  const ghEnv = process.env.GITHUB_ENV;
  if (ghEnv) appendFileSync(ghEnv, `${name}=${value}\n`);
}

function setOutput(name, value) {
  const ghOut = process.env.GITHUB_OUTPUT;
  if (ghOut) appendFileSync(ghOut, `${name}=${value}\n`);
}

function extractTarGz(archivePath, destDir) {
  const probe = spawnSync("tar", ["--version"], { encoding: "utf8" });
  if (probe.error || probe.status !== 0) {
    throw new Error("`tar` is not available on PATH; cannot extract the bundle");
  }
  const res = spawnSync("tar", ["-xzf", archivePath, "-C", destDir], { encoding: "utf8" });
  if (res.status !== 0) {
    throw new Error(`tar extraction failed (status ${res.status}): ${res.stderr || res.stdout}`);
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.tag || args.tag === true) {
    log("error: --tag is required, e.g. --tag prebuilt-0.1.154-1");
    return HARD_FAILURE;
  }

  const repo = typeof args.repo === "string" ? args.repo : DEFAULTS.repo;
  const variant = typeof args.variant === "string" ? args.variant : DEFAULTS.variant;
  const version = typeof args.version === "string" ? args.version : DEFAULTS.version;
  const dest = resolve(typeof args.dest === "string" ? args.dest : DEFAULTS.dest);
  const tokenEnv = typeof args["token-env"] === "string" ? args["token-env"] : DEFAULTS.tokenEnv;

  let target;
  try {
    target = typeof args.target === "string" ? args.target : hostTriple();
  } catch (err) {
    log(`error: ${err.message}`);
    return HARD_FAILURE;
  }

  const bundle = `llama-prebuilt-${target}-${variant}-${version}`;
  const base =
    typeof args["base-url"] === "string"
      ? args["base-url"].replace(/\/$/, "")
      : `https://github.com/${repo}/releases/download/${args.tag}`;
  const assetUrl = `${base}/${bundle}.tar.gz`;
  const checksumUrl = `${base}/${bundle}.tar.gz.sha256`;
  const token = process.env[tokenEnv] || null;

  const bundleDir = join(dest, bundle);
  // Idempotent short-circuit: if a previous run (or an actions/cache restore) already
  // populated this bundle, reuse it. This keeps the CI cache effective while still
  // exporting ZVEC_LLAMA_PREBUILT_DIR on every run so `npm run build` links prebuilt.
  if (!args.force && existsSync(join(bundleDir, "bindings.rs")) && existsSync(join(bundleDir, "lib"))) {
    exportEnv("ZVEC_LLAMA_PREBUILT_DIR", bundleDir);
    setOutput("prebuilt", "true");
    log(`reusing cached prebuilt llama.cpp for ${target}/${variant}: ${bundleDir}`);
    process.stdout.write(`${bundleDir}\n`);
    return 0;
  }

  log(`fetching ${assetUrl}`);
  let archive;
  try {
    archive = await download(assetUrl, token);
  } catch (err) {
    log(`error: ${err.message}`);
    return HARD_FAILURE;
  }
  if (archive === null) {
    log(`no prebuilt bundle for ${target}/${variant} at ${args.tag}; building llama.cpp from source`);
    setOutput("prebuilt", "false");
    return 0;
  }

  // Verify integrity when the sidecar is published; a mismatch is a hard failure.
  try {
    const sidecar = await download(checksumUrl, token);
    if (sidecar) {
      const expected = sidecar.toString("utf8").split(/\s+/)[0].trim().toLowerCase();
      const actual = sha256(archive).toLowerCase();
      if (expected !== actual) {
        log(`error: sha256 mismatch (expected ${expected}, got ${actual})`);
        return HARD_FAILURE;
      }
      log("sha256 verified");
    } else {
      log("warning: no .sha256 sidecar published; skipping verification");
    }
  } catch (err) {
    log(`error: checksum verification failed: ${err.message}`);
    return HARD_FAILURE;
  }

  try {
    rmSync(bundleDir, { recursive: true, force: true });
    mkdirSync(dest, { recursive: true });
    const archivePath = join(dest, `${bundle}.tar.gz`);
    writeFileSync(archivePath, archive);
    extractTarGz(archivePath, dest);
    rmSync(archivePath, { force: true });
  } catch (err) {
    log(`error: extraction failed: ${err.message}`);
    return HARD_FAILURE;
  }

  if (!existsSync(join(bundleDir, "bindings.rs")) || !existsSync(join(bundleDir, "lib"))) {
    log(`error: extracted bundle is malformed: ${bundleDir}`);
    return HARD_FAILURE;
  }

  exportEnv("ZVEC_LLAMA_PREBUILT_DIR", bundleDir);
  setOutput("prebuilt", "true");
  log(`using prebuilt llama.cpp for ${target}/${variant}: ${bundleDir}`);
  // Print the path on stdout so it can be captured manually if needed.
  process.stdout.write(`${bundleDir}\n`);
  return 0;
}

main().then(
  (code) => process.exit(code),
  (err) => {
    log(`error: ${err && err.stack ? err.stack : err}`);
    process.exit(HARD_FAILURE);
  },
);
