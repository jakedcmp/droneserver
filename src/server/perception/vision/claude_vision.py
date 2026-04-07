"""Claude Vision API analysis — extracted from droneserver.py."""

import base64
import json
import logging
import os

logger = logging.getLogger("perception")

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


async def analyze(png_bytes: bytes, prompt: str,
                  detections: list[dict], position: dict) -> dict:
    """Send image + context to Claude Vision API for reasoning.

    Returns structured JSON with description, findings, and recommendations.
    """
    if not ANTHROPIC_AVAILABLE or not os.environ.get("ANTHROPIC_API_KEY"):
        return {"description": "Claude Vision not available", "findings": [], "recommendation": ""}

    model = os.environ.get("CLAUDE_VISION_MODEL", "claude-haiku-4-5")
    client = anthropic.AsyncAnthropic()

    context_parts = []
    if position:
        context_parts.append(
            f"Drone position: lat={position.get('latitude_deg')}, "
            f"lon={position.get('longitude_deg')}, "
            f"alt={position.get('relative_altitude_m')}m"
        )
    if detections:
        det_summary = ", ".join(f"{d['class']} ({d['confidence']:.0%})" for d in detections[:10])
        context_parts.append(f"YOLO detections: {det_summary}")

    system_prompt = (
        "You are analyzing aerial drone imagery for a search/survey mission. "
        "Respond with JSON only. Schema: "
        '{"description": "...", "findings": [{"type": "...", "description": "...", '
        '"confidence": 0.0-1.0, "severity": "low|medium|high|critical"}], '
        '"recommendation": "..."}'
    )

    user_content = []
    img_b64 = base64.b64encode(png_bytes).decode("utf-8")
    user_content.append({
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
    })
    text = "\n".join(context_parts)
    if prompt:
        text += f"\n\nUser request: {prompt}"
    else:
        text += "\n\nDescribe what you see and flag any anomalies or objects of interest."
    user_content.append({"type": "text", "text": text})

    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )

    response_text = response.content[0].text
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        if "```" in response_text:
            json_str = response_text.split("```")[1]
            if json_str.startswith("json"):
                json_str = json_str[4:]
            return json.loads(json_str.strip())
        return {"description": response_text, "findings": [], "recommendation": ""}
