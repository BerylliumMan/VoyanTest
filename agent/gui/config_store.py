"""Configuration persistence for VoyanTest Agent."""
import json
import base64
import os

DEFAULT_CONFIG_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'VoyanTest')
CONFIG_FILE = 'agent_config.json'


class ConfigStore:
    """Manages agent configuration persistence as JSON."""

    def __init__(self, config_dir: str | None = None):
        self._config_dir = config_dir or DEFAULT_CONFIG_DIR
        os.makedirs(self._config_dir, exist_ok=True)
        self._path = os.path.join(self._config_dir, CONFIG_FILE)

    def load(self) -> dict:
        """Load config from JSON file. Return defaults if file doesn't exist."""
        defaults = {
            "server_url": "ws://localhost:8002",
            "agent_name": "",
            "headless": False,
            "username": "",
            "password": "",
            "auto_connect": False,
            "minimize_to_tray": True,
            "window_geometry": "600x500+100+100",
        }
        if not os.path.exists(self._path):
            return defaults
        try:
            with open(self._path, 'r') as f:
                data = json.load(f)
            # Decode password
            if data.get("password") and len(data["password"]) > 10:
                try:
                    data["password"] = base64.b64decode(data["password"]).decode()
                except Exception:
                    pass  # already plaintext
            return {**defaults, **data}
        except Exception:
            return defaults

    def save(self, config: dict) -> None:
        """Save config to JSON file with base64-obfuscated password."""
        data = dict(config)
        # Obfuscate password
        if data.get("password"):
            data["password"] = base64.b64encode(data["password"].encode()).decode()
        with open(self._path, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @property
    def path(self) -> str:
        return self._path


def get_config_path() -> str:
    return os.path.join(DEFAULT_CONFIG_DIR, CONFIG_FILE)
