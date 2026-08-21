"""Legacy module alias for the moved ChannelManager."""

import sys as _sys

from ftre.services.messaging.channel import manager as _target

_sys.modules[__name__] = _target
