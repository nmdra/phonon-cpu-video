#!/bin/sh
# No inference or model downloads. Pass an image on the host, or run in-image.
set -eu
if [ "$#" -eq 1 ]; then
    exec docker run --rm --network none \
        --entrypoint /opt/phonon-video/smoke-test.sh "$1"
fi
if [ "$#" -ne 0 ]; then
    echo "Usage: $0 [IMAGE]" >&2
    exit 2
fi
[ "$(id -u)" != 0 ]
test -x /usr/local/bin/phonon-entrypoint
/opt/phonon-video/entrypoint.sh --help >/dev/null
ffmpeg -version >/dev/null
ffprobe -version >/dev/null
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
ffmpeg -nostdin -hide_banner -loglevel error -f lavfi \
    -i sine=frequency=440:duration=0.2 -c:a pcm_s16le "$scratch/input.wav"
ffmpeg -nostdin -hide_banner -loglevel error -i "$scratch/input.wav" \
    -f lavfi -i color=c=black:s=160x90:r=5 \
    -map 1:v:0 -map 0:a:0 -c:v libx264 -pix_fmt yuv420p \
    -c:a aac -shortest "$scratch/input.mp4"
/opt/video-venv/bin/python - "$scratch/input.wav" "$scratch/input.mp4" <<'PY'
from pathlib import Path
import sys
import wave

from app.conversion import prepared_audio
from app.settings import Settings

for filename in sys.argv[1:]:
    source = Path(filename)
    with prepared_audio(source, Settings.from_env()) as normalized:
        normalized = Path(normalized)
        assert normalized != source
        with wave.open(str(normalized), "rb") as audio:
            assert audio.getframerate() == 16000
            assert audio.getnchannels() == 1
            assert audio.getsampwidth() == 2
            assert audio.getnframes() > 0
    assert not normalized.exists(), "normalized scratch was not removed"
    assert source.exists(), "source was modified"
print("No-model smoke checks passed")
PY
