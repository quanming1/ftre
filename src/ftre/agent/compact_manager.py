"""Legacy module alias for the moved CompactManager."""

import sys as _sys

from ftre.services.agent.runtime.compaction import manager as _target

_sys.modules[__name__] = _target
