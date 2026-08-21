"""Legacy module alias for the moved task Tool."""

import sys as _sys

from ftre.services.tools.builtin import task as _target

_sys.modules[__name__] = _target
