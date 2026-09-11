#!/usr/bin/env python3
"""
Test de detección de esquinas y procesamiento OMR.
Genera una imagen de prueba y verifica que las esquinas se detectan correctamente.
"""
import os
import sys
import json
import base64
import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.omr_processor import (
    detect_qr_codes,
    preprocess_image,
    is_bubble_filled,
    decode_image_base64
)

# Create test image with corner marks
def create_test_image():
    """Crea una imagen de prueba con marcas de esquina y burbujas."""
    # Letter size: 612 x 792 points, but we'll use pixel coords
    width, height = 800, 1000
    img = np.ones((height, width, 3), dtype=np.uint8) * 255  # White background

    # Draw corner marks (black squares)
    corner_size = 20
    margin = 30

    # Top-left
    cv2.rectangle(img, (margin, margin), (margin + corner_size, margin + corner_size), (0, 0, 0), -1)
    # Top-right
    cv2.rectangle(img, (width - margin - corner_size, margin), (width - margin, margin + corner_size), (0, 0, 0), -1)
    # Bottom-left
    cv2.rectangle(img, (margin, height - margin - corner_size), (margin + corner_size, height - margin), (0, 0, 0), -1)
    # Bottom-right
    cv2.rectangle(img, (width - margin - corner_size, height - margin - corner_size), (width - margin, height - margin), (0, 0, 0), -1)

    # Draw some test bubbles (unfilled)
    for row in range(5):
        for col in range(5):
            cx = 150 + col * 80
            cy = 200 + row * 40
            cv2.circle(img, (cx, cy), 12, (0, 0, 0), 2)  # Unfilled

    # Fill some bubbles (mark as answers)
    cv2.circle(img, (150 + 2 * 80, 200 + 0 * 40), 12, (0, 0, 0), -1)  # Row 0, Col 2 (C)
    cv2.circle(img, (150 + 1 * 80, 200 + 1 * 40), 12, (0, 0, 0), -1)  # Row 1, Col 1 (B)
    cv2.circle(img, (150 + 3 * 80, 200 + 2 * 40), 12, (0, 0, 0), -1)  # Row 2, Col 3 (D)

    return img


def detect_corner_marks(image):
    """Detecta las 4 marcas de esquina en la imagen."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    h, w = gray.shape

    # Threshold to get black marks
    _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)

    # Find contours
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Collect all candidate squares
    candidates = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        # Corner marks should be roughly square and small
        if area < 100 or area > 2000:
            continue

        # Check if roughly square
        x, y, bw, bh = cv2.boundingRect(cnt)
        aspect_ratio = bw / bh if bh > 0 else 0
        if 0.7 < aspect_ratio < 1.3:
            M = cv2.moments(cnt)
            if M["m00"] != 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                # Score: distance from nearest corner (lower = better)
                dist_tl = cx**2 + cy**2
                dist_tr = (cx - w)**2 + cy**2
                dist_bl = cx**2 + (cy - h)**2
                dist_br = (cx - w)**2 + (cy - h)**2
                min_dist = min(dist_tl, dist_tr, dist_bl, dist_br)
                candidates.append((cx, cy, area, min_dist))

    if len(candidates) < 4:
        return None

    # For each corner of the image, find the closest candidate
    corners = {}
    corner_positions = {
        "top_left": (0, 0),
        "top_right": (w, 0),
        "bottom_left": (0, h),
        "bottom_right": (w, h)
    }

    used = set()
    for corner_name, (target_x, target_y) in corner_positions.items():
        best = None
        best_dist = float('inf')
        for i, (cx, cy, area, _) in enumerate(candidates):
            if i in used:
                continue
            dist = (cx - target_x)**2 + (cy - target_y)**2
            if dist < best_dist:
                best_dist = dist
                best = i
        if best is not None:
            used.add(best)
            corners[corner_name] = candidates[best]

    return corners if len(corners) == 4 else None


def apply_perspective_transform(image, corners):
    """Aplica transformación de perspectiva para enderezar la imagen."""
    if not corners:
        return image

    # Source points (corners detected)
    src_points = np.float32([
        [corners["top_left"][0], corners["top_left"][1]],
        [corners["top_right"][0], corners["top_right"][1]],
        [corners["bottom_right"][0], corners["bottom_right"][1]],
        [corners["bottom_left"][0], corners["bottom_left"][1]]
    ])

    # Destination points (rectangular)
    h, w = image.shape[:2]
    dst_points = np.float32([
        [0, 0],
        [w, 0],
        [w, h],
        [0, h]
    ])

    # Apply perspective transform
    matrix = cv2.getPerspectiveTransform(src_points, dst_points)
    transformed = cv2.warpPerspective(image, matrix, (w, h))

    return transformed


def main():
    print("=" * 60)
    print("  TEST DE DETECCIÓN DE ESQUINAS OMR")
    print("=" * 60)
    print()

    # Create test image
    print("[1/4] Creando imagen de prueba...")
    img = create_test_image()
    print(f"      ✓ Imagen creada: {img.shape[1]}x{img.shape[0]} px")

    # Save test image
    output_dir = os.path.join(os.path.dirname(__file__), "demo", "output")
    os.makedirs(output_dir, exist_ok=True)
    test_img_path = os.path.join(output_dir, "test_corners.png")
    cv2.imwrite(test_img_path, img)
    print(f"      ✓ Guardada en: {test_img_path}")

    # Detect corners
    print()
    print("[2/4] Detectando marcas de esquina...")
    corners = detect_corner_marks(img)

    if corners:
        print("      ✓ 4 esquinas detectadas:")
        for name, corner in corners.items():
            print(f"        {name}: ({corner[0]}, {corner[1]}) - área: {corner[2]}")
    else:
        print("      ✗ No se detectaron 4 esquinas")

    # Apply perspective transform
    print()
    print("[3/4] Aplicando transformación de perspectiva...")
    if corners:
        transformed = apply_perspective_transform(img, corners)
        transformed_path = os.path.join(output_dir, "test_corners_transformed.png")
        cv2.imwrite(transformed_path, transformed)
        print(f"      ✓ Imagen transformada guardada en: {transformed_path}")
    else:
        print("      ✗ No se pudo aplicar transformación (faltan esquinas)")

    # Test bubble detection
    print()
    print("[4/4] Probando detección de burbujas...")

    # Find bubbles in original image
    processed = preprocess_image(img)
    contours, _ = cv2.findContours(processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    bubbles = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 50 or area > 3000:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            continue
        circularity = 4 * np.pi * area / (perimeter * perimeter)
        if circularity > 0.4:
            M = cv2.moments(cnt)
            if M["m00"] != 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                filled = is_bubble_filled(img, (cx, cy), radius=10)
                bubbles.append({"x": cx, "y": cy, "area": area, "filled": filled})

    print(f"      ✓ {len(bubbles)} burbujas detectadas")
    filled_count = sum(1 for b in bubbles if b["filled"])
    print(f"      ✓ {filled_count} burbujas marcadas (rellenas)")

    # Show results
    print()
    print("=" * 60)
    print("  RESULTADOS")
    print("=" * 60)
    print(f"  Esquinas detectadas: {'SÍ' if corners else 'NO'}")
    print(f"  Transformación: {'Aplicada' if corners else 'Pendiente'}")
    print(f"  Burbujas totales: {len(bubbles)}")
    print(f"  Burbujas marcadas: {filled_count}")
    print()
    print(f"  Archivos generados:")
    print(f"    - {test_img_path}")
    if corners:
        print(f"    - {transformed_path}")
    print()


if __name__ == "__main__":
    main()
