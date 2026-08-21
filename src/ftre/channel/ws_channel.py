"""Legacy module alias for the moved WebSocketChannel."""

import sys as _sys

from ftre.services.messaging.channel.providers.websocket import channel as _target

_sys.modules[__name__] = _target
