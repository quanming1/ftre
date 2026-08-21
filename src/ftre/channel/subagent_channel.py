"""Legacy module alias for the moved SubagentChannel."""

import sys as _sys

from ftre.services.messaging.channel.providers.subagent import channel as _target

_sys.modules[__name__] = _target
