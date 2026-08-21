"""Legacy module alias for the moved EventBus implementation."""

import sys as _sys

from ftre.services.messaging.bus import bus as _target

_sys.modules[__name__] = _target
