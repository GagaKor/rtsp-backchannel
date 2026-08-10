"""Public Python API for ONVIF inspection and backchannel audio."""

from .capabilities import (
    CameraCapabilityProfile,
    CameraCapabilityReport,
    CameraCapabilityService,
    CameraCapabilityVersion,
    CameraCapabilityWarning,
    Media2CapabilityReport,
    PtzCapabilityReport,
    PtzNode,
    PtzServiceCapabilities,
    PtzSpaces,
    get_camera_capabilities,
)

from .onvif import (
    DeviceInfo,
    DiscoveredDevice,
    StreamUri,
    discover_devices,
    get_stream_uris,
)
from .playback import PlaybackResult, play_file
from .ptz import (
    PtzSession,
    PtzSessionOptions,
    PtzStatus,
    PtzVector,
    open_ptz_session,
)

__all__ = [
    "CameraCapabilityProfile",
    "CameraCapabilityReport",
    "CameraCapabilityService",
    "CameraCapabilityVersion",
    "CameraCapabilityWarning",
    "DeviceInfo",
    "DiscoveredDevice",
    "Media2CapabilityReport",
    "PlaybackResult",
    "PtzCapabilityReport",
    "PtzNode",
    "PtzServiceCapabilities",
    "PtzSession",
    "PtzSessionOptions",
    "PtzSpaces",
    "PtzStatus",
    "PtzVector",
    "StreamUri",
    "discover_devices",
    "get_camera_capabilities",
    "get_stream_uris",
    "open_ptz_session",
    "play_file",
]
