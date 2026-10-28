# Phonon CPU + Video

A standalone container adapter for [Fermion Research Phonon](https://github.com/fermionresearch/phonon). FFmpeg inside the image converts local video and audio to mono 16 kHz PCM WAV. The original CPU engine performs transcription. No GPU or host FFmpeg is required.

**Development status:** local implementation, not a published release. Image references below use a local build. Real speech inference and native ARM64 runtime acceptance must pass before release. See `PHONON_CPU_VIDEO_PLAN.md` for decisions and remaining gates.

## Build

```sh
docker build -t phonon-cpu-video:local .
```

The base is `ghcr.io/fermionresearch/phonon-cpu:2.0.12`, pinned to the verified multi-platform index digest in `Dockerfile`. Runtime user is the upstream `phonon` user. Wrapper dependencies use a separate virtual environment and a hash-locked requirements export. Models are not baked into the image.

Supported container platforms are `linux/amd64` and `linux/arm64`. A successful build is not proof of inference support. CPU instruction requirements and model licenses remain those of [upstream](https://github.com/fermionresearch/phonon/blob/main/docs/cpu.md).

## CLI

```sh
docker run --rm \
  -v "$PWD:/data:ro" \
  -v phonon-cache:/home/phonon/.cache \
  phonon-cpu-video:local \
  transcribe phonon-2.5 /data/meeting.mp4 --json
```

The adapter accepts `transcribe MODEL FILE [upstream options]`. It converts to a private temporary WAV, invokes the original entrypoint, preserves stdout and exit status, then removes temporary files. Conversion diagnostics go to stderr. Supported inputs are WAV, MP3, FLAC, Ogg, MP4/MOV, Matroska/WebM, and AVI, based on content, not extension. The first audio stream is selected. Video with no audio and unknown/nonfinite duration are rejected. WAV is normalized as well, so all supported inputs take the same validated path.

Commands other than `transcribe` and `serve-video` delegate to the original entrypoint. Native `serve` remains unchanged, including upstream streaming support and authentication behavior. Use native `serve` only with deployment controls appropriate to upstream.

The first inference run can download model weights. Mount the cache for persistence. For an offline model, pass an upstream-compatible model directory mounted into the container instead of a model name. This project does not redistribute weights.

## Authenticated HTTP

Set a strong bearer key outside source control:

```sh
read -r -s PHONON_VIDEO_API_KEY
export PHONON_VIDEO_API_KEY

docker run --rm -p 127.0.0.1:8000:8000 \
  -e PHONON_VIDEO_API_KEY \
  -v phonon-cache:/home/phonon/.cache \
  phonon-cpu-video:local serve-video phonon-2.5
```

```sh
curl --fail-with-body http://127.0.0.1:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer $PHONON_VIDEO_API_KEY" \
  -F 'model=phonon-2.5' \
  -F 'file=@meeting.mp4' \
  -F 'response_format=json'
```

`serve-video MODEL` runs one upstream model on `127.0.0.1:8001` and exposes the adapter on port 8000. Supported server options are `--host`, `--port`, `--api-key`, and `--threads`. Prefer the environment variable for the key to avoid shell history. The request `model` must exactly match the startup model string. There is no per-request model loading or automatic alias switching.

- `POST /v1/audio/transcriptions`: multipart with exactly one `file`, a `model`, and optional upstream text fields
- `GET /health`: 200 only when the adapter and upstream worker are ready; otherwise 503
- No proxy route for upstream streaming, model listing, or arbitrary paths

Text fields include `response_format` (`json`, `text`, `verbose_json`), `hotwords`, `hotwords[]`, `hotword_lambda`, `prompt`, `timestamp_granularities`, `timestamp_granularities[]`, `language`, and `temperature`. Unsupported semantics remain upstream's responsibility. Upstream is English-only and deterministic; accepting these last two fields does not add translation or sampling support. Repeated list fields are preserved; duplicate scalar fields are rejected. The adapter returns upstream status, content type, and bounded body.

Authentication occurs before body processing. Excess work is rejected with 429 rather than queued. Upload limit errors return 413; invalid media returns 422. Worker failure returns 5xx. If inference times out or a client disconnects during inference, the worker is stopped before capacity is reused, because closing an HTTP connection does not cancel upstream inference. Worker restart may take time. Check `/health` before retrying.

For Compose, export the key and run `docker compose up --build`. The example binds only host loopback. Put a TLS-enabled reverse proxy in front for remote access. Do not expose upstream port 8001.

## Limits

Each `Settings` field can be overridden with `PHONON_VIDEO_` plus its uppercase field name. Invalid, nonpositive, or nonfinite values fail startup.

| Variable suffix | Default | Meaning |
|---|---:|---|
| `MAX_UPLOAD_BYTES` | 104857600 | Total received multipart request bytes and maximum CLI input size |
| `MAX_DURATION` | 600 | Media duration in seconds |
| `PROBE_TIMEOUT` | 30 | Probe deadline in seconds |
| `CONVERSION_TIMEOUT` | 120 | Conversion deadline in seconds |
| `INFERENCE_TIMEOUT` | 900 | HTTP inference deadline in seconds |
| `UPLOAD_TIMEOUT` | 120 | Entire upload deadline in seconds |
| `MAX_CONCURRENCY` | 1 | Active requests, including upload and conversion; no waiting queue |
| `SCRATCH_BUDGET_BYTES` | 268435456 | Admission budget for uploaded input, immutable input copy, and decoded WAV |
| `SCRATCH_DIR` | `/tmp/phonon-video` | Private request directories |
| `MAX_TEXT_FIELDS` | 16 | Text field count |
| `MAX_TEXT_FIELD_BYTES` | 4096 | Per-field UTF-8 byte limit |

Decoded output is independently capped at `ceil(MAX_DURATION * 32000) + 65536` bytes. Startup requires scratch budget for `MAX_CONCURRENCY * (2 * MAX_UPLOAD_BYTES + decoded_output_limit)` and sufficient free space. This admission accounting is not an OS filesystem quota. Give the scratch mount a size limit, allow filesystem overhead, and do not share it with unrelated writers. Raising concurrency increases worst-case disk and CPU use and does not make the single upstream engine parallel.

## Media security and deployment

Only explicitly detected demuxers and local-file input protocols are permitted. Playlists are rejected. MOV external data references are rejected and disabled. User filenames never become shell commands or scratch paths. Probe and conversion have bounded output and process deadlines.

**These checks are not an OS sandbox and cannot prevent exploitation of a decoder vulnerability.** Do not process hostile media in a privileged container. Use a maintained container runtime, non-root user, dropped capabilities, `no-new-privileges`, limited memory/PIDs/CPU, read-only input mounts, bounded scratch, and restricted network egress. Do not mount secrets or sensitive host directories. Use a network-isolated deployment with preloaded models for untrusted inputs. The normal connected mode needs egress for first-run model downloads. The Compose example is a baseline, not a complete multi-tenant security boundary.

## Development checks

Python 3.12 or newer, FFmpeg/FFprobe, and [uv](https://docs.astral.sh/uv/) are required for host tests. Tests generate synthetic fixtures locally; they do not download models.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python -m compileall -q app scripts
uv run pip-audit --disable-pip -r requirements.lock
scripts/smoke-test.sh phonon-cpu-video:local
```

After a dependency change, update both lock files:

```sh
uv lock
uv export --locked --no-dev --no-emit-project \
  --format requirements-txt --output-file requirements.lock
```

CI runs tests and Docker smoke checks on native amd64 and arm64. Synthetic media tests prove conversion and proxy behavior, not recognition quality. Real model tests are a separate release gate.

## Release procedure

Do not publish until the owner selects this project's license and completes the third-party distribution review described in `NOTICE`.

1. Create the remote repository in the approved account. Configure main-branch review requirements, required CI, release tag rules, and a protected `release` environment with required reviewers. Reviewers must check code licensing and FFmpeg/model obligations.
2. Merge reviewed code with passing CI. Create a semantic version tag on that commit.
3. Supply the manual publish workflow with a licensed spoken WAV fixture URL/hash, expected words, and an offline model archive URL/hash. Preserve source, license, model revision, hashes, download budget, and expected transcript in release records. Do not use signed URLs or secrets in public workflow inputs.
4. The workflow builds candidate images, scans them (high/critical findings block release), and runs real CLI and HTTP inference on native amd64 and arm64. The model archive hash pins the exact tested weights. No release is valid with build-only or emulated runtime coverage.
5. Approve promotion only after all checks pass. Promote the exact tested manifest without rebuilding. Record the release digest and test/scan evidence. No `latest` tag is automatically published.

### Offline cache archive layout

The release helper expects the **contents** of `/home/phonon/.cache` at the archive root, not a parent `cache/` directory. Prepare a complete cache with the selected model revision on a trusted machine. Hugging Face caches often contain symlinks; dereference them when archiving:

```sh
tar --dereference -C /path/to/complete-cache -czf model-cache.tar.gz .
sha256sum model-cache.tar.gz licensed-speech.wav
```

The helper rejects symlinks, hardlinks, special files, duplicate entries, traversal, and archives above its member/expanded-byte limits. Test that the cache works with network access disabled before dispatch. Do not include tokens or unrelated cache files. The disposable extracted cache is writable by container UID 1001. Record model repository/revision and asset licenses in release records. URLs must be public HTTPS without embedded credentials; workflow inputs and evidence are not secret storage.

GitHub/GHCR creation and publishing are not performed by local setup. `GITHUB_TOKEN` package permissions should be scoped to the release job. PR jobs must never receive package write permissions.

## Attribution and license status

This adapter is independent of Fermion Research and does not claim upstream endorsement. See `NOTICE`. No license for the new adapter code has been selected yet, so redistribution of a combined image is blocked pending owner choice and third-party review.
