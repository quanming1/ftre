"""Legacy module alias for moved Command types."""

import sys as _sys

from ftre.services.command import types as _target

_sys.modules[__name__] = _target
