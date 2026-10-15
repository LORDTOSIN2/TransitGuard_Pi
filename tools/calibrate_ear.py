"""
EAR Calibration Tool
-----------------------
Measures YOUR baseline eye-open EAR value over a few seconds so you can
set a threshold tuned to your own eye shape, instead of trusting a
generic default. Different people's eyes naturally produce different
EAR values even with eyes fully open.

Usage:
    python3 calibrate_ear.py

Instructions when running:
    Sit at your normal driving position, face the camera directly,
    keep your eyes open normally (blink naturally, don't stare), for
    the full 10 seconds.

Install:
    pip install opencv-python mediapipe numpy

Uses the same face_landmarker.task model as vehicle_monitor.py — make
sure it's in the same folder as this script.
"""

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
import time

MODEL_PATH = "face_landmarker.task"
CALIBRATION_DURATION_SEC = 10

LEFT_EYE_CORNERS = (362, 263)
LEFT_EYE_VERTICAL_PAIRS = [(385, 380), (386, 374), (387, 373)]

RIGHT_EYE_CORNERS = (33, 133)
RIGHT_EYE_VERTICAL_PAIRS = [(160, 144), (159, 145), (158, 153)]


def euclidean(p1, p2):
    return np.linalg.norm(np.array(p1) - np.array(p2))


def aspect_ratio(landmarks, corners, vertical_pairs, w, h):
    """Matches vehicle_monitor.py's 3-point-averaged aspect ratio exactly,
    so the threshold this tool suggests is valid for that script."""
    c1 = (landmarks[corners[0]].x * w, landmarks[corners[0]].y * h)
    c2 = (landmarks[corners[1]].x * w, landmarks[corners[1]].y * h)
    horizontal = euclidean(c1, c2)
    if horizontal == 0:
        return 0
    vertical_sum = 0.0
    for a, b in vertical_pairs:
        pa = (landmarks[a].x * w, landmarks[a].y * h)
        pb = (landmarks[b].x * w, landmarks[b].y * h)
        vertical_sum += euclidean(pa, pb)
    return (vertical_sum / len(vertical_pairs)) / horizontal


def main():
    base_options = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
    options = mp_vision.FaceLandmarkerOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.VIDEO,
        num_faces=1,
    )
    landmarker = mp_vision.FaceLandmarker.create_from_options(options)

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Could not open camera.")
        return

    print(f"Calibrating for {CALIBRATION_DURATION_SEC} seconds...")
    print("Face the camera directly, keep eyes open normally (blinking is fine).\n")

    readings = []
    start_time = time.time()

    while time.time() - start_time < CALIBRATION_DURATION_SEC:
        ret, frame = cap.read()
        if not ret:
            break

        h, w, _ = frame.shape
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
        timestamp_ms = int((time.time() - start_time) * 1000)
        result = landmarker.detect_for_video(mp_image, timestamp_ms)

        if result.face_landmarks:
            landmarks = result.face_landmarks[0]
            left_ear = aspect_ratio(landmarks, LEFT_EYE_CORNERS, LEFT_EYE_VERTICAL_PAIRS, w, h)
            right_ear = aspect_ratio(landmarks, RIGHT_EYE_CORNERS, RIGHT_EYE_VERTICAL_PAIRS, w, h)
            avg_ear = (left_ear + right_ear) / 2.0
            readings.append(avg_ear)
            cv2.putText(frame, f"EAR: {avg_ear:.3f}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        remaining = CALIBRATION_DURATION_SEC - (time.time() - start_time)
        cv2.putText(frame, f"Calibrating... {remaining:.1f}s left", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        cv2.imshow("EAR Calibration", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()
    landmarker.close()

    if not readings:
        print("No face detected during calibration — try again with better lighting/positioning.")
        return

    readings = np.array(readings)
    # Drop the top/bottom 10% to reduce the influence of blinks and outliers
    trimmed = np.sort(readings)[int(len(readings) * 0.1):int(len(readings) * 0.9)]
    baseline = np.mean(trimmed) if len(trimmed) > 0 else np.mean(readings)

    suggested_threshold = baseline * 0.75

    print(f"\nCalibration complete.")
    print(f"Your baseline eyes-open EAR: {baseline:.3f}")
    print(f"Suggested EAR_THRESHOLD:     {suggested_threshold:.3f}")
    print(f"\nSet this in vehicle_monitor.py:")
    print(f"    EAR_THRESHOLD = {suggested_threshold:.3f}")


if __name__ == "__main__":
    main()
