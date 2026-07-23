from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


class ProcessCancelled(RuntimeError):
    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


class ProcessOutputLimitExceeded(RuntimeError):
    def __init__(
        self,
        command: Sequence[str],
        limit_bytes: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(f"Process output exceeded {limit_bytes} captured bytes")
        self.command = tuple(command)
        self.limit_bytes = limit_bytes
        self.stdout = stdout
        self.stderr = stderr
        self.captured_bytes = len(stdout.encode("utf-8")) + len(
            stderr.encode("utf-8")
        )


class ProcessControl(Protocol):
    def cancel_requested(self) -> bool: ...

    def process_started(self, pid: int, kind: str) -> None: ...

    def process_finished(self, pid: int) -> None: ...


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_controlled_process(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout_seconds: int,
    kind: str,
    control: ProcessControl | None = None,
    stdout_line_callback: Callable[[str], None] | None = None,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    if max_output_bytes is not None and max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    if max_output_bytes is not None and stdout_line_callback is not None:
        raise ValueError(
            "max_output_bytes cannot be combined with stdout_line_callback"
        )
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process_started_at = time.monotonic()
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        env=dict(env) if env is not None else None,
    )
    if control is not None:
        try:
            control.process_started(process.pid, kind)
        except Exception:
            terminate_process_tree(process)
            raise

    if stdout_line_callback is not None:
        return _collect_streaming_process(
            process,
            command=command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            kind=kind,
            control=control,
            stdout_line_callback=stdout_line_callback,
        )
    if max_output_bytes is not None:
        return _collect_bounded_process(
            process,
            command=command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            kind=kind,
            control=control,
            max_output_bytes=max_output_bytes,
            process_started_at=process_started_at,
        )

    started = time.monotonic()
    pending_input = input_text
    stdout = ""
    stderr = ""
    try:
        while True:
            if control is not None and control.cancel_requested():
                terminate_process_tree(process)
                raise ProcessCancelled(
                    f"Cancelled while running {kind} (PID {process.pid})",
                    stdout=stdout,
                    stderr=stderr,
                )
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                terminate_process_tree(process)
                raise subprocess.TimeoutExpired(command, timeout_seconds, stdout, stderr)
            try:
                stdout, stderr = process.communicate(
                    input=pending_input,
                    timeout=min(0.2, remaining),
                )
                break
            except subprocess.TimeoutExpired as exc:
                pending_input = None
                if isinstance(exc.stdout, str):
                    stdout = exc.stdout
                if isinstance(exc.stderr, str):
                    stderr = exc.stderr
    except KeyboardInterrupt:
        terminate_process_tree(process)
        raise
    finally:
        if process.poll() is None:
            terminate_process_tree(process)
        if control is not None:
            control.process_finished(process.pid)

    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _utf8_prefix(value: str, limit_bytes: int) -> str:
    if limit_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return value
    return encoded[:limit_bytes].decode("utf-8", errors="ignore")


def _collect_bounded_process(
    process: subprocess.Popen[str],
    *,
    command: Sequence[str],
    input_text: str | None,
    timeout_seconds: int,
    kind: str,
    control: ProcessControl | None,
    max_output_bytes: int,
    process_started_at: float,
) -> subprocess.CompletedProcess[str]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Bounded process requires stdout and stderr pipes")
    if input_text is not None:
        if process.stdin is None:
            raise RuntimeError("Bounded process requires an stdin pipe")

    chunks: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=16)
    stop_readers = threading.Event()
    feeder_errors: list[BaseException] = []

    def drain(name: str, pipe: object) -> None:
        reader = getattr(pipe, "read")
        try:
            while not stop_readers.is_set():
                chunk = reader(8192)
                if chunk == "":
                    break
                while not stop_readers.is_set():
                    try:
                        chunks.put((name, chunk), timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            while not stop_readers.is_set():
                try:
                    chunks.put((name, None), timeout=0.1)
                    break
                except queue.Full:
                    continue

    reader_threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in reader_threads:
        thread.start()

    feeder_thread: threading.Thread | None = None
    if input_text is not None:
        stdin = process.stdin
        if stdin is None:
            raise RuntimeError("Bounded process requires an stdin pipe")

        def feed_stdin() -> None:
            try:
                for offset in range(0, len(input_text), 8192):
                    stdin.write(input_text[offset : offset + 8192])
                    stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            except BaseException as exc:
                feeder_errors.append(exc)
            finally:
                try:
                    stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        feeder_thread = threading.Thread(
            target=feed_stdin,
            name=f"process-stdin-{process.pid}",
            daemon=True,
        )
        feeder_thread.start()

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    captured_bytes = 0
    completed_streams: set[str] = set()

    def captured_streams() -> tuple[str, str]:
        return "".join(stdout_parts), "".join(stderr_parts)

    try:
        while process.poll() is None or len(completed_streams) < 2:
            if feeder_errors:
                terminate_process_tree(process)
                raise RuntimeError("Failed to stream process stdin") from feeder_errors[0]
            if control is not None and control.cancel_requested():
                terminate_process_tree(process)
                stdout, stderr = captured_streams()
                raise ProcessCancelled(
                    f"Cancelled while running {kind} (PID {process.pid})",
                    stdout=stdout,
                    stderr=stderr,
                )
            elapsed = time.monotonic() - process_started_at
            if elapsed >= timeout_seconds:
                terminate_process_tree(process)
                stdout, stderr = captured_streams()
                raise subprocess.TimeoutExpired(
                    command, timeout_seconds, stdout, stderr
                )
            try:
                name, chunk = chunks.get(
                    timeout=min(0.2, max(0.001, timeout_seconds - elapsed))
                )
            except queue.Empty:
                continue
            if chunk is None:
                completed_streams.add(name)
                continue
            chunk_bytes = len(chunk.encode("utf-8"))
            remaining = max_output_bytes - captured_bytes
            saved_chunk = (
                chunk if chunk_bytes <= remaining else _utf8_prefix(chunk, remaining)
            )
            if saved_chunk:
                if name == "stdout":
                    stdout_parts.append(saved_chunk)
                else:
                    stderr_parts.append(saved_chunk)
                captured_bytes += len(saved_chunk.encode("utf-8"))
            if chunk_bytes > remaining:
                terminate_process_tree(process)
                stdout, stderr = captured_streams()
                raise ProcessOutputLimitExceeded(
                    command, max_output_bytes, stdout, stderr
                )
        process.wait()
    except KeyboardInterrupt:
        terminate_process_tree(process)
        raise
    finally:
        stop_readers.set()
        if process.poll() is None:
            terminate_process_tree(process)
        if feeder_thread is not None:
            feeder_thread.join()
        for thread in reader_threads:
            thread.join()
        process.stdout.close()
        process.stderr.close()
        if control is not None:
            control.process_finished(process.pid)

    stdout, stderr = captured_streams()
    if feeder_errors:
        raise RuntimeError("Failed to stream process stdin") from feeder_errors[0]
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _collect_streaming_process(
    process: subprocess.Popen[str],
    *,
    command: Sequence[str],
    input_text: str | None,
    timeout_seconds: int,
    kind: str,
    control: ProcessControl | None,
    stdout_line_callback: Callable[[str], None],
) -> subprocess.CompletedProcess[str]:
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Streaming process requires stdout and stderr pipes")
    if input_text is not None:
        if process.stdin is None:
            raise RuntimeError("Streaming process requires an stdin pipe")
        process.stdin.write(input_text)
        process.stdin.close()

    lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def drain(name: str, pipe: object) -> None:
        reader = getattr(pipe, "readline")
        try:
            while True:
                line = reader()
                if line == "":
                    break
                lines.put((name, line))
        finally:
            lines.put((name, None))

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    started = time.monotonic()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    completed_streams: set[str] = set()
    try:
        while process.poll() is None or len(completed_streams) < 2:
            if control is not None and control.cancel_requested():
                terminate_process_tree(process)
                raise ProcessCancelled(
                    f"Cancelled while running {kind} (PID {process.pid})",
                    stdout="".join(stdout_parts),
                    stderr="".join(stderr_parts),
                )
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds:
                terminate_process_tree(process)
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    "".join(stdout_parts),
                    "".join(stderr_parts),
                )
            try:
                name, line = lines.get(timeout=min(0.2, timeout_seconds - elapsed))
            except queue.Empty:
                continue
            if line is None:
                completed_streams.add(name)
            elif name == "stdout":
                stdout_parts.append(line)
                stdout_line_callback(line)
            else:
                stderr_parts.append(line)
        process.wait()
    except KeyboardInterrupt:
        terminate_process_tree(process)
        raise
    finally:
        if process.poll() is None:
            terminate_process_tree(process)
        for thread in threads:
            thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
        if control is not None:
            control.process_finished(process.pid)

    return subprocess.CompletedProcess(
        list(command), process.returncode, "".join(stdout_parts), "".join(stderr_parts)
    )
