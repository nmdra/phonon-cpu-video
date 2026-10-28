# Plan: Transcript export, output grouping, and output files

## Goal

Add TXT, SRT, and WebVTT export and optional target-duration output grouping to both CLI and HTTP transcription. Add a CLI output-file option. Keep recognition, model loading, media conversion, and existing commands unchanged unless a new option is used.

This is an implementation plan only. It does not authorize a release, model downloads, or inference runs.

## Current State and Evidence

- CLI `transcribe MODEL FILE [upstream options]` prepares a WAV, starts the original entrypoint, inherits stdout/stderr, forwards shutdown signals, and returns the child status. Other native commands delegate unchanged: `app/cli.py:26-99,102-118`.
- The installed upstream distribution is `fermion-research 0.2.15` in `phonon-cpu-video:local`, whose base is pinned in `Dockerfile:2`. Source inspection, not an inference run, established these contracts:
  - `/usr/local/lib/python3.12/site-packages/fermion/transcribe.py:308-328`: CLI `--json` returns `text`, model/profile/backend/engine metadata, timing fields, `segment_count`, `segments`, `words`, and `truncated`. Segments contain `id`, `start`, `end`, and `text`; word records use `text`. Without `--json`, CLI prints plain text.
  - `/usr/local/lib/python3.12/site-packages/fermion/server.py:974-982,1025-1074`: HTTP supports `json`, `text`, and `verbose_json`. Default `json` contains only `text`. `verbose_json` supplies timed `segments` and `x_fermion` metadata. Optional word records use `word`, not CLI's `text` key.
  - `/usr/local/lib/python3.12/site-packages/fermion/_speech/cues.py:194-208`: HTTP verbose segments also contain OpenAI-style fields such as `seek`, `tokens`, and confidence placeholders.
  - `/usr/local/lib/python3.12/site-packages/fermion/_speech/segment.py:57-70,305-315`: without word timing support, segment bounds can be coarse decoded-window bounds. Grouping must not promise fine-grained alignment.
- HTTP accepts a fixed multipart field allowlist, preserves repeated list fields, and currently forwards upstream responses without transcript processing: `app/api.py:25-38,202-280`.
- HTTP inference responses have a 4 MiB body limit: `app/lifecycle.py:14-27,118-140`. Media and upload bounds are separate: `app/settings.py:10-60`.
- Existing high-level test seams are `cli.main` with `fake_upstream` (`tests/test_cli.py:30-79`) and `create_app` with `FakeWorker`, `run_case`, and `post` (`tests/test_api.py:15-78`). They require no models or network. Signal and response-bound regression coverage already exists in `tests/test_cli.py:158-247` and `tests/test_api.py:349-390`.
- Native amd64 and arm64 CI runs pytest, Ruff, and network-disabled Docker smoke checks: `.github/workflows/ci.yml:14-62`.
- Format references:
  - [W3C WebVTT syntax](https://www.w3.org/TR/webvtt1/#syntax): UTF-8, `WEBVTT` header, ordered timed cues, dot-separated milliseconds, end later than start, and cue text syntax.
  - [Library of Congress SubRip description](https://www.loc.gov/preservation/digital/formats/fdd/fdd000569.shtml): numbered blocks, comma-separated milliseconds, timing arrow, text, and blank-line separators. This is a format description, not a normative SRT standard.

Container source paths above are evidence from the installed image. Recheck these contracts when changing the pinned upstream base.

## Decisions

### Interfaces and compatibility

The user selected CLI and HTTP support and approximate target-duration grouping.

CLI adapter-owned options, after `MODEL FILE`:

```text
--output-format {txt,srt,vtt,json}
--segment-duration SECONDS
--output PATH
```

- No new options: preserve existing stdout/stderr, argument forwarding, exit status, and signal behavior. Existing `--json` continues to work.
- `--output-format json` requests upstream CLI JSON. `--json` with that format is allowed and must not result in duplicate upstream flags. `--json` with explicit `txt`, `srt`, or `vtt` is a usage error before inference.
- `--segment-duration` alone selects TXT output. With `--json` or `--output-format json`, it adds grouped output records to JSON as described below.
- `--output` alone preserves the native choice: plain text normally, JSON with `--json`. It does not infer format from the filename extension. `--output -` means stdout.
- Consume only the three adapter-owned options; preserve all other upstream arguments and their order. Reject missing values and duplicate adapter-owned options. Extend `transcribe --help`; keep unrelated native command dispatch unchanged.
- Output is UTF-8. Managed TXT output ends in one newline when nonempty. Preserve inherited upstream stderr for progress and diagnostics. File output suppresses transcript stdout.

HTTP request contract:

```text
response_format = json | verbose_json | text | txt | srt | vtt
segment_duration = finite positive number of seconds, optional
```

- Existing requests without new formats or grouping keep current response behavior. Keep `text` as the existing format name; `txt` is an adapter-supported alias.
- Consume `segment_duration` locally. Never forward it as an upstream inference parameter. Do not accept an output path or write client-selected server files.
- SRT/VTT and any grouping request need `verbose_json` internally. Do not request word timestamps just to group segments. Preserve all unrelated supported request fields.
- For grouped `response_format=json`, return `text`, `output_segments`, and `output_segment_count`. Do not unexpectedly expose the internal verbose response as ordinary JSON.
- For grouped `verbose_json`, preserve all native fields and add `output_segments` and `output_segment_count`. For grouped CLI JSON, do the same. Native `segments`, `segment_count`, words, and `x_fermion` stay unchanged: output groups are not decoder segments.
- MIME types: TXT `text/plain; charset=utf-8`, SRT `application/x-subrip; charset=utf-8`, VTT `text/vtt; charset=utf-8`, JSON `application/json`. Preserve upstream non-success status, body, and content type without attempting export.

These are additive adapter contracts. Do not add an upstream-looking `--response-format` CLI flag: existing unknown arguments must still pass through.

### Shared transcript processing

Create a standard-library-only module, `app/transcript.py`, for shared parsing, validation, grouping, and serialization. No new third-party dependency or lock-file change is expected.

- Use an internal immutable cue type with `start`, `end`, and `text`. Accept both observed CLI JSON and HTTP verbose JSON at the boundary. Retain the original JSON mapping separately for metadata-preserving output.
- Validate JSON structure and text types. Timed operations require a segment list and finite numeric, non-Boolean timestamps with `0 <= start < end`. Require ordered, non-overlapping segments; reject invalid timing rather than sorting, clamping, or estimating it. Do not fabricate cues from total duration or word counts.
- No timed segments with nonempty transcript text is an export error for SRT/VTT or grouping. A genuine empty transcript with absent or empty segments is valid: empty TXT/SRT, `WEBVTT\n\n` for VTT, and empty output groups for grouped JSON. Blank-text segments do not create output cues.
- TXT without grouping uses top-level transcript text. Grouped TXT renders one paragraph per output group, separated by a blank line, without timestamp labels.
- Keep original transcript text and native metadata unchanged in JSON. Grouped record text joins stripped constituent segment text with a single space. Do not claim byte-identical reconstruction of original whitespace.
- Invalid/missing timestamp data is an adapter output error: CLI exit 1 with stderr only; HTTP 502 with a generic error. Invalid caller options are CLI exit 2 or HTTP 400. These errors do not restart a healthy HTTP inference worker.
- Normalize expected UTF-8/JSON decoding, excessive nesting (`RecursionError`), structure, and numeric-conversion failures into `TranscriptError`. This includes finite but unrepresentable timestamp values such as `1e308`. Catch `TranscriptError` before HTTP's multipart `ValueError`/`UnicodeError` handler and general worker-recovery handler. Do not expose upstream transcript content in errors.

### Target-duration grouping

Grouping changes presentation only. It does not split audio, rerun inference, split source segments, or estimate word positions.

Use this deterministic rule for a positive target `T`:

1. Start a group at the first nonblank source segment.
2. Append complete consecutive segments until `group.end - group.start >= T`, then emit the group.
3. Before appending, if the gap from the current group end to the next segment start is `>= T`, emit the current group first. Do not bridge a silence as long as the target.
4. Emit the final remaining group, even when shorter than `T`.
5. A source segment already longer than `T` remains one group. Targets smaller than upstream segment duration cannot produce finer boundaries.

Each group is `{id, start, end, text}`, with zero-based sequential IDs, the first constituent start, and the last constituent end. Without a target, subtitle export uses source segments as individual cues. Grouping preserves the outer source boundaries, not every internal cue boundary. It is not an exact-duration or maximum-duration promise.

### Subtitle rendering

- SRT: one-based consecutive cue numbers, `HH:MM:SS,mmm --> HH:MM:SS,mmm`, payload, and a blank line.
- VTT: `WEBVTT` and a blank line, then `HH:MM:SS.mmm --> HH:MM:SS.mmm`, payload, and a blank line. Cue identifiers are not needed.
- Convert seconds to integer milliseconds once with documented half-up rounding, including carries across seconds, minutes, and hours. Reject a cue whose rounded end is not later than its rounded start; do not invent timing.
- Preserve literal transcript content rather than interpreting it as subtitle markup. Collapse payload whitespace to one line, replace NUL with U+FFFD, and escape `&`, `<`, and `>` for subtitle payloads. This prevents embedded blank lines, timing arrows, or tags from creating cue structure. TXT and JSON are not subtitle-escaped.
- Do not add styling, speaker attribution, forced line-width rules, translation, or subtitle-quality claims. Approximate long groups may be unsuitable for readable on-screen subtitles; document that trade-off.

### Bounded capture and safe file output

- Preserve the existing uncaptured CLI path when no new option is used.
- Managed CLI export/grouping captures upstream JSON stdout while leaving stderr inherited. Output-file-only mode captures native stdout. Enforce a shared 4 MiB input/output transcript byte limit. Reject invalid JSON or an oversized result before emitting transcript output.
- Define the shared limit in `app/transcript.py` and use it as the unchanged default for `Worker.response_limit`. Enforce it again after HTTP serialization. An oversized managed CLI result returns 1; oversized HTTP export returns 502. Do not raise model/media limits as part of this feature.
- Drain CLI stdout incrementally using selectors while polling the child and preserving existing signal deadlines. Do not use unbounded `communicate()`, accumulate unbounded bytes, or block forever on a pipe held open by a descendant. On output-limit failure, stop the process group and clean up.
- `--output PATH` refuses an existing destination, including symlinks. Do not add an overwrite option in this change. Require an existing parent directory; do not create directories automatically.
- Stage file output in a private temporary file in the destination directory. Publish only after successful upstream completion, validation, serialization, and close. Use atomic no-clobber publication with `os.link` followed by removal of the staging file on supported Linux filesystems. Fail clearly if no-clobber publication is unsupported; do not fall back to truncating or replacing the destination.
- Check obvious destination errors before inference. Recheck atomically at publication to handle races. Never remove a competing destination or the original media. Remove only the staging file owned by this invocation on failure, cancellation, or a nonzero upstream exit.
- Successful `os.link` is the file-publication commit point. Before that point, failures and cancellation leave no final destination created by this invocation. After that point, retain the complete published file even if staging-file removal fails; report a stderr cleanup warning and keep a successful exit status. A signal received after publication may return its signal-derived status, with a diagnostic that the complete file was published. Never roll back by deleting the destination.
- Managed post-processing must check the recorded shutdown signal before emitting stdout and immediately before the publication commit point. A signal during parsing, rendering, or staging must prevent publication if received before commit and return `128 + signum`. Keep this check even after the inference child has exited. Protect the final signal check and atomic link from a signal-handler race, for example by masking shutdown signals across those two operations and treating deferred delivery as post-commit. Stdout is not transactional: a downstream pipe failure or interruption during the final write can leave partial consumer output; file output supplies atomic publication.
- Preserve nonzero upstream exit status. In managed modes, discard captured transcript bytes on upstream failure and leave no final output file. Diagnostic messages still go to stderr.

## Scope

In scope:

- Shared serializers and deterministic target-duration output grouping.
- CLI options, bounded stdout capture, safe output-file publication, and help.
- HTTP format/grouping integration and rendered-response bounds.
- No-model unit and integration tests, documentation, and an Unreleased changelog entry.

Out of scope:

- Audio chunking, exact-duration splitting, inference changes, word alignment, diarization, or recognition corrections.
- Browser UI, asynchronous jobs, batch processing, persistent HTTP result files, output-directory creation, or overwrite support.
- New CLI inference timeouts, unrelated resource-limit changes, base-image upgrades, vulnerability remediation, releases, or image publication.

## Tasks

- [x] **1. Implement shared transcript processing with focused tests.** Add immutable cue and output-option types, `TranscriptError`, observed-schema parsing, timestamp validation, the exact grouping algorithm above, grouped JSON augmentation, TXT/SRT/VTT serialization, and the shared 4 MiB byte limit. Keep native metadata separate from output groups. Include fixtures for both observed upstream shapes with explicit source provenance in test comments. Test Unicode, invalid UTF-8, deeply nested JSON, escaping, blank/empty content, coarse segments, silence gaps, tiny/invalid targets, Boolean/NaN/infinite and huge finite timestamps, out-of-order and overlapping segments, hour/minute rounding carries, sub-millisecond collapse, and unchanged metadata/word arrays. Use byte-for-byte format assertions, not snapshots generated by the code under test. **Files:** new `app/transcript.py`, new `tests/test_transcript.py`. **Seam:** pure parser/grouping/render functions. **Verify:** `uv run --frozen pytest tests/test_transcript.py`.

- [x] **2. Add managed CLI options and bounded output capture.** Extend `main`/`_transcribe` without replacing the legacy no-option path. Strip adapter-owned options, validate conflicts and destinations before media processing, request upstream JSON only when needed, and preserve unrelated argument order. Add bounded nonblocking stdout draining and reuse the current signal/process-group cleanup contract. Preserve upstream nonzero statuses and do not print partial managed output. Extend fake executables to emit valid CLI transcript JSON, malformed and oversized stdout, nonzero status, delayed output, and a descendant holding stdout open. Keep existing native delegation and signal tests passing. **Files:** `app/cli.py`, `tests/test_cli.py`. **Seam:** `cli.main` plus the existing fake upstream subprocess. **Verify:** `uv run --frozen pytest tests/test_cli.py tests/test_transcript.py`.

- [x] **3. Add safe CLI file output.** Implement destination staging and atomic no-clobber publication. Test plain/native JSON output-file-only mode, each explicit export format, `--output -`, Unicode/spaced paths, existing regular files, symlinks, input equal to output, a destination created during inference, read-only or missing parents, unsupported publication, malformed/oversized output, upstream failure, signals during inference and post-processing, and an injected staging-unlink failure after commit. Verify pre-commit signals prevent stdout emission/publication and post-commit cleanup failure preserves the complete destination. For permission tests, mock the failure or use a subprocess as needed so tests remain valid under root. Assert source and existing destination bytes stay unchanged, stdout stays empty for file output, and staging files are removed on every failure. **Files:** `app/cli.py`, `tests/test_cli.py`. **Seam:** `cli.main` and real temporary destination directories, with injected failure seams where necessary. **Verify:** `uv run --frozen pytest tests/test_cli.py`.

- [x] **4. Integrate HTTP export and grouping.** Add scalar `segment_duration` to the multipart allowlist. Validate supported new options before conversion/inference, map `txt` to text semantics, and request `verbose_json` internally only when timing data is needed. Never forward `segment_duration`. Preserve native no-option response behavior and upstream non-success responses. Render successful managed responses through the shared module and enforce both existing upstream and new serialized-output bounds. Catch transcript-format errors separately so they do not enter worker recovery. Test grouped JSON/verbose JSON metadata, TXT paragraphs, SRT/VTT MIME/body, invalid/duplicate options, missing timestamps, malformed JSON, output overflow, and a successful subsequent request after each formatting failure. Retain auth, capacity, timeout, cancellation, cleanup, and worker-recovery regression tests. **Files:** `app/api.py`, `app/lifecycle.py`, `tests/test_api.py`. **Seam:** `create_app` with `FakeWorker`/`run_case`; `httpx.MockTransport` for worker byte bounds. **Verify:** `uv run --frozen pytest tests/test_api.py tests/test_transcript.py`.

- [ ] **5. Document and run acceptance checks.** Update the CLI/HTTP contracts, grouping limitations, UTF-8 formats, no-clobber behavior, 4 MiB managed output limit, errors, and native-vs-output segment fields. Include writable `/results` Docker mount examples because `/data` remains read-only; include `curl -F response_format=srt -F segment_duration=30 -o transcript.srt`. State that JSON format is explicit and not inferred from output extension. Keep README short with a link to detailed examples. Add a Keep a Changelog Unreleased entry. Run focused tests and the full no-model suite; use existing native CI for architecture coverage rather than introducing a model requirement. **Files:** `docs/guide.md`, `README.md`, `CHANGELOG.md`. **Seam:** documented CLI/HTTP examples and existing CI. **Verify:** commands in Verification; confirm help and docs describe the same option names and defaults.

Use scoped Conventional Commits for independent completed tasks. Do not commit generated transcripts, model files, or test cache data. Do not publish a new image as part of implementation without a separate request.

## Verification

Run these during implementation, not as claims about this plan-only change:

```sh
uv run --frozen pytest tests/test_transcript.py tests/test_cli.py tests/test_api.py
uv run --frozen pytest
uv run --frozen ruff check app tests scripts/release-inference.py
uv run --frozen ruff format --check app tests scripts/release-inference.py
uv run --frozen python -m compileall -q app scripts
uv run --frozen pyright app
git diff --check
```

First confirm the available Pyright binary. Pyright is configured in `pyproject.toml`, but is not a declared development dependency; if unavailable, use the active pi-lens LSP probe on changed application files rather than installing a new tool without approval. Run pi-lens delta diagnostics after code edits and full application diagnostics before completion. Existing native CI supplies amd64/arm64 test and network-disabled Docker smoke coverage. A local image build is optional, not a requirement to download new base layers or models.

Acceptance criteria:

1. Existing CLI commands and HTTP requests without new options retain behavior and original regression tests pass.
2. CLI and HTTP produce equivalent TXT/SRT/VTT cue content from equivalent CLI/verbose JSON fixture data.
3. A 30-second target merges complete segments according to the documented algorithm; it neither splits a source segment nor bridges a silence of 30 seconds or more.
4. JSON grouping adds output records without altering native segment counts, timings, words, or model metadata.
5. File output produces complete UTF-8 content with no transcript stdout. Pre-commit failure leaves no final file created by this invocation and does not overwrite a competing file. Cleanup removes staging and media scratch files on normal failure paths. A post-commit cleanup error keeps the complete final file and emits a warning rather than attempting rollback.
6. Logs never enter managed JSON/subtitle stdout. Malformed output and over-limit output fail before emission/publication, without partial exports or stalled child processes. Expected parser and numeric failures produce `TranscriptError`, not HTTP upload errors or worker recovery.
7. HTTP export failures return 502, release capacity, clean scratch files, and do not restart a healthy worker. Upstream failures remain unchanged.
8. No new inference, model downloads, dependencies, or publishing actions are required for automated acceptance.
