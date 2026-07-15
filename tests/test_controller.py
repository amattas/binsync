import threading
import tempfile
from collections import defaultdict

import networkx as nx
import pytest

from declib.artifacts import Comment, Function, GlobalVariable, Segment, Struct

from binsync.controller import BSController
from binsync.core.client import Client
from binsync.core.state import State


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


class FailingFunctionDict(dict):
    def __setitem__(self, key, value):
        raise RuntimeError("function set made no change")


class FailingCallgraphDecompilerInterface(FakeDecompilerInterface):
    def get_callgraph(self):
        raise AttributeError("'int' object has no attribute 'function'")


class PairedFunctionDecompilerInterface(FakeDecompilerInterface):
    def __init__(self):
        super().__init__()
        self.functions = {
            0x400100: Function(addr=0x400100, size=0x20, name="caller"),
            0x400200: Function(addr=0x400200, size=0x20, name="callee"),
        }


class BackendCallgraphDecompilerInterface(PairedFunctionDecompilerInterface):
    def get_callgraph(self):
        graph = nx.DiGraph()
        graph.add_edge(self.functions[0x400100], self.functions[0x400200])
        return graph


class CodeXrefDecompilerInterface(PairedFunctionDecompilerInterface):
    def xrefs_to(self, artifact, decompile=False, only_code=False):
        if artifact.addr == 0x400200 and only_code:
            return [self.functions[0x400100]]
        return []


@pytest.mark.parametrize(
    ("artifact", "collection_name", "force_push_method", "identifier"),
    [
        (
            Function(addr=0x400100, size=0x20, name="renamed_func"),
            "functions",
            "force_push_functions",
            0x400100,
        ),
        (
            GlobalVariable(addr=0x500000, name="global_counter", type_="int", size=4),
            "global_vars",
            "force_push_global_vars",
            0x500000,
        ),
        (Struct(name="Record", size=8, members={}), "structs", "force_push_types", "Record"),
        (
            Segment(name=".text", start_addr=0x400000, end_addr=0x401000, permissions="r-x"),
            "segments",
            "force_push_segments",
            ".text",
        ),
    ],
    ids=("function", "global-variable", "struct", "segment"),
)
def test_force_push_immediately_persists_artifact(
    artifact, collection_name, force_push_method, identifier
):
    with tempfile.TemporaryDirectory() as tmpdir:
        client = Client("user0", tmpdir, "fake_hash", init_repo=True)
        try:
            deci = FakeDecompilerInterface()
            getattr(deci, collection_name)[identifier] = artifact

            controller = BSController(decompiler_interface=deci, headless=True)
            controller.client = client

            getattr(controller, force_push_method)([identifier])

            persisted_state = client.get_state(user="user0", fetch_cache=False)
            assert getattr(persisted_state, collection_name)[identifier] == artifact
        finally:
            client.shutdown()


@pytest.mark.parametrize(
    ("comment_addr", "decompiler_comment", "expected_comment"),
    [
        (0x400108, "raw ghidra comment", "raw ghidra comment"),
        (
            0x400110,
            Comment(addr=0x400110, comment="comment artifact"),
            "comment artifact",
        ),
        (0x400130, "outside function", None),
    ],
    ids=("raw-string", "comment-artifact", "outside-function"),
)
def test_force_push_function_collects_only_in_range_comments(
    comment_addr, decompiler_comment, expected_comment
):
    with tempfile.TemporaryDirectory() as tmpdir:
        client = Client("user0", tmpdir, "fake_hash", init_repo=True)
        try:
            deci = FakeDecompilerInterface()
            deci.comments[comment_addr] = decompiler_comment
            controller = BSController(decompiler_interface=deci, headless=True)
            controller.client = client

            controller.force_push_functions([0x400100])

            persisted_state = client.get_state(user="user0", fetch_cache=False)
            if expected_comment is None:
                assert comment_addr not in persisted_state.comments
            else:
                assert persisted_state.comments[comment_addr].comment == expected_comment
                assert persisted_state.comments[comment_addr].func_addr == 0x400100
        finally:
            client.shutdown()


@pytest.mark.parametrize(
    ("decompiler_interface_cls", "expected_nodes", "expected_edges"),
    [
        (BackendCallgraphDecompilerInterface, {0x400100, 0x400200}, {(0x400100, 0x400200)}),
        (FailingCallgraphDecompilerInterface, {0x400100}, set()),
        (CodeXrefDecompilerInterface, {0x400100, 0x400200}, {(0x400100, 0x400200)}),
    ],
    ids=("backend-callgraph-edge", "backend-callgraph-failure", "code-xref-edge"),
)
def test_progress_callgraph_collects_backend_and_fallback_paths(
    decompiler_interface_cls, expected_nodes, expected_edges
):
    controller = BSController(decompiler_interface=decompiler_interface_cls(), headless=True)

    graph = controller.get_progress_callgraph()

    assert {func.addr for func in graph.nodes} == expected_nodes
    assert {(src.addr, dst.addr) for src, dst in graph.edges} == expected_edges
    for src_addr, dst_addr in expected_edges:
        assert (controller.deci.functions[src_addr], controller.deci.functions[dst_addr]) in graph.edges


def test_fill_function_continues_to_address_keyed_comments_when_function_set_noops():
    with tempfile.TemporaryDirectory() as tmpdir:
        client = Client("user0", tmpdir, "fake_hash", init_repo=True)
        try:
            deci = FakeDecompilerInterface()
            deci.functions = FailingFunctionDict(deci.functions)
            controller = BSController(decompiler_interface=deci, headless=True)
            controller.client = client

            user_state = State("user1")
            user_state.set_function(Function(addr=0x400100, size=0x20, name="remote_name"))
            user_state.set_comment(Comment(addr=0x400108, comment="remote comment", func_addr=0x400100))

            assert controller.fill_artifact(
                0x400100, artifact_type=Function, state=user_state, user="user1"
            ) is True
            assert deci.comments[0x400108].comment == "remote comment"
        finally:
            client.shutdown()


@pytest.mark.parametrize(
    ("blocking", "expected_result"),
    [(True, "manual sync ran"), (False, None)],
    ids=("blocking", "nonblocking"),
)
def test_schedule_job_runs_manual_work_when_auto_commit_is_disabled(blocking, expected_result):
    controller = BSController(decompiler_interface=FakeDecompilerInterface(), headless=True)
    controller._auto_commit_enabled = False
    controller.push_job_scheduler.start_worker_thread()
    job_ran = threading.Event()

    def manual_sync():
        job_ran.set()
        return "manual sync ran"

    try:
        result = controller.schedule_job(manual_sync, blocking=blocking)
        assert job_ran.wait(timeout=2)
        assert result == expected_result
    finally:
        controller.push_job_scheduler.stop_worker_thread()
