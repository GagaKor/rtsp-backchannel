"""PTZ value types shared by the read-only capability report and PTZ control.

They live here so neither module has to import the other: ``capabilities.py``
re-imports these for its read-only capability report, and ``ptz.py`` uses
them for PTZ movement control, but neither module imports the other.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PtzSpaces:
    absolute_pan_tilt: bool = False
    absolute_zoom: bool = False
    relative_pan_tilt: bool = False
    relative_zoom: bool = False
    continuous_pan_tilt: bool = False
    continuous_zoom: bool = False


@dataclass(frozen=True)
class PtzNode:
    token: str
    spaces: PtzSpaces
    name: str | None = None
    maximum_presets: int | None = None
    home_supported: bool | None = None
    auxiliary_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class PtzServiceCapabilities:
    e_flip: bool | None = None
    reverse: bool | None = None
    get_compatible_configurations: bool | None = None
    move_status: bool | None = None
    status_position: bool | None = None


__all__ = [
    "PtzNode",
    "PtzServiceCapabilities",
    "PtzSpaces",
]
