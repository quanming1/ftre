"""Legacy module alias for the moved plan Tool."""

import sys as _sys

from ftre.services.tools.builtin import plan as _target

_sys.modules[__name__] = _target
