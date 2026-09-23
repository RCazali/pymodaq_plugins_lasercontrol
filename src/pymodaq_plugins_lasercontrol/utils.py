# -*- coding: utf-8 -*-
"""
Per-machine configuration of the Laser Control plugin (PyMoDAQ plugin-template convention).

The user file lives in the PyMoDAQ local folder (``~/.pymodaq/config_lasercontrol.toml``) and is
therefore outside git, one per machine (cahier des charges T1.5). Missing entries are completed
from ``resources/config_template.toml``.
"""
from pathlib import Path

from pymodaq_utils.config import BaseConfig, USER  # noqa: F401  (USER kept for template parity)


class Config(BaseConfig):
    """Main class to deal with configuration values for this plugin"""
    config_template_path = Path(__file__).parent.joinpath('resources/config_template.toml')
    config_name = f"config_{__package__.split('pymodaq_plugins_')[1]}"


def config_get(config, *path, default=None):
    """Tolerant read of a config value (works with every pymodaq_utils BaseConfig version)."""
    try:
        return config(*path)
    except Exception:
        return default
