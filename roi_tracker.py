"""Módulo para extraer y validar la región de interés (ROI) de un fotograma OpenCV."""

from __future__ import annotations

from typing import Any

import numpy as np


class ROIConfigError(ValueError):
    """Excepción para configuraciones de ROI inválidas."""


def extract_roi(frame: np.ndarray, roi: dict[str, Any]) -> np.ndarray:
    """
    Extrae la región de interés de un fotograma.

    Parámetros
    ----------
    frame : np.ndarray
        Fotograma BGR como array de NumPy (leído con OpenCV).
    roi : dict[str, Any]
        Diccionario con las claves ``x``, ``y``, ``w`` y ``h`` (íntegros >= 0)
        que identifican la región de interés dentro del fotograma.

    Devuelve
    --------
    np.ndarray
        Recorte de la ROI (view del array original).

    Excepciones
    -----------
    ROIConfigError
        Si el fotograma es ``None``, si ``roi`` no tiene las claves esperadas,
        si los valores no son enteros no negativos o si la ROI excede los
        límites de la imagen.
    """
    if frame is None:
        raise ROIConfigError("El fotograma es None; no se puede extraer la ROI.")

    if not isinstance(frame, np.ndarray):
        raise ROIConfigError(
            f"Se esperaba un ndarray de NumPy como fotograma, recibido {type(frame)!r}."
        )

    if frame.ndim != 3:
        raise ROIConfigError(
            f"Se esperaba un fotograma 2D (H x W x C), recibido con ndim={frame.ndim}."
        )

    height, width = frame.shape[:2]
    if height <= 0 or width <= 0:
        raise ROIConfigError(
            f"Dimensiones de imagen inválidas: h={height}, w={width}."
        )

    required_keys = ("x", "y", "w", "h")
    missing = [k for k in required_keys if k not in roi]
    if missing:
        raise ROIConfigError(f"ROI incompleta; faltan claves: {missing}.")

    values = {key: roi[key] for key in required_keys}
    for key, value in values.items():
        if not isinstance(value, (int, np.integer)) or isinstance(value, bool):
            raise ROIConfigError(
                f"ROI clave '{key}' debe ser un entero no negativo, recibido {value!r}."
            )
        if value < 0:
            raise ROIConfigError(f"ROI clave '{key}' no puede ser negativa: {value}.")

    x, y, w, h = (int(values[key]) for key in required_keys)

    if w == 0 or h == 0:
        raise ROIConfigError("ROI debe tener ancho y alto estrictamente positivos.")

    if x + w > width:
        raise ROIConfigError(
            f"ROI excede el ancho de la imagen: x={x}, w={w}, image_width={width}."
        )
    if y + h > height:
        raise ROIConfigError(
            f"ROI excede el alto de la imagen: y={y}, h={h}, image_height={height}."
        )

    return frame[y : y + h, x : x + w]
