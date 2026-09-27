"""Draft-quality passport frame selection from an existing video frame set.

This ranks frames for human review; it does not certify compliance with any
country's passport or identity-document rules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import cv2


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _range_score(value: float, low: float, high: float, ideal_low: float, ideal_high: float) -> float:
    if ideal_low <= value <= ideal_high:
        return 1.0
    if value < ideal_low:
        return _clamp((value - low) / max(ideal_low - low, 1e-6))
    return _clamp((high - value) / max(high - ideal_high, 1e-6))


def _score_frame(path: str, face_cascade: Any, eye_cascade: Any) -> dict[str, Any] | None:
    image = cv2.imread(path)
    if image is None or image.size == 0:
        return None
    height, width = image.shape[:2]
    if min(width, height) < 240:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = face_cascade.detectMultiScale(
        gray,
        scaleFactor=1.08,
        minNeighbors=5,
        minSize=(max(70, width // 10), max(70, height // 10)),
    )
    if len(faces) == 0:
        return None
    x, y, face_width, face_height = max(faces, key=lambda box: box[2] * box[3])
    face_area_ratio = (face_width * face_height) / float(width * height)
    face_center_x = (x + face_width / 2) / width
    face_center_y = (y + face_height / 2) / height
    center_distance = ((face_center_x - 0.5) ** 2 + ((face_center_y - 0.43) * 0.8) ** 2) ** 0.5
    center_score = _clamp(1.0 - center_distance / 0.42)
    size_score = _range_score(face_area_ratio, 0.015, 0.70, 0.12, 0.38)

    face_roi = gray[y : y + face_height, x : x + face_width]
    upper_face = face_roi[: max(1, int(face_height * 0.62)), :]
    eyes = eye_cascade.detectMultiScale(
        upper_face,
        scaleFactor=1.08,
        minNeighbors=5,
        minSize=(max(12, face_width // 12), max(8, face_height // 18)),
    )
    eye_score = 1.0 if len(eyes) >= 2 else 0.45 if len(eyes) == 1 else 0.0

    laplacian = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharpness_score = _clamp(laplacian / 350.0)
    brightness = float(gray.mean())
    contrast = float(gray.std())
    exposure_score = _range_score(brightness, 35.0, 225.0, 82.0, 190.0)
    contrast_score = _range_score(contrast, 12.0, 105.0, 28.0, 78.0)

    # Haar frontal-face detection already favours a forward-facing head. The
    # geometry and eye checks penalize side-facing, occluded, or poorly framed
    # candidates without making an identity or demographic judgment.
    score = (
        0.28 * center_score
        + 0.22 * size_score
        + 0.18 * eye_score
        + 0.18 * sharpness_score
        + 0.09 * exposure_score
        + 0.05 * contrast_score
    )
    return {
        "path": path,
        "score": score,
        "face_ratio": face_area_ratio,
        "center_score": center_score,
        "eye_score": eye_score,
        "sharpness": laplacian,
        "brightness": brightness,
        "face": (int(x), int(y), int(face_width), int(face_height)),
    }


def select_passport_frames(frame_paths: list[str], max_results: int = 3) -> list[dict[str, Any]]:
    """Return diverse, high-scoring draft candidates from extracted frames."""
    face_cascade = cv2.CascadeClassifier(
        os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    )
    eye_cascade = cv2.CascadeClassifier(
        os.path.join(cv2.data.haarcascades, "haarcascade_eye_tree_eyeglasses.xml")
    )
    if face_cascade.empty() or eye_cascade.empty():
        raise RuntimeError("OpenCV face-detection models are unavailable.")

    scored = [
        result for path in frame_paths if (result := _score_frame(path, face_cascade, eye_cascade))
    ]
    scored.sort(key=lambda result: result["score"], reverse=True)
    selected: list[dict[str, Any]] = []
    selected_indices: list[int] = []
    for result in scored:
        index = frame_paths.index(result["path"])
        if any(abs(index - other) < 3 for other in selected_indices):
            continue
        selected.append(result)
        selected_indices.append(index)
        if len(selected) >= max_results:
            break
    return selected


def crop_passport_portrait(candidate: dict[str, Any], output_path: str) -> str:
    """Create one centered 35:45 portrait draft from a scored frame."""
    image = cv2.imread(candidate["path"])
    if image is None or image.size == 0:
        raise RuntimeError("The selected frame could not be read.")
    height, width = image.shape[:2]
    x, y, face_width, face_height = candidate["face"]
    target_ratio = 35 / 45
    crop_height = min(height, max(int(face_height / 0.43), int(height * 0.72)))
    crop_width = min(width, max(int(crop_height * target_ratio), int(face_width * 1.65)))
    crop_height = min(height, max(int(crop_width / target_ratio), crop_height))
    face_center_x = x + face_width / 2
    face_center_y = y + face_height / 2
    # Keep the eyes/face in a natural upper-middle position and include shoulders.
    left = int(face_center_x - crop_width / 2)
    top = int(face_center_y - crop_height * 0.38)
    left = max(0, min(left, width - crop_width))
    top = max(0, min(top, height - crop_height))
    crop = image[top : top + crop_height, left : left + crop_width]
    if crop.size == 0:
        raise RuntimeError("Could not create the portrait crop.")
    crop = cv2.resize(crop, (700, 900), interpolation=cv2.INTER_AREA)
    if not cv2.imwrite(output_path, crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95]):
        raise RuntimeError("Could not save the portrait draft.")
    return output_path
