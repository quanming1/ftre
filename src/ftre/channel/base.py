"""Legacy module alias for the moved Channel base contract."""

import sys as _sys

from ftre.services.messaging.channel import base as _target

_sys.modules[__name__] = _target
