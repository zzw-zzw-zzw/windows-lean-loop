from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO


MAX_ERROR_BODY_BYTES = 256 * 1024


def _copy_response(source: BinaryIO, target: BinaryIO) -> None:
    while True:
        chunk = source.readline()
        if not chunk:
            break
        target.write(chunk)
        target.flush()


def _error(message: str, exit_code: int) -> int:
    sys.stderr.write(message.strip() + "\n")
    sys.stderr.flush()
    return exit_code


def _request(envelope: dict[str, Any]) -> int:
    endpoint = str(envelope["endpoint"])
    api_key = str(envelope["api_key"])
    payload_path = Path(str(envelope["payload_path"]))
    timeout_seconds = int(envelope["timeout_seconds"])
    payload = payload_path.read_bytes()
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "windows-lean-loop/python-transport",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            _copy_response(response, sys.stdout.buffer)
        return 0
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_ERROR_BODY_BYTES)
        if body:
            sys.stdout.buffer.write(body)
            sys.stdout.buffer.flush()
        return _error(f"HTTP {exc.code}: {exc.reason}", 22)
    except (TimeoutError, socket.timeout) as exc:
        return _error(f"Request timed out: {exc}", 28)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return _error(f"Request timed out: {exc.reason}", 28)
        return _error(f"HTTPS transport failed: {exc.reason}", 35)
    except (ConnectionError, OSError) as exc:
        return _error(f"HTTPS transport failed: {exc}", 35)


def main() -> int:
    try:
        envelope = json.load(sys.stdin)
        if not isinstance(envelope, dict):
            raise ValueError("transport input must be a JSON object")
        return _request(envelope)
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        return _error(f"Invalid Python transport request: {exc}", 2)


if __name__ == "__main__":
    raise SystemExit(main())
