# Table Tennis Score Tracking

Extrae la evolución del marcador de un partido de tenis de mesa a partir de un vídeo (local o de YouTube), leyendo con OCR el marcador físico de la mesa.

El marcador tiene cuatro números, de izquierda a derecha: **puntos J1, juegos J1, juegos J2 y puntos J2**. Puede ser de tarjetas abatibles o digital. La herramienta:

1. Recorta la zona del marcador que marcas en un fotograma.
2. Sigue el marcador si la cámara se mueve y lo endereza si está inclinado.
3. Lee los cuatro números como una sola línea con [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR).
4. Filtra las lecturas inestables o imposibles y guarda solo los cambios de marcador.
5. Reconstruye el partido: puntos y juegos de cada jugador, cambios de lado y estadísticas.

## Funcionalidades

- **Interfaz web local** (`app.py`):
  - Historial de todos los partidos analizados con su resultado.
  - Nuevo análisis: pegar un enlace de YouTube o una ruta local, marcar el marcador en un fotograma y analizar en segundo plano.
  - Ficha de partido: evolución del marcador, resultado de cada juego, tabla de puntos, corrección manual de lecturas y exportación a CSV.
  - Estadísticas por partido y por jugador: saque y resto, rachas, remontadas, bolas de juego, juegos a 10-10 y tiempo entre puntos.
- **Línea de comandos** (`score_tracker.py`): analiza un vídeo y guarda los cambios de marcador en un CSV.

## Requisitos

- Python 3.12 (probado en Windows 11)
- Dependencias de `requirements.txt`: PaddlePaddle, PaddleOCR 3.x, yt-dlp y Flask
- Para descargar de YouTube, yt-dlp puede necesitar [ffmpeg](https://ffmpeg.org/) en el `PATH`

## Instalación

```bash
git clone https://github.com/Alex120699/table-tennis-score-tracking.git
cd table-tennis-score-tracking
python -m venv .venv
.venv\Scripts\activate        # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
```

La primera vez que se ejecuta el OCR, PaddleOCR descarga el modelo de reconocimiento.

## Uso

### Interfaz web

```bash
python app.py
```

Abre <http://127.0.0.1:5000> en el navegador (el puerto se cambia con la variable de entorno `PORT`).

1. **Nuevo análisis**: pega el enlace de YouTube (o la ruta de un vídeo local).
2. Marca el rectángulo del marcador en el fotograma que aparece y prueba la lectura.
3. Lanza el análisis. Puedes seguir navegando mientras se procesa.
4. Al terminar, abre la ficha del partido para ver el resultado, poner los nombres de los jugadores, indicar el formato (al mejor de N) y descargar el CSV.

Los partidos se guardan en `data/matches.db` (SQLite), y los vídeos descargados en `videos/`.

### Línea de comandos

```bash
# Localizar el marcador en un vídeo nuevo: guarda calibracion.png del segundo 60
python score_tracker.py --video "https://www.youtube.com/watch?v=..." --calibrate 60

# Analizar el vídeo con la zona del marcador (roi) definida en config.json
python score_tracker.py --video "https://www.youtube.com/watch?v=..." -v
```

| Opción | Descripción |
| --- | --- |
| `--config RUTA` | Fichero de configuración (por defecto `config.json`) |
| `--video RUTA_O_URL` | Vídeo local o URL; sobrescribe `video_path` del config |
| `--calibrate SEG` | Solo guarda las imágenes de calibración del segundo `SEG` y termina |
| `-v`, `--verbose` | Muestra cada lectura del OCR |

El resultado se guarda en `score.csv` con columnas de tiempo, los cuatro números leídos, el tipo de evento y los puntos y juegos de cada jugador.

## Configuración (`config.json`)

| Clave | Descripción |
| --- | --- |
| `video_path` | Vídeo por defecto para la línea de comandos |
| `roi` | Zona del marcador en píxeles (`x`, `y`, `w`, `h`) |
| `interval_seconds` | Cada cuántos segundos se lee un fotograma |
| `stable_samples` | Lecturas iguales seguidas necesarias para aceptar un cambio |
| `download` | Carpeta y resolución máxima de los vídeos descargados |
| `preprocess` | Altura de línea, margen e inclinación (`shear`) antes del OCR |
| `track` | Seguimiento del marcador cuando la cámara se mueve |
| `ocr` | Modelo de PaddleOCR, dispositivo (`cpu`/`gpu`) y confianza mínima |

## Estructura

| Archivo | Contenido |
| --- | --- |
| `score_tracker.py` | Lectura del vídeo, seguimiento del marcador, OCR y reconstrucción del partido |
| `roi_tracker.py` | Recorte y validación de la zona del marcador |
| `stats.py` | Estadísticas de partido y de jugador |
| `db.py` | Historial de partidos en SQLite |
| `app.py` | Servidor Flask con la API y la interfaz web |
| `static/` | Interfaz web (`index.html`, `app.js`) |
