"""Legacy module alias for built-in tool truncation helpers."""

import sys as _sys

from ftre.services.tools.builtin import _truncate as _target

_sys.modules[__name__] = _target
