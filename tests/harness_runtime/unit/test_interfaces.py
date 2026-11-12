import inspect
import typing

from harness import interfaces

# Methods spec 5.2 lists for each module (P0 only).
SPEC_METHODS = {
    "TaskService": {"create", "claim", "heartbeat", "checkpoint", "commit"},
    "AgentRegistry": {"resolve"},
    "ContextBuilder": {"build"},
    "AgentRunner": {"run"},
    "ModelAdapter": {"next_step"},
    "ToolGateway": {"invoke"},
    "Connector": {"execute", "reconcile"},
    "ArtifactStore": {"put", "read"},
    "Verifier": {"verify"},
}


def test_spec_methods_present():
    for name, methods in SPEC_METHODS.items():
        assert methods <= set(dir(getattr(interfaces, name))), name


def test_async_where_spec_is_async():
    for name, methods in SPEC_METHODS.items():
        cls = getattr(interfaces, name)
        for method in methods:
            expected_async = (name, method) != ("AgentRegistry", "resolve")
            assert inspect.iscoroutinefunction(getattr(cls, method)) is expected_async, (name, method)


def test_annotations_resolve():
    for name in SPEC_METHODS:
        for fn in vars(getattr(interfaces, name)).values():
            if inspect.isfunction(fn):
                typing.get_type_hints(fn)
