"""Extrae la evolución del marcador de un partido de tenis de mesa a partir de un vídeo.

El marcador tiene cuatro números, de izquierda a derecha: puntos J1, juegos J1, juegos J2,
puntos J2 (tarjetas abatibles o marcador digital). Se recorta la zona del marcador, se sigue
si la cámara se mueve, se endereza si está inclinado y se lee como una sola línea con el
reconocedor de texto de PaddleOCR.

Uso:
    python score_tracker.py [--config config.json] [--video RUTA_O_URL] [-v]
    python score_tracker.py --video URL --calibrate 60   # localizar el marcador en un vídeo nuevo
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from roi_tracker import extract_roi

log = logging.getLogger("score_tracker")

Score = tuple[int, int, int, int]  # (puntos izq., juegos izq., juegos der., puntos der.)

CARD_NAMES = ("izq_puntos", "izq_juegos", "der_juegos", "der_puntos")
# Límites razonables para descartar lecturas absurdas.
MAX_GAMES = 4
# Caracteres que el OCR suele leer entre tarjetas ("6.115") y que se ignoran.
SEPARATORS = " .,:;'`-_|"


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolve_video(
    source: str, cfg: dict[str, Any], progress_hook: Callable[[dict[str, Any]], None] | None = None
) -> tuple[str, str, str | None]:
    """Devuelve (ruta local, título, fecha); si ``source`` es una URL (YouTube, etc.) la descarga con yt-dlp.

    La fecha ("AAAA-MM-DD") es la de subida a YouTube o, para un archivo local, la de su última
    modificación: una aproximación a la fecha del partido que luego se puede corregir.

    Se descarga solo la pista de vídeo y se prefiere H.264 (avc1), que OpenCV decodifica sin
    problemas; AV1/VP9 pueden fallar según la build de OpenCV. Si ya está descargado, se reutiliza.
    """
    if not source.startswith(("http://", "https://")):
        path = Path(source)
        date = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat() if path.is_file() else None
        return source, path.name, date
    import yt_dlp

    dl_cfg = cfg.get("download", {})
    max_height = dl_cfg.get("max_height", 1080)
    out_dir = Path(dl_cfg.get("dir", "videos"))
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": f"bv*[vcodec^=avc1][height<={max_height}]/bv*[height<={max_height}]",
        "outtmpl": str(out_dir / "%(id)s_%(height)sp.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": progress_hook is not None,
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    # YouTube necesita un runtime de JS para ofrecer todos los formatos; yt-dlp solo usa deno por defecto.
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(source, download=True)
        path = ydl.prepare_filename(info)
    log.info("Vídeo: %s (%sx%s) -> %s", info.get("title"), info.get("width"), info.get("height"), path)
    upload = info.get("upload_date")  # "AAAAMMDD"
    date = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}" if upload else None
    return path, info.get("title") or Path(path).name, date


def video_info(video_path: str) -> dict[str, float]:
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    info = {
        "fps": fps,
        "duration": cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps,
        "width": cap.get(cv2.CAP_PROP_FRAME_WIDTH),
        "height": cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
    }
    cap.release()
    return info


def read_frame(video_path: str, at_s: float) -> np.ndarray:
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, at_s * 1000)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"No se pudo leer el fotograma en {at_s} s")
    return frame


def iter_samples(video_path: str, interval_s: float) -> Iterator[tuple[float, np.ndarray]]:
    """Devuelve (segundo, fotograma) cada ``interval_s`` segundos del vídeo."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise FileNotFoundError(f"No se pudo abrir el vídeo: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(fps * interval_s))
    index = 0
    try:
        while True:
            # grab() avanza sin convertir el fotograma; solo decodificamos los muestreados.
            if not cap.grab():
                break
            if index % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    yield index / fps, frame
            index += 1
    finally:
        cap.release()


def deskew(board: np.ndarray, shear: float) -> np.ndarray:
    """Endereza dígitos inclinados (marcador visto en perspectiva) con un cizallamiento horizontal.

    ``shear`` > 0 corrige dígitos inclinados hacia la derecha (parte de arriba desplazada a la derecha).
    """
    if not shear:
        return board
    h, w = board.shape[:2]
    matrix = np.float32([[1, shear, -shear * h / 2], [0, 1, 0]])
    return cv2.warpAffine(board, matrix, (w, h), borderMode=cv2.BORDER_REPLICATE)


def prepare_line(board: np.ndarray, shear: float, line_height: int, pad: int) -> np.ndarray:
    """Endereza el marcador, añade margen y lo escala a ``line_height`` px para el reconocedor."""
    line = deskew(board, shear)
    line = cv2.copyMakeBorder(line, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    scale = line_height / line.shape[0]
    return cv2.resize(line, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def is_plausible_points(a: int, b: int) -> bool:
    """Un juego termina a 11 con 2 de diferencia: por encima de 11 la diferencia nunca pasa de 2."""
    return max(a, b) <= 11 or abs(a - b) <= 2


def split_candidates(digits: str) -> list[Score]:
    """Todas las formas de repartir las cifras en puntos(1-2) juegos(1) juegos(1) puntos(1-2)."""
    candidates = []
    for len1 in (1, 2):
        len4 = len(digits) - len1 - 2
        if len4 not in (1, 2):
            continue
        parts = (digits[:len1], digits[len1], digits[len1 + 1], digits[len1 + 2:])
        if any(len(x) == 2 and x[0] == "0" for x in parts):
            continue
        p1, g1, g2, p2 = (int(x) for x in parts)
        if g1 <= MAX_GAMES and g2 <= MAX_GAMES and is_plausible_points(p1, p2):
            candidates.append((p1, g1, g2, p2))
    return candidates


def parse_board(text: str, confidence: float, min_confidence: float, prev: Score | None) -> Score | None:
    """Convierte la lectura de la línea completa del marcador en un ``Score``.

    "6004" es inequívoco; "10015" puede ser 10-0-1-5 o 1-0-0-15: se descartan los marcadores
    imposibles y, si sigue habiendo varias opciones, se elige la más cercana al marcador anterior.
    """
    if confidence < min_confidence:
        return None
    text = text.replace("O", "0").replace("o", "0")
    if any(not (c.isdigit() or c in SEPARATORS) for c in text):
        return None
    candidates = split_candidates("".join(c for c in text if c.isdigit()))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates or prev is None:
        return None
    distance = lambda s: sum(abs(a - b) for a, b in zip(s, prev))  # noqa: E731
    candidates.sort(key=distance)
    if distance(candidates[0]) == distance(candidates[1]):
        return None
    return candidates[0]


# Modelos OCR ya cargados (tardan varios segundos), compartidos entre instancias de BoardReader.
_MODELS: dict[tuple[str, str], Any] = {}


class BoardReader:
    """Lee el marcador entero como una línea de texto con el reconocedor de PaddleOCR.

    Con dígitos pequeños e inclinados, leer la línea completa es mucho más fiable que leer cada
    tarjeta por separado (el modelo aprovecha el contexto); el reparto en cuatro números lo hace
    ``parse_board``.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        from paddleocr import TextRecognition  # import diferido: tarda varios segundos

        ocr_cfg = cfg.get("ocr", {})
        key = (ocr_cfg.get("model", "PP-OCRv6_medium_rec"), ocr_cfg.get("device", "cpu"))
        if key not in _MODELS:
            # enable_mkldnn=False evita un NotImplementedError de oneDNN en Paddle 3.3 sobre Windows.
            _MODELS[key] = TextRecognition(model_name=key[0], device=key[1], enable_mkldnn=False)
        self.model = _MODELS[key]
        self.min_confidence = ocr_cfg.get("min_confidence", 0.85)
        pre = cfg.get("preprocess", {})
        self.line_height = pre.get("line_height", 48)
        self.pad = pre.get("pad", 4)
        self.shear = pre.get("shear", 0.0)

    def read(self, board: np.ndarray, prev: Score | None = None,
             shear: float | None = None) -> tuple[Score | None, tuple[str, float]]:
        shear = self.shear if shear is None else shear
        result = self.model.predict([prepare_line(board, shear, self.line_height, self.pad)])[0]
        raw = (result["rec_text"], float(result["rec_score"]))
        return parse_board(*raw, self.min_confidence, prev), raw


class BoardTracker:
    """Sigue el marcador cuando la cámara se mueve un poco (cámara en mano, cambio de lado...).

    Busca la plantilla del marcador (recortada en el fotograma de calibración) alrededor de la
    última posición conocida y también alrededor de la posición calibrada (para recuperarse si
    se ha perdido). Cuando un jugador tapa el marcador, la mejor coincidencia suele ser otro
    objeto parecido algo más lejos; por eso un salto de más de ``max_step`` píxeles solo se
    acepta si la coincidencia es muy clara (``strong_score``).
    """

    def __init__(self, frame: np.ndarray, roi: dict[str, int], margin: int = 60, min_score: float = 0.5,
                 max_step: int = 20, strong_score: float = 0.85) -> None:
        self.template = cv2.cvtColor(extract_roi(frame, roi), cv2.COLOR_BGR2GRAY)
        self.anchor = dict(roi)
        self.roi = dict(roi)
        self.margin = margin
        self.min_score = min_score
        self.max_step = max_step
        self.strong_score = strong_score
        self.last_score = 1.0

    def _search(self, gray: np.ndarray, around: dict[str, int]) -> tuple[float, int, int] | None:
        height, width = gray.shape[:2]
        x, y, w, h = around["x"], around["y"], around["w"], around["h"]
        x0, y0 = max(0, x - self.margin), max(0, y - self.margin)
        x1, y1 = min(width, x + w + self.margin), min(height, y + h + self.margin)
        window = gray[y0:y1, x0:x1]
        if window.shape[0] < h or window.shape[1] < w:
            return None
        _, score, _, (bx, by) = cv2.minMaxLoc(cv2.matchTemplate(window, self.template, cv2.TM_CCOEFF_NORMED))
        return float(score), x0 + bx, y0 + by

    def locate(self, frame: np.ndarray) -> dict[str, int]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found = [r for r in (self._search(gray, self.roi), self._search(gray, self.anchor)) if r]
        if not found:
            return self.roi
        score, x, y = max(found)
        self.last_score = score
        step = max(abs(x - self.roi["x"]), abs(y - self.roi["y"]))
        if score >= self.min_score and (step <= self.max_step or score >= self.strong_score):
            self.roi = {**self.roi, "x": x, "y": y}
        return self.roi


@dataclass
class CameraSegment:
    """Una posición de cámara: se usa desde ``start`` hasta el inicio de la siguiente."""

    start: float
    roi: dict[str, int]
    shear: float
    calibration_time: float  # fotograma donde se marcó el marcador (plantilla del seguimiento)


def camera_segments(cfg: dict[str, Any]) -> list[CameraSegment]:
    """Posiciones de cámara del config: ``segments`` o, si no hay, la ``roi`` única de siempre."""
    if cfg.get("segments"):
        segments = [
            CameraSegment(float(s["start"]), {k: int(round(s["roi"][k])) for k in ("x", "y", "w", "h")},
                          float(s.get("shear", 0.0)), float(s.get("calibration_time", s["start"])))
            for s in cfg["segments"]
        ]
    else:
        segments = [CameraSegment(0.0, cfg["roi"], float(cfg.get("preprocess", {}).get("shear", 0.0)),
                                  float(cfg.get("calibration_time", 0.0)))]
    segments.sort(key=lambda seg: seg.start)
    segments[0].start = 0.0
    return segments


class SegmentedLocator:
    """Elige la posición de cámara que toca en cada instante y sigue el marcador dentro de ella.

    Cada posición tiene su propio ``BoardTracker`` (plantilla tomada de su fotograma de
    calibración), creado la primera vez que se necesita.
    """

    def __init__(self, video_path: str, segments: list[CameraSegment], track_cfg: dict[str, Any]) -> None:
        self.video_path = video_path
        self.segments = segments
        self.tracking = track_cfg.get("enabled", True)
        self.params = {k: v for k, v in track_cfg.items() if k != "enabled"}
        self.trackers: dict[int, BoardTracker] = {}

    def index_at(self, time_s: float) -> int:
        return max(i for i, seg in enumerate(self.segments) if seg.start <= time_s)

    def locate(self, frame: np.ndarray, time_s: float) -> tuple[int, dict[str, int], float, float | None]:
        """Devuelve (posición, roi, inclinación, coincidencia del seguimiento)."""
        i = self.index_at(time_s)
        seg = self.segments[i]
        if not self.tracking:
            return i, seg.roi, seg.shear, None
        if i not in self.trackers:
            self.trackers[i] = BoardTracker(read_frame(self.video_path, seg.calibration_time), seg.roi, **self.params)
        tracker = self.trackers[i]
        return i, tracker.locate(frame), seg.shear, tracker.last_score


def classify_change(prev: Score | None, new: Score) -> str:
    """Describe el cambio entre dos marcadores estables (en términos de lado izquierdo/derecho)."""
    if prev is None:
        return "inicio"
    p1, g1, g2, p2 = prev
    q1, h1, h2, q2 = new
    if (h1, h2) == (g1, g2):
        if (q1, q2) == (p1 + 1, p2):
            return "punto izq."
        if (q1, q2) == (p1, p2 + 1):
            return "punto der."
        if (q1, q2) in ((p1 - 1, p2), (p1, p2 - 1)):
            return "corrección"
        if (q1, q2) == (0, 0):
            return "reinicio puntos"
        return "irregular"
    if (h1, h2) == (g1 + 1, g2):
        return "juego izq."
    if (h1, h2) == (g1, g2 + 1):
        return "juego der."
    return "irregular"


@dataclass
class ScoreEvent:
    time_s: float
    score: Score
    event: str


class ScoreTracker:
    """Filtra lecturas ruidosas: un marcador solo se acepta tras ``stable_samples`` lecturas
    consecutivas idénticas. Las muestras ilegibles (oclusiones) se ignoran sin romper la racha.
    """

    def __init__(self, stable_samples: int = 3) -> None:
        self.stable_samples = stable_samples
        self.current: Score | None = None
        self._candidate: Score | None = None
        self._candidate_since = 0.0
        self._count = 0

    def update(self, time_s: float, reading: Score | None) -> ScoreEvent | None:
        if reading is None:
            return None
        if reading != self._candidate:
            self._candidate, self._candidate_since, self._count = reading, time_s, 0
        self._count += 1
        if self._count == self.stable_samples and reading != self.current:
            event = ScoreEvent(self._candidate_since, reading, label_change(self.current, reading))
            self.current = reading
            return event
        return None


def mirrored(score: Score) -> Score:
    """El mismo marcador visto con los lados intercambiados."""
    return score[3], score[2], score[1], score[0]


def label_change(prev: Score | None, new: Score) -> str:
    """Etiqueta de un cambio de marcador, reconociendo el giro del marcador al cambiar de juego."""
    label = classify_change(prev, new)
    # Al acabar un juego el árbitro suele girar el marcador con los jugadores: no es un error.
    if label == "irregular" and prev is not None and classify_change(prev, mirrored(new)).startswith("juego"):
        label = "juego + cambio de lado"
    return label


def detect_side_swaps(events: list[ScoreEvent]) -> bool:
    """¿El marcador cambia de lado con los jugadores al acabar cada juego?

    Los jugadores cambian de campo tras cada juego y muchos árbitros giran el marcador para que
    cada número siga junto a su jugador. En cada cambio de juego se comprueba qué hipótesis
    (sin cambio / con cambio de lado) explica mejor el nuevo marcador; si ambas lo explican,
    decide quién iba ganando en puntos justo antes.
    """
    votes = 0
    for prev, cur in zip(events, events[1:]):
        p, c = prev.score, cur.score
        if c[1] + c[2] <= p[1] + p[2]:
            continue
        stay = classify_change(p, c).startswith("juego")
        swap = classify_change(p, mirrored(c)).startswith("juego")
        if stay != swap:
            votes += 1 if swap else -1
        elif stay and p[0] != p[3]:
            leader_left = p[0] > p[3]
            stay_left_won = c[1] > p[1]
            swap_left_won = mirrored(c)[1] > p[1]
            if stay_left_won != swap_left_won:
                votes += 1 if swap_left_won == leader_left else -1
    return votes > 0


@dataclass
class PlayerState:
    time_s: float
    points: tuple[int, int]  # (jugador A, jugador B); A es quien empieza a la izquierda
    games: tuple[int, int]
    a_left: bool
    event: str


@dataclass
class GameResult:
    time_s: float
    winner: int  # 0 = A, 1 = B
    points: tuple[int, int]
    inferred: bool  # el último punto no llegó a leerse y se ha deducido


def player_view(events: list[ScoreEvent], swap_sides: bool | None = None) -> tuple[list[PlayerState], list[GameResult], bool]:
    """Traduce el marcador izquierda/derecha a puntos y juegos de cada jugador.

    ``swap_sides=None`` lo detecta automáticamente. Además del cambio tras cada juego, se
    reconoce un cambio de lado en mitad del juego decisivo (a los 5 puntos): el marcador pasa a
    ser la imagen especular de una transición válida.
    """
    if swap_sides is None:
        swap_sides = detect_side_swaps(events)
    states: list[PlayerState] = []
    games: list[GameResult] = []
    a_left = True
    prev: Score | None = None
    last_points: tuple[int, int] | None = None
    for e in events:
        s = e.score
        label = classify_change(prev, s)
        if prev is not None:
            new_game = s[1] + s[2] > prev[1] + prev[2]
            if (new_game and swap_sides) or (
                not new_game and label == "irregular"
                and classify_change(prev, mirrored(s)) != "irregular"
            ):
                a_left = not a_left
        oriented = s if a_left else mirrored(s)
        state = PlayerState(e.time_s, (oriented[0], oriented[3]), (oriented[1], oriented[2]), a_left, "")
        if states and sum(state.games) > sum(states[-1].games):
            winner = 0 if state.games[0] > states[-1].games[0] else 1
            pts = last_points or states[-1].points
            games.append(finish_game(e.time_s, winner, pts))
            state.event = f"juego {'AB'[winner]}"
            last_points = None
        elif states:
            diff = (state.points[0] - states[-1].points[0], state.points[1] - states[-1].points[1])
            state.event = {(1, 0): "punto A", (0, 1): "punto B"}.get(diff, label)
        else:
            state.event = "inicio"
        if sum(state.points) > 0:
            last_points = state.points
        states.append(state)
        prev = s
    return states, games, swap_sides


@dataclass
class MatchSummary:
    states: list[PlayerState]
    games: list[GameResult]
    swap_sides: bool
    games_won: tuple[int, int]
    winner: int | None  # None si el vídeo no llega al final del partido


def summarize_match(events: list[ScoreEvent], swap_sides: bool | None = None, best_of: int = 5) -> MatchSummary:
    """Vista por jugador más el resultado del partido.

    Al acabar el partido no se suele marcar el último punto (ni sumar el juego), así que si el
    último marcador leído deja al que va ganando a un punto de cerrar el juego *y* ese juego le
    da el partido, se cuenta como ganado. Si el marcador ya muestra un juego terminado (11-8)
    sin que se haya sumado, también se cuenta.
    """
    states, games, swap_sides = player_view(events, swap_sides)
    if not states:
        return MatchSummary(states, games, swap_sides, (0, 0), None)
    to_win = best_of // 2 + 1
    last = states[-1]
    games_won = list(last.games)
    a, b = last.points
    if a != b:
        leader = 0 if a > b else 1
        w, l = last.points[leader], last.points[1 - leader]
        finished = w >= 11 and w - l >= 2
        closes_match = w + 1 >= 11 and w + 1 - l >= 2 and games_won[leader] + 1 == to_win
        if finished or closes_match:
            games.append(finish_game(last.time_s, leader, last.points))
            games_won[leader] += 1
    winner = next((i for i in (0, 1) if games_won[i] >= to_win), None)
    return MatchSummary(states, games, swap_sides, (games_won[0], games_won[1]), winner)


def finish_game(time_s: float, winner: int, points: tuple[int, int]) -> GameResult:
    """Resultado de un juego a partir del último marcador leído antes del cambio.

    El árbitro suele poner 0-0 enseguida, así que a menudo no se llega a leer el punto final:
    si con un punto más el ganador cerraría el juego, se deduce ese resultado.
    """
    w, l = points[winner], points[1 - winner]
    if w >= 11 and w - l >= 2:
        return GameResult(time_s, winner, points, False)
    if w + 1 >= 11 and w + 1 - l >= 2:
        final = (w + 1, l) if winner == 0 else (l, w + 1)
        return GameResult(time_s, winner, final, True)
    return GameResult(time_s, winner, points, True)


def format_time(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def save_debug_image(frame: np.ndarray, cfg: dict[str, Any], path: str) -> None:
    """Guarda imágenes para calibrar las coordenadas del config.

    ``path``: fotograma completo con la ROI en rojo y una rejilla cada 100 px (para localizar
    el marcador en un vídeo nuevo). ``<path>_board``: la ROI enderezada y ampliada x4.
    """
    full = frame.copy()
    height, width = full.shape[:2]
    for x in range(0, width, 100):
        cv2.line(full, (x, 0), (x, height), (0, 255, 0), 1)
        cv2.putText(full, str(x), (x + 2, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    for y in range(0, height, 100):
        cv2.line(full, (0, y), (width, y), (0, 255, 0), 1)
        cv2.putText(full, str(y), (2, y - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    roi = cfg["roi"]
    cv2.rectangle(full, (roi["x"], roi["y"]), (roi["x"] + roi["w"], roi["y"] + roi["h"]), (0, 0, 255), 2)
    cv2.imwrite(path, full)

    try:
        board = extract_roi(frame, roi).copy()
    except ValueError as exc:
        print(f"La ROI no cabe en el fotograma ({width}x{height}): {exc}")
        return
    board = deskew(board, cfg.get("preprocess", {}).get("shear", 0.0))
    stem = Path(path)
    cv2.imwrite(str(stem.with_name(f"{stem.stem}_board{stem.suffix}")),
                cv2.resize(board, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))


def calibrate(cfg: dict[str, Any], at_s: float, path: str) -> None:
    """Guarda las imágenes de calibración del fotograma en el segundo ``at_s`` y termina."""
    frame = read_frame(cfg["video_path"], at_s)
    save_debug_image(frame, cfg, path)
    print(f"Imágenes de calibración guardadas en {path} ({frame.shape[1]}x{frame.shape[0]})")


@dataclass
class Sample:
    """Una lectura del vídeo, con lo necesario para diagnosticar problemas."""

    time_s: float
    reading: Score | None  # None = ilegible o descartada
    text: str  # texto bruto del OCR
    confidence: float
    roi: dict[str, int]
    track_score: float | None  # coincidencia con la plantilla del marcador (None sin seguimiento)


@dataclass
class RunStats:
    samples: int = 0
    unreadable: int = 0
    track_lost: int = 0
    longest_gap: tuple[float, float] = (0.0, 0.0)
    irregular: int = 0

    def summary(self) -> str:
        pct = 100 * self.unreadable / self.samples if self.samples else 0
        start, end = self.longest_gap
        return (f"{self.samples} lecturas, {self.unreadable} ilegibles ({pct:.0f}%), "
                f"mayor tramo sin lectura válida {format_time(start)}–{format_time(end)} ({end - start:.0f} s), "
                f"seguimiento perdido en {self.track_lost} lecturas, {self.irregular} cambios irregulares")


# Un tramo sin ninguna lectura válida más largo que esto se avisa en el log: suele indicar que
# el marcador se tapó, se movió fuera de la zona o la cámara cambió de plano.
LONG_GAP_S = 60.0


def run(
    cfg: dict[str, Any],
    on_sample: Callable[[Sample], None] | None = None,
    on_event: Callable[[ScoreEvent], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    reader: BoardReader | None = None,
    logger: logging.Logger = log,
) -> list[ScoreEvent]:
    """Recorre el vídeo y devuelve los cambios de marcador.

    ``on_sample`` recibe cada lectura, ``on_event`` cada cambio aceptado y ``should_stop``
    permite cancelar desde otro hilo. Cada lectura se registra en ``logger`` a nivel DEBUG, los
    cambios a nivel INFO y los problemas (tramos largos sin lectura, cambios irregulares) a nivel
    WARNING; al final se registra un resumen.
    """
    reader = reader or BoardReader(cfg)
    tracker = ScoreTracker(cfg.get("stable_samples", 3))
    track_cfg = cfg.get("track", {})
    segments = camera_segments(cfg)
    locator = SegmentedLocator(cfg["video_path"], segments, track_cfg)
    min_track = track_cfg.get("min_score", 0.5)
    logger.info("Análisis de %s: cada %.1f s, %d lecturas estables por cambio, %d posición(es) de cámara",
                cfg["video_path"], cfg.get("interval_seconds", 1.0), tracker.stable_samples, len(segments))
    for n, seg in enumerate(segments, 1):
        logger.info("  Posición %d desde %s: roi=%s inclinación=%.2f (calibrada en %s)", n, format_time(seg.start),
                    seg.roi, seg.shear, format_time(seg.calibration_time))
    current_segment = 0
    events: list[ScoreEvent] = []
    stats = RunStats()
    last_valid = 0.0

    def close_gap(now: float) -> None:
        if now - last_valid > stats.longest_gap[1] - stats.longest_gap[0]:
            stats.longest_gap = (last_valid, now)
        if now - last_valid >= LONG_GAP_S:
            logger.warning("%.0f s sin lecturas válidas (%s–%s): ¿marcador tapado o fuera de la zona?",
                           now - last_valid, format_time(last_valid), format_time(now))

    time_s = 0.0
    for time_s, frame in iter_samples(cfg["video_path"], cfg.get("interval_seconds", 1.0)):
        if should_stop and should_stop():
            logger.info("Detenido por el usuario en %s", format_time(time_s))
            break
        segment, roi, shear, track_score = locator.locate(frame, time_s)
        if segment != current_segment:
            current_segment = segment
            logger.info("%s cambio a la posición de cámara %d", format_time(time_s), segment + 1)
        reading, (text, conf) = reader.read(extract_roi(frame, roi), tracker.current, shear)
        sample = Sample(time_s, reading, text, conf, roi, track_score)
        stats.samples += 1
        if reading is None:
            stats.unreadable += 1
        else:
            close_gap(time_s)
            last_valid = time_s
        if track_score is not None and track_score < min_track:
            stats.track_lost += 1
        logger.debug("%s lectura=%s ocr=%r conf=%.2f roi=(%d,%d) seguimiento=%s", format_time(time_s),
                     "-".join(map(str, reading)) if reading else "ilegible", text, conf, roi["x"], roi["y"],
                     f"{sample.track_score:.2f}" if sample.track_score is not None else "off")
        if on_sample:
            on_sample(sample)
        event = tracker.update(time_s, reading)
        if event:
            events.append(event)
            if on_event:
                on_event(event)
            line = (f"{format_time(event.time_s)}  {event.score[0]:>2} ({event.score[1]}) - "
                    f"({event.score[2]}) {event.score[3]:<2}  {event.event}")
            if event.event == "irregular":
                stats.irregular += 1
                prev = events[-2].score if len(events) > 1 else None
                logger.warning("%s  (cambio irregular desde %s: ¿lectura errónea o puntos sin leer?)", line, prev)
            else:
                logger.info(line)
    close_gap(time_s)
    logger.info("Resumen: %s", stats.summary())
    return events


def write_csv(events: list[ScoreEvent], path: str, swap_sides: bool | None = None,
              names: tuple[str, str] = ("A", "B")) -> None:
    """CSV con el marcador tal cual (izquierda/derecha) y traducido a cada jugador."""
    states, _, _ = player_view(events, swap_sides)
    a, b = names
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["tiempo_s", "tiempo", *CARD_NAMES, "evento",
                         f"puntos_{a}", f"juegos_{a}", f"puntos_{b}", f"juegos_{b}", f"{a}_a_la_izquierda"])
        for e, p in zip(events, states):
            writer.writerow([f"{e.time_s:.1f}", format_time(e.time_s), *e.score, e.event,
                             p.points[0], p.games[0], p.points[1], p.games[1], int(p.a_left)])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--video", help="Ruta local o URL (YouTube...); sobrescribe video_path del config")
    parser.add_argument("--calibrate", type=float, metavar="SEG",
                        help="Solo guarda calibracion.png y calibracion_board.png del segundo SEG y termina")
    parser.add_argument("-v", "--verbose", action="store_true", help="Muestra cada lectura OCR")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    for noisy in ("paddlex", "paddle", "paddleocr", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = load_config(args.config)
    if args.video:
        cfg["video_path"] = args.video
    cfg["video_path"], _, _ = resolve_video(cfg["video_path"], cfg)
    if not Path(cfg["video_path"]).is_file():
        parser.error(f"no existe el vídeo: {cfg['video_path']}")
    if args.calibrate is not None:
        calibrate(cfg, args.calibrate, "calibracion.png")
        return
    events = run(cfg)
    write_csv(events, cfg.get("output_csv", "score.csv"))
    print(f"\n{len(events)} cambios de marcador guardados en {cfg.get('output_csv', 'score.csv')}")


if __name__ == "__main__":
    main()
