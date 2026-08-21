"""Legacy module alias for built-in tool diff helpers."""

import sys as _sys

from ftre.services.tools.builtin import _diff as _target

_sys.modules[__name__] = _target
