"""Pure storage/result bounds, distinct from provider quota reservations."""
import math

from google.cloud.firestore_v1._helpers import encode_dict
from google.cloud.firestore_v1.types import Document

from app.execution.errors import OutputLimitExceeded

MAX_TOOL_INPUT_BYTES = 32_000
MAX_TOOL_OUTPUT_BYTES = 32_000
MAX_SESSION_BYTES = 512_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 20_000


def bounded_json(value, maximum, *, tool=None):
    """Check JSON-compatible values without constructing their serialized copy.

    This bounds application serialization, not an SDK's initial allocation.
    Container counts/depth and finite numerics are bounded independently.
    """
    size, nodes, ancestors = 0, 0, set()

    def add(amount):
        nonlocal size
        size += amount
        if size > maximum:
            raise OutputLimitExceeded(tool)

    def text(item):
        if len(item) > maximum - size:
            raise OutputLimitExceeded(tool)
        # Worst-case JSON escaping is six bytes per control character. Count
        # exactly the escape category without copying a possibly large string.
        add(2)
        for character in item:
            add(6 if ord(character) < 32 else 2 if character in {'"', '\\'} else len(character.encode("utf-8")))

    def visit(item, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise OutputLimitExceeded(tool)
        if isinstance(item, str):
            text(item)
        elif item is None or type(item) is bool:
            add(5)
        elif type(item) is int:
            if not -(2 ** 63) <= item <= 2 ** 63 - 1:
                raise OutputLimitExceeded(tool)
            add(len(str(item)))
        elif type(item) is float:
            if not math.isfinite(item):
                raise OutputLimitExceeded(tool)
            add(len(repr(item)))
        elif isinstance(item, (dict, list, tuple)):
            if id(item) in ancestors or len(item) > MAX_JSON_NODES - nodes:
                raise OutputLimitExceeded(tool)
            ancestors.add(id(item))
            add(2 + len(item))
            try:
                if isinstance(item, dict):
                    for key, child in item.items():
                        if not isinstance(key, str):
                            raise OutputLimitExceeded(tool)
                        text(key)
                        add(1)
                        visit(child, depth + 1)
                else:
                    for child in item:
                        visit(child, depth + 1)
            finally:
                ancestors.remove(id(item))
        else:
            raise OutputLimitExceeded(tool)

    try:
        visit(value, 0)
    except UnicodeError:
        raise OutputLimitExceeded(tool) from None
    return value


def bounded_session(session):
    bounded_json(session, MAX_SESSION_BYTES)
    # Also measure the actual Firestore value encoding, not only a JSON proxy.
    encoded = Document(fields=encode_dict(session))
    if len(Document.pb(encoded).SerializeToString()) > MAX_SESSION_BYTES:
        raise OutputLimitExceeded()
    return session
