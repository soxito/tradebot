"""
JARVIS Vision API  –  GPU-accelerated face analysis
====================================================

Powered by:
  • MediaPipe Tasks FaceLandmarker  – 478-point face landmarks + 52 blendshapes
    github.com/google-ai-edge/mediapipe  (36k ⭐)
    Uses the modern Tasks API (mediapipe.tasks.python.vision) which is the only
    API shipped in mediapipe 0.10.x wheels on Python 3.13 / Apple Silicon.
    The `jawOpen` blendshape gives a robust talking signal on top of MAR.
  • face_recognition (dlib)  – face identity embeddings
    github.com/ageitgey/face_recognition  (53k ⭐)
  • OpenCV  – frame decoding + preprocessing

WebSocket endpoint  (primary, real-time):
  WS  /vision/face-stream
      Client → { "frame": "<base64 JPEG>", "ts": 1234567890 }
      Server → { "face": bool, "mar": float, "jaw_open": float, "talking": bool,
                 "identity_match": bool, "box": {x,y,w,h},
                 "landmarks": [{x,y,z}, ...], "ts": ... }

REST endpoints:
  POST /vision/enroll-face   { "frame": "<base64 JPEG>" }
  GET  /vision/profile
  DELETE /vision/profile
  POST /vision/lip-data      { "samples": [...], "mar_avg": float }

GPU notes:
  • The desktop Python Tasks wheel runs the model on CPU via the XNNPACK SIMD
    delegate (very fast for single-face 320×240). The GPU delegate is attempted
    first and falls back to CPU automatically if unavailable.
  • macOS: XNNPACK uses Accelerate/SIMD; Linux/Windows CUDA builds use GPU.
"""

from __future__ import annotations

import base64
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel

router = APIRouter(prefix="/vision", tags=["vision"])

# ── Model bundle (auto-downloaded on first use) ──────────────────────────────
_MODEL_DIR  = Path(__file__).parent / "models"
_MODEL_PATH = _MODEL_DIR / "face_landmarker.task"
_MODEL_URL  = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)


def _ensure_model() -> bool:
    """Download the FaceLandmarker task bundle if not present. Returns True if ready."""
    try:
        if _MODEL_PATH.exists() and _MODEL_PATH.stat().st_size > 100_000:
            return True
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Downloading MediaPipe FaceLandmarker model → {_MODEL_PATH}")
        urllib.request.urlretrieve(_MODEL_URL, _MODEL_PATH)
        return _MODEL_PATH.exists() and _MODEL_PATH.stat().st_size > 100_000
    except Exception as e:
        logger.error(f"Failed to download FaceLandmarker model: {e}")
        return False


# ── Optional imports (graceful fallback) ─────────────────────────────────────

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False
    logger.warning("opencv-python not installed — vision disabled")

try:
    import mediapipe as mp
    from mediapipe.tasks import python as _mp_python
    from mediapipe.tasks.python import vision as _mp_vision
    _MP = True
    logger.info("mediapipe Tasks API available — FaceLandmarker enabled")
except ImportError:
    _MP = False
    logger.warning("mediapipe not installed  →  bash scripts/setup-face-vision.sh")

try:
    import face_recognition as fr
    _FR = True
    logger.info("face_recognition available — face identity enabled")
except ImportError:
    _FR = False
    logger.warning("face_recognition not installed  →  pip install face_recognition  (needs cmake)")


def _make_landmarker():
    """
    Build a per-connection FaceLandmarker in VIDEO mode.

    Delegate selection:
      • Default = CPU (XNNPACK) — SIMD-accelerated, stable on all platforms.
        On Apple Silicon this is the correct fast path; the macOS Metal GPU
        delegate hard-aborts on desktop `mp.Image` inference (MediaPipe bug),
        so it is NOT used unless explicitly forced.
      • GPU (CUDA) — opt-in via env  JARVIS_VISION_GPU=1  for Linux/Windows
        builds where the GPU delegate is stable.
    """
    if not _MP or not _ensure_model():
        return None

    use_gpu = os.getenv("JARVIS_VISION_GPU", "0") == "1"
    delegate = (_mp_python.BaseOptions.Delegate.GPU if use_gpu
                else _mp_python.BaseOptions.Delegate.CPU)

    try:
        base = _mp_python.BaseOptions(
            model_asset_path=str(_MODEL_PATH),
            delegate=delegate,
        )
        opts = _mp_vision.FaceLandmarkerOptions(
            base_options=base,
            running_mode=_mp_vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=True,   # gives jawOpen / mouth signals
            output_facial_transformation_matrixes=False,
            min_face_detection_confidence=0.4,
            min_face_presence_confidence=0.4,
            min_tracking_confidence=0.4,
        )
        lm = _mp_vision.FaceLandmarker.create_from_options(opts)
        logger.info(f"FaceLandmarker ready (delegate={'GPU' if use_gpu else 'CPU/XNNPACK'})")
        return lm
    except Exception as e:
        logger.error(f"FaceLandmarker init failed: {e}")
        return None


# ── MediaPipe FaceMesh lip landmark indices (478-point model) ────────────────
# Upper inner lip: 13   Lower inner lip: 14
# Left corner:     61   Right corner:    291
# Outer upper lip: 0,267,269,270,409,291,375,321,405,314,17,84,181,91,146
# Outer lower lip: 61,185,40,39,37,0,267,269,270,409
#
# We send back the first 80 landmarks which fully cover the lip region.
_LIP_INNER_TOP   = 13
_LIP_INNER_BOT   = 14
_LIP_CORNER_L    = 61
_LIP_CORNER_R    = 291
_LIPS_ALL_IDX    = [  # 40 key lip points from the 478 model
    0, 13, 14, 17, 37, 38, 39, 40, 61, 62, 63, 64, 65, 66, 67, 84,
    87, 88, 91, 95, 96, 146, 178, 179, 180, 181, 183, 184, 185, 191,
    267, 269, 270, 291, 308, 310, 311, 312, 314, 317, 318, 321, 324,
    375, 402, 403, 404, 405, 407, 408, 409, 415,
]

# ── In-memory store ───────────────────────────────────────────────────────────
_store: Dict[str, Any] = {
    "enrolled":       False,
    "face_encoding":  None,     # numpy array (128,) from face_recognition
    "enroll_time":    None,
    "lip_samples":    [],       # rolling buffer ≤2000 entries
    "speech_pairs":   [],       # (lip_seq, transcript) for learning ≤500
}

# ── Pydantic schemas ──────────────────────────────────────────────────────────

class EnrollRequest(BaseModel):
    frame: str       # base64 JPEG

class LipSample(BaseModel):
    t: float
    mar: float

class LipDataRequest(BaseModel):
    samples: List[LipSample]
    mar_avg: float
    transcript: Optional[str] = None

# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64_to_bgr(b64: str) -> Optional[np.ndarray]:
    """Decode base64 JPEG → BGR numpy array."""
    if not _CV2:
        return None
    try:
        raw = base64.b64decode(b64)
        arr = np.frombuffer(raw, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _compute_mar(landmarks: list) -> float:
    """
    Mouth Aspect Ratio from MediaPipe 478-point normalised landmarks.
    Uses:  top inner (13), bottom inner (14), left corner (61), right corner (291).
    Landmarks are normalised (0–1) so the ratio is resolution-independent.
    """
    if len(landmarks) < 292:
        return 0.0
    top   = landmarks[_LIP_INNER_TOP]
    bot   = landmarks[_LIP_INNER_BOT]
    left  = landmarks[_LIP_CORNER_L]
    right = landmarks[_LIP_CORNER_R]

    def d(a, b):
        return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5

    vert = d(top, bot)
    horiz = d(left, right)
    return vert / horiz if horiz > 0.001 else 0.0


def _analyse_mar_history(mars: List[float]) -> Dict[str, float]:
    if not mars:
        return {"talking_ratio": 0.0, "avg_mar": 0.0, "peak_mar": 0.0}
    arr = np.array(mars)
    return {
        "talking_ratio": float(np.mean(arr > 0.30)),
        "avg_mar":       float(np.mean(arr)),
        "peak_mar":      float(np.max(arr)),
        "std_mar":       float(np.std(arr)),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.websocket("/face-stream")
async def face_stream_ws(websocket: WebSocket) -> None:
    """
    Real-time WebSocket face analysis.

    Expects JSON frames from the JARVIS extension's face-vision.js.
    Processes each frame with MediaPipe FaceLandmarker (Tasks API) and
    face_recognition (dlib). Returns landmarks, MAR, jawOpen, talking state,
    bounding box, and identity match.
    """
    await websocket.accept()
    client = websocket.client
    logger.info(f"Face-stream WS connected from {client}")

    landmarker = _make_landmarker() if _MP else None
    # VIDEO mode requires strictly-increasing millisecond timestamps.
    frame_ts_ms = 0

    try:
        while True:
            try:
                msg = await websocket.receive_json()
            except Exception:
                break

            b64 = msg.get("frame", "")
            ts  = msg.get("ts", time.time())

            if not b64:
                continue

            # Decode frame
            bgr = _b64_to_bgr(b64) if _CV2 else None
            if bgr is None:
                await websocket.send_json({"face": False, "mar": 0, "jaw_open": 0,
                                            "talking": False, "identity_match": False,
                                            "ts": ts, "landmarks": [], "box": None})
                continue

            h, w = bgr.shape[:2]
            result: Dict[str, Any] = {
                "face": False, "mar": 0.0, "jaw_open": 0.0, "talking": False,
                "identity_match": False, "ts": ts,
                "landmarks": [], "box": None,
            }

            # ── MediaPipe FaceLandmarker (Tasks API) ─────────────────────
            if landmarker is not None:
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                frame_ts_ms += 33  # monotonic (~30fps virtual clock)
                try:
                    det = landmarker.detect_for_video(mp_image, frame_ts_ms)
                except Exception as e:
                    logger.debug(f"FaceLandmarker error: {e}")
                    det = None

                if det and det.face_landmarks:
                    lm_list = det.face_landmarks[0]   # 478 NormalizedLandmark

                    # Bounding box (from landmark extent)
                    xs = [lm.x * w for lm in lm_list]
                    ys = [lm.y * h for lm in lm_list]
                    bx, by = int(min(xs)), int(min(ys))
                    bw = int(max(xs) - min(xs))
                    bh = int(max(ys) - min(ys))
                    result["box"] = {"x": bx, "y": by, "w": bw, "h": bh}

                    # MAR (geometric mouth-open ratio)
                    mar = _compute_mar(lm_list)
                    result["mar"]  = round(mar, 4)
                    result["face"] = True

                    # jawOpen blendshape (robust talking signal)
                    jaw_open = 0.0
                    if det.face_blendshapes:
                        for cat in det.face_blendshapes[0]:
                            if cat.category_name == "jawOpen":
                                jaw_open = float(cat.score)
                                break
                    result["jaw_open"] = round(jaw_open, 4)

                    # Talking = mouth geometrically open OR jaw blendshape active
                    result["talking"] = bool(mar > 0.30 or jaw_open > 0.25)

                    # Lip landmarks (priority) + sampled face mesh for overlay
                    lip_pts = []
                    for idx in _LIPS_ALL_IDX:
                        if idx < len(lm_list):
                            lm = lm_list[idx]
                            lip_pts.append({
                                "x": round(lm.x * w, 2),
                                "y": round(lm.y * h, 2),
                                "z": round(lm.z, 4),
                            })
                    face_pts = []
                    for idx in range(0, min(150, len(lm_list)), 3):
                        lm = lm_list[idx]
                        face_pts.append({
                            "x": round(lm.x * w, 2),
                            "y": round(lm.y * h, 2),
                        })
                    result["landmarks"] = lip_pts + face_pts

            # ── Face identity check ──────────────────────────────────────
            if _FR and result["face"] and _store["enrolled"] and _store["face_encoding"] is not None:
                try:
                    rgb_fr  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                    encs    = fr.face_encodings(rgb_fr)
                    if encs:
                        dist = np.linalg.norm(np.array(encs[0]) - np.array(_store["face_encoding"]))
                        result["identity_match"] = bool(dist < 0.55)
                        result["identity_dist"]  = round(float(dist), 4)
                except Exception as e:
                    logger.debug(f"Face recognition error: {e}")

            await websocket.send_json(result)

    except WebSocketDisconnect:
        logger.info(f"Face-stream WS disconnected from {client}")
    except Exception as e:
        logger.error(f"Face-stream WS error: {e}")
    finally:
        if landmarker is not None:
            try:
                landmarker.close()
            except Exception:
                pass


@router.post("/enroll-face")
async def enroll_face(req: EnrollRequest) -> Dict[str, Any]:
    """
    Enroll the user's face.
    Receives a base64 JPEG frame, extracts face encoding, stores for future matching.
    """
    if not _FR or not _CV2:
        raise HTTPException(503, "face_recognition / opencv not installed")

    bgr = _b64_to_bgr(req.frame)
    if bgr is None:
        raise HTTPException(400, "Invalid image data")

    rgb   = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    locs  = fr.face_locations(rgb, model="hog")  # 'cnn' for GPU
    encs  = fr.face_encodings(rgb, locs)

    if not encs:
        raise HTTPException(422, "No face detected in enrollment image — ensure good lighting and face fully visible")

    if len(encs) > 1:
        raise HTTPException(422, "Multiple faces detected — ensure only your face is visible")

    _store["enrolled"]      = True
    _store["face_encoding"] = encs[0].tolist()
    _store["enroll_time"]   = time.time()

    logger.info("Face enrolled successfully")
    return {"enrolled": True, "faces_detected": len(locs)}


@router.get("/profile")
async def get_profile() -> Dict[str, Any]:
    return {
        "enrolled":               _store["enrolled"],
        "enrollment_time":        _store["enroll_time"],
        "lip_samples_buffered":   len(_store["lip_samples"]),
        "speech_pairs_collected": len(_store["speech_pairs"]),
        "mediapipe_available":    _MP,
        "face_recognition_available": _FR,
        "opencv_available":       _CV2,
        "model_ready":            _MODEL_PATH.exists(),
    }


@router.delete("/profile")
async def clear_profile() -> Dict[str, bool]:
    _store.update({
        "enrolled": False, "face_encoding": None, "enroll_time": None,
        "lip_samples": [], "speech_pairs": [],
    })
    return {"cleared": True}


@router.post("/lip-data")
async def receive_lip_data(req: LipDataRequest) -> Dict[str, Any]:
    """
    Receive lip motion time-series from the extension for learning.
    Stores samples and optionally correlates with speech transcripts.
    """
    _store["lip_samples"].extend([s.dict() for s in req.samples])
    _store["lip_samples"] = _store["lip_samples"][-2000:]

    if req.transcript and len(req.samples) >= 5:
        pair = {
            "lip_seq":    [{"t": s.t, "mar": s.mar} for s in req.samples[-30:]],
            "transcript": req.transcript,
            "ts":         time.time(),
        }
        _store["speech_pairs"].append(pair)
        _store["speech_pairs"] = _store["speech_pairs"][-500:]
        logger.debug(f"Stored lip+speech pair: {req.transcript!r}")

    analysis = _analyse_mar_history([s.mar for s in req.samples])
    return {
        "received":     len(req.samples),
        "analysis":     analysis,
        "total_pairs":  len(_store["speech_pairs"]),
    }
