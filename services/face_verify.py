import face_recognition
import numpy as np
from fastapi import UploadFile
from typing import Optional
import os

# Directory to store user face images (should be in config in production)
FACE_IMAGE_DIR = "static/face_images"
os.makedirs(FACE_IMAGE_DIR, exist_ok=True)

def save_face_image(user_id: int, file: UploadFile) -> str:
    ext = os.path.splitext(file.filename)[-1].lower()
    path = os.path.join(FACE_IMAGE_DIR, f"user_{user_id}{ext}")
    with open(path, "wb") as f:
        f.write(file.file.read())
    return path

def get_face_encoding(image_path: str) -> Optional[np.ndarray]:
    image = face_recognition.load_image_file(image_path)
    encodings = face_recognition.face_encodings(image)
    if not encodings:
        return None
    return encodings[0]

def compare_faces(known_encoding: np.ndarray, unknown_encoding: np.ndarray) -> float:
    # Returns similarity (1.0 = perfect match, 0.0 = no match)
    distance = np.linalg.norm(known_encoding - unknown_encoding)
    similarity = max(0.0, 1.0 - distance)  # crude, for demo
    return similarity
