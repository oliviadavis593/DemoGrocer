"""Minimal response classes for the FastAPI stub."""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass
from typing import Any


class Response:
    """Base response object."""

    media_type = "text/plain"

    def __init__(self, content: Any, status_code: int = 200, media_type: str | None = None) -> None:
        self.content = content
        self.status_code = status_code
        self.media_type = media_type or self.media_type
        self.headers: dict[str, str] = {}

    @property
    def text(self) -> str:
        return str(self.content)

    @property
    def body(self) -> bytes:
        if isinstance(self.content, bytes):
            return self.content
        return self.text.encode("utf-8")

    def json(self) -> Any:
        if isinstance(self.content, (dict, list)):
            return self.content
        return json.loads(str(self.content))


class HTMLResponse(Response):
    media_type = "text/html"

    def __init__(self, content: str, status_code: int = 200) -> None:
        super().__init__(content, status_code=status_code)


class JSONResponse(Response):
    media_type = "application/json"

    def __init__(self, content: Any, status_code: int = 200) -> None:
        super().__init__(content, status_code=status_code)

    @property
    def text(self) -> str:
        return json.dumps(self.content)


@dataclass
class ClientResponse:
    status_code: int
    _text: str
    _body: bytes
    media_type: str
    headers: dict[str, str]

    @classmethod
    def from_response(cls, response: Response) -> "ClientResponse":
        copied_headers = {key.lower(): value for key, value in response.headers.items()}
        if "content-type" not in copied_headers and response.media_type:
            copied_headers["content-type"] = response.media_type
        return cls(
            status_code=response.status_code,
            _text=response.text,
            _body=response.body,
            media_type=response.media_type,
            headers=copied_headers,
        )

    @property
    def text(self) -> str:
        return self._text

    @property
    def content(self) -> bytes:
        return self._body

    def json(self) -> Any:
        return json.loads(self._text)


class StreamingResponse(Response):
    media_type = "application/octet-stream"

    def __init__(self, content: Any, media_type: str | None = None, status_code: int = 200) -> None:
        if hasattr(content, "read"):
            payload = content.read()
        else:
            payload = content
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        super().__init__(payload, status_code=status_code, media_type=media_type or self.media_type)

    @property
    def body(self) -> bytes:
        data = self.content
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8")
        return bytes(data)


class FileResponse(Response):
    def __init__(self, path: str | Path, *, media_type: str | None = None, filename: str | None = None) -> None:
        file_path = Path(path)
        payload = file_path.read_bytes()
        super().__init__(payload, media_type=media_type or "application/octet-stream")
        name = filename or file_path.name
        self.headers.setdefault("Content-Disposition", f"attachment; filename=\"{name}\"")
