# syntax=docker/dockerfile:1
FROM ghcr.io/fermionresearch/phonon-cpu:2.0.12@sha256:fcfd06c83e5355b2d06c21084f9f3579b30296298906c0addd78e9938cb1b8f6

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /opt/phonon-video/requirements.lock
RUN python -m venv /opt/video-venv \
    && /opt/video-venv/bin/python -m pip install --no-cache-dir --require-hashes \
       -r /opt/phonon-video/requirements.lock

COPY LICENSE NOTICE /opt/phonon-video/
COPY app /opt/phonon-video/app
COPY --chmod=755 scripts/entrypoint.sh /opt/phonon-video/entrypoint.sh
COPY --chmod=755 scripts/smoke-test.sh /opt/phonon-video/smoke-test.sh
ENV PYTHONPATH=/opt/phonon-video \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/phonon-video
USER phonon
EXPOSE 8000
ENTRYPOINT ["/opt/phonon-video/entrypoint.sh"]
CMD ["--help"]
