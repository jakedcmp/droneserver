"""YOLO object detection — extracted from droneserver.py."""

import asyncio
import logging
import os

logger = logging.getLogger("perception")

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False

try:
    import numpy as np
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

_yolo_model = None


def _get_model():
    """Load YOLO model on first call. Auto-downloads from Ultralytics Hub (~6MB for nano)."""
    global _yolo_model
    if _yolo_model is None:
        model_name = os.environ.get("YOLO_MODEL", "yolo11n.pt")
        logger.info(f"Loading YOLO model: {model_name}")
        _yolo_model = YOLO(model_name)
        logger.info("YOLO model loaded")
    return _yolo_model


def run_yolo(png_bytes: bytes, confidence: float = 0.3) -> list[dict]:
    """Run YOLO inference on PNG bytes. Returns list of detections."""
    if not YOLO_AVAILABLE or not CV2_AVAILABLE:
        return []
    img_array = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if img is None:
        return []
    model = _get_model()
    results = model(img, conf=confidence, verbose=False)
    detections = []
    for r in results:
        for box in r.boxes:
            detections.append({
                "class": r.names[int(box.cls[0])],
                "confidence": round(float(box.conf[0]), 3),
                "bbox": [round(float(x), 1) for x in box.xyxy[0].tolist()],
            })
    return detections


async def run_yolo_async(png_bytes: bytes, confidence: float = 0.3) -> list[dict]:
    """Async wrapper — runs YOLO in executor to avoid blocking."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: run_yolo(png_bytes, confidence))
