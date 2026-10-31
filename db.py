"""Historial de partidos analizados (SQLite, en data/matches.db).

Cada partido guarda el vídeo de origen, la calibración usada, los cambios de marcador leídos
(izquierda/derecha, tal cual) y las opciones de presentación (nombres, cambio de lado, al mejor
de N). El resultado por jugador se calcula al vuelo con ``score_tracker.summarize_match``, así
que cambiar una opción no obliga a repetir el análisis.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import score_tracker as st
import stats

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "matches.db"
THUMBS_DIR = DATA_DIR / "thumbs"

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,          -- enlace de YouTube o ruta local, tal como se introdujo
    video_path   TEXT NOT NULL,
    title        TEXT NOT NULL,
    duration     REAL NOT NULL,
    player_a     TEXT NOT NULL,          -- empieza a la izquierda
    player_b     TEXT NOT NULL,
    best_of      INTEGER NOT NULL DEFAULT 5,
    swap         TEXT NOT NULL DEFAULT 'auto',  -- auto | yes | no
    calibration  TEXT NOT NULL,          -- JSON
    events       TEXT NOT NULL DEFAULT '[]',    -- JSON: [{t, score, event}]
    status       TEXT NOT NULL,          -- running | done | stopped | error
    message      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
"""

SWAP_OPTIONS = {"auto": None, "yes": True, "no": False}


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migraciones de columnas añadidas después de crear la tabla.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    if "first_server" not in columns:  # quién sacó el primer punto: 0 = A, 1 = B, NULL = no se sabe
        conn.execute("ALTER TABLE matches ADD COLUMN first_server INTEGER")
    if "match_date" not in columns:  # fecha en que se jugó ("AAAA-MM-DD"), editable
        conn.execute("ALTER TABLE matches ADD COLUMN match_date TEXT")
    if "events_ocr" not in columns:  # copia de la lectura del OCR antes de la primera corrección manual
        conn.execute("ALTER TABLE matches ADD COLUMN events_ocr TEXT")
    return conn


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_match(source: str, video: dict[str, Any], calibration: dict[str, Any], names: tuple[str, str],
                 best_of: int, swap: str, match_date: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO matches (source, video_path, title, duration, player_a, player_b, best_of, swap,
                                    calibration, status, created_at, match_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
            (source, video["path"], video["title"], video["duration"], names[0], names[1], best_of, swap,
             json.dumps(calibration), now(), match_date),
        )
        return int(cur.lastrowid)


def reset_match(match_id: int, calibration: dict[str, Any], names: tuple[str, str], best_of: int,
                swap: str) -> None:
    """Reanálisis: mismo partido del historial, eventos nuevos."""
    with connect() as conn:
        conn.execute(
            """UPDATE matches SET calibration = ?, player_a = ?, player_b = ?, best_of = ?, swap = ?,
                                  events = '[]', events_ocr = NULL, status = 'running', message = '',
                                  finished_at = NULL
               WHERE id = ?""",
            (json.dumps(calibration), names[0], names[1], best_of, swap, match_id),
        )


def append_event(match_id: int, event: dict[str, Any]) -> None:
    with connect() as conn:
        row = conn.execute("SELECT events FROM matches WHERE id = ?", (match_id,)).fetchone()
        events = json.loads(row["events"]) + [event]
        conn.execute("UPDATE matches SET events = ? WHERE id = ?", (json.dumps(events), match_id))


def save_corrected_events(match_id: int, events: list[dict[str, Any]]) -> None:
    """Guarda eventos corregidos a mano, ordenados y reetiquetados.

    La primera vez se guarda una copia de la lectura del OCR para poder restaurarla.
    """
    cleaned, prev = [], None
    for e in sorted(events, key=lambda e: e["t"]):
        if prev == tuple(e["score"]):
            continue  # igual que el anterior (p. ej. tras corregir una lectura errónea): no es un cambio
        e["event"] = st.label_change(prev, tuple(e["score"]))
        prev = tuple(e["score"])
        cleaned.append(e)
    events = cleaned
    with connect() as conn:
        conn.execute("UPDATE matches SET events_ocr = COALESCE(events_ocr, events), events = ? WHERE id = ?",
                     (json.dumps(events), match_id))


def restore_ocr_events(match_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE matches SET events = events_ocr, events_ocr = NULL "
                           "WHERE id = ? AND events_ocr IS NOT NULL", (match_id,))
        return cur.rowcount > 0


def finish_match(match_id: int, status: str, message: str = "") -> None:
    with connect() as conn:
        conn.execute("UPDATE matches SET status = ?, message = ?, finished_at = ? WHERE id = ?",
                     (status, message, now(), match_id))


def update_options(match_id: int, **fields: Any) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("player_a", "player_b", "best_of", "swap", "first_server", "match_date")}
    if not allowed:
        return
    with connect() as conn:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE matches SET {sets} WHERE id = ?", (*allowed.values(), match_id))


def delete_match(match_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))
    (THUMBS_DIR / f"{match_id}.jpg").unlink(missing_ok=True)


def running_ids() -> list[int]:
    with connect() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM matches WHERE status = 'running'")]


def mark_interrupted(match_ids: list[int]) -> None:
    """Marca como detenidos análisis que se quedaron a medias (se cerró el servidor que los hacía)."""
    with connect() as conn:
        conn.executemany("UPDATE matches SET status = 'stopped', message = 'Interrumpido al cerrar el servidor' "
                         "WHERE id = ? AND status = 'running'", [(i,) for i in match_ids])


def last_calibration(video_path: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT calibration, player_a, player_b, best_of FROM matches WHERE video_path = ? "
                           "ORDER BY id DESC LIMIT 1", (video_path,)).fetchone()
    if not row:
        return None
    calib = json.loads(row["calibration"])
    calib.update(names=[row["player_a"], row["player_b"]], best_of=row["best_of"])
    return calib


def summary(row: sqlite3.Row) -> st.MatchSummary:
    events = [st.ScoreEvent(e["t"], tuple(e["score"]), e["event"]) for e in json.loads(row["events"])]
    return st.summarize_match(events, SWAP_OPTIONS.get(row["swap"]), row["best_of"])


def match_dict(row: sqlite3.Row, full: bool = False) -> dict[str, Any]:
    """Partido listo para la API: datos guardados + resultado calculado."""
    m = summary(row)
    names = (row["player_a"], row["player_b"])
    data = {
        "id": row["id"],
        "source": row["source"],
        "title": row["title"],
        "duration": row["duration"],
        "names": names,
        "best_of": row["best_of"],
        "swap": row["swap"],
        "swap_detected": m.swap_sides,
        "status": row["status"],
        "message": row["message"],
        "created_at": row["created_at"],
        "match_date": row["match_date"],
        "date": row["match_date"] or row["created_at"][:10],  # para ordenar: la del partido o, si no, la del análisis
        "finished_at": row["finished_at"],
        "games_won": m.games_won,
        "winner": m.winner,
        "games": [{"t": g.time_s, "winner": g.winner, "points": g.points, "inferred": g.inferred}
                  for g in m.games],
        "thumb": (THUMBS_DIR / f"{row['id']}.jpg").is_file(),
        "first_server": row["first_server"],
        "manual_edits": sum(bool(e.get("manual")) for e in json.loads(row["events"])),
        "has_ocr_backup": row["events_ocr"] is not None,
        "stats": stats.match_stats(m, row["first_server"]),
    }
    if full:
        data["events"] = json.loads(row["events"])
        data["players"] = [{"t": p.time_s, "points": p.points, "games": p.games, "a_left": p.a_left,
                            "event": p.event} for p in m.states]
        data["calibration"] = json.loads(row["calibration"])
        data["video_path"] = row["video_path"]
    return data


def list_matches() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM matches "
                            "ORDER BY COALESCE(match_date, substr(created_at, 1, 10)) DESC, id DESC").fetchall()
    return [match_dict(r) for r in rows]


def get_match(match_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()


def write_match_csv(row: sqlite3.Row, path: Path) -> None:
    events = [st.ScoreEvent(e["t"], tuple(e["score"]), e["event"]) for e in json.loads(row["events"])]
    st.write_csv(events, str(path), SWAP_OPTIONS.get(row["swap"]), (row["player_a"], row["player_b"]))
