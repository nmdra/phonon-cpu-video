"""Bounded, authenticated multipart ingress for the single-model worker."""

from __future__ import annotations

import asyncio
import hmac
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .conversion import MediaError, prepared_audio
from .lifecycle import Worker
from .settings import Settings

# Only upstream text parameters are accepted. No paths or URLs are forwarded.
FIELDS = {
    "model",
    "response_format",
    "hotwords",
    "hotwords[]",
    "language",
    "prompt",
    "temperature",
    "hotword_lambda",
    "timestamp_granularities",
    "timestamp_granularities[]",
}
LIST_FIELDS = {"hotwords[]", "timestamp_granularities[]"}


class IngressError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class Upload:
    """Incremental parser with limits independent of Starlette spool behavior."""

    def __init__(self, content_type: str, target: Path, settings: Settings):
        media, options = parse_options_header(content_type)
        boundary = options.get(b"boundary")
        if media != b"multipart/form-data" or not boundary or len(boundary) > 200:
            raise IngressError("Expected multipart/form-data with a boundary")
        self.settings = settings
        self.file = target.open("wb")
        self.fields = {}
        self.files = 0
        self.parts = 0
        self.complete = False
        self.parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self.begin,
                "on_header_field": self.header_field,
                "on_header_value": self.header_value,
                "on_header_end": self.header_end,
                "on_headers_finished": self.headers_done,
                "on_part_data": self.data,
                "on_part_end": self.end,
                "on_end": self.finish,
            },
        )

    def begin(self):
        self.parts += 1
        if self.parts > self.settings.max_text_fields + 1:
            raise IngressError("Too many multipart fields", 413)
        self.headers, self.hname, self.hvalue = {}, bytearray(), bytearray()
        self.header_bytes = 0
        self.value = bytearray()

    def header_field(self, data, start, end):
        self.header_bytes += end - start
        if self.header_bytes > 8192:
            raise IngressError("Multipart headers too large", 413)
        self.hname.extend(data[start:end])

    def header_value(self, data, start, end):
        self.header_bytes += end - start
        if self.header_bytes > 8192:
            raise IngressError("Multipart headers too large", 413)
        self.hvalue.extend(data[start:end])

    def header_end(self):
        key = bytes(self.hname).lower()
        if key in self.headers:
            raise IngressError("Duplicate multipart header")
        self.headers[key] = bytes(self.hvalue)
        self.hname.clear()
        self.hvalue.clear()

    def headers_done(self):
        disposition, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        if disposition != b"form-data":
            raise IngressError("Invalid multipart disposition")
        self.name = options.get(b"name", b"").decode("utf-8", errors="strict")
        self.is_file = b"filename" in options
        if self.is_file:
            self.files += 1
            if self.name != "file" or self.files != 1:
                raise IngressError("Exactly one file field is allowed")
        elif self.name not in FIELDS or (self.name in self.fields and self.name not in LIST_FIELDS):
            raise IngressError("Unknown or duplicate field")

    def data(self, data, start, end):
        if self.is_file:
            self.file.write(data[start:end])
        else:
            if len(self.value) + end - start > self.settings.max_text_field_bytes:
                raise IngressError("Text field exceeds 4 KiB", 413)
            self.value.extend(data[start:end])

    def end(self):
        if not self.is_file:
            value = self.value.decode("utf-8", errors="strict")
            if self.name in LIST_FIELDS:
                self.fields.setdefault(self.name, []).append(value)
            else:
                self.fields[self.name] = value

    def finish(self):
        self.complete = True

    def close(self):
        self.file.close()


def create_app(
    settings: Settings,
    worker=None,
    *,
    model: str | None = None,
    api_key: str | None = None,
    threads: int | None = None,
):
    key = api_key if api_key is not None else os.environ.get("PHONON_VIDEO_API_KEY")
    if worker is None:
        if not model:
            raise ValueError("A startup model is required")
        worker = Worker(model, key, threads=threads)
    model = model or worker.model
    if not model:
        raise ValueError("A startup model is required")
    # The converter makes an immutable input copy as well as decoded PCM.
    reservation = settings.required_scratch_bytes
    active = 0
    ready = False
    recovery_lock = asyncio.Lock()
    generation = 0

    @asynccontextmanager
    async def lifespan(app):
        nonlocal ready
        if settings.scratch_budget_bytes < reservation:
            raise ValueError("Scratch budget cannot support configured concurrency")
        settings.scratch_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(settings.scratch_dir).free < reservation:
            raise ValueError("Insufficient free scratch space")
        try:
            await worker.start()
            ready = True
            yield
        finally:
            ready = False
            await worker.stop()

    async def recover(failed_generation):
        nonlocal ready, generation
        async with recovery_lock:
            if failed_generation != generation:
                return
            ready = False
            generation += 1
            await worker.stop()
            try:
                await worker.start()
                ready = True
            except Exception:
                ready = False

    async def health(request):
        healthy = ready and await worker.health()
        return JSONResponse(
            {"status": "ok" if healthy else "unhealthy"}, status_code=200 if healthy else 503
        )

    async def disconnected(request):
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    async def transcribe(request: Request):
        nonlocal active
        authorization = request.headers.get("authorization", "")
        if key and not hmac.compare_digest(authorization.encode(), ("Bearer " + key).encode()):
            return JSONResponse(
                {"error": "Unauthorized"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )
        if not ready:
            return JSONResponse({"error": "Worker unavailable"}, 503)
        # No await between admission check and increment: zero queued requests.
        if active >= settings.max_concurrency:
            return JSONResponse({"error": "Capacity exhausted"}, 429)
        active += 1
        directory = None
        upload = None
        context = None
        conversion_task = inference_task = disconnect_task = None
        inference_generation = None
        try:
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError:
                    raise IngressError("Invalid Content-Length") from None
                if size < 0 or size > settings.max_upload_bytes:
                    raise IngressError("Request exceeds upload limit", 413)
            directory = tempfile.TemporaryDirectory(prefix="request-", dir=settings.scratch_dir)
            source = Path(directory.name) / "upload"
            upload = Upload(request.headers.get("content-type", ""), source, settings)
            total = 0
            try:
                async with asyncio.timeout(settings.upload_timeout):
                    async for chunk in request.stream():
                        total += len(chunk)
                        if total > settings.max_upload_bytes:
                            raise IngressError("Request exceeds upload limit", 413)
                        upload.parser.write(chunk)
                    upload.parser.finalize()
            except TimeoutError:
                raise IngressError("Upload timed out", 408) from None
            finally:
                upload.close()
            if not upload.complete or upload.files != 1 or source.stat().st_size == 0:
                raise IngressError("A complete, nonempty file is required")
            if upload.fields.get("model") != model:
                raise IngressError("model must match the configured model")
            disconnect_task = asyncio.create_task(disconnected(request))
            context = prepared_audio(source, settings)
            conversion_task = asyncio.create_task(asyncio.to_thread(context.__enter__))
            done, _ = await asyncio.wait(
                {conversion_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if disconnect_task in done:
                # Conversion is bounded by its own process deadlines. The finally
                # block waits for it before removing the request's scratch files.
                raise IngressError("Client disconnected", 499)
            audio = conversion_task.result()
            if not ready:
                raise IngressError("Worker unavailable", 503)
            inference_generation = generation
            inference_task = asyncio.create_task(worker.transcribe(audio, upload.fields))
            done, _ = await asyncio.wait(
                {inference_task, disconnect_task},
                timeout=settings.inference_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if inference_task not in done:
                # Killing the upstream process, not HTTP cancellation, ends inference.
                await recover(inference_generation)
                if disconnect_task in done:
                    raise IngressError("Client disconnected", 499)
                raise IngressError("Inference timed out", 504)
            response = inference_task.result()
            return Response(
                response.content,
                status_code=response.status_code,
                headers={"content-type": response.headers.get("content-type", "application/json")},
            )
        except MediaError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
        except IngressError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        except (ClientDisconnect, UnicodeError, ValueError, MultipartParseError):
            return JSONResponse({"error": "Invalid or disconnected multipart request"}, 400)
        except asyncio.CancelledError:
            if inference_task is not None:
                await asyncio.shield(recover(inference_generation))
            raise
        except Exception:
            if inference_task is not None:
                await recover(inference_generation)
            return JSONResponse({"error": "Upstream processing failed"}, 502)
        finally:
            for task in (inference_task, disconnect_task):
                if task is not None:
                    task.cancel()
            tasks = [task for task in (inference_task, disconnect_task) if task is not None]
            for task in tasks:
                try:
                    await task
                except BaseException:
                    pass
            if conversion_task is not None:
                try:
                    await asyncio.shield(conversion_task)
                except BaseException:
                    context = None
            if context is not None:
                await asyncio.to_thread(context.__exit__, None, None, None)
            if upload is not None:
                upload.close()
            if directory is not None:
                directory.cleanup()
            active -= 1

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/v1/audio/transcriptions", transcribe, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.worker = worker
    return app


def run_server(model, host, port, api_key, threads, settings):
    import uvicorn

    uvicorn.run(
        create_app(settings, model=model, api_key=api_key, threads=threads), host=host, port=port
    )


serve_video = run_server
