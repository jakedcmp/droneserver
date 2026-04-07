"""In-memory LRU image store for the perception service."""

import logging

logger = logging.getLogger("perception")

_image_store: dict[str, dict] = {}
_image_store_bytes: int = 0
_IMAGE_STORE_MAX_BYTES: int = 500 * 1024 * 1024  # 500MB cap


def put(image_ref: str, meta: dict):
    """Add an image to the store with FIFO eviction if over budget."""
    global _image_store_bytes
    # If updating an existing key, subtract old size first
    if image_ref in _image_store:
        old_size = len(_image_store[image_ref].get("png_bytes", b""))
        _image_store_bytes -= old_size
        del _image_store[image_ref]
    png_size = len(meta.get("png_bytes", b""))
    while _image_store_bytes + png_size > _IMAGE_STORE_MAX_BYTES and _image_store:
        oldest_key = next(iter(_image_store))
        oldest = _image_store.pop(oldest_key)
        _image_store_bytes -= len(oldest.get("png_bytes", b""))
    _image_store[image_ref] = meta
    _image_store_bytes += png_size


def get(image_ref: str) -> dict | None:
    """Retrieve image metadata by reference."""
    return _image_store.get(image_ref)


def stats() -> dict:
    """Return store statistics."""
    return {
        "count": len(_image_store),
        "bytes_used": _image_store_bytes,
        "max_bytes": _IMAGE_STORE_MAX_BYTES,
    }
