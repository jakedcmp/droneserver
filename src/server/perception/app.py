"""Perception service — FastAPI app for camera capture, YOLO, and Claude Vision."""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel

from .camera.base import CameraSource
from .camera.registry import create_camera_source
from . import image_store
from .vision.yolo import run_yolo_async, YOLO_AVAILABLE
from .vision.claude_vision import analyze as claude_vision_analyze, ANTHROPIC_AVAILABLE

logger = logging.getLogger("perception")
VIDEO_JPEG_QUALITY = max(1, min(100, int(os.environ.get("VIDEO_JPEG_QUALITY", "85"))))
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(handler)
    logger.propagate = False


# --- Pydantic models ---

class PositionData(BaseModel):
    latitude_deg: float = 0.0
    longitude_deg: float = 0.0
    relative_altitude_m: float = 0.0
    absolute_altitude_m: float = 0.0


class CaptureRequest(BaseModel):
    camera_name: str = "front_center"
    image_type: str = "scene"
    label: str = ""
    mission_id: str = "no-mission"
    position: PositionData = PositionData()


class CaptureMultiRequest(BaseModel):
    cameras: list[str] = ["front_center", "bottom_center"]
    label: str = ""
    mission_id: str = "no-mission"
    position: PositionData = PositionData()


class AnalyzeRequest(BaseModel):
    image_ref: str
    prompt: str = ""
    yolo_confidence: float = 0.3
    use_claude_vision: bool = True


class CaptureAndAnalyzeRequest(BaseModel):
    camera_name: str = "front_center"
    image_type: str = "scene"
    label: str = ""
    mission_id: str = "no-mission"
    position: PositionData = PositionData()
    prompt: str = ""
    yolo_confidence: float = 0.3
    use_claude_vision: bool = True


class CameraPoseRequest(BaseModel):
    camera_name: str = "front_center"
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    yaw_deg: float = 0.0


# --- App state ---
_camera_source: CameraSource | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _camera_source
    _camera_source = create_camera_source()
    logger.info(f"Perception service started — camera: {_camera_source.get_info()['source']}")
    yield
    logger.info("Perception service shutting down")


app = FastAPI(title="Perception Service", lifespan=lifespan)


# --- Endpoints ---

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "camera": _camera_source.get_info()["source"] if _camera_source else "not initialized",
        "yolo_available": YOLO_AVAILABLE,
        "anthropic_available": ANTHROPIC_AVAILABLE,
        "image_store": image_store.stats(),
    }


@app.post("/capture")
async def capture(req: CaptureRequest):
    if not _camera_source:
        raise HTTPException(503, "Camera not initialized")

    image_ref = f"img-{req.mission_id}-{uuid.uuid4().hex[:6]}"
    position_dict = req.position.model_dump()

    try:
        frame = await _camera_source.capture_frame(req.camera_name, req.image_type)
        meta = {
            "image_ref": image_ref,
            "label": req.label,
            "camera_name": req.camera_name,
            "image_type": req.image_type,
            "position": position_dict,
            "timestamp": time.time(),
            "source": frame.source,
            "mission_id": req.mission_id,
            "width": frame.width,
            "height": frame.height,
            "png_bytes": frame.png_bytes,
        }
        image_store.put(image_ref, meta)
        return {
            "status": "success",
            "image_ref": image_ref,
            "label": req.label,
            "camera_name": req.camera_name,
            "image_type": req.image_type,
            "position": position_dict,
            "source": frame.source,
            "width": frame.width,
            "height": frame.height,
            "png_size_bytes": len(frame.png_bytes),
            "mission_id": req.mission_id,
        }
    except Exception as e:
        logger.warning(f"Capture failed: {e}")
        # Synthetic fallback
        meta = {
            "image_ref": image_ref,
            "label": req.label,
            "camera_name": req.camera_name,
            "image_type": req.image_type,
            "position": position_dict,
            "timestamp": time.time(),
            "source": "synthetic",
            "mission_id": req.mission_id,
            "width": 0,
            "height": 0,
            "png_bytes": b"",
        }
        image_store.put(image_ref, meta)
        return {
            "status": "success",
            "image_ref": image_ref,
            "label": req.label,
            "camera_name": req.camera_name,
            "image_type": req.image_type,
            "position": position_dict,
            "source": "synthetic",
            "width": 0,
            "height": 0,
            "png_size_bytes": 0,
            "mission_id": req.mission_id,
            "note": f"Capture failed ({e}) — synthetic fallback",
        }


@app.post("/capture-multi")
async def capture_multi(req: CaptureMultiRequest):
    if not _camera_source:
        raise HTTPException(503, "Camera not initialized")

    position_dict = req.position.model_dump()
    captures = []

    for cam in req.cameras:
        image_ref = f"img-{req.mission_id}-{uuid.uuid4().hex[:6]}"
        try:
            frame = await _camera_source.capture_frame(cam, "scene")
            meta = {
                "image_ref": image_ref,
                "label": f"{req.label}-{cam}" if req.label else cam,
                "camera_name": cam,
                "image_type": "scene",
                "position": position_dict,
                "timestamp": time.time(),
                "source": frame.source,
                "mission_id": req.mission_id,
                "width": frame.width,
                "height": frame.height,
                "png_bytes": frame.png_bytes,
            }
            image_store.put(image_ref, meta)
            captures.append({
                "image_ref": image_ref, "camera": cam,
                "width": frame.width, "height": frame.height,
                "png_size_bytes": len(frame.png_bytes),
            })
        except Exception as e:
            logger.warning(f"Multi-camera capture failed for {cam}: {e}")
            meta = {
                "image_ref": image_ref,
                "label": f"{req.label}-{cam}" if req.label else cam,
                "camera_name": cam, "image_type": "scene",
                "position": position_dict, "timestamp": time.time(),
                "source": "synthetic", "mission_id": req.mission_id,
                "width": 0, "height": 0, "png_bytes": b"",
            }
            image_store.put(image_ref, meta)
            captures.append({
                "image_ref": image_ref, "camera": cam,
                "width": 0, "height": 0, "source": "synthetic",
            })

    return {"status": "success", "captures": captures, "position": position_dict}


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    meta = image_store.get(req.image_ref)
    if meta is None:
        raise HTTPException(404, f"Image ref '{req.image_ref}' not found")

    source = meta.get("source", "synthetic")
    png_bytes = meta.get("png_bytes", b"")
    position = meta.get("position", {})

    if source == "synthetic" or not png_bytes:
        return {
            "status": "success",
            "image_ref": req.image_ref,
            "source": source,
            "position": position,
            "analysis": "No real image data — synthetic stub.",
            "yolo_detections": [],
            "yolo_count": 0,
            "claude_vision": None,
            "prompt": req.prompt,
        }

    # Tier 1: YOLO
    yolo_detections = []
    try:
        yolo_detections = await run_yolo_async(png_bytes, req.yolo_confidence)
        logger.info(f"YOLO: {len(yolo_detections)} detections above {req.yolo_confidence}")
    except Exception as e:
        logger.warning(f"YOLO failed: {e}")

    # Tier 2: Claude Vision
    claude_analysis = None
    has_detections = len(yolo_detections) > 0
    if req.use_claude_vision and (has_detections or req.prompt):
        try:
            claude_analysis = await claude_vision_analyze(
                png_bytes=png_bytes,
                prompt=req.prompt,
                detections=yolo_detections,
                position=position,
            )
            logger.info("Claude Vision: analysis complete")
        except Exception as e:
            logger.warning(f"Claude Vision failed: {e}")

    return {
        "status": "success",
        "image_ref": req.image_ref,
        "source": source,
        "position": position,
        "yolo_detections": yolo_detections,
        "yolo_count": len(yolo_detections),
        "claude_vision": claude_analysis,
        "prompt": req.prompt,
    }


@app.post("/capture-and-analyze")
async def capture_and_analyze(req: CaptureAndAnalyzeRequest):
    # Step 1: Capture
    capture_req = CaptureRequest(
        camera_name=req.camera_name, image_type=req.image_type,
        label=req.label, mission_id=req.mission_id, position=req.position,
    )
    capture_result = await capture(capture_req)
    if capture_result.get("status") != "success":
        return capture_result

    image_ref = capture_result["image_ref"]

    # Step 2: Analyze
    analyze_req = AnalyzeRequest(
        image_ref=image_ref, prompt=req.prompt,
        yolo_confidence=req.yolo_confidence, use_claude_vision=req.use_claude_vision,
    )
    analysis_result = await analyze(analyze_req)

    return {
        "status": "success",
        "capture": {
            "image_ref": image_ref,
            "source": capture_result.get("source"),
            "camera_name": req.camera_name,
            "width": capture_result.get("width"),
            "height": capture_result.get("height"),
            "png_size_bytes": capture_result.get("png_size_bytes"),
        },
        "position": capture_result.get("position", {}),
        "analysis": {
            "yolo_detections": analysis_result.get("yolo_detections", []),
            "yolo_count": analysis_result.get("yolo_count", 0),
            "claude_vision": analysis_result.get("claude_vision"),
        },
        "prompt": req.prompt,
    }


@app.post("/camera/pose")
async def set_camera_pose(req: CameraPoseRequest):
    if not _camera_source:
        raise HTTPException(503, "Camera not initialized")
    try:
        await _camera_source.set_camera_pose(
            req.camera_name, req.pitch_deg, req.roll_deg, req.yaw_deg,
        )
        return {
            "status": "success",
            "camera_name": req.camera_name,
            "pitch_deg": req.pitch_deg,
            "roll_deg": req.roll_deg,
            "yaw_deg": req.yaw_deg,
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


@app.get("/camera/info")
async def get_camera_info():
    if not _camera_source:
        raise HTTPException(503, "Camera not initialized")
    info = _camera_source.get_info()
    return {"status": "success", **info}


@app.get("/images/{image_ref}")
async def get_image_meta(image_ref: str):
    meta = image_store.get(image_ref)
    if meta is None:
        raise HTTPException(404, f"Image ref '{image_ref}' not found")
    # Return metadata without png_bytes
    return {
        "image_ref": meta["image_ref"],
        "label": meta.get("label", ""),
        "camera_name": meta.get("camera_name", ""),
        "image_type": meta.get("image_type", ""),
        "position": meta.get("position", {}),
        "timestamp": meta.get("timestamp"),
        "source": meta.get("source", ""),
        "mission_id": meta.get("mission_id", ""),
        "width": meta.get("width", 0),
        "height": meta.get("height", 0),
        "png_size_bytes": len(meta.get("png_bytes", b"")),
    }


@app.get("/images/{image_ref}/png")
async def get_image_png(image_ref: str):
    meta = image_store.get(image_ref)
    if meta is None:
        raise HTTPException(404, f"Image ref '{image_ref}' not found")
    png_bytes = meta.get("png_bytes", b"")
    if not png_bytes:
        raise HTTPException(404, "No image data available (synthetic)")
    return Response(content=png_bytes, media_type="image/png")


# Note: /findings endpoint removed — core (droneserver) owns mission findings.
# Perception stores images and raw detections only.


# --- Video streaming ---

@app.websocket("/ws/video/{camera_name}")
async def ws_video(websocket: WebSocket, camera_name: str):
    """Stream JPEG frames over WebSocket. Client can send {"fps": N} to adjust rate."""
    if not _camera_source:
        await websocket.close(code=1011, reason="Camera not initialized")
        return

    available = _camera_source.get_available_cameras()
    if camera_name not in available:
        await websocket.close(code=1008, reason=f"Unknown camera: {camera_name}")
        return

    await websocket.accept()
    fps = 5.0
    logger.info(f"Video WS connected: {camera_name} @ {fps}fps")

    # Task to listen for client fps adjustments
    fps_lock = asyncio.Lock()

    async def listen_for_control():
        nonlocal fps
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                    if "fps" in msg:
                        new_fps = max(1.0, min(30.0, float(msg["fps"])))
                        async with fps_lock:
                            fps = new_fps
                        logger.info(f"Video WS {camera_name}: fps adjusted to {fps}")
                except (json.JSONDecodeError, ValueError):
                    pass
        except WebSocketDisconnect:
            pass

    control_task = asyncio.create_task(listen_for_control())

    try:
        while True:
            async with fps_lock:
                current_fps = fps
            interval = 1.0 / current_fps
            start = time.monotonic()

            try:
                frame = await _camera_source.capture_frame(camera_name, "scene")
            except Exception as e:
                logger.warning(f"Video WS capture error: {e}")
                await asyncio.sleep(interval)
                continue

            # Convert PNG to JPEG for bandwidth efficiency
            frame_bytes = frame.png_bytes
            frame_format = "png"
            try:
                import cv2
                import numpy as np
                img_1d = np.frombuffer(frame.png_bytes, dtype=np.uint8)
                img = cv2.imdecode(img_1d, cv2.IMREAD_COLOR)
                _, jpeg_buf = cv2.imencode(
                    '.jpg',
                    img,
                    [cv2.IMWRITE_JPEG_QUALITY, VIDEO_JPEG_QUALITY],
                )
                frame_bytes = jpeg_buf.tobytes()
                frame_format = "jpeg"
            except ImportError:
                pass  # Fall back to PNG if cv2 unavailable

            # Send JSON metadata as text frame
            meta = {
                "camera": camera_name,
                "ts": time.time(),
                "width": frame.width,
                "height": frame.height,
                "fps": current_fps,
                "format": frame_format,
            }
            await websocket.send_text(json.dumps(meta))

            # Send frame as binary
            await websocket.send_bytes(frame_bytes)

            elapsed = time.monotonic() - start
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)

    except WebSocketDisconnect:
        logger.info(f"Video WS disconnected: {camera_name}")
    except Exception as e:
        logger.warning(f"Video WS error: {e}")
    finally:
        control_task.cancel()


# --- Entry point ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
