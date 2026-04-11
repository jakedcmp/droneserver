"""AirSim camera source — captures frames from Cosys-AirSim simulator."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import math
import os
import time
from collections.abc import AsyncIterator

from .base import CameraSource, FrameResult

logger = logging.getLogger("perception")

try:
    import cosysairsim as airsim
    AIRSIM_AVAILABLE = True
except ImportError:
    AIRSIM_AVAILABLE = False

try:
    import numpy as np
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


class AirSimSource(CameraSource):
    """Camera source backed by Cosys-AirSim RPC."""

    def __init__(self, host: str | None = None):
        self._host = host or os.environ.get("AIRSIM_HOST", "airsim")
        self._client: "airsim.MultirotorClient | None" = None
        self._client_lock = asyncio.Lock()
        self._rpc_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="airsim-rpc",
        )
        self._jpeg_quality = max(1, min(100, int(os.environ.get("VIDEO_JPEG_QUALITY", "85"))))

    async def _run_rpc(self, func):
        """Run all AirSim RPC calls on one dedicated thread.

        cosysairsim internally uses tornado/msgpack-rpc state that is not safe to
        bounce across arbitrary executor threads.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._rpc_executor, func)

    async def _ensure_client(self) -> "airsim.MultirotorClient":
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    port = int(os.environ.get("AIRSIM_PORT", "41451"))

                    def _connect():
                        client = airsim.MultirotorClient(ip=self._host, port=port)
                        client.confirmConnection()
                        return client

                    self._client = await self._run_rpc(_connect)
                    logger.info(f"AirSim connected to {self._host}:{port}")
        return self._client

    async def capture_frame(self, camera_name: str, image_type: str = "scene") -> FrameResult:
        if not AIRSIM_AVAILABLE or not CV2_AVAILABLE:
            raise RuntimeError("AirSim or OpenCV not available")

        type_map = {"scene": 0, "depth": 2, "segmentation": 5}
        airsim_type = type_map.get(image_type, 0)

        client = await self._ensure_client()

        responses = await self._run_rpc(
            lambda: client.simGetImages([
                airsim.ImageRequest(camera_name, airsim_type, False, False)
            ])
        )

        if not responses or responses[0].width <= 0:
            raise RuntimeError(f"AirSim returned empty image for {camera_name}")

        resp = responses[0]
        img_1d = np.frombuffer(resp.image_data_uint8, dtype=np.uint8)
        img_bgr = img_1d.reshape(resp.height, resp.width, 3)
        _, png_buf = cv2.imencode('.png', img_bgr)

        return FrameResult(
            png_bytes=png_buf.tobytes(),
            width=resp.width,
            height=resp.height,
            source="airsim",
        )

    async def get_stream(self, camera_name: str, fps: float = 5.0) -> AsyncIterator[bytes]:
        interval = 1.0 / fps
        while True:
            start = time.monotonic()
            try:
                frame = await self.capture_frame(camera_name, "scene")
                # Convert PNG to JPEG for streaming efficiency
                if CV2_AVAILABLE:
                    img_1d = np.frombuffer(frame.png_bytes, dtype=np.uint8)
                    img = cv2.imdecode(img_1d, cv2.IMREAD_COLOR)
                    _, jpeg_buf = cv2.imencode(
                        '.jpg',
                        img,
                        [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality],
                    )
                    yield jpeg_buf.tobytes()
                else:
                    yield frame.png_bytes
            except Exception as e:
                logger.warning(f"Stream capture failed: {e}")
            elapsed = time.monotonic() - start
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)

    async def set_camera_pose(self, camera_name: str, pitch_deg: float,
                               roll_deg: float, yaw_deg: float) -> None:
        if not AIRSIM_AVAILABLE:
            return
        client = await self._ensure_client()
        pose = airsim.Pose(
            airsim.Vector3r(0, 0, 0),
            airsim.to_quaternion(
                math.radians(pitch_deg),
                math.radians(roll_deg),
                math.radians(yaw_deg),
            ),
        )
        await self._run_rpc(lambda: client.simSetCameraPose(camera_name, pose))

    def get_available_cameras(self) -> list[str]:
        return ["front_center", "bottom_center"]

    def get_info(self) -> dict:
        return {
            "source": "airsim",
            "host": self._host,
            "connected": self._client is not None,
            "cameras": {
                "front_center": {"resolution": "1920x1080", "image_types": ["scene", "depth"], "gimbal": True},
                "bottom_center": {"resolution": "1280x720", "image_types": ["scene", "segmentation"], "gimbal": False},
            },
        }
