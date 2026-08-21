"""Legacy re-export for Session message conversion."""

from ftre.services.session.message.converter import *
from ftre.services.session.message.converter import (
    _as_msg,  # noqa: F401 - legacy callers import this private helper
)
