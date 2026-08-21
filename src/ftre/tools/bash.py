"""Legacy module alias for the moved bash Tool."""

import sys as _sys

from ftre.services.tools.builtin import bash as _target

_sys.modules[__name__] = _target
