# -*- coding: utf-8 -*-
"""
Temporary stub of AI inference to unblock boot & E2E.
Replace with the real implementation later.
"""
from typing import Dict, Any

def run_inference_on_image(device_id: str, image_path: str) -> dict:
    # minimal no-op inference result
    return {"ok": True, "device_id": device_id, "image_path": image_path, "labels": [], "score": 0.0}
