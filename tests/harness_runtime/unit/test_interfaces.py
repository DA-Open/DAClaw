import inspect
import typing

from harness import interfaces

# Methods spec 5.2 lists for each module (P0), plus the P1/P2 entry points.
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
    # P1/P2: types only for now, so the implementations can be written against them.
    "SubscriptionRepository": {"create", "get", "list_for_scope", "update", "record_event", "consumed"},
    "OutboxRepository": {"enqueue", "get", "list_due", "update", "cancel_for_task"},
    "MemoryStore": {"put", "get", "search", "supersede"},
    "DeliveryService": {"reserve_notice", "dispatch"},
    "ProactiveController": {"on_event"},
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
