"""Legacy module alias for moved built-in Command handlers."""

import sys as _sys

from ftre.services.command import builtin as _target

_sys.modules[__name__] = _target
