"""Plataforma web local para score_tracker: historial de partidos analizados.

- Historial: todos los partidos analizados con su resultado.
- Nuevo análisis: pegar un enlace de YouTube (o una ruta local) -> marcar el marcador en un
  fotograma -> analizar el vídeo (en segundo plano; se puede seguir navegando).
- Ficha de partido: evolución, resultado de cada juego, tabla de puntos y CSV.

Uso:
    python app.py            # abre http://127.0.0.1:5000
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import os
import re
import tempfile
import threading
import time
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any

import cv2
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

import db
import score_tracker as st
import stats

ROOT = Path(__file__).parent
BASE_CFG = st.load_config(ROOT / "config.json")

LOG_DIR = db.DATA_DIR / "logs"
LOG_FORMAT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
log = logging.getLogger("app")

app = Flask(__name__, static_folder=None)


def setup_logging() -> None:
    """data/logs/app.log (rotativo) + consola. Cada análisis tiene además su propio fichero."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    file_handler = logging.handlers.RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=5,
                                                        encoding="utf-8")
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setLevel(logging.INFO)  # las lecturas una a una (DEBUG) solo van al log del partido
        handler.setFormatter(LOG_FORMAT)
        root.addHandler(handler)
    for noisy in ("paddlex", "paddle", "paddleocr", "PIL", "werkzeug", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def match_log_path(match_id: int) -> Path:
    return LOG_DIR / f"match_{match_id}.log"


@app.errorhandler(Exception)
def unhandled_error(exc: Exception):
    if isinstance(exc, HTTPException):
        # En la API el mensaje tiene que llegar a la página, que espera {"error": ...}.
        return (jsonify(error=exc.description), exc.code) if request.path.startswith("/api/") else exc
    log.exception("Error no controlado en %s %s", request.method, request.path)
    return jsonify(error=f"Error interno: {exc}"), 500

# Tarea en curso (descarga o análisis). Uso local: un vídeo cada vez.
lock = threading.Lock()
ocr_lock = threading.Lock()  # el predictor de Paddle no se usa desde dos hilos a la vez
stop_flag = threading.Event()
job: dict[str, Any] = {
    "stage": "idle",  # idle | downloading | ready | running | error
    "message": "",
    "download_pct": 0.0,
    "video": None,
    "calibration": None,  # calibración sugerida (la del último análisis de ese vídeo)
    "progress": 0.0,
    "current_time": 0.0,
    "started_at": None,
    "last_reading": None,
    "match_id": None,
}


def set_job(**kwargs: Any) -> None:
    with lock:
        job.update(kwargs)


def url_start_time(source: str) -> float:
    """Lee el parámetro ``t`` de un enlace de YouTube (``t=95`` o ``t=95s``)."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(source).query)
    value = query.get("t", ["0"])[0].rstrip("s")
    return float(value) if value.isdigit() else 0.0


def job_video() -> dict[str, Any]:
    if not job["video"]:
        abort(409, "No hay ningún vídeo cargado")
    return job["video"]


def normalize_calibration(calib: dict[str, Any] | None) -> dict[str, Any] | None:
    """Calibración con una lista de posiciones de cámara (``segments``).

    Formato actual: ``{interval, segments: [{start, t, roi, shear, rough}]}``. Las calibraciones
    guardadas antes de existir las posiciones (``{t, roi, shear, rough}``) se convierten en una
    única posición desde el segundo 0.
    """
    if not calib:
        return calib
    if "segments" not in calib:
        single = {k: calib[k] for k in ("t", "roi", "shear", "rough") if k in calib}
        calib = {**{k: v for k, v in calib.items() if k not in single}, "segments": [{"start": 0.0, **single}]}
    return calib


def build_cfg(video_path: str, calib: dict[str, Any]) -> dict[str, Any]:
    calib = normalize_calibration(calib)
    segments = sorted(calib["segments"], key=lambda seg: seg.get("start", 0))
    if not segments or any(not seg.get("roi") for seg in segments):
        raise ValueError("falta marcar el marcador en alguna posición de cámara")
    cfg = copy.deepcopy(BASE_CFG)
    cfg["video_path"] = video_path
    cfg["segments"] = [{"start": float(seg.get("start", 0)), "roi": seg["roi"], "shear": float(seg.get("shear", 0)),
                        "calibration_time": float(seg.get("t", seg.get("start", 0)))} for seg in segments]
    first = st.camera_segments(cfg)[0]
    cfg["roi"], cfg["preprocess"]["shear"], cfg["calibration_time"] = first.roi, first.shear, first.calibration_time
    if calib.get("interval"):
        cfg["interval_seconds"] = float(calib["interval"])
    return cfg


def jpeg(image) -> Any:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        abort(500)
    return app.response_class(buf.tobytes(), mimetype="image/jpeg")


def save_thumbnail(match_id: int, video_path: str, t: float) -> None:
    frame = st.read_frame(video_path, t)
    scale = 480 / frame.shape[1]
    db.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(db.THUMBS_DIR / f"{match_id}.jpg"), cv2.resize(frame, None, fx=scale, fy=scale,
                                                                   interpolation=cv2.INTER_AREA))


def valid_date(value: Any) -> str | None:
    """"AAAA-MM-DD" válida o None (vacía o mal formada)."""
    try:
        return date.fromisoformat(value).isoformat() if value else None
    except (TypeError, ValueError):
        return None


def match_or_404(match_id: int):
    row = db.get_match(match_id)
    if row is None:
        abort(404)
    return row


# --------------------------------------------------------------------------- páginas


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/static/<path:name>")
def static_file(name: str):
    return send_from_directory(ROOT / "static", name)


# --------------------------------------------------------------------------- tarea en curso


@app.get("/api/job")
def job_status():
    with lock:
        return jsonify(job)


@app.post("/api/videos")
def load_video():
    """Descarga (si es un enlace) y prepara un vídeo para calibrarlo."""
    source = (request.json or {}).get("source", "").strip()
    if not source:
        return jsonify(error="Falta el enlace o la ruta del vídeo"), 400
    if job["stage"] in ("downloading", "running"):
        return jsonify(error="Hay una tarea en curso; espera a que termine"), 409

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = 100.0 * d.get("downloaded_bytes", 0) / total if total else 0.0
            set_job(download_pct=pct, message=f"Descargando… {pct:.0f}%")

    def worker() -> None:
        try:
            path, title, date = st.resolve_video(source, BASE_CFG, hook)
            if not Path(path).is_file():
                raise FileNotFoundError(f"No existe el vídeo: {path}")
            path = str(Path(path).resolve())  # misma clave en el historial venga de URL o de ruta
            log.info("Vídeo listo: %s (%s)", title, path)
            set_job(stage="ready", message="", download_pct=100.0,
                    video={"path": path, "title": title, "source": source, "start": url_start_time(source), "date": date,
                           **st.video_info(path)},
                    calibration=normalize_calibration(db.last_calibration(path)))
        except Exception as exc:  # noqa: BLE001 - se muestra al usuario
            log.exception("No se pudo cargar el vídeo %s", source)
            set_job(stage="error", message=f"No se pudo cargar el vídeo: {exc}")

    log.info("Cargando vídeo: %s", source)
    set_job(stage="downloading", message="Preparando descarga…", download_pct=0.0, video=None,
            calibration=None, match_id=None)
    threading.Thread(target=worker, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/frame")
def frame():
    return jpeg(st.read_frame(job_video()["path"], float(request.args.get("t", 0))))


@app.post("/api/board")
def board():
    """Marcador recortado y enderezado, tal como lo verá el OCR."""
    data = request.json or {}
    try:
        roi = {k: int(round(data["roi"][k])) for k in ("x", "y", "w", "h")}
        image = st.extract_roi(st.read_frame(job_video()["path"], float(data["t"])), roi)
        return jpeg(st.deskew(image, float(data.get("shear", 0))))
    except (KeyError, ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/test")
def test_read():
    """Lee el marcador en un fotograma con el recuadro y la inclinación dados ({t, roi, shear})."""
    data = request.json or {}
    try:
        roi = {k: int(round(data["roi"][k])) for k in ("x", "y", "w", "h")}
        image = st.extract_roi(st.read_frame(job_video()["path"], float(data["t"])), roi)
        with ocr_lock:
            reading, (text, conf) = st.BoardReader(BASE_CFG).read(image, shear=float(data.get("shear", 0)))
    except (KeyError, ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(reading=reading, text=text, confidence=conf)


@app.post("/api/test-video")
def test_video():
    """Prueba la calibración en momentos repartidos por todo el vídeo antes de analizarlo entero.

    Un recuadro que incluye las rayas de debajo de las cifras o la mano del árbitro puede leerse
    bien en el fotograma de calibración y fallar en la mitad del partido; esto lo detecta en
    unos segundos.
    """
    data = request.json or {}
    if job["stage"] == "running":
        return jsonify(error="Hay un análisis en curso"), 409
    samples = 20
    try:
        video = job_video()
        cfg = build_cfg(video["path"], data)
        reader = st.BoardReader(cfg)
        locator = st.SegmentedLocator(video["path"], st.camera_segments(cfg), cfg.get("track", {}))
        span = max(1.0, video["duration"] - 10)
        results = []
        with ocr_lock:
            for i in range(samples):
                t = 5 + span * i / (samples - 1)
                frame = st.read_frame(video["path"], t)
                segment, roi, shear, _ = locator.locate(frame, t)
                reading, (text, conf) = reader.read(st.extract_roi(frame, roi), shear=shear)
                results.append({"t": t, "segment": segment, "reading": reading, "text": text, "confidence": conf})
    except (KeyError, ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400
    readable = sum(r["reading"] is not None for r in results)
    log.info("Prueba de calibración en %s: %d/%d legibles", video["title"], readable, samples)
    return jsonify(readable=readable, total=samples, samples=results)


@app.post("/api/job/stop")
def stop():
    stop_flag.set()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- partidos


@app.get("/api/matches")
def list_matches():
    return jsonify(db.list_matches())


@app.post("/api/matches")
def create_match():
    """Crea el partido en el historial y lanza el análisis del vídeo cargado."""
    data = request.json or {}
    if job["stage"] == "running":
        return jsonify(error="Ya hay un análisis en curso"), 409
    video = job_video()
    calib = data.get("calibration") or {}
    try:
        cfg = build_cfg(video["path"], calib)
    except (KeyError, ValueError, TypeError) as exc:
        return jsonify(error=f"Calibración incompleta: {exc}"), 400
    calib = normalize_calibration(calib)
    names = data.get("names") or ["Jugador A", "Jugador B"]
    options = ((names[0], names[1]), int(data.get("best_of", 5)), data.get("swap", "auto"))
    replace_id = data.get("replace_id")
    if replace_id:
        # Reanálisis de un partido del historial: se conservan id y fecha, se sustituyen los eventos.
        if match_or_404(int(replace_id))["video_path"] != video["path"]:
            return jsonify(error="El vídeo cargado no es el del partido a reanalizar"), 400
        match_id = int(replace_id)
        db.reset_match(match_id, calib, *options)
        log.info("Reanalizando el partido %d", match_id)
    else:
        match_id = db.create_match(video["source"], video, calib, *options,
                                   valid_date(data.get("match_date")) or video.get("date"))
    save_thumbnail(match_id, video["path"], cfg["calibration_time"])
    duration = video["duration"] or 1.0

    def on_sample(sample: st.Sample) -> None:
        set_job(progress=min(1.0, sample.time_s / duration), current_time=sample.time_s,
                last_reading=sample.reading, message="")

    def on_event(event: st.ScoreEvent) -> None:
        db.append_event(match_id, {"t": event.time_s, "score": list(event.score), "event": event.event})

    # Log propio del partido con todas las lecturas (DEBUG); lo importante sube también a app.log.
    mlog = logging.getLogger(f"match.{match_id}")
    handler = logging.FileHandler(match_log_path(match_id), mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(LOG_FORMAT)
    mlog.addHandler(handler)
    mlog.info("Partido %d: %s vs %s · %s · %s", match_id, names[0], names[1], video["title"], video["source"])

    def worker() -> None:
        try:
            with ocr_lock:
                st.run(cfg, on_sample=on_sample, on_event=on_event, should_stop=stop_flag.is_set, logger=mlog)
            if stop_flag.is_set():
                db.finish_match(match_id, "stopped", f"Detenido en {st.format_time(job['current_time'])}")
            else:
                db.finish_match(match_id, "done")
            mlog.info("Análisis terminado (%s)", "detenido" if stop_flag.is_set() else "completo")
            set_job(stage="ready", progress=1.0 if not stop_flag.is_set() else job["progress"])
        except Exception as exc:  # noqa: BLE001
            mlog.exception("Error durante el análisis")
            db.finish_match(match_id, "error", str(exc))
            set_job(stage="error", message=f"Error durante el análisis: {exc}")
        finally:
            mlog.removeHandler(handler)
            handler.close()

    stop_flag.clear()
    set_job(stage="running", message="Cargando OCR…", progress=0.0, current_time=0.0, started_at=time.time(),
            last_reading=None, match_id=match_id)
    threading.Thread(target=worker, daemon=True).start()
    return jsonify(id=match_id)


@app.get("/api/matches/<int:match_id>")
def get_match(match_id: int):
    return jsonify(db.match_dict(match_or_404(match_id), full=True))


@app.patch("/api/matches/<int:match_id>")
def update_match(match_id: int):
    match_or_404(match_id)
    data = request.json or {}
    fields: dict[str, Any] = {}
    if "names" in data:
        fields["player_a"], fields["player_b"] = (n.strip() or d for n, d in zip(data["names"], "AB"))
    if data.get("best_of") in (1, 3, 5, 7):
        fields["best_of"] = data["best_of"]
    if data.get("swap") in db.SWAP_OPTIONS:
        fields["swap"] = data["swap"]
    if "first_server" in data and data["first_server"] in (0, 1, None):
        fields["first_server"] = data["first_server"]
    if "match_date" in data:
        fields["match_date"] = valid_date(data["match_date"])
    db.update_options(match_id, **fields)
    return jsonify(db.match_dict(match_or_404(match_id), full=True))


def editable_match(match_id: int):
    row = match_or_404(match_id)
    if row["status"] == "running":
        abort(409, "El partido se está analizando; espera a que termine para corregirlo")
    return row


def corrected_event(data: dict[str, Any], a_left: bool, duration: float) -> dict[str, Any]:
    """Evento a partir de una corrección hecha en términos de jugador (A, B).

    Se guarda como el OCR: izquierda/derecha, según en qué lado estaba A en ese momento.
    """
    t = float(data["t"])
    pa, pb = (int(v) for v in data["points"])
    ga, gb = (int(v) for v in data["games"])
    if not (0 <= t <= duration and 0 <= pa <= 40 and 0 <= pb <= 40 and 0 <= ga <= 4 and 0 <= gb <= 4):
        raise ValueError("valores fuera de rango")
    score = [pa, ga, gb, pb] if a_left else [pb, gb, ga, pa]
    return {"t": t, "score": score, "event": "", "manual": True}


def match_view(match_id: int) -> tuple[Any, dict[str, Any]]:
    row = match_or_404(match_id)
    return row, db.match_dict(row, full=True)


@app.post("/api/matches/<int:match_id>/events")
def insert_event(match_id: int):
    """Añade un punto que el OCR no leyó. ``after``: índice del evento tras el que va (o -1)."""
    row = editable_match(match_id)
    m = db.match_dict(row, full=True)
    data = request.json or {}
    after = int(data.get("after", -1))
    a_left = m["players"][after]["a_left"] if 0 <= after < len(m["players"]) else True
    try:
        event = corrected_event(data, a_left, m["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify(error=f"Punto no válido: {exc}"), 400
    db.save_corrected_events(match_id, m["events"] + [event])
    log.info("Partido %d: añadido a mano %s %s-%s (juegos %s-%s)", match_id, st.format_time(event["t"]),
             *data["points"], *data["games"])
    return jsonify(match_view(match_id)[1])


@app.put("/api/matches/<int:match_id>/events/<int:index>")
def update_event(match_id: int, index: int):
    row = editable_match(match_id)
    m = db.match_dict(row, full=True)
    if not 0 <= index < len(m["events"]):
        abort(404)
    data = request.json or {}
    try:
        event = corrected_event(data, m["players"][index]["a_left"], m["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify(error=f"Punto no válido: {exc}"), 400
    old = m["players"][index]
    events = m["events"]
    events[index] = event
    db.save_corrected_events(match_id, events)
    log.info("Partido %d: corregido %s %s-%s (juegos %s-%s) -> %s %s-%s (juegos %s-%s)", match_id,
             st.format_time(old["t"]), *old["points"], *old["games"], st.format_time(event["t"]),
             *data["points"], *data["games"])
    return jsonify(match_view(match_id)[1])


@app.delete("/api/matches/<int:match_id>/events/<int:index>")
def delete_event(match_id: int, index: int):
    row = editable_match(match_id)
    m = db.match_dict(row, full=True)
    if not 0 <= index < len(m["events"]):
        abort(404)
    old = m["players"][index]
    db.save_corrected_events(match_id, m["events"][:index] + m["events"][index + 1:])
    log.info("Partido %d: borrada la lectura %s %s-%s (juegos %s-%s)", match_id, st.format_time(old["t"]),
             *old["points"], *old["games"])
    return jsonify(match_view(match_id)[1])


@app.post("/api/matches/<int:match_id>/events/restore")
def restore_events(match_id: int):
    editable_match(match_id)
    if db.restore_ocr_events(match_id):
        log.info("Partido %d: restaurada la lectura original del OCR", match_id)
    return jsonify(match_view(match_id)[1])


@app.delete("/api/matches/<int:match_id>")
def delete_match(match_id: int):
    match_or_404(match_id)
    if job["stage"] == "running" and job["match_id"] == match_id:
        return jsonify(error="Detén el análisis antes de borrarlo"), 409
    db.delete_match(match_id)
    match_log_path(match_id).unlink(missing_ok=True)
    log.info("Partido %d borrado del historial", match_id)
    return jsonify(ok=True)


def finished_matches() -> list[dict[str, Any]]:
    """Partidos que cuentan para los jugadores: análisis terminado y con algún punto leído."""
    return [m for m in db.list_matches() if m["status"] != "running" and m["stats"]["points"]["total"]]


@app.get("/api/players")
def list_players():
    return jsonify(stats.players_overview(finished_matches()))


@app.get("/api/players/<key>")
def get_player(key: str):
    profile = stats.player_profile(key, finished_matches())
    if profile is None:
        abort(404)
    return jsonify(profile)


@app.get("/api/matches/<int:match_id>/frame")
def match_frame(match_id: int):
    row = match_or_404(match_id)
    if not Path(row["video_path"]).is_file():
        abort(404)
    return jpeg(st.read_frame(row["video_path"], float(request.args.get("t", 0))))


LOG_SAMPLE = re.compile(
    r"(\d+):(\d\d):(\d\d) lectura=(\S+) ocr=(?:'(.*?)'|\"(.*?)\") conf=([\d.]+) roi=\((\d+),(\d+)\)")
_log_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}


def logged_samples(match_id: int) -> list[dict[str, Any]]:
    """Lecturas segundo a segundo del último análisis, sacadas de su log (vacío si no hay log)."""
    path = match_log_path(match_id)
    if not path.is_file():
        return []
    mtime = path.stat().st_mtime
    if match_id in _log_cache and _log_cache[match_id][0] == mtime:
        return _log_cache[match_id][1]
    samples = []
    for m in LOG_SAMPLE.finditer(path.read_text(encoding="utf-8")):
        h, mi, s, reading, text1, text2, conf, x, y = m.groups()
        samples.append({
            "t": int(h) * 3600 + int(mi) * 60 + int(s),
            "reading": None if reading == "ilegible" else [int(v) for v in reading.split("-")],
            "text": text1 if text1 is not None else text2, "confidence": float(conf),
            "roi": {"x": int(x), "y": int(y)},
        })
    _log_cache[match_id] = (mtime, samples)
    return samples


@app.get("/api/matches/<int:match_id>/inspect")
def inspect_moment(match_id: int):
    """Qué leyó el OCR alrededor del instante ``t``: recuadro, inclinación y lecturas de cada segundo.

    Si el partido tiene log se devuelve lo que leyó de verdad el análisis; si no (partidos
    analizados antes de existir los logs), se vuelve a leer en el momento.
    """
    row = match_or_404(match_id)
    t = float(request.args.get("t", 0))
    cfg = build_cfg(row["video_path"], json.loads(row["calibration"]))
    segments = st.camera_segments(cfg)
    locator = st.SegmentedLocator(row["video_path"], segments, cfg.get("track", {}))
    segment = locator.index_at(t)
    size = {"w": segments[segment].roi["w"], "h": segments[segment].roi["h"]}
    window = (t - 2.5, t + 3.5)
    samples = [dict(s, roi={**s["roi"], **size}) for s in logged_samples(match_id) if window[0] <= s["t"] <= window[1]]
    source = "log"
    if not samples and Path(row["video_path"]).is_file():
        if job["stage"] == "running":
            return jsonify(error="Hay un análisis en curso; vuelve a intentarlo cuando termine"), 409
        source = "ocr"
        reader = st.BoardReader(cfg)
        with ocr_lock:
            for second in range(int(window[0]) + 1, int(window[1]) + 1):
                if second < 0 or second > row["duration"]:
                    continue
                frame = st.read_frame(row["video_path"], second)
                seg, roi, shear, _ = locator.locate(frame, second)
                reading, (text, conf) = reader.read(st.extract_roi(frame, roi), shear=shear)
                samples.append({"t": second, "reading": reading, "text": text, "confidence": conf, "roi": roi})
    return jsonify(t=t, segment=segment, shear=segments[segment].shear, source=source, samples=samples)


@app.get("/api/matches/<int:match_id>/board")
def match_board(match_id: int):
    """El marcador recortado y enderezado en el instante ``t``, tal como lo recibe el OCR."""
    row = match_or_404(match_id)
    try:
        roi = {k: int(request.args[k]) for k in ("x", "y", "w", "h")}
        image = st.extract_roi(st.read_frame(row["video_path"], float(request.args.get("t", 0))), roi)
        return jpeg(st.deskew(image, float(request.args.get("shear", 0))))
    except (KeyError, ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400


@app.get("/api/matches/<int:match_id>/thumb")
def match_thumb(match_id: int):
    path = db.THUMBS_DIR / f"{match_id}.jpg"
    if not path.is_file():
        abort(404)
    return send_file(path.resolve(), mimetype="image/jpeg")


@app.get("/api/matches/<int:match_id>/log")
def match_log(match_id: int):
    """Registro completo del análisis: cada lectura, cada cambio, avisos y resumen."""
    path = match_log_path(match_id)
    if not path.is_file():
        return app.response_class("Este partido no tiene registro (se analizó antes de existir los logs).",
                                  mimetype="text/plain; charset=utf-8")
    return send_file(path.resolve(), mimetype="text/plain; charset=utf-8")


@app.get("/api/logs")
def app_log():
    """Últimas líneas del log general del servidor."""
    path = LOG_DIR / "app.log"
    lines = path.read_text(encoding="utf-8").splitlines()[-500:] if path.is_file() else []
    return app.response_class("\n".join(lines) or "Log vacío", mimetype="text/plain; charset=utf-8")


@app.get("/api/matches/<int:match_id>/csv")
def match_csv(match_id: int):
    row = match_or_404(match_id)
    path = Path(tempfile.gettempdir()) / f"partido_{match_id}.csv"
    db.write_match_csv(row, path)
    name = f"{row['player_a']} vs {row['player_b']}.csv".replace("/", "-")
    return send_file(path, as_attachment=True, download_name=name)


if __name__ == "__main__":
    setup_logging()
    # Un análisis en marcha escribe en su log cada segundo. Si el log lleva rato quieto, el servidor
    # que lo hacía se cerró; si no, lo está haciendo otro servidor con la misma base de datos.
    stale = [i for i in db.running_ids()
             if not match_log_path(i).is_file() or time.time() - match_log_path(i).stat().st_mtime > 120]
    if stale:
        db.mark_interrupted(stale)
        log.warning("Análisis a medias de un cierre anterior (%s); marcados como detenidos", stale)
    port = int(os.environ.get("PORT", 5000))
    log.info("Servidor iniciado: abre http://127.0.0.1:%d en el navegador", port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
