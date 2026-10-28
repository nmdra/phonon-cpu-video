#!/bin/sh
set -eu
exec /opt/video-venv/bin/python -m app "$@"
