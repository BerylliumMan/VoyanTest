"""VoyanTest Agent GUI package."""

from agent.gui.config_store import ConfigStore

try:
    from agent.gui.config_dialog import ConfigDialog
except ImportError:
    ConfigDialog = None  # type: ignore[assignment]

__all__ = ["ConfigStore", "ConfigDialog"]
