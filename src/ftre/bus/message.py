"""Legacy module alias for the moved Bus message types."""

import sys as _sys

from ftre.services.messaging.bus import message as _target

_sys.modules[__name__] = _target
