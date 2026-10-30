from .base import REGISTRY, NodeType


def get_type(type_name: str) -> NodeType:
    nt = REGISTRY.get(type_name)
    if nt is None:
        raise ValueError(f"Unknown node type '{type_name}'")
    return nt


def load_all() -> None:
    # Importing registers the node types.
    from . import ai, core, files, integrations, listener_triggers, rag, services, telephony, transform, triggers, voice  # noqa: F401


load_all()
