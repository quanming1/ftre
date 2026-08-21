"""Legacy module alias for the moved edit Tool."""

import sys as _sys

from ftre.services.tools.builtin import edit as _target

_sys.modules[__name__] = _target
