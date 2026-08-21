"""Legacy module alias for the moved write Tool."""

import sys as _sys

from ftre.services.tools.builtin import write as _target

_sys.modules[__name__] = _target
