import logging
import os
import sys
import atexit
import tempfile
from pathlib import Path
from time import sleep
import subprocess

from declib.ui.version import set_ui_version
set_ui_version("PySide6")
from declib.ui.qt_objects import QMainWindow, QApplication, QTimer
from declib.api import DecompilerInterface
from declib.api.decompiler_server import DecompilerServer

from binsync.ui.control_panel import ControlPanel
from binsync.ui.config_dialog import ConfigureBSDialog
from binsync.controller import BSController

_l = logging.getLogger(__name__)
BINSYNC_GHIDRA_SERVER_URL = "BINSYNC_GHIDRA_SERVER_URL"
BINSYNC_GHIDRA_UI_LOG_PATH = "BINSYNC_GHIDRA_UI_LOG_PATH"


def _ghidra_ui_log_path():
    configured_path = os.environ.get(BINSYNC_GHIDRA_UI_LOG_PATH)
    return Path(configured_path) if configured_path else Path(tempfile.gettempdir()) / "binsync-ghidra-ui.log"


class ControlPanelWindow(QMainWindow):
    """
    The class for the window that shows changes/info to BinSync data. This includes things like
    changes to functions or structs.
    """

    def __init__(self, deci=None):
        super(ControlPanelWindow, self).__init__()
        self.setWindowTitle("BinSync")
        self.width_hint = 300

        self._interface = deci or DecompilerInterface.discover()
        self.controller = BSController(decompiler_interface=self._interface)
        self.control_panel = ControlPanel(self.controller)
        self._init_widgets()

    def _init_widgets(self):
        self.control_panel.show()
        self.setCentralWidget(self.control_panel)

    #
    # handlers
    #

    def configure(self):
        config = ConfigureBSDialog(self.controller)
        config.exec_()
        return self.controller.check_client()

    def closeEvent(self, event):
        try:
            self.controller.stop_worker_routines()
        except Exception:
            _l.exception("Failed to stop BinSync worker routines before closing Ghidra UI")

        try:
            if hasattr(self._interface, "shutdown_server"):
                self._interface.shutdown_server()
            else:
                self._interface.shutdown()
        except Exception:
            _l.exception("Failed to shut down Ghidra remote interface")
        # Brief delay to allow threads to finish cleanup
        # With the Scheduler timeout fix, threads should exit quickly
        QTimer.singleShot(200, QApplication.quit)


def start_ghidra_ui():
    from declib.api.decompiler_client import DecompilerClient
    server_url = os.environ.get(BINSYNC_GHIDRA_SERVER_URL)
    deci = DecompilerClient.discover(server_url=server_url) if server_url else DecompilerClient.discover()
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    # Prevent the application from quitting when the last window is closed
    app.setQuitOnLastWindowClosed(False)
    cp_window = ControlPanelWindow(deci=deci)

    # control panel should stay hidden until a good config happens
    cp_window.hide()
    connected = cp_window.configure()
    if connected:
        cp_window.show()
    else:
        sys.exit(1)
    app.exec()

class GhidraRemoteInterfaceWrapper:
    """
    This class is a wrapper class to start the Ghidra Interface with a server so that the GUI can connect in
    another process.
    """

    def __init__(self, *args, **kwargs):
        #import remote_pdb; remote_pdb.RemotePdb('localhost', 4444).set_trace()
        self.server = DecompilerServer(force_decompiler="ghidra")
        self.gui_process = None
        self._shutdown_done = False
        self.server.start()
        atexit.register(self.shutdown)
        sleep(1)
        _l.info("Server started on socket: %s", self.server.socket_path)
        self.gui_process = self.start_gui_in_new_process(socket_path=self.server.socket_path)
        if getattr(self.server, "requires_main_thread", False):
            self.server.wait_for_shutdown()

    @staticmethod
    def start_gui_in_new_process(socket_path=None):
        _l.info("Starting the Ghidra BinSync UI in a new process...")
        # Try command sets in order of preference.
        # We prefer sys.executable to ensure the current Python environment is used.
        commands = [
            [sys.executable, "-m", "binsync", "-s", "ghidra"],
            ["binsync", "-s", "ghidra"],
            ["python", "-m", "binsync", "-s", "ghidra"],
        ]

        proc = None
        env = os.environ.copy()
        if socket_path:
            env[BINSYNC_GHIDRA_SERVER_URL] = f"unix://{socket_path}"
        log_path = _ghidra_ui_log_path()
        env[BINSYNC_GHIDRA_UI_LOG_PATH] = str(log_path)

        for cmd in commands:
            _l.info(f"Attempting to start UI with command: {' '.join(cmd)}")
            log_file = None
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_file = log_path.open("a", buffering=1, encoding="utf-8")
                log_file.write(f"\n--- Starting Ghidra BinSync UI: {' '.join(cmd)} ---\n")
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=log_file,
                    env=env,
                    text=True
                )
                sleep(1)
                
                # Check if process is still running after 1 second
                # poll() returns None if process is running
                if proc.poll() is None:
                    _l.info(f"Successfully started UI process with PID {proc.pid}")
                    break
                else:
                    _l.warning("Process exited prematurely; see Ghidra BinSync UI log: %s", log_path)
            except Exception as e:
                _l.warning(f"Failed to run command '{cmd[0]}': {e}")
            finally:
                if log_file is not None:
                    try:
                        log_file.close()
                    except Exception:
                        pass
                
        if proc is None:
             raise RuntimeError("Exhausted all methods to start the Ghidra BinSync UI.")
        
        # Check if the process exited prematurely (if the loop finished without breaking)
        if proc.poll() is not None:
             raise RuntimeError("Exhausted all methods to start the Ghidra BinSync UI.")
             
        _l.info("Ghidra BinSync UI process started with PID %d", proc.pid)
        return proc

    def shutdown(self):
        if getattr(self, "_shutdown_done", False):
            return

        self._shutdown_done = True
        proc = getattr(self, "gui_process", None)
        if proc is not None and proc.poll() is None:
            _l.info("Terminating Ghidra BinSync UI process with PID %d", proc.pid)
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _l.warning("Ghidra BinSync UI process did not exit after terminate; killing it.")
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _l.warning("Ghidra BinSync UI process did not exit after kill.")
            except Exception as e:
                _l.warning("Failed to terminate Ghidra BinSync UI process: %s", e)

        server = getattr(self, "server", None)
        if server is not None:
            try:
                server.stop()
            except Exception as e:
                _l.warning("Failed to stop Ghidra BinSync server: %s", e)

    @property
    def gui_plugin(self):
        """
        Just a stub to conform to the interface expected by the decompiler.
        """
        return None
