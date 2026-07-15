import importlib
import sys
import types

import pytest


class FakeBaseBinjaInterface:
    def __init__(self, *args, **kwargs):
        self.bv = kwargs.get("bv")
        self.artifact_watchers_started = False

    def _init_gui_components(self, *args, **kwargs):
        return True

    def start_artifact_watchers(self):
        self.artifact_watchers_started = True


class FakeController:
    def __init__(self, decompiler_interface=None):
        self.deci = decompiler_interface
        self.connected = False
        self.stopped = False

    def check_client(self):
        return self.connected

    def stop_worker_routines(self):
        self.stopped = True


class FakeSidebarWidget:
    def __init__(self, name):
        self.name = name
        self.layout = None

    def setLayout(self, layout):
        self.layout = layout


class FakeLayout:
    def __init__(self):
        self.widgets = []

    def addWidget(self, widget):
        self.widgets.append(widget)


def import_binja_module(monkeypatch):
    fake_binja_interface_mod = types.ModuleType("declib.decompilers.binja.interface")
    fake_binja_interface_mod.BinjaInterface = FakeBaseBinjaInterface
    monkeypatch.setitem(sys.modules, "declib.decompilers.binja.interface", fake_binja_interface_mod)

    fake_ui = types.ModuleType("binaryninjaui")
    fake_ui.UIAction = object
    fake_ui.UIActionHandler = object
    fake_ui.Menu = object
    fake_ui.SidebarWidget = FakeSidebarWidget
    fake_ui.SidebarWidgetType = object
    fake_ui.Sidebar = object
    monkeypatch.setitem(sys.modules, "binaryninjaui", fake_ui)

    module_name = "binsync.interface_overrides.binja"
    parent_module = importlib.import_module("binsync.interface_overrides")
    monkeypatch.setattr(parent_module, "binja", None, raising=False)
    delattr(parent_module, "binja")
    monkeypatch.setitem(sys.modules, module_name, None)
    sys.modules.pop(module_name)
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "BSController", FakeController)
    monkeypatch.setattr(module, "ControlPanel", lambda controller: {"controller": controller})
    monkeypatch.setattr(module, "QVBoxLayout", FakeLayout)
    return module


def test_binja_controller_for_bv_creates_bound_controller_per_view(monkeypatch):
    binja = import_binja_module(monkeypatch)
    plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
    plugin.controllers = {}

    bv_1 = object()
    bv_2 = object()

    controller_1 = plugin.controller_for_bv(bv_1)
    controller_1_again = plugin.controller_for_bv(bv_1)
    controller_2 = plugin.controller_for_bv(bv_2)

    assert controller_1 is controller_1_again
    assert controller_1 is not controller_2
    assert controller_1.deci.bv is bv_1
    assert controller_2.deci.bv is bv_2


@pytest.mark.parametrize(
    "has_current_view",
    [False, True],
    ids=["no-view", "successful-configuration"],
)
def test_binja_launch_config_handles_current_view(monkeypatch, has_current_view):
    binja = import_binja_module(monkeypatch)
    plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
    plugin.controllers = {}
    bv = object()
    configured_controllers = []

    class FakeView:
        def getData(self):
            return bv

    class FakeContext:
        def getCurrentView(self):
            return FakeView() if has_current_view else None

    class FakeActionContext:
        context = FakeContext()

    class FakeConfigureBSDialog:
        def __init__(self, controller):
            self.controller = controller

        def exec_(self):
            configured_controllers.append(self.controller)
            self.controller.connected = True

    monkeypatch.setattr(binja, "ConfigureBSDialog", FakeConfigureBSDialog)

    plugin._launch_bs_config(FakeActionContext())

    if not has_current_view:
        assert plugin.controllers == {}
        assert configured_controllers == []
        return

    controller = plugin.controllers[bv]
    assert plugin.bv is bv
    assert configured_controllers == [controller]
    assert controller.connected is True
    assert controller.deci.artifact_watchers_started is True


@pytest.mark.parametrize(
    "has_binary_view",
    [True, False],
    ids=["valid-view", "missing-view"],
)
def test_binja_sidebar_handles_binary_view(monkeypatch, has_binary_view):
    binja = import_binja_module(monkeypatch)
    plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
    plugin.controllers = {}

    bv = object() if has_binary_view else None

    if not has_binary_view:
        with pytest.raises(ValueError, match="BinaryView is required"):
            binja.BinSyncSidebarWidget(bv, plugin)

        assert plugin.controllers == {}
        return

    widget = binja.BinSyncSidebarWidget(bv, plugin)

    assert widget._controller.deci.bv is bv
    assert widget._widget == {"controller": widget._controller}
    assert widget.layout.widgets == [widget._widget]


def test_binja_stop_controllers_stops_and_clears_all_controllers(monkeypatch):
    binja = import_binja_module(monkeypatch)
    plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
    stop_attempts = []

    class StopController:
        def __init__(self, name, raises=False):
            self.name = name
            self.raises = raises
            self.stopped = False

        def stop_worker_routines(self):
            stop_attempts.append(self.name)
            if self.raises:
                raise RuntimeError("failed to stop")
            self.stopped = True

    controllers = [
        StopController("first"),
        StopController("failing", raises=True),
        StopController("last"),
    ]
    plugin.controllers = {object(): controller for controller in controllers}

    plugin.stop_controllers()

    assert stop_attempts == ["first", "failing", "last"]
    assert controllers[0].stopped is True
    assert controllers[1].stopped is False
    assert controllers[2].stopped is True
    assert plugin.controllers == {}
