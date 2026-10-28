"""Ingress tests use a fake worker and never load a model or use the network."""

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.api import create_app
from app.settings import Settings


class FakeWorker:
    model = "test-model"

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.healthy = True
        self.calls = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.result = httpx.Response(
            200, content=b'{"text":"hello"}', headers={"content-type": "application/json"}
        )

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def health(self):
        return self.healthy

    async def transcribe(self, path, fields):
        self.calls.append((Path(path).read_bytes(), dict(fields)))
        self.entered.set()
        if self.release:
            await self.release.wait()
        return self.result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    @contextmanager
    def converted(source, settings):
        yield source

    monkeypatch.setattr("app.api.prepared_audio", converted)
    return Settings(scratch_dir=tmp_path), FakeWorker()


def run_case(settings, worker, function, api_key: str | None = "secret"):
    async def run():
        app = create_app(settings, worker, api_key=api_key)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                await function(client, app)
        assert worker.stopped >= 1
        assert list(settings.scratch_dir.iterdir()) == []

    asyncio.run(run())


def post(client, **kwargs):
    kwargs.setdefault("headers", {"Authorization": "Bearer secret"})
    kwargs.setdefault("data", {"model": "test-model"})
    kwargs.setdefault("files", {"file": ("sample.mp4", b"media", "video/mp4")})
    return client.post("/v1/audio/transcriptions", **kwargs)


def verbose_transcript():
    # Observed fermion-research 0.2.15 HTTP verbose_json schema; synthetic speech.
    return {
        "text": "hello world again",
        "duration": 65.0,
        "segments": [
            {"id": 0, "start": 0.0, "end": 10.0, "text": "hello", "tokens": []},
            {"id": 1, "start": 10.0, "end": 32.0, "text": "world", "tokens": []},
            {"id": 2, "start": 62.0, "end": 65.0, "text": "again", "tokens": []},
        ],
        "words": [{"word": "hello", "start": 0.0, "end": 10.0}],
        "x_fermion": {"model": "test-model", "segment_count": 3},
    }


@pytest.mark.parametrize(
    ("format_name", "target", "expected", "mime"),
    [
        ("txt", None, b"native text", "text/plain; charset=utf-8"),
        ("text", "30", b"hello world\n\nagain\n", "text/plain; charset=utf-8"),
        ("txt", "30", b"hello world\n\nagain\n", "text/plain; charset=utf-8"),
        (
            "srt",
            "30",
            b"1\n00:00:00,000 --> 00:00:32,000\nhello world\n\n"
            b"2\n00:01:02,000 --> 00:01:05,000\nagain\n\n",
            "application/x-subrip; charset=utf-8",
        ),
        (
            "vtt",
            "30",
            b"WEBVTT\n\n00:00:00.000 --> 00:00:32.000\nhello world\n\n"
            b"00:01:02.000 --> 00:01:05.000\nagain\n\n",
            "text/vtt; charset=utf-8",
        ),
    ],
)
def test_http_exports(setup, format_name, target, expected, mime):
    settings, worker = setup
    worker.result = httpx.Response(
        200,
        content=json.dumps(verbose_transcript()).encode() if target else b"native text",
        headers={"content-type": "text/plain; charset=utf-8"},
    )

    async def case(client, app):
        fields = {"model": "test-model", "response_format": format_name, "hotwords": "one"}
        if target:
            fields["segment_duration"] = target
        response = await post(client, data=fields)
        assert response.status_code == 200
        assert response.content == expected
        assert response.headers["content-type"] == mime
        forwarded = worker.calls[-1][1]
        assert "segment_duration" not in forwarded
        assert forwarded["response_format"] == ("verbose_json" if target else "text")
        assert forwarded["hotwords"] == "one"
        assert worker.started == 1 and worker.stopped == 0

    run_case(settings, worker, case)


@pytest.mark.parametrize("format_name", [None, "json", "verbose_json"])
def test_http_grouped_json_keeps_public_shape(setup, format_name):
    settings, worker = setup
    native = verbose_transcript()
    worker.result = httpx.Response(200, json=native)

    async def case(client, app):
        fields = {"model": "test-model", "segment_duration": "30"}
        if format_name:
            fields["response_format"] = format_name
        response = await post(client, data=fields)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        result = response.json()
        assert result["output_segments"] == [
            {"id": 0, "start": 0.0, "end": 32.0, "text": "hello world"},
            {"id": 1, "start": 62.0, "end": 65.0, "text": "again"},
        ]
        assert result["output_segment_count"] == 2
        if format_name == "verbose_json":
            assert {k: v for k, v in result.items() if not k.startswith("output_")} == native
        else:
            assert set(result) == {"text", "output_segments", "output_segment_count"}
        assert worker.calls[-1][1] == {"model": "test-model", "response_format": "verbose_json"}

    run_case(settings, worker, case)


@pytest.mark.parametrize("format_name", ["srt", "vtt"])
def test_subtitle_export_without_grouping_uses_source_cues(setup, format_name):
    settings, worker = setup
    worker.result = httpx.Response(200, json=verbose_transcript())

    async def case(client, app):
        result = await post(client, data={"model": "test-model", "response_format": format_name})
        assert result.status_code == 200
        assert result.content.count(b" --> ") == 3
        assert worker.calls[-1][1]["response_format"] == "verbose_json"

    run_case(settings, worker, case)


@pytest.mark.parametrize(
    "fields",
    [{"segment_duration": value} for value in ("", "0", "-1", "NaN", "inf", "1e309", "bad")]
    + [{"response_format": "unsupported"}],
)
def test_invalid_output_options_fail_before_conversion(setup, monkeypatch, fields):
    settings, worker = setup

    def forbidden(*args):
        pytest.fail("Invalid options reached media conversion")

    monkeypatch.setattr("app.api.prepared_audio", forbidden)

    async def case(client, app):
        result = await post(client, data={"model": "test-model", **fields})
        assert result.status_code == 400
        assert not worker.calls

    run_case(settings, worker, case)


def test_segment_duration_is_scalar(setup):
    settings, worker = setup

    async def case(client, app):
        fields = [
            ("model", (None, "test-model")),
            ("segment_duration", (None, "10")),
            ("segment_duration", (None, "20")),
            ("file", ("x", b"media")),
        ]
        assert (await post(client, data={}, files=fields)).status_code == 400
        assert not worker.calls

    run_case(settings, worker, case)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json-private-transcript",
        b"\xff",
        b'{"text":"private-transcript"}',
        b'{"text":"x","segments":[{"start":false,"end":1,"text":"x"}]}',
        b'{"text":"x","segments":[{"start":0,"end":1e308,"text":"x"}]}',
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_export_failure_does_not_restart_worker(setup, body):
    settings, worker = setup
    worker.result = httpx.Response(200, content=body)

    async def case(client, app):
        result = await post(client, data={"model": "test-model", "response_format": "srt"})
        assert result.status_code == 502
        assert b"private-transcript" not in result.content
        assert worker.started == 1 and worker.stopped == 0
        assert list(settings.scratch_dir.iterdir()) == []
        worker.result = httpx.Response(200, json=verbose_transcript())
        recovered = await post(client, data={"model": "test-model", "response_format": "vtt"})
        assert recovered.status_code == 200
        assert worker.started == 1 and worker.stopped == 0

    run_case(settings, worker, case)


def test_rendered_response_limit_does_not_restart_worker(setup):
    settings, worker = setup
    payload = "&" * 900000
    worker.result = httpx.Response(
        200, json={"text": payload, "segments": [{"start": 0, "end": 1, "text": payload}]}
    )

    async def case(client, app):
        result = await post(client, data={"model": "test-model", "response_format": "vtt"})
        assert result.status_code == 502
        assert worker.started == 1 and worker.stopped == 0
        assert list(settings.scratch_dir.iterdir()) == []
        worker.result = httpx.Response(200, json=verbose_transcript())
        assert (await post(client)).status_code == 200

    run_case(settings, worker, case)


@pytest.mark.parametrize("format_name", [None, "txt", "srt", "vtt"])
def test_upstream_capture_overflow_preserves_export_worker(setup, monkeypatch, format_name):
    from app.lifecycle import Worker

    settings, worker = setup

    async def case(client, app):
        backend = Worker("test-model", response_limit=8)
        backend.ready = True
        backend.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 9)),
            base_url="http://upstream",
        )
        monkeypatch.setattr(worker, "transcribe", backend.transcribe)
        fields = {"model": "test-model"}
        if format_name:
            fields["response_format"] = format_name
        try:
            response = await post(client, data=fields)
            assert response.status_code == 502
            # Native requests retain their original recovery behavior.
            restarts = 0 if format_name else 1
            assert worker.started == 1 + restarts and worker.stopped == restarts
            assert list(settings.scratch_dir.iterdir()) == []
            await backend.client.aclose()
            backend.client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=verbose_transcript())
                ),
                base_url="http://upstream",
            )
            backend.response_limit = 4 * 1024 * 1024
            recovered = await post(client, data={"model": "test-model", "response_format": "vtt"})
            assert recovered.status_code == 200
            assert worker.started == 1 + restarts and worker.stopped == restarts
        finally:
            await backend.stop()

    run_case(settings, worker, case)


def test_upstream_export_failure_is_not_parsed(setup):
    settings, worker = setup
    worker.result = httpx.Response(
        422, content=b"native failure", headers={"content-type": "application/problem+json"}
    )

    async def case(client, app):
        result = await post(client, data={"model": "test-model", "response_format": "vtt"})
        assert result.status_code == 422
        assert result.content == b"native failure"
        assert result.headers["content-type"] == "application/problem+json"
        assert worker.started == 1 and worker.stopped == 0

    run_case(settings, worker, case)


def test_optional_auth_checks_before_upload(setup):
    settings, worker = setup

    async def case(client, app):
        async def unread():
            raise AssertionError("Body was consumed before authentication")
            yield b""

        response = await client.post("/v1/audio/transcriptions", content=unread())
        assert response.status_code == 401
        assert not worker.calls

    run_case(settings, worker, case)


def test_local_requests_need_no_key(setup, monkeypatch):
    settings, worker = setup
    monkeypatch.delenv("PHONON_VIDEO_API_KEY", raising=False)

    async def case(client, app):
        response = await post(client, headers={})
        assert response.status_code == 200
        assert len(worker.calls) == 1

    run_case(settings, worker, case, api_key=None)


def test_success_and_content_type_preservation(setup):
    settings, worker = setup
    worker.result = httpx.Response(
        201, content=b"hello", headers={"content-type": "text/plain; charset=utf-8"}
    )

    async def case(client, app):
        response = await post(
            client, data={"model": "test-model", "response_format": "text", "hotwords": "one,two"}
        )
        assert response.status_code == 201
        assert response.content == b"hello"
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        assert worker.calls == [
            (b"media", {"model": "test-model", "response_format": "text", "hotwords": "one,two"})
        ]
        assert (await client.get("/health")).status_code == 200
        worker.healthy = False
        assert (await client.get("/health")).status_code == 503

    run_case(settings, worker, case)


@pytest.mark.parametrize(
    "fields", [{}, {"model": "other"}, {"model": "test-model", "url": "https://evil"}]
)
def test_invalid_fields(setup, fields):
    settings, worker = setup

    async def case(client, app):
        assert (await post(client, data=fields)).status_code == 400
        assert not worker.calls

    run_case(settings, worker, case)


def test_files_and_field_limits(setup):
    settings, worker = setup

    async def case(client, app):
        assert (
            await post(client, files=[("file", ("one", b"a")), ("file", ("two", b"b"))])
        ).status_code == 400
        assert (
            await post(client, data={"model": "test-model", "hotwords": "x" * 4097})
        ).status_code == 413
        assert (await post(client, files={"other": ("one", b"a")})).status_code == 400
        assert (await post(client, files={"file": ("empty", b"")})).status_code == 400
        assert not worker.calls

    run_case(settings, worker, case)


def test_total_bytes_without_content_length(setup):
    settings, worker = setup
    settings = replace(settings, max_upload_bytes=512)

    async def case(client, app):
        async def chunks():
            yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\n'
            yield b"x" * 513

        result = await client.post(
            "/v1/audio/transcriptions",
            content=chunks(),
            headers={
                "authorization": "Bearer secret",
                "content-type": "multipart/form-data; boundary=boundary",
            },
        )
        assert result.status_code == 413
        assert not worker.calls

    run_case(settings, worker, case)


def test_overload_before_body(setup):
    settings, worker = setup

    async def case(client, app):
        worker.release = asyncio.Event()
        first = asyncio.create_task(post(client))
        await worker.entered.wait()

        async def unread():
            raise AssertionError("Overloaded request body consumed")
            yield b""

        response = await client.post(
            "/v1/audio/transcriptions", content=unread(), headers={"authorization": "Bearer secret"}
        )
        assert response.status_code == 429
        worker.release.set()
        assert (await first).status_code == 200

    run_case(settings, worker, case)


def test_upstream_error_status(setup):
    settings, worker = setup
    worker.result = httpx.Response(
        422, content=b'{"error":"upstream"}', headers={"content-type": "application/problem+json"}
    )

    async def case(client, app):
        response = await post(client)
        assert response.status_code == 422
        assert response.content == worker.result.content
        assert response.headers["content-type"] == "application/problem+json"

    run_case(settings, worker, case)


def test_timeout_stops_worker_before_capacity_reuse(setup):
    settings, worker = setup
    settings = replace(settings, inference_timeout=0.02)

    async def case(client, app):
        worker.release = asyncio.Event()
        response = await post(client)
        assert response.status_code == 504
        assert worker.stopped == 1
        assert worker.started == 2
        assert list(settings.scratch_dir.iterdir()) == []
        worker.release = None
        assert (await post(client)).status_code == 200

    run_case(settings, worker, case)


def test_upload_timeout(setup):
    settings, worker = setup
    settings = replace(settings, upload_timeout=0.02)

    async def case(client, app):
        async def slow():
            await asyncio.sleep(0.1)
            yield b"unused"

        result = await client.post(
            "/v1/audio/transcriptions",
            content=slow(),
            headers={
                "authorization": "Bearer secret",
                "content-type": "multipart/form-data; boundary=b",
            },
        )
        assert result.status_code == 408
        assert not worker.calls

    run_case(settings, worker, case)


def test_disconnect_stops_worker_and_cleans(setup):
    settings, worker = setup

    async def case(client, app):
        worker.release = asyncio.Event()
        body = (
            b'--b\r\nContent-Disposition: form-data; name="model"\r\n\r\ntest-model\r\n'
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n'
            b"\r\nmedia\r\n--b--\r\n"
        )
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            if messages:
                return messages.pop()
            await worker.entered.wait()
            return {"type": "http.disconnect"}

        sent = []

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/audio/transcriptions",
                "raw_path": b"/v1/audio/transcriptions",
                "query_string": b"",
                "headers": [
                    (b"authorization", b"Bearer secret"),
                    (b"content-type", b"multipart/form-data; boundary=b"),
                ],
                "client": ("127.0.0.1", 1),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        assert sent[0]["status"] == 499
        assert worker.stopped == 1
        assert worker.started == 2
        assert list(settings.scratch_dir.iterdir()) == []

    run_case(settings, worker, case)


def test_array_fields_and_count_limit(setup):
    settings, worker = setup

    async def case(client, app):
        files = [
            ("model", (None, "test-model")),
            ("hotwords[]", (None, "one")),
            ("hotwords[]", (None, "two")),
            ("file", ("x", b"media")),
        ]
        assert (await post(client, data={}, files=files)).status_code == 200
        assert worker.calls[-1][1]["hotwords[]"] == ["one", "two"]
        files = [("model", (None, "test-model")), ("file", ("x", b"media"))]
        files += [("hotwords[]", (None, "word"))] * 16
        assert (await post(client, data={}, files=files)).status_code == 413

    run_case(settings, worker, case)


def test_malformed_and_truncated_multipart(setup):
    settings, worker = setup

    async def case(client, app):
        for body in (
            b"not multipart",
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\ndata',
        ):
            response = await client.post(
                "/v1/audio/transcriptions",
                content=body,
                headers={
                    "authorization": "Bearer secret",
                    "content-type": "multipart/form-data; boundary=b",
                },
            )
            assert response.status_code == 400
        assert not worker.calls

    run_case(settings, worker, case)


def test_worker_streaming_and_response_bound(tmp_path):
    from app.lifecycle import Worker

    async def run():
        path = tmp_path / "audio.wav"
        path.write_bytes(b"wave-data")

        async def handle(request):
            body = await request.aread()
            assert b"wave-data" in body
            assert b"test-model" in body
            assert request.url.path == "/v1/audio/transcriptions"
            return httpx.Response(
                503, content=b"upstream unavailable", headers={"content-type": "text/plain"}
            )

        worker = Worker("test-model", "secret", response_limit=30)
        worker.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://upstream"
        )
        worker.ready = True
        result = await worker.transcribe(
            path, {"model": "test-model", "hotwords[]": ["one", "two"]}
        )
        assert result.status_code == 503
        assert result.content == b"upstream unavailable"
        worker.response_limit = 2
        with pytest.raises(RuntimeError, match="response exceeds"):
            await worker.transcribe(path, {"model": "test-model"})
        await worker.stop()

    asyncio.run(run())


def test_startup_checks_free_disk(setup, monkeypatch):
    from collections import namedtuple

    settings, worker = setup
    disk = namedtuple("disk", "total used free")
    monkeypatch.setattr("app.api.shutil.disk_usage", lambda path: disk(1, 1, 0))

    async def run():
        app = create_app(settings, worker, api_key="secret")
        with pytest.raises(ValueError, match="scratch"):
            async with app.router.lifespan_context(app):
                pass
        assert worker.started == 0

    asyncio.run(run())


def test_real_conversion_audio_and_video(tmp_path):
    import shutil
    import subprocess
    import wave

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg tools not installed")
    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0" * 3200)
    video = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-c:a", "aac", str(video)],
        check=True,
        timeout=10,
    )
    scratch = tmp_path / "scratch"
    settings = Settings(scratch_dir=scratch)
    worker = FakeWorker()

    async def case(client, app):
        for media in (source, video):
            result = await post(client, files={"file": (media.name, media.read_bytes())})
            assert result.status_code == 200, result.text
            assert worker.calls[-1][0][:4] == b"RIFF"
        result = await post(client, files={"file": ("bad.wav", b"not audio")})
        assert result.status_code == 422

    run_case(settings, worker, case)


def test_supervisor_command_and_shutdown(monkeypatch):
    from app.lifecycle import Worker

    async def run():
        class Process:
            pid = 45678
            returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

        commands = []
        signals = []

        async def spawn(*args, **kwargs):
            commands.append((args, kwargs))
            return Process()

        async def handle(request):
            assert request.url.path == "/health"
            assert request.headers["authorization"] == "Bearer secret"
            return httpx.Response(200)

        client_type = httpx.AsyncClient
        monkeypatch.setattr("app.lifecycle.asyncio.create_subprocess_exec", spawn)
        monkeypatch.setattr("app.lifecycle.os.killpg", lambda pid, sig: signals.append((pid, sig)))
        monkeypatch.setattr(
            "app.lifecycle.httpx.AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs),
        )
        worker = Worker("test-model", "secret", threads=2)
        await worker.start()
        assert await worker.health()
        assert commands == [
            (
                (
                    "fermion",
                    "serve",
                    "test-model",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8001",
                    "--served-model-name",
                    "test-model",
                    "--api-key",
                    "secret",
                    "--threads",
                    "2",
                ),
                {"start_new_session": True},
            )
        ]
        await worker.stop()
        assert signals and signals[0][0] == 45678
        assert not await worker.health()
        assert worker.client is None
        assert worker.process is None

    asyncio.run(run())


def test_recovery_generation_and_admission(setup, monkeypatch):
    settings, worker = setup
    settings = replace(settings, max_concurrency=3, scratch_budget_bytes=2 * 1024**3)

    async def case(client, app):
        old_entered = asyncio.Event()
        fail_old = asyncio.Event()
        restarting = asyncio.Event()
        restart = asyncio.Event()
        replacement_entered = asyncio.Event()
        finish_replacement = asyncio.Event()
        calls = 0

        async def transcribe(path, fields):
            nonlocal calls
            calls += 1
            if calls == 1:
                old_entered.set()
                await fail_old.wait()
                raise RuntimeError("old worker failed")
            if calls == 2:
                raise RuntimeError("trigger recovery")
            replacement_entered.set()
            await finish_replacement.wait()
            return worker.result

        async def start():
            worker.started += 1
            restarting.set()
            await restart.wait()

        monkeypatch.setattr(worker, "transcribe", transcribe)
        monkeypatch.setattr(worker, "start", start)
        old = asyncio.create_task(post(client))
        await old_entered.wait()
        failed = asyncio.create_task(post(client))
        await restarting.wait()
        assert (await post(client)).status_code == 503
        assert (await client.get("/health")).status_code == 503
        restart.set()
        assert (await failed).status_code == 502
        replacement = asyncio.create_task(post(client))
        await replacement_entered.wait()
        fail_old.set()
        assert (await old).status_code == 502
        assert worker.started == 2
        assert worker.stopped == 1
        assert (await client.get("/health")).status_code == 200
        finish_replacement.set()
        assert (await replacement).status_code == 200

    run_case(settings, worker, case)


def test_disconnect_during_conversion_never_starts_inference(setup, monkeypatch):
    import threading

    settings, worker = setup
    entered = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    @contextmanager
    def convert(source, settings):
        entered.set()
        assert release.wait(5)
        try:
            yield source
        finally:
            cleaned.set()

    monkeypatch.setattr("app.api.prepared_audio", convert)

    async def case(client, app):
        request = httpx.Request(
            "POST",
            "http://test/v1/audio/transcriptions",
            data={"model": "test-model"},
            files={"file": ("x", b"media")},
        )
        body = request.read()
        uploaded = False
        disconnected = asyncio.Event()

        async def receive():
            nonlocal uploaded
            if not uploaded:
                uploaded = True
                return {"type": "http.request", "body": body, "more_body": False}
            assert await asyncio.to_thread(entered.wait, 5)
            disconnected.set()
            return {"type": "http.disconnect"}

        sent = []

        async def send(message):
            sent.append(message)

        task = asyncio.create_task(
            app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/v1/audio/transcriptions",
                    "query_string": b"",
                    "headers": [
                        (b"authorization", b"Bearer secret"),
                        (b"content-type", request.headers["content-type"].encode()),
                    ],
                },
                receive,
                send,
            )
        )
        await disconnected.wait()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not worker.calls
        release.set()
        await task
        assert sent[0]["status"] == 499
        assert cleaned.is_set()
        assert not worker.calls
        assert worker.started == 1
        assert worker.stopped == 0

    try:
        run_case(settings, worker, case)
    finally:
        release.set()


def test_worker_rejects_occupied_port_before_spawn(monkeypatch):
    from app.lifecycle import Worker

    async def run():
        async def forbidden_spawn(*args, **kwargs):
            pytest.fail("Spawned worker on an occupied port")

        monkeypatch.setattr("app.lifecycle.asyncio.create_subprocess_exec", forbidden_spawn)
        server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            worker = Worker("test-model", "secret", port=port)
            with pytest.raises(RuntimeError, match="already in use"):
                await worker.start()
            assert worker.process is None
            assert worker.client is None
            assert not worker.ready

    asyncio.run(run())


def test_worker_stop_kills_descendant_after_leader_exits(tmp_path):
    import os
    import signal
    import sys

    from app.lifecycle import Worker

    async def run():
        marker = tmp_path / "descendant"
        script = (
            "import os, signal, time, pathlib; "
            "pid = os.fork(); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN) if pid == 0 else None; "
            f"pathlib.Path({str(marker) + '.tmp'!r}).write_text(str(os.getpid())) "
            "if pid == 0 else None; "
            f"os.replace({str(marker) + '.tmp'!r}, {str(marker)!r}) if pid == 0 else None; "
            "time.sleep(60) if pid == 0 else None"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, start_new_session=True
        )
        worker = Worker("test-model", "secret")
        worker.process = process
        descendant = None
        try:
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(0.01)
                descendant = int(marker.read_text())
                await process.wait()
            assert process.returncode == 0
            await worker.stop()
            async with asyncio.timeout(5):
                while True:
                    stat = Path(f"/proc/{descendant}/stat")
                    if not stat.exists() or stat.read_text().split()[2] == "Z":
                        break
                    await asyncio.sleep(0.01)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    asyncio.run(run())
