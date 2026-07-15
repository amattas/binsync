import io
import subprocess
import sys
from types import SimpleNamespace

import pytest

import binsync.interface_overrides.ghidra as ghidra_module
from binsync.interface_overrides.ghidra import ControlPanelWindow, GhidraRemoteInterfaceWrapper


class MockProcess:
    def __init__(self, returncode=None, timeout_on_first_wait=False):
        self.pid = 1234
        self.stderr = io.StringIO("")
        self.terminated = False
        self.killed = False
        self.waited = False
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts = []
        self._returncode = returncode
        self._timeout_on_first_wait = timeout_on_first_wait

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self.terminate_calls += 1

    def wait(self, timeout=None):
        self.waited = True
        self.wait_timeouts.append(timeout)
        if self._timeout_on_first_wait and len(self.wait_timeouts) == 1:
            raise subprocess.TimeoutExpired("ghidra-ui", timeout)
        self._returncode = 0

    def kill(self):
        self.killed = True
        self.kill_calls += 1
        self._returncode = -9


class MockServer:
    def __init__(self):
        self.socket_path = "/tmp/fake-ghidra.sock"
        self.started = False
        self.stopped = False
        self.stop_calls = 0
        self.waited = False
        self.requires_main_thread = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.stop_calls += 1

    def wait_for_shutdown(self):
        self.waited = True


def _patch_popen_capture(monkeypatch, mock_proc):
    """Patch subprocess.Popen to capture call args/kwargs and return mock_proc."""
    popen_calls = []

    def mock_popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return mock_proc

    monkeypatch.setattr("binsync.interface_overrides.ghidra.subprocess.Popen", mock_popen)
    return popen_calls


class TestGhidra:
    """Tests for the Ghidra remote interface wrapper and UI process lifecycle.

    Note: a plain class is used instead of unittest.TestCase because pytest
    parameterization does not work on TestCase methods and results in cleaner tests.

    This file covers GhidraRemoteInterfaceWrapper (launching/retrying the out-of-process
    Ghidra UI, discovering the DecompilerClient/server URL, and tearing the UI process and
    server down) and ControlPanelWindow.closeEvent's shutdown ordering. Headless BSController
    behavior belongs in test_controller.py, Binary Ninja interface-override behavior in
    test_binja.py, and decompiler-agnostic Qt panel shutdown / table context-menu dispatch in
    test_ui_panels.py.
    """

    @pytest.mark.parametrize(
        "socket_path",
        [None, "/tmp/declib.sock"],
        ids=("default-server", "explicit-server"),
    )
    def test_launch_server_url(self, monkeypatch, tmp_path, socket_path):
        """GhidraRemoteInterfaceWrapper.start_gui_in_new_process must only inject
        BINSYNC_GHIDRA_SERVER_URL into the child process env when a socket_path is given, so the
        launched UI process picks up an explicit server address without the default-discovery
        path being disturbed. Cases cover the default server (no socket_path) and an explicit
        socket_path.
        """
        mock_proc = MockProcess()
        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, raising=False)
        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, raising=False)
        monkeypatch.setattr(ghidra_module.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr("binsync.interface_overrides.ghidra.sleep", lambda _seconds: None)
        popen_calls = _patch_popen_capture(monkeypatch, mock_proc)

        proc = GhidraRemoteInterfaceWrapper.start_gui_in_new_process(socket_path=socket_path)

        assert proc is mock_proc
        launch_env = popen_calls[0][1]["env"]
        if socket_path is None:
            assert ghidra_module.BINSYNC_GHIDRA_SERVER_URL not in launch_env
        else:
            assert launch_env[ghidra_module.BINSYNC_GHIDRA_SERVER_URL] == f"unix://{socket_path}"

    @pytest.mark.parametrize(
        "explicit_log_path",
        [False, True],
        ids=("default-log-path", "explicit-log-path"),
    )
    def test_launch_log_path(self, monkeypatch, tmp_path, explicit_log_path):
        """GhidraRemoteInterfaceWrapper.start_gui_in_new_process must resolve the UI log file
        path from BINSYNC_GHIDRA_UI_LOG_PATH when set, otherwise fall back to a default path in
        the system temp directory, and redirect both stdout and stderr of the launched process to
        that same log file. Cases cover the default log path and an explicit log path.
        """
        mock_proc = MockProcess()
        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, raising=False)
        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, raising=False)
        monkeypatch.setattr(ghidra_module.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr("binsync.interface_overrides.ghidra.sleep", lambda _seconds: None)

        if explicit_log_path:
            expected_log_path = tmp_path / "ghidra-ui.log"
            monkeypatch.setenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, str(expected_log_path))
        else:
            expected_log_path = tmp_path / "binsync-ghidra-ui.log"

        popen_calls = _patch_popen_capture(monkeypatch, mock_proc)

        proc = GhidraRemoteInterfaceWrapper.start_gui_in_new_process(socket_path=None)

        assert proc is mock_proc
        launch_kwargs = popen_calls[0][1]
        assert launch_kwargs["env"][ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH] == str(expected_log_path)
        assert launch_kwargs["stdout"].name == str(expected_log_path)
        assert launch_kwargs["stderr"] is launch_kwargs["stdout"]

    @pytest.mark.parametrize(
        "server_url",
        [None, "unix:///tmp/declib.sock"],
        ids=("discover-default-server", "discover-explicit-server"),
    )
    def test_ui_discovers_server_url(self, monkeypatch, server_url):
        """start_ghidra_ui must call DecompilerClient.discover with the server_url read from
        BINSYNC_GHIDRA_SERVER_URL when set, and with no server_url kwarg when the env var is
        unset, so the UI connects to the correct backend server on startup. Cases cover the
        default (env var unset) and an explicit server URL.
        """
        discover_calls = []
        mock_deci = object()

        class MockDecompilerClient:
            @staticmethod
            def discover(*args, **kwargs):
                discover_calls.append((args, kwargs))
                return mock_deci

        class MockApplication:
            @staticmethod
            def instance():
                return MockApplication()

            def setQuitOnLastWindowClosed(self, _value):
                pass

            def exec(self):
                pass

        class MockControlPanelWindow:
            def __init__(self, deci=None):
                self.deci = deci

            def hide(self):
                pass

            def configure(self):
                return True

            def show(self):
                pass

        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, raising=False)
        if server_url is not None:
            monkeypatch.setenv(ghidra_module.BINSYNC_GHIDRA_SERVER_URL, server_url)
        monkeypatch.setattr("declib.api.decompiler_client.DecompilerClient", MockDecompilerClient)
        monkeypatch.setattr(ghidra_module, "QApplication", MockApplication)
        monkeypatch.setattr(ghidra_module, "ControlPanelWindow", MockControlPanelWindow)

        ghidra_module.start_ghidra_ui()

        expected_kwargs = {"server_url": server_url} if server_url is not None else {}
        assert discover_calls == [((), expected_kwargs)]

    @pytest.mark.parametrize(
        ("returncode", "timeout_on_first_wait", "expected_terminate_calls", "expected_waits", "expected_kill_calls"),
        [
            (None, False, 1, [3], 0),
            (None, True, 1, [3, 1], 1),
            (0, False, 0, [], 0),
        ],
        ids=("normal-process", "timeout-and-kill", "already-exited-process"),
    )
    def test_wrapper_shutdown(
        self, returncode, timeout_on_first_wait, expected_terminate_calls, expected_waits, expected_kill_calls
    ):
        """GhidraRemoteInterfaceWrapper.shutdown must terminate the UI process only if it is
        still running, escalate to kill() if it does not exit within the wait timeout, always
        stop the server exactly once, and be idempotent when called twice, so a stuck or already
        exited Ghidra UI process never leaks or blocks shutdown. Cases cover a normal running
        process, a process that times out on terminate and must be killed, and a process that has
        already exited before shutdown is called.
        """
        mock_proc = MockProcess(returncode=returncode, timeout_on_first_wait=timeout_on_first_wait)
        mock_server = MockServer()
        wrapper = GhidraRemoteInterfaceWrapper.__new__(GhidraRemoteInterfaceWrapper)
        wrapper.gui_process = mock_proc
        wrapper.server = mock_server

        wrapper.shutdown()
        wrapper.shutdown()

        assert mock_proc.terminate_calls == expected_terminate_calls
        assert mock_proc.wait_timeouts == expected_waits
        assert mock_proc.kill_calls == expected_kill_calls
        assert mock_server.stopped is True
        assert mock_server.stop_calls == 1

    @pytest.mark.parametrize("requires_main_thread", [True, False], ids=("main-thread", "background-thread"))
    def test_wrapper_main_thread_wait(self, monkeypatch, requires_main_thread):
        """GhidraRemoteInterfaceWrapper.__init__ must start the DecompilerServer and launch the
        UI process, and must block on server.wait_for_shutdown only when the server reports it
        requires the main thread, so servers needing the main event loop do not return control
        prematurely while background-thread servers do not block construction. Cases cover a
        server that requires the main thread and one that does not.
        """
        mock_proc = MockProcess()
        mock_server = MockServer()
        mock_server.requires_main_thread = requires_main_thread
        monkeypatch.setattr("binsync.interface_overrides.ghidra.sleep", lambda _seconds: None)
        monkeypatch.setattr("binsync.interface_overrides.ghidra.DecompilerServer", lambda **_kwargs: mock_server)
        monkeypatch.setattr("binsync.interface_overrides.ghidra.atexit.register", lambda _callback: None)
        monkeypatch.setattr(
            GhidraRemoteInterfaceWrapper,
            "start_gui_in_new_process",
            staticmethod(lambda socket_path=None: mock_proc),
        )

        wrapper = GhidraRemoteInterfaceWrapper()

        assert wrapper.gui_process is mock_proc
        assert mock_server.started is True
        assert mock_server.waited is requires_main_thread

    @pytest.mark.parametrize(
        ("shutdown_method", "expected_shutdown_call"),
        [("shutdown_server", "shutdown_server"), ("shutdown", "interface_shutdown")],
        ids=("modern-interface", "legacy-interface"),
    )
    def test_control_panel_close_shutdown(self, monkeypatch, shutdown_method, expected_shutdown_call):
        """ControlPanelWindow.closeEvent must stop the controller's worker routines, shut down
        the remote interface (using shutdown_server on modern interfaces or shutdown on legacy
        ones), and quit the QApplication, in that order, so closing the panel window always
        cleanly tears down background work before the interface and app exit. Cases cover a
        modern interface exposing shutdown_server and a legacy interface exposing only shutdown.
        """
        calls = []

        class MockController:
            def stop_worker_routines(self):
                calls.append("stop_workers")

            def shutdown(self):
                calls.append("controller_shutdown")

        monkeypatch.setattr("binsync.interface_overrides.ghidra.QTimer.singleShot", lambda _delay, callback: callback())
        monkeypatch.setattr("binsync.interface_overrides.ghidra.QApplication.quit", lambda: calls.append("quit"))

        remote_interface = SimpleNamespace()
        setattr(remote_interface, shutdown_method, lambda: calls.append(expected_shutdown_call))
        window = SimpleNamespace(controller=MockController(), _interface=remote_interface)

        ControlPanelWindow.closeEvent(window, object())

        assert calls == ["stop_workers", expected_shutdown_call, "quit"]

    @pytest.mark.parametrize("successful_attempt", [2, None], ids=("retry-succeeds", "exhausted"))
    def test_launch_retries_or_exhausts(self, monkeypatch, tmp_path, successful_attempt):
        """GhidraRemoteInterfaceWrapper.start_gui_in_new_process must retry launching the UI
        process through its fallback methods (starting with sys.executable, then the "binsync"
        entry point) when a launch attempt exits with a failure code, and must raise a
        RuntimeError once all methods are exhausted, so a flaky or misconfigured first launch
        method does not permanently prevent the UI from starting. Cases cover a retry that
        eventually succeeds and one where every method fails.
        """
        popen_calls = []
        processes = []
        monkeypatch.delenv(ghidra_module.BINSYNC_GHIDRA_UI_LOG_PATH, raising=False)
        monkeypatch.setattr(ghidra_module.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(ghidra_module, "sleep", lambda _seconds: None)

        def mock_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))
            attempt = len(popen_calls)
            proc = MockProcess(returncode=None if attempt == successful_attempt else 1)
            processes.append(proc)
            return proc

        monkeypatch.setattr(ghidra_module.subprocess, "Popen", mock_popen)

        if successful_attempt is None:
            with pytest.raises(RuntimeError, match="Exhausted all methods"):
                GhidraRemoteInterfaceWrapper.start_gui_in_new_process()
            assert len(popen_calls) == 3
        else:
            proc = GhidraRemoteInterfaceWrapper.start_gui_in_new_process()
            assert proc is processes[successful_attempt - 1]
            assert len(popen_calls) == successful_attempt

        assert popen_calls[0][0][0][0] == ghidra_module.sys.executable
        if len(popen_calls) > 1:
            assert popen_calls[1][0][0][0] == "binsync"


if __name__ == "__main__":
    pytest.main(args=sys.argv)
