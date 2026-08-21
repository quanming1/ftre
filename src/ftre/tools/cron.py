"""Legacy module alias for the moved cron Tool."""

import sys as _sys

from ftre.services.tools.builtin import cron as _target

_sys.modules[__name__] = _target
