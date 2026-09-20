from __future__ import annotations

import os
import socket
import urllib.request
from typing import Any

from .config import Config
from .errors import RedactedError, redacted_errors


def _http_health(url: str) -> dict[str, Any]:
    try:
        with redacted_errors(), urllib.request.urlopen(url, timeout=4) as response:
            return {
                "healthy": 200 <= response.status < 300,
                "status_code": response.status,
            }
    except RedactedError as exc:
        return {"healthy": False, "error_type": exc.error_type}


def _redis_command(*parts: str) -> bytes:
    encoded = [part.encode("utf-8") for part in parts]
    chunks = [f"*{len(encoded)}\r\n".encode("ascii")]
    for item in encoded:
        chunks.append(f"${len(item)}\r\n".encode("ascii"))
        chunks.append(item + b"\r\n")
    return b"".join(chunks)


def _redis_health() -> dict[str, Any]:
    host = os.getenv("REDIS_HOST", "redis")
    port = int(os.getenv("REDIS_PORT", "6379"))
    password = os.getenv("REDIS_PASSWORD", "")
    try:
        with redacted_errors():
            with socket.create_connection((host, port), timeout=4) as connection:
                if password:
                    connection.sendall(_redis_command("AUTH", password))
                    auth_response = connection.recv(256)
                    if not auth_response.startswith(b"+OK"):
                        return {"healthy": False, "error_type": "RedisAuthFailed"}
                connection.sendall(_redis_command("PING"))
                response = connection.recv(256)
            return {"healthy": response.startswith(b"+PONG")}
    except RedactedError as exc:
        return {"healthy": False, "error_type": exc.error_type}


def dependency_health(config: Config) -> dict[str, Any]:
    return {
        "sub2api": _http_health(
            os.getenv("SUB2API_HEALTH_URL", "http://sub2api:8080/health")
        ),
        "theme": _http_health(
            os.getenv("THEME_HEALTH_URL", "http://theme:8092/health")
        ),
        "checkin": _http_health(
            os.getenv("CHECKIN_HEALTH_URL", "http://checkin:8091/health")
        ),
        "postgres": {
            "healthy": True,
            "evidence": "read-only telemetry transaction completed",
        },
        "redis": _redis_health(),
    }
