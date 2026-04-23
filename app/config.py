"""Backward-compatible settings shim.

The runtime settings surface is canonical in ``app.runtime_settings``. This
module remains only so older imports do not drift onto a second AppSettings
definition.
"""

from app.runtime_settings import AppSettings, get_settings

__all__ = ["AppSettings", "get_settings"]
