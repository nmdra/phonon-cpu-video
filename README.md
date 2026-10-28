# Phonon CPU + Video

This independent container adapter uses [Phonon by Fermion Research](https://github.com/fermionresearch/phonon) to transcribe local video and audio. FFmpeg in the image converts media to mono 16 kHz PCM WAV. The original CPU engine does the transcription. No GPU or host FFmpeg is required.

## Credits

Credit for the speech models, CPU inference engine, original CLI/API, and base container belongs to **Fermion Research and the upstream contributors**.

- [Original Phonon repository](https://github.com/fermionresearch/phonon)
- [Upstream CPU image](https://github.com/fermionresearch/phonon/pkgs/container/phonon-cpu)
- [Upstream CPU documentation](https://github.com/fermionresearch/phonon/blob/main/docs/cpu.md)

This repository adds FFmpeg media preparation, a CLI/HTTP adapter for video, packaging, and tests. It does not implement the speech recognition engine or claim ownership of the upstream models. This is not an official Fermion Research release or an endorsed product.

## Build

```sh
docker build -t phonon-cpu-video:local .
```

## CLI

```sh
docker run --rm \
  -v "$PWD:/data:ro" \
  -v phonon-cache:/home/phonon/.cache \
  phonon-cpu-video:local \
  transcribe phonon-2.5 /data/meeting.mp4 --json
```

## Local HTTP

Local personal use needs no API key. See [Local HTTP](docs/guide.md#local-http) for server and upload commands.

Do not expose the unauthenticated service on a public interface. For remote access, enable authentication and use a TLS-enabled reverse proxy. Do not expose upstream port 8001.

## Documentation

- [Build and supported platforms](docs/guide.md#build)
- [CLI and model cache](docs/guide.md#cli)
- [Resource limits](docs/guide.md#limits)
- [Media security and deployment](docs/guide.md#media-security-and-deployment)
- [Development checks](docs/guide.md#development-checks)
- [Release procedure](docs/guide.md#release-procedure)

## Attribution and license status

The adapter code uses [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for third-party attribution and license requirements. A combined image still requires the third-party distribution review.
