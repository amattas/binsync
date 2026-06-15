import importlib
import sys
import types


class FakeBaseBinjaInterface:
    def __init__(self, *args, **kwargs):
        self.bv = kwargs.get("bv")

    def _init_gui_components(self, *args, **kwargs):
        return True


class FakeController:
    def __init__(self, decompiler_interface=None):
        self.deci = decompiler_interface
        self.stopped = False

    def check_client(self):
        return False

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

    sys.modules.pop("binsync.interface_overrides.binja", None)
    module = importlib.import_module("binsync.interface_overrides.binja")
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


def test_binja_launch_config_without_current_view_is_noop(monkeypatch):
    binja = import_binja_module(monkeypatch)
    plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
    plugin.controllers = {}

    class FakeContext:
        def getCurrentView(self):
            return None

    class FakeActionContext:
        context = FakeContext()

    plugin._launch_bs_config(FakeActionContext())

    assert plugin.controllers == {}


def test_binja_sidebar_creates_controller_for_binary_view(monkeypatch):
    binja = import_binja_module(monkeypatch)
    plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
    plugin.controllers = {}

    bv = object()

    widget = binja.BinSyncSidebarWidget(bv, plugin)

    assert widget._controller.deci.bv is bv
    assert widget._widget == {"controller": widget._controller}
    assert widget.layout.widgets == [widget._widget]


def test_binja_stop_controllers_stops_and_clears_all_controllers(monkeypatch):
    binja = import_binja_module(monkeypatch)
    plugin = binja.BinjaBSInterface.__new__(binja.BinjaBSInterface)
    controller = FakeController()
    plugin.controllers = {object(): controller}

    plugin.stop_controllers()

    assert controller.stopped is True
    assert plugin.controllers == {}
