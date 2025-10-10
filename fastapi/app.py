"""Minimal FastAPI-like application framework used for tests."""
from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs

try:  # pragma: no cover - optional dependency
    from pydantic import BaseModel as PydanticBaseModel
except ModuleNotFoundError:  # pragma: no cover
    PydanticBaseModel = None  # type: ignore[misc]


def _is_pydantic_model(annotation: Any) -> bool:
    return (
        PydanticBaseModel is not None
        and inspect.isclass(annotation)
        and issubclass(annotation, PydanticBaseModel)
    )

from . import responses

RouteKey = Tuple[str, str]


def _compile_path(path: str) -> tuple[Optional[re.Pattern[str]], tuple[str, ...]]:
    if "{" not in path:
        return None, ()
    expr = "^"
    params: list[str] = []
    last = 0
    for match in re.finditer(r"{([^}:]+)(?::([^}]+))?}", path):
        expr += re.escape(path[last:match.start()])
        name, converter = match.group(1), match.group(2) or "segment"
        params.append(name)
        if converter == "path":
            expr += f"(?P<{name}>.+)"
        else:
            expr += f"(?P<{name}>[^/]+)"
        last = match.end()
    expr += re.escape(path[last:]) + "$"
    return re.compile(expr), tuple(params)


class HTTPException(Exception):
    """Simplified HTTP exception."""

    def __init__(self, status_code: int, detail: Any = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Depends:
    """Marker for dependency injection."""

    def __init__(self, dependency: Callable[..., Any]) -> None:
        self.dependency = dependency


class Query:
    """Descriptor for query parameters."""

    def __init__(self, default: Any, *, ge: Optional[float] = None, le: Optional[float] = None) -> None:
        self.default = default
        self.ge = ge
        self.le = le


class Body:
    """Descriptor for body parameters."""

    def __init__(self, default: Any = inspect._empty) -> None:
        self.default = default


@dataclass
class Route:
    path: str
    method: str
    handler: Callable[..., Any]
    response_class: Optional[type] = None
    pattern: Optional[re.Pattern[str]] = None


class FastAPI:
    """Highly simplified FastAPI drop-in replacement for tests."""

    def __init__(self, *, title: str | None = None) -> None:
        self.title = title or "FastAPI"
        self.routes: Dict[RouteKey, Route] = {}
        self.dynamic_routes: list[Route] = []
        self.dependency_overrides: Dict[Callable[..., Any], Callable[..., Any]] = {}
        self.exception_handlers: Dict[type[BaseException], Callable[[BaseException], Any]] = {}
        self.mounts: list[tuple[str, Any, Optional[str]]] = []

    # Route registration -----------------------------------------------------------------
    def get(
        self,
        path: str,
        *,
        response_class: Optional[type] = None,
        **_kwargs: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            pattern, _ = _compile_path(path)
            route = Route(path, "GET", func, response_class, pattern=pattern)
            if pattern is None:
                self.routes[(path, "GET")] = route
            else:
                self.dynamic_routes.append(route)
            return func

        return decorator

    def post(
        self,
        path: str,
        *,
        response_class: Optional[type] = None,
        **_kwargs: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            pattern, _ = _compile_path(path)
            route = Route(path, "POST", func, response_class, pattern=pattern)
            if pattern is None:
                self.routes[(path, "POST")] = route
            else:
                self.dynamic_routes.append(route)
            return func

        return decorator

    def mount(self, path: str, app: Any, name: Optional[str] = None) -> None:
        self.mounts.append((path, app, name))

    # Request handling -------------------------------------------------------------------
    def _call(
        self,
        func: Callable[..., Any],
        params: Optional[Mapping[str, Any]] = None,
        body: Any = None,
    ) -> Any:
        sig = inspect.signature(func)
        kwargs: Dict[str, Any] = {}
        params = dict(params or {})
        body_params: Mapping[str, Any]
        if isinstance(body, Mapping):
            body_params = body  # type: ignore[assignment]
        elif body is None:
            body_params = {}
        else:
            body_params = {"body": body}
        body_params = dict(body_params)
        for name, parameter in sig.parameters.items():
            default = parameter.default
            annotation = parameter.annotation
            if isinstance(default, Depends):
                kwargs[name] = self._resolve_dependency(default)
            elif isinstance(default, Query):
                raw_value = params.get(name, default.default)
                value = self._convert_type(raw_value, annotation)
                if default.ge is not None and value < default.ge:
                    raise HTTPException(422, {name: f"must be >= {default.ge}"})
                if default.le is not None and value > default.le:
                    raise HTTPException(422, {name: f"must be <= {default.le}"})
                kwargs[name] = value
            else:
                if isinstance(default, Body):
                    if isinstance(body, Mapping) and name not in body_params:
                        body_params[name] = body
                    default = default.default
                if _is_pydantic_model(annotation):
                    source: Optional[Mapping[str, Any]]
                    if isinstance(body, Mapping):
                        source = body
                    else:
                        entry = body_params.get(name)
                        source = entry if isinstance(entry, Mapping) else None
                    if source is None:
                        if default is inspect._empty:
                            raise HTTPException(400, f"Missing required parameter '{name}'")
                        kwargs[name] = default
                    else:
                        kwargs[name] = annotation(**source)  # type: ignore[call-arg]
                elif name in body_params:
                    kwargs[name] = body_params[name]
                elif name in params:
                    kwargs[name] = params[name]
                elif default is inspect._empty:
                    raise HTTPException(400, f"Missing required parameter '{name}'")
                else:
                    kwargs[name] = default
        return func(**kwargs)

    def _resolve_dependency(self, depends: Depends) -> Any:
        dependency = depends.dependency
        dependency = self.dependency_overrides.get(dependency, dependency)
        return self._call(dependency, {})

    def _convert_type(self, value: Any, annotation: Any) -> Any:
        if annotation in (inspect._empty, Any):
            return value
        try:
            if annotation is int:
                return int(value)
            if annotation is float:
                return float(value)
        except (TypeError, ValueError):
            raise HTTPException(422, f"Invalid value {value!r}")
        return value

    def _build_response(self, result: Any, response_class: Optional[type]) -> responses.Response:
        if isinstance(result, responses.Response):
            return result
        if response_class and issubclass(response_class, responses.Response):
            return response_class(result)
        if isinstance(result, Mapping):
            return responses.JSONResponse(content=result)
        if isinstance(result, (list, tuple)):
            return responses.JSONResponse(content=list(result))
        return responses.HTMLResponse(content=str(result))

    def _handle_request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        body: Any = None,
    ) -> responses.ClientResponse:
        method_upper = method.upper()
        route = self.routes.get((path, method_upper))
        path_params: Dict[str, str] = {}
        if not route:
            for candidate in self.dynamic_routes:
                if candidate.method != method_upper or candidate.pattern is None:
                    continue
                match = candidate.pattern.match(path)
                if match:
                    route = candidate
                    path_params = match.groupdict()
                    break
        if not route:
            response = responses.JSONResponse(content={"detail": "Not found"}, status_code=404)
            return responses.ClientResponse.from_response(response)
        try:
            merged_params = dict(params or {})
            merged_params.update(path_params)
            result = self._call(route.handler, merged_params, body)
            response = self._build_response(result, route.response_class)
        except HTTPException as exc:
            response = responses.JSONResponse(content={"detail": exc.detail}, status_code=exc.status_code)
        except Exception as exc:
            handler = self._find_exception_handler(exc)
            if handler is None:
                raise
            result = handler(exc)
            if isinstance(result, responses.Response):
                response = result
            elif isinstance(result, Mapping):
                response = responses.JSONResponse(content=result)
            else:
                response = responses.HTMLResponse(content=str(result))
        return responses.ClientResponse.from_response(response)

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Callable[[], Awaitable[Mapping[str, Any]]],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        scope_type = scope.get("type")

        if scope_type == "lifespan":
            while True:
                message = await receive()
                message_type = message["type"]
                if message_type == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message_type == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        if scope_type != "http":
            raise RuntimeError(f"Unsupported ASGI scope type: {scope_type}")

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")
        raw_query = scope.get("query_string", b"")
        query_params: Dict[str, Any] = {}
        if raw_query:
            parsed = parse_qs(raw_query.decode())
            query_params = {key: values[-1] for key, values in parsed.items() if values}

        body_data: Any = None
        if method in {"POST", "PUT", "PATCH"}:
            body_bytes = b""
            more_body = True
            while more_body:
                message = await receive()
                message_type = message.get("type")
                if message_type != "http.request":
                    continue
                body_bytes += message.get("body", b"")
                more_body = message.get("more_body", False)
            if body_bytes:
                content_type = ""
                for header_key, header_value in scope.get("headers", []):
                    if header_key.lower() == b"content-type":
                        content_type = header_value.decode().lower()
                        break
                if "application/json" in content_type:
                    try:
                        body_data = json.loads(body_bytes.decode())
                    except json.JSONDecodeError:
                        body_data = None
                else:
                    body_data = body_bytes.decode()

        try:
            client_response = self._handle_request(method, path, query_params, body_data)
            body = client_response.content
            if isinstance(body, str):
                body = body.encode("utf-8")
            status_code = client_response.status_code
            media_type_value = client_response.headers.get("content-type", client_response.media_type)
            headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in client_response.headers.items()]
            if not any(key == b"content-type" for key, _ in headers) and media_type_value:
                headers.append((b"content-type", media_type_value.encode("latin-1")))
        except Exception:
            body = b"Internal Server Error"
            status_code = 500
            headers = [(b"content-type", b"text/plain")]

        headers_dict = dict(headers)
        headers_dict.setdefault("content-length".encode("latin-1"), str(len(body)).encode("latin-1"))
        headers = list(headers_dict.items())

        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    # Exception handling -----------------------------------------------------------
    def add_exception_handler(
        self, exc_type: type[BaseException], handler: Callable[[BaseException], Any]
    ) -> None:
        self.exception_handlers[exc_type] = handler

    def exception_handler(
        self, exc_type: type[BaseException]
    ) -> Callable[[Callable[[BaseException], Any]], Callable[[BaseException], Any]]:
        def decorator(func: Callable[[BaseException], Any]) -> Callable[[BaseException], Any]:
            self.add_exception_handler(exc_type, func)
            return func

        return decorator

    def _find_exception_handler(self, exc: BaseException) -> Optional[Callable[[BaseException], Any]]:
        for exc_type, handler in self.exception_handlers.items():
            if isinstance(exc, exc_type):
                return handler
        return None


__all__ = ["FastAPI", "Depends", "HTTPException", "Query"]
