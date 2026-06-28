"""Domain pack hook system — chainable pre/post processing pipelines."""
from dataclasses import dataclass, field
from typing import Callable, Any


@dataclass
class DomainHooks:
    """Base class for domain pack hooks. Domain packs subclass this in hooks.py.

    All methods return lists of callables — they are executed in order (chainable).
    """

    def pre_ingest(self) -> list[Callable]:
        """Document text preprocessors. (text: str, meta: dict) -> str"""
        return []

    def post_extract(self) -> list[Callable]:
        """Post-extraction enhancers. (entities: list[dict], relations: list[dict], doc_id: str) -> tuple[list, list]"""
        return []

    def validators(self) -> list[Callable]:
        """Custom triplet validators. (subj_type: str, rel: str, obj_type: str, context: dict) -> bool"""
        return []


def run_pre_ingest(hooks_module, text: str, meta: dict) -> str:
    """Run all pre_ingest hooks in chain."""
    if hooks_module is None:
        return text

    hooks_obj = _get_hooks(hooks_module)
    if hooks_obj is None:
        return text

    for hook in hooks_obj.pre_ingest():
        text = hook(text, meta)
    return text


def run_post_extract(hooks_module, entities: list[dict], relations: list[dict], doc_id: str = "") -> tuple[list, list]:
    """Run all post_extract hooks in chain."""
    if hooks_module is None:
        return entities, relations

    hooks_obj = _get_hooks(hooks_module)
    if hooks_obj is None:
        return entities, relations

    for hook in hooks_obj.post_extract():
        entities, relations = hook(entities, relations, doc_id)
    return entities, relations


def run_validators(hooks_module, subj_type: str, rel: str, obj_type: str, context: dict | None = None) -> bool:
    """Run all custom validators. All must pass (AND semantics)."""
    if hooks_module is None:
        return True

    hooks_obj = _get_hooks(hooks_module)
    if hooks_obj is None:
        return True

    ctx = context or {}
    for validator in hooks_obj.validators():
        if not validator(subj_type, rel, obj_type, ctx):
            return False
    return True


def _get_hooks(hooks_module) -> DomainHooks | None:
    """Instantiate DomainHooks from a module. Finds the first DomainHooks subclass."""
    if hasattr(hooks_module, "_hooks_instance"):
        return hooks_module._hooks_instance

    for name in dir(hooks_module):
        obj = getattr(hooks_module, name)
        if isinstance(obj, type) and issubclass(obj, DomainHooks) and obj is not DomainHooks:
            instance = obj()
            hooks_module._hooks_instance = instance
            return instance
    return None
