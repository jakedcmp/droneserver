"""Camera source factory — selects implementation based on environment."""

import logging
import os

from .base import CameraSource
from .mock_source import MockSource

logger = logging.getLogger("perception")


def create_camera_source() -> CameraSource:
    """Return AirSimSource if AIRSIM_HOST is set and available, else MockSource."""
    airsim_host = os.environ.get("AIRSIM_HOST")
    if airsim_host:
        try:
            from .airsim_source import AirSimSource, AIRSIM_AVAILABLE
            if AIRSIM_AVAILABLE:
                logger.info(f"Using AirSim camera source (host={airsim_host})")
                return AirSimSource(host=airsim_host)
            else:
                logger.warning("AIRSIM_HOST set but cosysairsim not installed — falling back to MockSource")
        except Exception as e:
            logger.warning(f"AirSim init failed ({e}) — falling back to MockSource")
    else:
        logger.info("No AIRSIM_HOST — using MockSource")
    return MockSource()
