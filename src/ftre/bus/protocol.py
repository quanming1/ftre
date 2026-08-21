"""Legacy module alias for the moved Bus protocol types."""

import sys as _sys

from ftre.services.messaging.bus import protocol as _target

_sys.modules[__name__] = _target
