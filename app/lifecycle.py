"""Supervised, loopback-only upstream inference process."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import httpx

from .transcript import TRANSCRIPT_LIMIT


class ResponseLimitError(RuntimeError):
    """Upstream returned too much data, which does not imply worker failure."""


class Worker:
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        *,
        threads: int | None = None,
        port: int = 8001,
        response_limit: int = TRANSCRIPT_LIMIT,
    ):
        self.model = model
        self.api_key = api_key
        self.threads = threads
        self.port = port
        self.response_limit = response_limit
        self.process = None
        self.client = None
        self.ready = False

    async def start(self):
        if self.process is not None:
            raise RuntimeError("Worker already started")
        try:
            async with asyncio.timeout(2):
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
        except ConnectionRefusedError:
            pass
        else:
            writer.close()
            await writer.wait_closed()
            raise RuntimeError(f"Upstream port {self.port} is already in use")
        command = [
            "fermion",
            "serve",
            self.model,
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--served-model-name",
            self.model,
        ]
        if self.api_key:
            command += ["--api-key", self.api_key]
        if self.threads is not None:
            command += ["--threads", str(self.threads)]
        self.process = await asyncio.create_subprocess_exec(*command, start_new_session=True)
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            timeout=None,
            trust_env=False,
        )
        try:
            async with asyncio.timeout(900):
                while self.process.returncode is None:
                    try:
                        response = await self.client.get("/health", timeout=2)
                        if response.is_success:
                            self.ready = True
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.25)
                raise RuntimeError("Upstream worker exited during startup")
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        self.ready = False
        process, self.process = self.process, None
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                pass
            finally:
                # The session leader can exit while descendants remain alive.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def health(self):
        if (
            not self.ready
            or self.process is None
            or self.process.returncode is not None
            or self.client is None
        ):
            return False
        try:
            return (await self.client.get("/health", timeout=2)).is_success
        except httpx.HTTPError:
            return False

    async def transcribe(self, path: Path, fields: dict[str, str | list[str]]):
        if not self.ready or self.client is None:
            raise RuntimeError("Worker is unavailable")
        # Keep the file open until httpx has consumed the multipart stream.
        with path.open("rb") as audio:
            async with self.client.stream(
                "POST",
                "/v1/audio/transcriptions",
                data=fields,
                files={"file": ("audio.wav", audio, "audio/wav")},
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > self.response_limit:
                        raise ResponseLimitError("Upstream response exceeds limit")
                return httpx.Response(
                    response.status_code,
                    content=bytes(body),
                    headers={
                        "content-type": response.headers.get("content-type", "application/json")
                    },
                )
