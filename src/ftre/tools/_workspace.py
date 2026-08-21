"""Legacy module alias for built-in workspace tool helpers."""

import sys as _sys

from ftre.services.tools.builtin import _workspace as _target

_sys.modules[__name__] = _target
