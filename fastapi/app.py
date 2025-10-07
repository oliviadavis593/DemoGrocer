"""Minimal FastAPI-like application framework used for tests."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from . import responses

RouteKey = Tuple[str, str]


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


@dataclass
class Route:
    path: str
    method: str
    handler: Callable[..., Any]
    response_class: Optional[type] = None


class FastAPI:
    """Highly simplified FastAPI drop-in replacement for tests."""

    def __init__(self, *, title: str | None = None) -> None:
        self.title = title or "FastAPI"
        self.routes: Dict[RouteKey, Route] = {}
        self.dependency_overrides: Dict[Callable[..., Any], Callable[..., Any]] = {}

    # Route registration -----------------------------------------------------------------
    def get(self, path: str, *, response_class: Optional[type] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.routes[(path, "GET")] = Route(path, "GET", func, response_class)
            return func

        return decorator

    # Request handling -------------------------------------------------------------------
    def _call(self, func: Callable[..., Any], params: Optional[Mapping[str, Any]] = None) -> Any:
        sig = inspect.signature(func)
        kwargs: Dict[str, Any] = {}
        params = params or {}
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
                if default is inspect._empty:
                    raise HTTPException(400, f"Missing required parameter '{name}'")
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

    def _handle_request(self, method: str, path: str, params: Optional[Mapping[str, Any]] = None) -> responses.ClientResponse:
        route = self.routes.get((path, method.upper()))
        if not route:
            raise HTTPException(404, "Not found")
        try:
            result = self._call(route.handler, params)
            response = self._build_response(result, route.response_class)
        except HTTPException as exc:
            response = responses.JSONResponse(content={"detail": exc.detail}, status_code=exc.status_code)
        return responses.ClientResponse.from_response(response)


__all__ = ["FastAPI", "Depends", "HTTPException", "Query"]
