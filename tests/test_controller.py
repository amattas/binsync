from collections import defaultdict

from declib.artifacts import Function

from binsync.controller import BSController


class FakeDecompilerInterface:
    name = "fake"
    binary_hash = "fake_hash"
    default_func_prefix = "sub_"

    def __init__(self):
        self.artifact_change_callbacks = defaultdict(list)
        self.functions = {
            0x400100: Function(addr=0x400100, size=0x20, name="renamed_func")
        }
        self.comments = {}
        self.global_vars = {}
        self.enums = {}
        self.typedefs = {}
        self.structs = {}
        self.patches = {}
        self.segments = {}

    def get_func_size(self, addr):
        func = self.functions.get(addr)
        return func.size if func is not None else 0

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass

    def shutdown(self):
        pass

    def start_artifact_watchers(self):
        pass

    def stop_artifact_watchers(self):
        pass


class FailingCallgraphDecompilerInterface(FakeDecompilerInterface):
    def get_callgraph(self):
        raise AttributeError("'int' object has no attribute 'function'")


class CodeXrefDecompilerInterface(FakeDecompilerInterface):
    def __init__(self):
        super().__init__()
        self.functions = {
            0x400100: Function(addr=0x400100, size=0x20, name="caller"),
            0x400200: Function(addr=0x400200, size=0x20, name="callee"),
        }

    def xrefs_to(self, artifact, decompile=False, only_code=False):
        if artifact.addr == 0x400200 and only_code:
            return [self.functions[0x400100]]
        return []


def test_progress_callgraph_falls_back_to_decompiler_functions_when_backend_callgraph_fails():
    controller = BSController(decompiler_interface=FailingCallgraphDecompilerInterface(), headless=True)

    graph = controller.get_progress_callgraph()

    assert {func.addr for func in graph.nodes} == {0x400100}


def test_progress_callgraph_collects_code_xref_edges_without_backend_callgraph():
    controller = BSController(decompiler_interface=CodeXrefDecompilerInterface(), headless=True)

    graph = controller.get_progress_callgraph()

    assert (controller.deci.functions[0x400100], controller.deci.functions[0x400200]) in graph.edges
