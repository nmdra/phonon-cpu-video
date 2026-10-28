# Changelog

This file records notable changes to this project.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/2.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add TXT, SRT, and WebVTT export through the CLI and HTTP adapter
- Add target-duration output grouping without changing recognition or native segment metadata
- Add bounded CLI output capture and atomic no-clobber transcript file publication
- Add CPU-only video and audio normalization inside the upstream Phonon container
- Add a CLI adapter that retains upstream model arguments, output, and exit status
- Add local multipart HTTP transcription for one model, with optional authentication and readiness checks
- License the adapter code under Apache-2.0
- Add resource limits, private scratch files, decoder protocol restrictions, and worker recovery
- Add CI on native amd64/arm64 and verify candidate images before release promotion

No release has been published. Comparison links will be added after the remote repository and the first release exist.
