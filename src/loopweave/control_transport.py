"""Platform-neutral control transport for managed sessions.

The control protocol is identical on every platform (newline-delimited JSON
request/response authenticated with the run ``control_token``).  Only the
transport differs:

* POSIX  -> Unix domain socket file (``var/<run>.sock``)
* Windows -> per-run named pipe (``\\\\.\\pipe\\loopweave-control-...``)

The registry/``run.json`` ``socket_path`` field is reused as the generic
"control endpoint string" on both platforms, so no database migration is
needed and old POSIX records keep working unchanged.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Union

EndpointLike = Union[str, Path]


def is_named_pipe_endpoint(endpoint: EndpointLike) -> bool:
    lowered = str(endpoint).lower()
    return lowered.startswith(r"\\?\pipe" + "\\") or lowered.startswith(
        r"\\.\pipe" + "\\"
    )


def resolve_control_endpoint(endpoint: EndpointLike) -> str:
    """Normalize a stored control endpoint for the active platform."""
    value = str(endpoint)
    if sys.platform == "win32" or is_named_pipe_endpoint(value):
        return value
    return str(Path(value))


def control_endpoint_available(endpoint: EndpointLike) -> bool:
    """True when a server currently listens on the control endpoint."""
    value = resolve_control_endpoint(endpoint)
    if is_named_pipe_endpoint(value):
        from .win32_pipe import pipe_server_available

        return pipe_server_available(value)
    # Non-pipe endpoints (POSIX socket files, legacy records, or synthetic
    # test paths) keep the filesystem existence semantics.
    return Path(value).exists()


def send_control_message(
    endpoint: EndpointLike,
    payload: dict[str, Any],
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Send one authenticated JSON request and return the parsed response."""
    value = resolve_control_endpoint(endpoint)
    if sys.platform == "win32" or is_named_pipe_endpoint(value):
        return _send_pipe_message(value, payload, timeout=timeout)
    from .supervisor import send_control_message as _posix_sender

    return _posix_sender(Path(value), payload, timeout=timeout)


def _send_pipe_message(
    pipe_name: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    from .win32_pipe import (
        NamedPipeError,
        pipe_close,
        pipe_connect,
        pipe_read,
        pipe_write_all,
    )

    handle = pipe_connect(pipe_name, timeout=timeout)
    try:
        request = (json.dumps(payload, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        pipe_write_all(handle, request)
        response = bytearray()
        deadline = time.monotonic() + max(0.0, timeout)
        while b"\n" not in response and time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            chunk = pipe_read(handle, 65536, timeout_ms=remaining_ms)
            if not chunk:
                break
            response.extend(chunk)
    except NamedPipeError as error:
        raise NamedPipeError(
            f"control pipe delivery failed: {error}"
        ) from error
    finally:
        pipe_close(handle)
    if not response:
        raise NamedPipeError("control pipe returned no response")
    return json.loads(bytes(response).split(b"\n", 1)[0].decode("utf-8"))


def default_endpoint_for_run(run_id: str) -> str:
    """Return the control endpoint a new run should advertise.

    POSIX records the Unix socket path under ``var/``; Windows uses a
    per-run named pipe so the ephemeral handle cannot be confused with a
    filesystem path.
    """
    if sys.platform == "win32":
        import uuid

        return rf"\\.\pipe\loopweave-control-{run_id}-{uuid.uuid4().hex[:8]}"
    raise RuntimeError(
        "POSIX control endpoints are allocated by the CLI run directory"
    )
