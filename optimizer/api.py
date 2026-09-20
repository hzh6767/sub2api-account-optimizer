from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol

from .domain import ProbeOutcome, RoundTimeoutError
from .mutations import Mutation


class APIError(RuntimeError):
    def __init__(
        self,
        status_code: int | None,
        category: str,
        message: str = "admin API request failed",
        reset_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.category = category
        self.reset_at = reset_at


class TargetedProbeSafetyError(RuntimeError):
    pass


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object: ...


class UrllibTransport:
    def __init__(self, base_url: str, timeout_seconds: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        headers: dict[str, str],
        *,
        stream: bool = False,
    ) -> object:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request_headers = {"Accept": "application/json", **headers}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout_seconds)
            if stream:
                return response
            with response:
                raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            category = _status_category(exc.code)
            reset_at = _rate_limit_reset(exc.headers) if exc.code == 429 else None
            exc.close()
            raise APIError(exc.code, category, reset_at=reset_at) from None
        except urllib.error.URLError as exc:
            category = _network_category(str(exc.reason))
            raise APIError(None, category) from None
        except RoundTimeoutError:
            raise
        except TimeoutError:
            raise APIError(None, "timeout") from None


def _status_category(status_code: int) -> str:
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limit"
    if status_code in (408, 504, 524):
        return "timeout"
    if 500 <= status_code <= 599:
        return "server"
    return "upstream"


def _network_category(message: str) -> str:
    lowered = message.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "name or service" in lowered or "dns" in lowered:
        return "dns"
    if "certificate" in lowered or "tls" in lowered or "ssl" in lowered:
        return "tls"
    if "refused" in lowered:
        return "connection_refused"
    return "network"


def _duration_reset(value: str, now: datetime) -> datetime | None:
    matches = list(re.finditer(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value))
    if not matches or "".join(match.group(0) for match in matches) != value:
        return None
    multipliers = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    seconds = sum(
        float(match.group(1)) * multipliers[match.group(2)] for match in matches
    )
    return now + timedelta(seconds=max(0.0, seconds))


def _reset_value(
    raw: object, now: datetime, *, numeric_is_relative: bool
) -> datetime | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    duration = _duration_reset(value, now)
    if duration is not None:
        return duration
    try:
        numeric = float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (
            parsed
            if parsed.tzinfo is not None
            else parsed.replace(tzinfo=timezone.utc)
        )
    if numeric_is_relative:
        return now + timedelta(seconds=max(0.0, numeric))
    if numeric > 10_000_000_000:
        numeric /= 1_000
    try:
        return datetime.fromtimestamp(numeric, timezone.utc)
    except RoundTimeoutError:
        raise
    except (OverflowError, OSError, ValueError):
        return None


def _rate_limit_reset(headers: object) -> datetime | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    now = datetime.now(timezone.utc)
    retry_after = getter("Retry-After")
    if retry_after:
        value = str(retry_after).strip()
        parsed = _reset_value(value, now, numeric_is_relative=True)
        if parsed is not None:
            return parsed
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if parsed is not None:
            return (
                parsed
                if parsed.tzinfo is not None
                else parsed.replace(tzinfo=timezone.utc)
            )

    for name in ("x-ratelimit-reset", "x-ratelimit-reset-requests"):
        raw = getter(name)
        if not raw:
            continue
        parsed = _reset_value(raw, now, numeric_is_relative=False)
        if parsed is not None:
            return parsed
    return None


def _event_rate_limit_reset(event: dict[str, object]) -> datetime | None:
    now = datetime.now(timezone.utc)
    sources = [event]
    nested = event.get("error")
    if isinstance(nested, dict):
        sources.append(nested)
    for source in sources:
        for key in ("reset_at", "reset_time", "rate_limit_reset_at"):
            parsed = _reset_value(
                source.get(key), now, numeric_is_relative=False
            )
            if parsed is not None:
                return parsed
        for key in ("retry_after", "retry_after_seconds"):
            parsed = _reset_value(source.get(key), now, numeric_is_relative=True)
            if parsed is not None:
                return parsed
    return None


def _event_failure(message: str) -> tuple[str, int | None]:
    status_match = re.search(r"\b([45]\d\d)\b", message)
    status_code = int(status_match.group(1)) if status_match else None
    if status_code is not None:
        return _status_category(status_code), status_code
    return _network_category(message), None


class AdminAPI:
    def __init__(
        self,
        base_url: str,
        *,
        auth_headers: Callable[[], dict[str, str]] | None = None,
        targeted_test_safe: bool,
        transport: Transport | None = None,
        admin_email: str = "",
        admin_password: str = "",
        admin_api_key_file: Path | None = None,
    ) -> None:
        self.transport = transport or UrllibTransport(base_url)
        self.targeted_test_safe = targeted_test_safe
        self._auth_headers_override = auth_headers
        self.admin_email = admin_email
        self.admin_password = admin_password
        self.admin_api_key_file = admin_api_key_file
        self._access_token: str | None = None

    def _auth_headers(self) -> dict[str, str]:
        if self._auth_headers_override is not None:
            return self._auth_headers_override()
        if self.admin_api_key_file is not None and self.admin_api_key_file.exists():
            value = self.admin_api_key_file.read_text("utf-8").strip()
            if value:
                return {"x-api-key": value}
        if self._access_token is None:
            self._access_token = self._login()
        return {"Authorization": f"Bearer {self._access_token}"}

    def _login(self) -> str:
        if not self.admin_email or not self.admin_password:
            raise APIError(None, "auth", "admin credentials are unavailable")
        response = self.transport.request(
            "POST",
            "/api/v1/auth/login",
            {"email": self.admin_email, "password": self.admin_password},
            {},
        )
        data = _unwrap(response)
        if not isinstance(data, dict) or data.get("requires_2fa"):
            raise APIError(
                None, "auth", "interactive two-factor authentication is required"
            )
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise APIError(
                None, "auth", "login response did not contain an access token"
            )
        return token

    def available_models(self, account_id: int) -> set[str]:
        response = self.transport.request(
            "GET",
            f"/api/v1/admin/accounts/{account_id}/models",
            None,
            self._auth_headers(),
        )
        data = _unwrap(response)
        if isinstance(data, dict):
            data = data.get("models", data.get("items", []))
        models: set[str] = set()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    models.add(item)
                elif isinstance(item, dict) and isinstance(item.get("id"), str):
                    models.add(item["id"])
        return models

    def probe_account(self, account_id: int, model: str) -> ProbeOutcome:
        if not self.targeted_test_safe:
            raise TargetedProbeSafetyError(
                "running Sub2API endpoint mutates account state on a single 401 and does not support minimal token limits"
            )
        started = time.monotonic()
        try:
            response = self.transport.request(
                "POST",
                f"/api/v1/admin/accounts/{account_id}/test",
                {"model_id": model, "prompt": "1", "mode": "optimizer"},
                self._auth_headers(),
                stream=True,
            )
        except APIError as exc:
            raise TargetedProbeSafetyError(
                "targeted account test transport failed before the optimizer handshake"
            ) from exc

        reported_model: str | None = None
        try:
            for raw_line in response:  # type: ignore[union-attr]
                line = (
                    raw_line.decode("utf-8", "replace")
                    if isinstance(raw_line, bytes)
                    else str(raw_line)
                )
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type == "test_start":
                    if event.get("mode") != "optimizer":
                        raise TargetedProbeSafetyError(
                            "targeted account test did not confirm optimizer mode"
                        )
                    requested_model = event.get("requested_model")
                    if requested_model != model:
                        raise TargetedProbeSafetyError(
                            "targeted account test did not echo the requested model"
                        )
                    reported_model = str(event.get("model", "")).strip()
                    if not reported_model:
                        raise TargetedProbeSafetyError(
                            "targeted account test did not report the actual model"
                        )
                elif event_type == "content" and str(event.get("text", "")).strip():
                    if reported_model is None:
                        raise TargetedProbeSafetyError(
                            "targeted account test emitted content before the optimizer handshake"
                        )
                    elapsed_ms = round((time.monotonic() - started) * 1000)
                    return ProbeOutcome(
                        True,
                        status_code=200,
                        ttft_ms=elapsed_ms,
                        duration_ms=elapsed_ms,
                        model=reported_model,
                        capability_verified=True,
                    )
                elif event_type == "error":
                    event_code = str(event.get("code", "")).strip().lower()
                    if event_code == "probe_configuration":
                        category, status_code = "probe_configuration", None
                    else:
                        if reported_model is None:
                            raise TargetedProbeSafetyError(
                                "targeted account test emitted an upstream error before the optimizer handshake"
                            )
                        category, status_code = _event_failure(
                            str(event.get("error", ""))
                        )
                    return ProbeOutcome(
                        False,
                        category,
                        status_code,
                        duration_ms=round((time.monotonic() - started) * 1000),
                        reset_at=_event_rate_limit_reset(event),
                        model=reported_model,
                        capability_verified=reported_model is not None,
                    )
                elif event_type == "test_complete" and event.get("success") is False:
                    if reported_model is None:
                        raise TargetedProbeSafetyError(
                            "targeted account test completed before the optimizer handshake"
                        )
                    return ProbeOutcome(
                        False,
                        "upstream",
                        duration_ms=round((time.monotonic() - started) * 1000),
                        model=reported_model,
                        capability_verified=reported_model is not None,
                    )
        except RoundTimeoutError:
            raise
        except TimeoutError:
            if reported_model is None:
                raise TargetedProbeSafetyError(
                    "targeted account test stream timed out before the optimizer handshake"
                ) from None
            return ProbeOutcome(
                False,
                "timeout",
                duration_ms=round((time.monotonic() - started) * 1000),
                model=reported_model,
                capability_verified=True,
            )
        except urllib.error.URLError as exc:
            if reported_model is None:
                raise TargetedProbeSafetyError(
                    "targeted account test stream failed before the optimizer handshake"
                ) from exc
            return ProbeOutcome(
                False,
                _network_category(str(exc.reason)),
                duration_ms=round((time.monotonic() - started) * 1000),
                model=reported_model,
                capability_verified=True,
            )
        except http.client.HTTPException as exc:
            if reported_model is None:
                raise TargetedProbeSafetyError(
                    "targeted account test stream failed before the optimizer handshake"
                ) from exc
            return ProbeOutcome(
                False,
                "network",
                duration_ms=round((time.monotonic() - started) * 1000),
                model=reported_model,
                capability_verified=True,
            )
        except OSError as exc:
            if reported_model is None:
                raise TargetedProbeSafetyError(
                    "targeted account test stream failed before the optimizer handshake"
                ) from exc
            return ProbeOutcome(
                False,
                _network_category(str(exc)),
                duration_ms=round((time.monotonic() - started) * 1000),
                model=reported_model,
                capability_verified=True,
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if reported_model is None:
            raise TargetedProbeSafetyError(
                "targeted account test ended before the optimizer handshake"
            )
        return ProbeOutcome(
            False,
            "empty_stream",
            duration_ms=round((time.monotonic() - started) * 1000),
            model=reported_model,
            capability_verified=reported_model is not None,
        )

    def apply(self, mutation: Mutation) -> None:
        if mutation.kind == "schedule":
            path = f"/api/v1/admin/accounts/{mutation.account_id}"
            method = "PUT"
        elif mutation.kind == "schedulable":
            path = f"/api/v1/admin/accounts/{mutation.account_id}/schedulable"
            method = "POST"
        else:
            raise ValueError(f"unsupported mutation kind: {mutation.kind}")
        response = self.transport.request(
            method, path, mutation.values, self._auth_headers()
        )
        _unwrap(response)


def _unwrap(response: object) -> object:
    if isinstance(response, dict) and "code" in response:
        if response.get("code") != 0:
            raise APIError(int(response.get("code") or 500), "upstream")
        return response.get("data")
    return response
