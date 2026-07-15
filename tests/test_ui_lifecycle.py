from types import SimpleNamespace

import pytest

import binsync.ui.panel_tabs.activity_table as activity_module
import binsync.ui.panel_tabs.ctx_table as context_module
import binsync.ui.panel_tabs.functions_table as functions_module
import binsync.ui.panel_tabs.globals_table as globals_module
import binsync.ui.panel_tabs.types_table as types_module
from binsync.ui.control_panel import ControlPanel
from binsync.ui.panel_tabs.activity_table import ActivityTableView
from binsync.ui.panel_tabs.ctx_table import QCTXTable
from binsync.ui.panel_tabs.functions_table import FunctionTableView
from binsync.ui.panel_tabs.globals_table import GlobalsTableView
from binsync.ui.panel_tabs.types_table import TypesTableView
from binsync.ui.panel_tabs.util_panel import QUtilPanel
from declib.artifacts import Function, GlobalVariable, Struct


class FakeUtilitiesPanel:
    def __init__(self):
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True


class FakeWorker:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeSignal:
    def __init__(self, callback=None):
        self.callbacks = [] if callback is None else [callback]
        self.emitted = False

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        self.emitted = True
        for callback in self.callbacks:
            callback(*args)


class FakeThread:
    def __init__(self, running=True, stop_on_wait=False):
        self.quit_called = False
        self.wait_timeouts = []
        self._running = running
        self._stop_on_wait = stop_on_wait

    def isRunning(self):
        return self._running

    def quit(self):
        self.quit_called = True
        self._running = False

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        if self._stop_on_wait:
            self._running = False


class FakeAction:
    def __init__(self, text, parent=None):
        self.text = text
        self.parent = parent
        self.triggered = FakeSignal()
        self.hovered = FakeSignal()

    def setCheckable(self, _checkable):
        pass

    def setChecked(self, _checked):
        pass


class FakeMenu:
    def __init__(self, parent=None, title=None):
        self.parent = parent
        self.title = title
        self.actions = {}
        self.submenus = {}
        self.hovered = FakeSignal()
        self.aboutToHide = FakeSignal()

    def setObjectName(self, _name):
        pass

    def addMenu(self, title):
        menu = FakeMenu(parent=self, title=title)
        self.submenus[title] = menu
        return menu

    def addAction(self, action_or_text, callback=None):
        action = action_or_text if isinstance(action_or_text, FakeAction) else FakeAction(action_or_text, parent=self)
        if callback is not None:
            action.triggered.connect(callback)
        self.actions[action.text] = action
        return action

    def addSeparator(self):
        pass

    def popup(self, _position):
        pass


class FakeIndex:
    @staticmethod
    def row():
        return 0

    @staticmethod
    def isValid():
        return True


class FakeProxyModel:
    @staticmethod
    def index(_row, _column):
        return FakeIndex()

    @staticmethod
    def mapToSource(index):
        return index


class FakePoint:
    @staticmethod
    def x():
        return -1

    @staticmethod
    def y():
        return -1


class FakeController:
    def __init__(self):
        self.fill_artifact = object()
        self.sync_all = object()
        self.scheduled_jobs = []

    def schedule_job(self, job, *args, **kwargs):
        self.scheduled_jobs.append((job, args, kwargs))


UI_DISPATCH_CASES = (
    pytest.param(
        SimpleNamespace(
            module=activity_module,
            view_cls=ActivityTableView,
            row_data=[["row-user", None, 0x400100, None]],
            action_path=("Sync",),
            target="fill_artifact",
            args=(0x400100,),
            kwargs={"artifact_type": Function, "user": "row-user"},
            valid_attr="_get_valid_funcs_for_user",
            valid_values=("0x400200",),
        ),
        id="activity-sync",
    ),
    pytest.param(
        SimpleNamespace(
            module=activity_module,
            view_cls=ActivityTableView,
            row_data=[["row-user", None, 0x400100, None]],
            action_path=("Sync-All",),
            target="sync_all",
            args=(),
            kwargs={"user": "row-user"},
            valid_attr="_get_valid_funcs_for_user",
            valid_values=("0x400200",),
        ),
        id="activity-sync-all",
    ),
    pytest.param(
        SimpleNamespace(
            module=activity_module,
            view_cls=ActivityTableView,
            row_data=[["row-user", None, 0x400100, None]],
            action_path=("Sync from row-user for...", "0x400200"),
            target="fill_artifact",
            args=(0x400200,),
            kwargs={"artifact_type": Function, "user": "row-user"},
            valid_attr="_get_valid_funcs_for_user",
            valid_values=("0x400200",),
        ),
        id="activity-function-submenu",
    ),
    pytest.param(
        SimpleNamespace(
            module=context_module,
            view_cls=QCTXTable,
            row_data=[["row-user", "remote_name", None]],
            saved_ctx=0x400100,
            action_path=("Sync",),
            target="fill_artifact",
            args=(0x400100,),
            kwargs={"artifact_type": Function, "user": "row-user"},
        ),
        id="context-sync",
    ),
    pytest.param(
        SimpleNamespace(
            module=functions_module,
            view_cls=FunctionTableView,
            row_data=[[0x400100, "remote_name", "row-user", None]],
            action_path=("Sync",),
            target="fill_artifact",
            args=(0x400100,),
            kwargs={"artifact_type": Function, "user": "row-user"},
            valid_attr="_get_valid_users_for_func",
            valid_values=("other-user",),
        ),
        id="function-sync",
    ),
    pytest.param(
        SimpleNamespace(
            module=functions_module,
            view_cls=FunctionTableView,
            row_data=[[0x400100, "remote_name", "row-user", None]],
            action_path=("Sync from...", "other-user"),
            target="fill_artifact",
            args=(0x400100,),
            kwargs={"artifact_type": Function, "user": "other-user"},
            valid_attr="_get_valid_users_for_func",
            valid_values=("other-user",),
        ),
        id="function-user-submenu",
    ),
    pytest.param(
        SimpleNamespace(
            module=globals_module,
            view_cls=GlobalsTableView,
            row_data=[[0x500000, "remote_global", "row-user", None]],
            action_path=("Sync from...", "other-user"),
            target="fill_artifact",
            args=(0x500000,),
            kwargs={"artifact_type": GlobalVariable, "user": "other-user"},
            valid_attr="_get_valid_users_for_gvar",
            valid_values=("other-user",),
        ),
        id="global-user-submenu",
    ),
    pytest.param(
        SimpleNamespace(
            module=types_module,
            view_cls=TypesTableView,
            row_data=[["Struct", "remote_type", "row-user", None]],
            action_path=("Sync from...", "other-user"),
            target="fill_artifact",
            args=("remote_type",),
            kwargs={"artifact_type": Struct, "user": "other-user"},
            valid_attr="_get_valid_users_for_type",
            valid_values=("other-user",),
        ),
        id="type-user-submenu",
    ),
)


@pytest.mark.parametrize("callbacks_owned", [True, False], ids=("owned", "foreign"))
def test_control_panel_close_only_clears_registered_callbacks(callbacks_owned):
    controller = SimpleNamespace()
    utilities_panel = FakeUtilitiesPanel()
    panel = SimpleNamespace(
        controller=controller,
        _utilities_panel=utilities_panel,
        update_callback=object(),
        ctx_callback=object(),
    )
    foreign_update_callback = object()
    foreign_ctx_callback = object()
    controller.ui_callback = panel.update_callback if callbacks_owned else foreign_update_callback
    controller.ctx_change_callback = panel.ctx_callback if callbacks_owned else foreign_ctx_callback
    controller.client_init_callback = object()

    ControlPanel.closeEvent(panel, object())

    assert utilities_panel.shutdown_called is True
    assert controller.ui_callback is (None if callbacks_owned else foreign_update_callback)
    assert controller.ctx_change_callback is (None if callbacks_owned else foreign_ctx_callback)
    assert controller.client_init_callback is None


@pytest.mark.parametrize(
    ("running", "stop_on_wait", "use_signal", "expected_quit", "expected_waits"),
    [
        (True, True, True, False, [1000]),
        (True, False, True, True, [1000, 1000]),
        (False, False, False, False, []),
    ],
    ids=("stops-during-first-wait", "requires-quit", "already-stopped-without-signal"),
)
def test_util_panel_shutdown_handles_worker_thread_states(
    running, stop_on_wait, use_signal, expected_quit, expected_waits
):
    worker = FakeWorker()
    thread = FakeThread(running=running, stop_on_wait=stop_on_wait)
    panel = SimpleNamespace(
        client_worker=worker,
        client_thread=thread,
        stop_client_worker=FakeSignal(worker.stop) if use_signal else None,
    )

    QUtilPanel.shutdown(panel)

    assert worker.stopped is True
    assert thread.quit_called is expected_quit
    assert thread.wait_timeouts == expected_waits
    assert panel.client_worker is None
    assert panel.client_thread is None


@pytest.mark.parametrize("case", UI_DISPATCH_CASES)
def test_ui_sync_actions_schedule_expected_controller_job(monkeypatch, case):
    root_menus = []

    def make_menu(parent=None):
        menu = FakeMenu(parent=parent)
        root_menus.append(menu)
        return menu

    monkeypatch.setattr(case.module, "QMenu", make_menu)
    monkeypatch.setattr(case.module, "QAction", FakeAction)

    controller = FakeController()
    model = SimpleNamespace(
        row_data=case.row_data,
        saved_ctx=getattr(case, "saved_ctx", None),
    )
    table = SimpleNamespace(
        controller=controller,
        model=model,
        proxymodel=FakeProxyModel(),
        HEADER=case.view_cls.HEADER,
        column_visibility=[True] * len(case.view_cls.HEADER),
        rowAt=lambda _y: 0,
        mapToGlobal=lambda point: point,
        _col_hide_handler=lambda _index: None,
        reset_tooltip_state=lambda: None,
        bind_tooltip_menu=lambda _menu: None,
        handle_menu_hovered_action=lambda _action: None,
        show_tooltip=lambda *_args, **_kwargs: None,
    )
    for attr in ("COL_ADDR", "COL_KIND", "COL_NAME", "COL_USER"):
        if hasattr(case.view_cls, attr):
            setattr(table, attr, getattr(case.view_cls, attr))

    valid_attr = getattr(case, "valid_attr", None)
    if valid_attr == "_get_valid_users_for_type":
        setattr(table, valid_attr, lambda _name, _kind: iter(case.valid_values))
    elif valid_attr is not None:
        setattr(table, valid_attr, lambda _identifier: iter(case.valid_values))

    event = SimpleNamespace(pos=lambda: FakePoint())
    case.view_cls.contextMenuEvent(table, event)

    menu = root_menus[0]
    for submenu_name in case.action_path[:-1]:
        menu = menu.submenus[submenu_name]
    menu.actions[case.action_path[-1]].triggered.emit(False)

    assert controller.scheduled_jobs == [
        (getattr(controller, case.target), case.args, case.kwargs)
    ]
