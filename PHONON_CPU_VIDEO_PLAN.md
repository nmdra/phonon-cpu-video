# Plan: Phonon CPU + Video Support

**Status:** Local implementation approved. User approved creating public `nmdra/phonon-cpu-video` and pushing source. Image publication still requires release gates.
**Repository:** standalone `phonon-cpu-video`, not a fork.
**Platforms:** linux/amd64 and linux/arm64. CPU only.

## Goal

Extend the official Phonon CPU container with FFmpeg so a user can transcribe local video through the CLI or multipart HTTP without host FFmpeg. Preserve native upstream commands and model caching. Do not implement inference, GPU support, URL downloads, streaming, subtitles, translation, or diarization.

## Current State and Evidence

The initial directory contains only this plan, without a Git repository or application.

- Upstream CPU docs name the `2.0.12` image, amd64/arm64, explicit model arguments, 16 kHz audio, and `/home/phonon/.cache`: https://github.com/fermionresearch/phonon/blob/main/docs/cpu.md
- Current source uses Python 3.12 slim, user `phonon` (1001), `/usr/local/bin/phonon-entrypoint`, `fermion`, and CPU torch: https://github.com/fermionresearch/phonon/blob/main/docker-cpu-phonon2/Dockerfile
- Current entrypoint delegates to `fermion` and defaults the served model alias for native `serve`: https://github.com/fermionresearch/phonon/blob/main/docker-cpu-phonon2/entrypoint.sh
- Registry inspection verified `2.0.12` index digest `sha256:fcfd06c83e5355b2d06c21084f9f3579b30296298906c0addd78e9938cb1b8f6`, with amd64 digest `sha256:682f8bdd5ca120e17cb13fe2485955e8989b556ca903fd830da4c5af95d30442` and arm64 digest `sha256:49410210ea65a4cbb9f58b2f031e6fe7024283cae16133ac425e3c33cc9839a6` using `docker buildx imagetools inspect`.
- Downloaded published `fermion-research==0.2.11` wheel for contract inspection, without model weights. Its `fermion/server.py` defines one startup model, bearer authentication, multipart transcription, and model cross-checking. Actual pinned amd64 image inspection reports `fermion-research==0.2.15`, user/group 1001, and confirms the startup flags and routes. Source on main and wheel 0.2.11 are not the image contract.

## Decisions

- Pin the base image index digest. Install distro FFmpeg as root during build, restore user 1001 at runtime. Preserve upstream entrypoint for commands other than `transcribe` and `serve-video`.
- Implement Python wrapper with a separate virtual environment to avoid changing inference dependencies. Record exact runtime dependency versions in a lock file.
- CLI contract: `transcribe MODEL FILE [upstream options]`. Normalize supported local media to mono 16 kHz PCM WAV. Never write conversion logs to stdout. Keep the original file unchanged. Preserve upstream exit status and forward termination signals.
- HTTP contract: `serve-video MODEL [--host HOST] [--port PORT] [--api-key KEY] [--threads N]`. Run one upstream model on loopback port 8001 with matching served-model name. Requests require the same `model`; arbitrary per-request model loading is not supported. Forward validated text fields including response format and hotwords; preserve upstream status, content type, and body. Native `serve` remains separate.
- Per owner request, local personal HTTP use needs no API key. Optional `PHONON_VIDEO_API_KEY` or `--api-key` enables authentication before parsing or storing uploads. Bind examples to host loopback. Do not expose unauthenticated service remotely; TLS termination belongs to a deployment proxy.
- Accept only known self-contained WAV, MP3, FLAC, Ogg, MP4/MOV, Matroska/WebM, and AVI containers. Force a detected allowed demuxer for both probe and decode; restrict input protocols, disallow external MOV data references, reject playlists. No arbitrary format auto-detection. This is decoder hardening, not a claim of protection against FFmpeg vulnerabilities. Document OS/container isolation requirements and test external references.
- Reject unknown or nonfinite duration, excess duration, missing audio, and malformed media. Select the first audio stream explicitly. Use argument arrays, private generated paths, bounded subprocess output, execution deadlines, process-group termination, and output size limits.
- HTTP defaults: 100 MiB total request, 600 seconds media, 30 seconds probe, 120 seconds conversion, 900 seconds inference, 120 seconds upload, one active request, zero waiting requests, at most 16 text fields of 4 KiB each. Reject excess concurrency with 429 before reading body. Reserve two worst-case input copies plus decoded output scratch per admitted request (256 MiB default budget); fail startup if configured scratch budget cannot support configured concurrency. Count received bytes, not only Content-Length. Limit decoded output independently of reported duration.
- Timeout during inference makes the local worker unhealthy and stops/restarts it before capacity is reused, since upstream cancellation is not guaranteed. Cleanup must not imply inference was cancelled.
- Candidate images may be pushed for testing. Promote the exact tested manifest to version tags only after native amd64/arm64 runtime checks pass. No rebuild during promotion. Restrict release to validated semantic version tags on reviewed main commits with passing CI. Do not publish in PR workflows.

## Tasks

- [x] 1. Conversion and limits. **Files:** `app/settings.py`, `app/conversion.py`, `tests/test_conversion.py`, `tests/test_settings.py`. **Seam:** local synthetic FFmpeg media and subprocess boundary. **Verify:** pytest conversion/settings tests with WAV, MP3, MP4, MKV, WebM, invalid input, missing audio, playlists, duration/output limits and cleanup.
- [x] 2. CLI and process supervision. **Files:** `app/cli.py`, `app/__main__.py`, `app/__init__.py`, `scripts/entrypoint.sh`, `tests/test_cli.py`. **Seam:** fake upstream executable plus real conversion. **Verify:** pytest CLI tests for argument preservation, exit code, stdout JSON, cleanup and native command delegation.
- [x] 3. HTTP ingress. **Files:** `app/api.py`, `app/lifecycle.py`, `tests/test_api.py`. **Seam:** in-process ASGI client with fake loopback upstream and real conversion where practical. **Verify:** authenticated audio/video success, authentication before body read, bounded multipart fields and uploads, overload, model mismatch, upstream status, health, timeout, disconnect, cleanup and shutdown tests.
- [x] 4. Reproducible packaging. **Files:** `pyproject.toml`, `uv.lock`, `Dockerfile`, `.dockerignore`, `.gitignore`, `compose.yaml`, `scripts/smoke-test.sh`. **Seam:** built container with inherited runtime. **Verify:** locked install, full pytest, compile/lint, Docker build and no-model smoke tests. Inspect actual image CLI, packages and user. Model downloads require approval.
- [x] 5. CI and staged release workflows (implemented and locally validated; hosted execution pending). **Files:** `.github/workflows/ci.yml`, `.github/workflows/publish.yml`, `.github/dependabot.yml`. **Seam:** GitHub Actions jobs. **Verify:** parse workflow YAML, pin Actions SHAs, tests and image smoke on native amd64/arm64, protected/manual candidate verification and digest-preserving promotion. Require real model inference acceptance before release promotion.
- [x] 6. User documentation. **Files:** `README.md`, `NOTICE`, `CHANGELOG.md`. **Seam:** documented commands and configuration match implementation. **Verify:** CLI help, limits table, local-only examples with no key and optional authentication, model cache, compatibility boundaries, decoder isolation guidance, architecture matrix and release gates. Owner selected Apache-2.0; include LICENSE and preserve upstream notices. Combined-image third-party review remains a release gate.

## Verification and Release Gates

Local checks: locked dependency installation, unit/integration tests with generated synthetic fixtures, lint, compile, active diagnostics, Docker build and conversion/native-command smoke tests where resources permit. Synthetic audio proves media handling, not speech recognition.

Release checks on **both native amd64 and arm64**: image user and CLI, WAV and video conversion, authenticated HTTP proxy, cleanup/termination, and real CLI + HTTP transcription with a licensed spoken fixture and a selected/pinned model (`phonon-2.5` unless discovery requires a documented change). Model revisions, fixture source/hash, expected words, download budget, and timeouts must be recorded before release. Build-only or emulated results do not meet release runtime gates.

Scan image/dependencies and review findings before promotion. Verify model cache persistence and read-only source mounts. Record tested image digests and scan/inference evidence. Only then promote the exact candidate manifest. Initial combined CLI/API release targets v0.2.0; no automatic latest tag before stable v1.

## External Gates and Open Decisions

- User approved public GitHub repository `nmdra/phonon-cpu-video` and source push. Rulesets, protected release environment, and package permissions remain external setup. Do not publish images during source push.
- Owner selected Apache-2.0 for adapter code. Verify upstream image/package/model licenses and FFmpeg distribution obligations before image release; adapter licensing does not replace third-party terms.
- Real inference downloads and native ARM64 availability require separate approval/resources. Report these checks as pending, not passed.
- Pinned amd64 image inspected; real model inference and native ARM64 execution remain pending.

## Local Verification Record

- Full suite: 128 tests passed, no model downloads.
- Ruff lint and format checks, actionlint, compile checks, locked dependency check, and Compose validation passed.
- CLI Pyright check: zero errors/warnings; active LSP checks for changed application files: zero diagnostics.
- Built `phonon-cpu-video:local` on amd64 and passed network-disabled no-model smoke checks with WAV and an MP4 containing audio plus synthetic video.
- Runtime dependencies: pip-audit reported no known vulnerabilities. Full image scanning is configured in release CI, not run locally.
- Independent reviews covered conversion, worker lifecycle, CLI, and release workflows. Fixed descendant cleanup, concurrent worker recovery, disconnect handling, host smoke invocation, and real video fixture generation.
- No model inference, ARM64 runtime acceptance, image publication, or release approval has been claimed.
