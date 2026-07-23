import os
import ctypes
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lean_loop.process_control import (
    ProcessCancelled,
    ProcessOutputLimitExceeded,
    run_controlled_process,
)
from lean_loop.queue import QueueProcessController, QueueStore


class _Control:
    def __init__(self) -> None:
        self.cancel = False
        self.started = threading.Event()
        self.pid = None
        self.active_pid = None
        self.finished = None

    def cancel_requested(self) -> bool:
        return self.cancel

    def process_started(self, pid: int, kind: str) -> None:
        self.pid = pid
        self.active_pid = pid
        self.started.set()

    def process_finished(self, pid: int) -> None:
        self.finished = pid
        if self.active_pid == pid:
            self.active_pid = None


class ControlledProcessTests(unittest.TestCase):
    def assert_control_cleaned(self, control: _Control) -> None:
        self.assertIsNotNone(control.pid)
        self.assertEqual(control.finished, control.pid)
        self.assertIsNone(control.active_pid)
        self.assertFalse(
            any(
                thread.is_alive()
                and thread.name == f"process-stdin-{control.pid}"
                for thread in threading.enumerate()
            )
        )
        self.assertFalse(
            any(
                thread.is_alive()
                and thread.name
                in {
                    f"process-stdout-{control.pid}",
                    f"process-stderr-{control.pid}",
                }
                for thread in threading.enumerate()
            )
        )

    @staticmethod
    def _windows_process_active(pid: int) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)

    def _wait_for_descendant_exit(self, pid_file: Path) -> int:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not pid_file.is_file():
            time.sleep(0.02)
        self.assertTrue(pid_file.is_file(), "descendant PID was not recorded")
        pid = int(pid_file.read_text(encoding="utf-8"))
        while time.monotonic() < deadline and self._windows_process_active(pid):
            time.sleep(0.02)
        self.assertFalse(
            self._windows_process_active(pid),
            f"descendant process {pid} survived collection cleanup",
        )
        return pid

    @staticmethod
    def _parent_exits_with_pipe_descendant_script(
        pid_file: Path, *, flood_output: bool = False
    ) -> str:
        child_script = (
            "import sys,time;"
            + (
                "sys.stdout.write('x'*200000);sys.stdout.flush();"
                if flood_output
                else ""
            )
            + "time.sleep(30)"
        )
        return (
            "import pathlib,subprocess,sys\n"
            "sys.stdin.readline()\n"
            f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}],"
            "stdout=sys.stdout,stderr=sys.stderr)\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid),encoding='utf-8')\n"
        )

    def test_cancellation_terminates_long_running_process(self) -> None:
        control = _Control()

        def request_cancel() -> None:
            self.assertTrue(control.started.wait(timeout=3))
            control.cancel = True

        thread = threading.Thread(target=request_cancel)
        thread.start()
        started = time.monotonic()
        with self.assertRaises(ProcessCancelled):
            run_controlled_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=20,
                kind="test",
                control=control,
            )
        thread.join(timeout=3)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(control.finished, control.pid)

    def test_database_cancel_request_stops_registered_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory))
            task = store.add_task(
                target_file="Main.lean",
                task_text="test cancellation",
                settings={},
            )
            store.claim_next(os.getpid())
            control = QueueProcessController(store, task["id"])

            def request_cancel() -> None:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if store.get_task(task["id"])["active_pid"]:
                        store.request_cancel(task["id"])
                        return
                    time.sleep(0.02)
                self.fail("process PID was not persisted")

            thread = threading.Thread(target=request_cancel)
            thread.start()
            with self.assertRaises(ProcessCancelled):
                run_controlled_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout_seconds=20,
                    kind="test",
                    control=control,
                )
            thread.join(timeout=3)
            row = store.get_task(task["id"])
            self.assertTrue(row["cancel_requested"])
            self.assertIsNone(row["active_pid"])

    def test_stream_progress_is_persisted_for_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory))
            task = store.add_task(
                target_file="Main.lean", task_text="stream", settings={}
            )
            store.claim_next(os.getpid())
            control = QueueProcessController(store, task["id"])
            control.process_progress(
                "api",
                {
                    "event": "response.reasoning_summary_text.delta",
                    "reasoning_events": 7,
                },
            )
            row = store.get_task(task["id"])
            self.assertIn("Model reasoning", row["activity_text"])
            self.assertIsNotNone(row["activity_at"])

    def test_bounded_collection_terminates_at_exact_captured_byte_limit(self) -> None:
        control = _Control()
        limit = 4096
        script = (
            "import sys\n"
            "sys.stdout.write('x' * 100000 + 'UNOBSERVED_TAIL')\n"
            "sys.stdout.flush()\n"
        )
        started = time.monotonic()
        with self.assertRaises(ProcessOutputLimitExceeded) as raised:
            run_controlled_process(
                [sys.executable, "-c", script],
                timeout_seconds=10,
                kind="bounded-output-test",
                max_output_bytes=limit,
                control=control,
            )
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(raised.exception.limit_bytes, limit)
        self.assertEqual(raised.exception.captured_bytes, limit)
        self.assertEqual(
            len(raised.exception.stdout.encode("utf-8"))
            + len(raised.exception.stderr.encode("utf-8")),
            limit,
        )
        self.assertNotIn("UNOBSERVED_TAIL", raised.exception.stdout)
        self.assert_control_cleaned(control)

    def test_bounded_collection_streams_large_stdin_while_draining_stdout(
        self,
    ) -> None:
        control = _Control()
        input_text = "i" * (512 * 1024)
        output_text = "o" * (512 * 1024)
        script = (
            "import sys\n"
            f"sys.stdout.write('o' * {len(output_text)})\n"
            "sys.stdout.flush()\n"
            "payload = sys.stdin.read()\n"
            "sys.stdout.write('\\nINPUT_BYTES=' + str(len(payload.encode('utf-8'))))\n"
            "sys.stdout.flush()\n"
        )
        started = time.monotonic()
        completed = run_controlled_process(
            [sys.executable, "-c", script],
            input_text=input_text,
            timeout_seconds=10,
            kind="bounded-duplex-test",
            max_output_bytes=2 * 1024 * 1024,
            control=control,
        )
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(completed.stdout.startswith(output_text))
        self.assertIn(f"INPUT_BYTES={len(input_text)}", completed.stdout)
        self.assert_control_cleaned(control)

    def test_bounded_collection_tolerates_child_closing_stdin(self) -> None:
        control = _Control()
        started = time.monotonic()
        completed = run_controlled_process(
            [sys.executable, "-c", "print('done')"],
            input_text="i" * (1024 * 1024),
            timeout_seconds=5,
            kind="bounded-closed-stdin-test",
            max_output_bytes=1024,
            control=control,
        )
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "done")
        self.assert_control_cleaned(control)

    def test_bounded_collection_timeout_covers_stdin_transfer(self) -> None:
        control = _Control()
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_controlled_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                input_text="i" * (2 * 1024 * 1024),
                timeout_seconds=1,
                kind="bounded-stdin-timeout-test",
                max_output_bytes=1024,
                control=control,
            )
        self.assertLess(time.monotonic() - started, 5)
        self.assert_control_cleaned(control)

    def test_bounded_collection_cancel_covers_stdin_transfer(self) -> None:
        control = _Control()

        def request_cancel() -> None:
            self.assertTrue(control.started.wait(timeout=3))
            time.sleep(0.1)
            control.cancel = True

        thread = threading.Thread(target=request_cancel)
        thread.start()
        started = time.monotonic()
        with self.assertRaises(ProcessCancelled):
            run_controlled_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                input_text="i" * (2 * 1024 * 1024),
                timeout_seconds=10,
                kind="bounded-stdin-cancel-test",
                max_output_bytes=1024,
                control=control,
            )
        thread.join(timeout=3)
        self.assertLess(time.monotonic() - started, 5)
        self.assert_control_cleaned(control)

    def test_bounded_collection_enforces_combined_stdout_stderr_limit(
        self,
    ) -> None:
        control = _Control()
        limit = 4096
        script = (
            "import sys\n"
            "sys.stdout.write('o' * 3000)\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('e' * 3000)\n"
            "sys.stderr.flush()\n"
        )
        with self.assertRaises(ProcessOutputLimitExceeded) as raised:
            run_controlled_process(
                [sys.executable, "-c", script],
                timeout_seconds=5,
                kind="bounded-combined-limit-test",
                max_output_bytes=limit,
                control=control,
            )
        self.assertEqual(raised.exception.captured_bytes, limit)
        self.assertEqual(
            len(raised.exception.stdout.encode("utf-8"))
            + len(raised.exception.stderr.encode("utf-8")),
            limit,
        )
        self.assertGreater(len(raised.exception.stdout), 0)
        self.assertGreater(len(raised.exception.stderr), 0)
        self.assert_control_cleaned(control)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree regression")
    def test_timeout_kills_descendant_after_parent_exits_with_inherited_pipes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "descendant.pid"
            control = _Control()
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_controlled_process(
                    [
                        sys.executable,
                        "-c",
                        self._parent_exits_with_pipe_descendant_script(pid_file),
                    ],
                    input_text="go\n",
                    timeout_seconds=1,
                    kind="descendant-timeout",
                    max_output_bytes=4096,
                    control=control,
                )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 5)
            descendant_pid = self._wait_for_descendant_exit(pid_file)
            self.assert_control_cleaned(control)
            print(
                "WINDOWS_PROCESS_TREE "
                f"mode=timeout elapsed_seconds={elapsed:.6f} "
                f"descendant_pid={descendant_pid} descendant_active=false "
                "process_finished=true active_pid_cleared=true"
            )

    @unittest.skipUnless(os.name == "nt", "Windows process-tree regression")
    def test_cancel_kills_descendant_after_parent_exits_with_inherited_pipes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "descendant.pid"
            control = _Control()

            def cancel_after_descendant_starts() -> None:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not pid_file.is_file():
                    time.sleep(0.02)
                control.cancel = True

            canceller = threading.Thread(target=cancel_after_descendant_starts)
            canceller.start()
            started = time.monotonic()
            with self.assertRaises(ProcessCancelled):
                run_controlled_process(
                    [
                        sys.executable,
                        "-c",
                        self._parent_exits_with_pipe_descendant_script(pid_file),
                    ],
                    input_text="go\n",
                    timeout_seconds=10,
                    kind="descendant-cancel",
                    max_output_bytes=4096,
                    control=control,
                )
            canceller.join(timeout=3)
            self.assertFalse(canceller.is_alive())
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 5)
            descendant_pid = self._wait_for_descendant_exit(pid_file)
            self.assert_control_cleaned(control)
            print(
                "WINDOWS_PROCESS_TREE "
                f"mode=cancel elapsed_seconds={elapsed:.6f} "
                f"descendant_pid={descendant_pid} descendant_active=false "
                "process_finished=true active_pid_cleared=true"
            )

    @unittest.skipUnless(os.name == "nt", "Windows process-tree regression")
    def test_output_limit_kills_descendant_after_parent_exits_with_inherited_pipes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "descendant.pid"
            control = _Control()
            started = time.monotonic()
            with self.assertRaises(ProcessOutputLimitExceeded):
                run_controlled_process(
                    [
                        sys.executable,
                        "-c",
                        self._parent_exits_with_pipe_descendant_script(
                            pid_file, flood_output=True
                        ),
                    ],
                    input_text="go\n",
                    timeout_seconds=10,
                    kind="descendant-output-limit",
                    max_output_bytes=4096,
                    control=control,
                )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 5)
            descendant_pid = self._wait_for_descendant_exit(pid_file)
            self.assert_control_cleaned(control)
            print(
                "WINDOWS_PROCESS_TREE "
                f"mode=output_limit elapsed_seconds={elapsed:.6f} "
                f"descendant_pid={descendant_pid} descendant_active=false "
                "process_finished=true active_pid_cleared=true"
            )


if __name__ == "__main__":
    unittest.main()
