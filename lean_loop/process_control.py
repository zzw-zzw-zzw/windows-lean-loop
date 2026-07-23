from __future__ import annotations

import ctypes
import errno
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


_WINDOWS_JOB_HANDLE_ATTRIBUTE = "_lean_loop_job_handle"
_WINDOWS_START_GATE_HANDLE_ATTRIBUTE = "_lean_loop_start_gate_handle"
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_WAIT_OBJECT_0 = 0
_WINDOWS_JOB_WRAPPER = """
import ctypes
import subprocess
import sys

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenEventW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
kernel32.OpenEventW.restype = ctypes.c_void_p
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
kernel32.WaitForSingleObject.restype = ctypes.c_ulong
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = ctypes.c_int
gate = kernel32.OpenEventW(0x00100000, False, sys.argv[1])
if not gate:
    raise ctypes.WinError(ctypes.get_last_error())
try:
    if kernel32.WaitForSingleObject(gate, 30000) != 0:
        raise TimeoutError("Timed out waiting for the process-tree safety gate")
finally:
    kernel32.CloseHandle(gate)
child = subprocess.Popen(
    sys.argv[2:],
    stdin=sys.stdin,
    stdout=sys.stdout,
    stderr=sys.stderr,
)
raise SystemExit(child.wait())
""".strip()


if os.name == "nt":
    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]


    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


    def _windows_kernel32():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.SetEvent.restype = ctypes.c_int
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        return kernel32


class ProcessCancelled(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        complete_captured_prefix_saved: bool = True,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.complete_captured_prefix_saved = complete_captured_prefix_saved


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


def _attach_windows_job(process: subprocess.Popen[str]) -> None:
    if os.name != "nt":
        return
    process_handle = getattr(process, "_handle", None)
    if not isinstance(process_handle, int):
        return
    kernel32 = _windows_kernel32()
    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise ctypes.WinError()
    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    if not kernel32.SetInformationJobObject(
        job_handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job_handle)
        raise ctypes.WinError(error)
    if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job_handle)
        raise ctypes.WinError(error)
    setattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, job_handle)


def _create_windows_start_gate(
    command: Sequence[str],
) -> tuple[list[str], int | None]:
    if os.name != "nt":
        return list(command), None
    kernel32 = _windows_kernel32()
    event_name = (
        f"Local\\LeanLoopProcessStart-{os.getpid()}-{uuid.uuid4().hex}"
    )
    gate_handle = kernel32.CreateEventW(None, True, False, event_name)
    if not gate_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return [
        sys.executable,
        "-c",
        _WINDOWS_JOB_WRAPPER,
        event_name,
        *command,
    ], gate_handle


def _ensure_windows_command_available(
    command: Sequence[str],
    *,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> None:
    if os.name != "nt" or not command:
        return
    executable = str(command[0])
    candidate = Path(executable)
    if candidate.is_absolute() or candidate.parent != Path("."):
        resolved = (
            candidate
            if candidate.is_absolute()
            else (cwd or Path.cwd()) / candidate
        )
        available = resolved.is_file()
    else:
        search_path = str(env.get("PATH") or "") if env is not None else None
        available = shutil.which(executable, path=search_path) is not None
    if not available:
        raise FileNotFoundError(
            errno.ENOENT,
            os.strerror(errno.ENOENT),
            executable,
        )


def _release_windows_start_gate(process: subprocess.Popen[str]) -> None:
    if os.name != "nt":
        return
    gate_handle = getattr(
        process, _WINDOWS_START_GATE_HANDLE_ATTRIBUTE, None
    )
    if not gate_handle:
        return
    if not _windows_kernel32().SetEvent(gate_handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _close_windows_resources(process: subprocess.Popen[str]) -> None:
    if os.name != "nt":
        return
    job_handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, None)
    gate_handle = getattr(
        process, _WINDOWS_START_GATE_HANDLE_ATTRIBUTE, None
    )
    setattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, None)
    setattr(process, _WINDOWS_START_GATE_HANDLE_ATTRIBUTE, None)
    kernel32 = _windows_kernel32()
    if job_handle:
        kernel32.CloseHandle(job_handle)
    if gate_handle:
        kernel32.CloseHandle(gate_handle)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    job_handle = getattr(process, _WINDOWS_JOB_HANDLE_ATTRIBUTE, None)
    try:
        if os.name == "nt" and job_handle:
            _windows_kernel32().TerminateJobObject(job_handle, 1)
        elif os.name == "nt" and process.poll() is None:
            subprocess.run(
                ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if process.poll() is None:
                process.kill()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        _close_windows_resources(process)
    try:
        if process.poll() is None:
            process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


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
    launch_command = list(command)
    start_gate_handle: int | None = None
    if os.name == "nt" and max_output_bytes is not None:
        _ensure_windows_command_available(command, cwd=cwd, env=env)
        launch_command, start_gate_handle = _create_windows_start_gate(command)
    try:
        process = subprocess.Popen(
            launch_command,
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
    except Exception:
        if start_gate_handle:
            _windows_kernel32().CloseHandle(start_gate_handle)
        raise
    if start_gate_handle:
        setattr(
            process,
            _WINDOWS_START_GATE_HANDLE_ATTRIBUTE,
            start_gate_handle,
        )
    try:
        _attach_windows_job(process)
        _release_windows_start_gate(process)
    except Exception:
        terminate_process_tree(process)
        raise
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

    chunks: queue.Queue[tuple[str, int | None]] = queue.Queue(maxsize=16)
    stop_readers = threading.Event()
    feeder_errors: list[BaseException] = []
    pending_chunks: dict[int, tuple[str, str]] = {}
    pending_lock = threading.Lock()
    next_chunk_sequence = 0

    def drain(name: str, pipe: object) -> None:
        nonlocal next_chunk_sequence
        reader = getattr(pipe, "read")
        try:
            while not stop_readers.is_set():
                chunk = reader(8192)
                if chunk == "":
                    break
                with pending_lock:
                    sequence = next_chunk_sequence
                    next_chunk_sequence += 1
                    pending_chunks[sequence] = (name, chunk)
                while not stop_readers.is_set():
                    try:
                        chunks.put((name, sequence), timeout=0.1)
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
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout),
            name=f"process-stdout-{process.pid}",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr),
            name=f"process-stderr-{process.pid}",
            daemon=True,
        ),
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

    def save_chunk(name: str, chunk: str) -> bool:
        nonlocal captured_bytes
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
        return chunk_bytes > remaining

    def finalize_captured_prefix() -> tuple[bool, bool]:
        stop_readers.set()
        deadline = time.monotonic() + 2
        for thread in reader_threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        complete = not any(thread.is_alive() for thread in reader_threads)
        with pending_lock:
            remaining_chunks = sorted(pending_chunks.items())
            pending_chunks.clear()
        limit_exceeded = False
        for _, (name, chunk) in remaining_chunks:
            if save_chunk(name, chunk):
                limit_exceeded = True
                break
        return limit_exceeded, complete

    try:
        while process.poll() is None or len(completed_streams) < 2:
            if feeder_errors:
                terminate_process_tree(process)
                raise RuntimeError("Failed to stream process stdin") from feeder_errors[0]
            if control is not None and control.cancel_requested():
                terminate_process_tree(process)
                limit_exceeded, complete_prefix = finalize_captured_prefix()
                stdout, stderr = captured_streams()
                if limit_exceeded:
                    raise ProcessOutputLimitExceeded(
                        command, max_output_bytes, stdout, stderr
                    )
                raise ProcessCancelled(
                    f"Cancelled while running {kind} (PID {process.pid})",
                    stdout=stdout,
                    stderr=stderr,
                    complete_captured_prefix_saved=complete_prefix,
                )
            elapsed = time.monotonic() - process_started_at
            if elapsed >= timeout_seconds:
                terminate_process_tree(process)
                limit_exceeded, complete_prefix = finalize_captured_prefix()
                stdout, stderr = captured_streams()
                if limit_exceeded:
                    raise ProcessOutputLimitExceeded(
                        command, max_output_bytes, stdout, stderr
                    )
                timeout = subprocess.TimeoutExpired(
                    command, timeout_seconds, stdout, stderr
                )
                timeout.complete_captured_prefix_saved = complete_prefix
                raise timeout
            try:
                name, sequence = chunks.get(
                    timeout=min(0.2, max(0.001, timeout_seconds - elapsed))
                )
            except queue.Empty:
                continue
            if sequence is None:
                completed_streams.add(name)
                continue
            with pending_lock:
                pending = pending_chunks.pop(sequence, None)
            if pending is None:
                continue
            _, chunk = pending
            if save_chunk(name, chunk):
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
        terminate_process_tree(process)
        deadline = time.monotonic() + 2
        if feeder_thread is not None:
            feeder_thread.join(timeout=max(0, deadline - time.monotonic()))
        for thread in reader_threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
        for thread in (
            *((feeder_thread,) if feeder_thread is not None else ()),
            *reader_threads,
        ):
            if thread.is_alive():
                thread.join(timeout=0.5)
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
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout),
            name=f"process-stdout-{process.pid}",
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr),
            name=f"process-stderr-{process.pid}",
            daemon=True,
        ),
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
