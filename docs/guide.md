# User and release guide

See [README.md](../README.md) for the project overview and credits.

## Build

```sh
docker build -t phonon-cpu-video:local .
```

The base is `ghcr.io/fermionresearch/phonon-cpu:2.0.12`, pinned to the verified multi-platform index digest in `Dockerfile`. The runtime user is the upstream `phonon` user. Adapter dependencies use a separate virtual environment and a hash-locked requirements export. The image does not contain model weights.

Supported container platforms are `linux/amd64` and `linux/arm64`. A successful build is not proof of inference support. CPU instruction requirements and model licenses remain those of [upstream](https://github.com/fermionresearch/phonon/blob/main/docs/cpu.md).

## CLI

```sh
docker run --rm \
  -v "$PWD:/data:ro" \
  -v phonon-cache:/home/phonon/.cache \
  phonon-cpu-video:local \
  transcribe phonon-2.5 /data/meeting.mp4 --json
```

The adapter accepts `transcribe MODEL FILE [upstream options]`. It converts the input to a private temporary WAV. It then invokes the original entrypoint. Without adapter output options, it preserves native stdout, stderr, arguments, and exit status. It removes temporary files after the command. Conversion diagnostics go to stderr. Supported inputs are WAV, MP3, FLAC, Ogg, MP4/MOV, Matroska/WebM, and AVI. The adapter uses content, not the file extension, to identify the format. The first audio stream is selected. Video with no audio and unknown/nonfinite duration are rejected. The adapter also normalizes WAV. All supported inputs use the same validated path.

Commands other than `transcribe` and `serve-video` delegate to the original entrypoint. Native `serve` remains unchanged, including upstream streaming support and authentication behavior. Use native `serve` only with deployment controls appropriate to upstream.

The first inference run can download model weights. Mount the cache for persistence. For an offline model, pass an upstream-compatible model directory mounted into the container instead of a model name. This project does not redistribute model weights.

### Transcript exports and output files

Adapter options follow `MODEL FILE`:

| Option | Behavior |
|---|---|
| `--output-format txt\|srt\|vtt\|json` | Select UTF-8 transcript output explicitly |
| `--segment-duration SECONDS` | Group complete timed segments toward a finite positive target |
| `--output PATH` | Publish a complete file without overwriting an existing destination |

Without these options, native output remains unchanged. `--segment-duration` alone selects TXT. With `--json` or `--output-format json`, grouping adds JSON output records instead. `--json` conflicts with an explicit non-JSON format. Adapter options cannot be repeated; unrelated upstream options keep their order.

For stdout subtitles:

```sh
docker run --rm \
  -v "$PWD:/data:ro" \
  -v phonon-cache:/home/phonon/.cache \
  phonon-cpu-video:local \
  transcribe phonon-2.5 /data/meeting.mp4 --output-format srt
```

File output needs a separate writable mount because `/data` remains read-only. Create `results` on the host first and grant the container's `phonon` user (UID 1001) write access through ownership or an ACL. The adapter does not create parent directories.

```sh
mkdir -p results
# Set ownership or an ACL for UID 1001 before running the container.
docker run --rm \
  -v "$PWD:/data:ro" \
  -v "$PWD/results:/results" \
  -v phonon-cache:/home/phonon/.cache \
  phonon-cpu-video:local \
  transcribe phonon-2.5 /data/meeting.mp4 \
  --output-format vtt --segment-duration 30 --output /results/meeting.vtt
```

- `--output` alone preserves native plain text, or native JSON with `--json`. A filename extension never selects the format. Use `--output-format json --output /results/meeting.json` to request JSON explicitly. `--output -` writes to stdout.
- File output produces no transcript stdout. Diagnostics and upstream progress remain on stderr. Existing destinations, including symlinks and the source itself, are refused. There is no overwrite option.
- The adapter stages the result beside the destination and publishes with an atomic no-clobber hard link after inference and validation succeed. This needs a filesystem that supports hard links. It never falls back to truncating or replacing a destination. A competing file created during inference is also preserved.
- Failures or cancellation before publication leave no final file from this invocation. After publication, the complete file remains even if staging cleanup fails; a cleanup warning goes to stderr. A post-publication signal can return a signal-derived status even though the complete output exists.
- Managed capture and rendered output each have a 4 MiB UTF-8 byte limit. Malformed, oversized, or invalidly timed transcript output returns CLI status 1 without publishing a result. Invalid adapter options return status 2. Native upstream failures keep their exit status. Stdout is not transactional: a broken pipe or interruption during the final write can leave partial consumer output. Use file output for atomic publication.

### Grouping and subtitle limits

Grouping changes presentation, not recognition or audio processing. For a target of 30 seconds, the adapter adds whole source segments until the elapsed group span reaches 30 seconds. It emits a shorter group before a silence gap of at least 30 seconds. The final group can be shorter. An individual source segment longer than the target remains intact.

This is neither exact-duration splitting nor a maximum-duration promise. Native timestamps can describe coarse decoder windows. A small target cannot create finer timing. Long groups can be unsuitable for readable on-screen subtitles. The adapter does not add word alignment, speakers, translation, line-width rules, or recognition corrections.

- Ungrouped TXT uses the native top-level text. Grouped TXT uses one paragraph per output group, separated by a blank line. Nonempty managed TXT ends in one newline.
- SRT uses numbered cues and comma-separated milliseconds. VTT starts with `WEBVTT` and uses dot-separated milliseconds. Without a target, each nonblank source segment is a cue.
- Subtitle payload whitespace is collapsed, NUL becomes U+FFFD, and `&`, `<`, and `>` are escaped as literal text. Times use half-up millisecond rounding. Missing, overlapping, out-of-order, nonfinite, or rounded zero-length timings cause an error rather than invented alignment.
- Grouped CLI JSON and HTTP verbose JSON retain native `segments`, `segment_count`, word arrays, and model metadata. They add `output_segments` and `output_segment_count`. Each output segment has a zero-based `id`, `start`, `end`, and `text`. Output groups are not decoder segments. Native transcript whitespace stays unchanged; grouped text joins stripped segment text with a single space.

## Local HTTP

No API key is needed for local personal use:

```sh
docker run --rm -p 127.0.0.1:8000:8000 \
  -v phonon-cache:/home/phonon/.cache \
  phonon-cpu-video:local serve-video phonon-2.5
```

```sh
curl --fail-with-body http://127.0.0.1:8000/v1/audio/transcriptions \
  -F 'model=phonon-2.5' \
  -F 'file=@meeting.mp4' \
  -F 'response_format=json'
```

`serve-video MODEL` runs one upstream worker with one model on `127.0.0.1:8001`. It exposes the adapter on port 8000. Supported server options are `--host`, `--port`, `--api-key`, and `--threads`. Authentication is off unless you set `PHONON_VIDEO_API_KEY` or `--api-key`. If you enable it, send `Authorization: Bearer YOUR_KEY` with requests. Prefer the environment variable for the key to avoid shell history. The request `model` must exactly match the startup model string. There is no per-request model loading or automatic alias switching.

- `POST /v1/audio/transcriptions`: multipart with exactly one `file`, a `model`, and optional upstream text fields
- `GET /health`: 200 only when the adapter and upstream worker are ready; otherwise 503
- No proxy route for upstream streaming, model listing, or arbitrary paths

Text fields include `response_format` (`json`, `text`, `verbose_json`, `txt`, `srt`, `vtt`), local `segment_duration`, `hotwords`, `hotwords[]`, `hotword_lambda`, `prompt`, `timestamp_granularities`, `timestamp_granularities[]`, `language`, and `temperature`. Unsupported semantics remain upstream's responsibility. The upstream worker is English-only and deterministic. Accepting `language` and `temperature` does not add translation or sampling support. The adapter preserves repeated list fields. It rejects duplicate scalar fields. Without new formats or grouping, the adapter returns upstream status, content type, and bounded body unchanged.

### HTTP exports

```sh
curl --fail-with-body http://127.0.0.1:8000/v1/audio/transcriptions \
  -F 'model=phonon-2.5' \
  -F 'file=@meeting.mp4' \
  -F 'response_format=srt' \
  -F 'segment_duration=30' \
  -o transcript.srt
```

`segment_duration` is an optional scalar finite positive number of seconds. The adapter consumes it locally, not as an inference parameter. `txt` aliases `text`. SRT/VTT and all grouping requests acquire `verbose_json` internally for timing. Word timestamps are not required. The endpoint does not accept an output path or write client-selected server files; `curl -o` writes on the client.

| Format | Response content type |
|---|---|
| `text`, `txt` | `text/plain; charset=utf-8` |
| `srt` | `application/x-subrip; charset=utf-8` |
| `vtt` | `text/vtt; charset=utf-8` |
| `json`, `verbose_json` | `application/json` |

Grouped ordinary `json` returns only `text`, `output_segments`, and `output_segment_count`, not the internal verbose metadata. Grouped `verbose_json` retains all native fields and adds those output records. Grouping and subtitle limits are the same as the CLI rules above.

Invalid formats, invalid durations, or duplicate scalar options return 400 before conversion and inference. Invalid transcript structure/timing or output over 4 MiB returns a generic 502. A formatting failure releases capacity and cleans scratch files but does not restart a healthy worker. Non-success upstream status, body, and content type pass through without export processing.

When enabled, authentication occurs before body processing. Excess work is rejected with 429 rather than queued. Upload limit errors return 413. Invalid media returns 422. Upstream worker failure returns 5xx. If inference times out or a client disconnects during inference, the adapter stops the upstream worker before it reuses capacity. Closing an HTTP connection does not cancel upstream inference. The upstream worker can take time to restart. Check `/health` before retrying.

For Compose, run `docker compose up --build`. No key setup is needed. The example binds only host loopback. Do not expose the unauthenticated service on a public interface. If you later need remote access, enable authentication and put a TLS-enabled reverse proxy in front. Do not expose upstream port 8001.

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

Decoded output is independently capped at `ceil(MAX_DURATION * 32000) + 65536` bytes. Startup requires a scratch space budget for `MAX_CONCURRENCY * (2 * MAX_UPLOAD_BYTES + decoded_output_limit)` and sufficient free space. This admission accounting is not an OS filesystem quota. Give the scratch space mount a size limit. Allow filesystem overhead. Do not share the mount with unrelated writers. Raising concurrency increases worst-case disk and CPU use. It does not make the single upstream worker parallel.

## Media security and deployment

Only explicitly detected demuxers and local-file input protocols are permitted. Playlists are rejected. MOV external data references are rejected and disabled. User filenames never become shell commands or scratch space paths. Probe and conversion have bounded output and process deadlines.

**These checks are not an OS sandbox and cannot prevent exploitation of a decoder vulnerability.** Do not process hostile media in a privileged container. Use a maintained container runtime, non-root user, dropped capabilities, `no-new-privileges`, limited memory/PIDs/CPU, read-only input mounts, bounded scratch space, and restricted network egress. Do not mount secrets or sensitive host directories. For untrusted inputs, use a network-isolated deployment with preloaded model weights. The normal connected mode needs egress to download model weights on the first run. The Compose example is a baseline, not a complete multi-tenant security boundary.

## Development checks

Python 3.12 or newer, FFmpeg/FFprobe, and [uv](https://docs.astral.sh/uv/) are required for host tests. Tests generate synthetic fixtures locally. They do not download model weights.

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

The adapter uses Apache-2.0. Complete the third-party distribution review described in `NOTICE` before publishing a combined image.

1. Create the remote repository in the approved account. Configure main-branch review requirements, required CI, release tag rules, and a protected `release` environment with required reviewers. Reviewers must check Apache-2.0 notices and FFmpeg/model obligations.
2. Merge reviewed code with passing CI. Create a semantic version tag on that commit.
3. Supply the manual publish workflow with a licensed spoken WAV fixture URL/hash, expected words, and an offline model archive URL/hash. Preserve source, license, model revision, hashes, download budget, and expected transcript in release records. Do not use signed URLs or secrets in public workflow inputs.
4. The workflow builds candidate images, scans them (high/critical findings block release), and runs real CLI and HTTP inference on native amd64 and arm64. The model archive hash pins the exact tested model weights. No release is valid with build-only or emulated runtime coverage.
5. Approve promotion only after all checks pass. Promote the exact tested manifest without rebuilding. Record the release digest and test/scan evidence. No `latest` tag is automatically published.

### Offline cache archive layout

The release helper expects the **contents** of `/home/phonon/.cache` at the archive root, not a parent `cache/` directory. Prepare a complete cache with the selected model revision on a trusted machine. Hugging Face caches often contain symlinks. Dereference them when archiving:

```sh
tar --dereference -C /path/to/complete-cache -czf model-cache.tar.gz .
sha256sum model-cache.tar.gz licensed-speech.wav
```

The helper rejects symlinks, hardlinks, special files, duplicate entries, traversal, and archives above its member/expanded-byte limits. Test that the cache works with network access disabled before dispatch. Do not include tokens or unrelated cache files. The disposable extracted cache is writable by container UID 1001. Record model repository/revision and asset licenses in release records. URLs must be public HTTPS without embedded credentials. Workflow inputs and evidence are not secret storage.

GitHub/GHCR creation and publishing are not performed by local setup. `GITHUB_TOKEN` package permissions should be scoped to the release job. PR jobs must never receive package write permissions.

## Attribution and license status

This adapter is independent of Fermion Research and does not claim upstream endorsement. The adapter code is licensed under [Apache-2.0](../LICENSE). See `NOTICE` for attribution and third-party software/model requirements. Combined-image distribution still requires the third-party review.
