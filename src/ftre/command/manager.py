"""Legacy module alias for the moved CommandManager."""

import sys as _sys

from ftre.services.command import manager as _target

_sys.modules[__name__] = _target
