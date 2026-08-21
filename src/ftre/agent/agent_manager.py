"""Legacy module alias for the moved Agent profile manager."""

import sys as _sys

from ftre.services.agent.profile import manager as _target

_sys.modules[__name__] = _target
