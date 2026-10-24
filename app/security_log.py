import json
import logging

_log = logging.getLogger("security")


def log_security_event(event: str, **fields) -> None:
    _log.warning(json.dumps({"event": event, **fields}))
