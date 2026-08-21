"""Legacy module alias for the moved read Tool."""

import sys as _sys

from ftre.services.tools.builtin import read as _target

_sys.modules[__name__] = _target
