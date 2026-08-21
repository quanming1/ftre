"""Legacy module alias for the moved send_message Tool."""

import sys as _sys

from ftre.services.tools.builtin import send_message as _target

_sys.modules[__name__] = _target
