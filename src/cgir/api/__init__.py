"""HTTP API surface — a thin FastAPI layer over :mod:`cgir.pipeline`."""

import re

_COMPONENT_ID_RE = re.compile(r"[A-Za-z0-9_.:-]+")


def valid_component_id(component_id: str) -> bool:
    """Component ids are dotted qualnames (letters, digits, ``_ . : -``). Both
    API surfaces build ``components/<id>.json`` paths from caller input; a path
    separator or ``..`` would read arbitrary ``*.json`` on the host."""
    return bool(_COMPONENT_ID_RE.fullmatch(component_id)) and ".." not in component_id
