"""Abstract camera source interface."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class FrameResult:
    """Result of a single frame capture."""
    png_bytes: bytes
    width: int
    height: int
    source: str  # "airsim", "mock", etc.


class CameraSource(ABC):
    """Abstract interface for camera frame providers."""

    @abstractmethod
    async def capture_frame(self, camera_name: str, image_type: str = "scene") -> FrameResult:
        """Capture a single frame from the named camera."""
        ...

    @abstractmethod
    async def get_stream(self, camera_name: str, fps: float = 5.0) -> AsyncIterator[bytes]:
        """Yield JPEG frames at the requested fps."""
        ...

    @abstractmethod
    async def set_camera_pose(self, camera_name: str, pitch_deg: float,
                               roll_deg: float, yaw_deg: float) -> None:
        """Set camera gimbal orientation."""
        ...

    @abstractmethod
    def get_available_cameras(self) -> list[str]:
        """Return list of available camera names."""
        ...

    @abstractmethod
    def get_info(self) -> dict:
        """Return camera system info."""
        ...
