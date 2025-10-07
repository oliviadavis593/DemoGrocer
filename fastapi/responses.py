"""Minimal response classes for the FastAPI stub."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class Response:
    """Base response object."""

    media_type = "text/plain"

    def __init__(self, content: Any, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    @property
    def text(self) -> str:
        return str(self.content)

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
    media_type: str

    @classmethod
    def from_response(cls, response: Response) -> "ClientResponse":
        return cls(status_code=response.status_code, _text=response.text, media_type=response.media_type)

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> Any:
        return json.loads(self._text)
