"""Mock camera source — generates test frames with overlay text."""

import asyncio
import io
import time
from collections.abc import AsyncIterator

from .base import CameraSource, FrameResult

try:
    import numpy as np
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


class MockSource(CameraSource):
    """Camera source that generates synthetic test frames."""

    def _generate_frame(self, camera_name: str, width: int = 1920, height: int = 1080) -> bytes:
        if not CV2_AVAILABLE:
            return b""
        # Dark blue background
        img = np.zeros((height, width, 3), dtype=np.uint8)
        img[:, :] = (40, 30, 20)  # BGR dark

        ts = time.strftime("%H:%M:%S")
        lines = [
            f"MOCK CAMERA: {camera_name}",
            f"Time: {ts}",
            f"Resolution: {width}x{height}",
            "No AirSim connected",
        ]
        y = 60
        for line in lines:
            cv2.putText(img, line, (40, y), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
            y += 50

        _, png_buf = cv2.imencode('.png', img)
        return png_buf.tobytes()

    async def capture_frame(self, camera_name: str, image_type: str = "scene") -> FrameResult:
        resolutions = {"front_center": (1920, 1080), "bottom_center": (1280, 720)}
        w, h = resolutions.get(camera_name, (1920, 1080))
        png_bytes = self._generate_frame(camera_name, w, h)
        return FrameResult(png_bytes=png_bytes, width=w, height=h, source="mock")

    async def get_stream(self, camera_name: str, fps: float = 5.0) -> AsyncIterator[bytes]:
        interval = 1.0 / fps
        while True:
            start = time.monotonic()
            frame = await self.capture_frame(camera_name, "scene")
            if CV2_AVAILABLE:
                img_1d = np.frombuffer(frame.png_bytes, dtype=np.uint8)
                img = cv2.imdecode(img_1d, cv2.IMREAD_COLOR)
                _, jpeg_buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 60])
                yield jpeg_buf.tobytes()
            else:
                yield frame.png_bytes
            elapsed = time.monotonic() - start
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)

    async def set_camera_pose(self, camera_name: str, pitch_deg: float,
                               roll_deg: float, yaw_deg: float) -> None:
        pass  # No-op for mock

    def get_available_cameras(self) -> list[str]:
        return ["front_center", "bottom_center"]

    def get_info(self) -> dict:
        return {
            "source": "mock",
            "connected": True,
            "cameras": {
                "front_center": {"resolution": "1920x1080", "image_types": ["scene"], "gimbal": False},
                "bottom_center": {"resolution": "1280x720", "image_types": ["scene"], "gimbal": False},
            },
        }
