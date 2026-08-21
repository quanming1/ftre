"""Legacy module alias for the moved Bus payload types."""

import sys as _sys

from ftre.services.messaging.bus import payloads as _target

_sys.modules[__name__] = _target
