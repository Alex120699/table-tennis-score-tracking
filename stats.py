"""Estadísticas de partido y de jugador a partir del marcador leído.

El análisis guarda cambios de marcador, no puntos. Aquí se reconstruye la lista de puntos de
cada juego (quién lo ganó y con qué marcador se jugó) y de ahí salen las estadísticas:

- Saque y resto: en tenis de mesa el saque cambia cada 2 puntos y, desde 10-10, cada punto;
  y quien sacó primero en un juego resta primero en el siguiente. Sabiendo quién sacó el
  primer punto del partido se sabe quién sacó cada punto cuyo marcador previo se conoce.
- Rachas, remontadas, bolas de juego, juegos a 10-10 y tiempo entre puntos.

Si entre dos lecturas faltan puntos de los dos jugadores (p. ej. de 3-3 a 5-4), se cuentan
para el total pero no se sabe su orden: no entran en saque, rachas ni bolas de juego.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

import score_tracker as st

# Un hueco mayor entre dos cambios de marcador no es la duración de un punto (tiempo muerto,
# marcador tapado, descanso entre juegos...).
MAX_POINT_GAP_S = 120.0


@dataclass
class Point:
    game: int  # número de juego en el partido (0 = el primero)
    winner: int  # 0 = jugador A, 1 = jugador B
    before: tuple[int, int] | None  # marcador (A, B) antes del punto; None si no se conoce el orden
    time_s: float  # instante en que el marcador lo reflejó
    duration: float | None  # tiempo desde el cambio de marcador anterior
    server: int | None = None


def _add_points(points: list[Point], game: int, frm: tuple[int, int], to: tuple[int, int], time_s: float,
                duration: float | None) -> None:
    da, db = to[0] - frm[0], to[1] - frm[1]
    if da < 0 or db < 0 or da + db == 0:
        return
    if da + db == 1:
        points.append(Point(game, 0 if da else 1, frm, time_s, duration))
    elif da == 0 or db == 0:
        # Varios puntos seguidos del mismo jugador: el orden da igual, el marcador previo se conoce.
        winner = 0 if da else 1
        for k in range(da + db):
            before = (frm[0] + k, frm[1]) if winner == 0 else (frm[0], frm[1] + k)
            points.append(Point(game, winner, before, time_s, None))
    else:
        points.extend(Point(game, 0, None, time_s, None) for _ in range(da))
        points.extend(Point(game, 1, None, time_s, None) for _ in range(db))


def _clean_sequence(seq: list[tuple[float, tuple[int, int]]],
                    bound: tuple[int, int] | None) -> list[tuple[float, tuple[int, int]]]:
    """Subsecuencia más larga de marcadores que nunca bajan y no pasan del resultado final.

    Descarta lecturas erróneas sueltas: un "10-0" al empezar un juego que acabó 4-11, o un "1"
    leído un momento en lugar de "10".
    """
    cand = [(t, p) for t, p in seq if bound is None or (p[0] <= bound[0] and p[1] <= bound[1])]
    if not cand:
        return []
    best, prev = [1] * len(cand), [-1] * len(cand)
    for i, (_, pi) in enumerate(cand):
        for j in range(i):
            pj = cand[j][1]
            if pj[0] <= pi[0] and pj[1] <= pi[1] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    i = max(range(len(cand)), key=lambda k: (best[k], k))
    chain = []
    while i != -1:
        chain.append(cand[i])
        i = prev[i]
    return chain[::-1]


def reconstruct_points(summary: st.MatchSummary) -> list[Point]:
    """Lista ordenada de puntos del partido a partir de los marcadores por jugador.

    Por cada juego se limpia la secuencia de marcadores (``_clean_sequence``) y se cierra con el
    resultado del resumen, que incluye el último punto deducido.
    """
    states = summary.states
    if not states:
        return []
    offset = sum(states[0].games)  # el vídeo puede empezar con juegos ya jugados
    results = {offset + i: g for i, g in enumerate(summary.games)}
    by_game: dict[int, list[tuple[float, tuple[int, int]]]] = {}
    for s in states:
        by_game.setdefault(sum(s.games), []).append((s.time_s, s.points))
    for g in results:
        by_game.setdefault(g, [])
    points: list[Point] = []
    for game in sorted(by_game):
        result = results.get(game)
        base, last_t = (0, 0), None
        for t, score in _clean_sequence(by_game[game], result.points if result else None):
            duration = t - last_t if last_t is not None and t - last_t <= MAX_POINT_GAP_S else None
            _add_points(points, game, base, score, t, duration)
            if score != base:
                base, last_t = score, t
        if result:
            _add_points(points, game, base, result.points, result.time_s, None)
    return points


def server_of(game: int, before: tuple[int, int], first_server: int) -> int:
    """Quién saca con el marcador ``before`` del juego ``game`` (0 = primer juego del partido)."""
    first = first_server if game % 2 == 0 else 1 - first_server
    k = sum(before)
    turn = (k // 2) % 2 if k < 20 else (k - 20) % 2  # desde 10-10 el saque cambia cada punto
    return first if turn == 0 else 1 - first


def match_stats(summary: st.MatchSummary, first_server: int | None) -> dict[str, Any]:
    points = reconstruct_points(summary)
    if first_server in (0, 1):
        for p in points:
            if p.before is not None:
                p.server = server_of(p.game, p.before, first_server)

    players = [{
        "points_won": 0, "serve_won": 0, "serve_played": 0, "return_won": 0, "return_played": 0,
        "longest_run": None, "comeback": None, "gp_had": 0, "gp_converted": 0, "gp_faced": 0, "gp_saved": 0,
        "deuce_won": 0,
    } for _ in (0, 1)]
    for p in points:
        players[p.winner]["points_won"] += 1
        if p.server is not None:
            s = p.server
            players[s]["serve_played"] += 1
            players[1 - s]["return_played"] += 1
            if p.winner == s:
                players[s]["serve_won"] += 1
            else:
                players[1 - s]["return_won"] += 1
        if p.before is not None:
            for who in (0, 1):  # bola de juego: con un punto más, ``who`` gana el juego
                mine, theirs = p.before[who], p.before[1 - who]
                if mine >= 10 and mine > theirs:
                    players[who]["gp_had"] += 1
                    players[1 - who]["gp_faced"] += 1
                    if p.winner == who:
                        players[who]["gp_converted"] += 1
                    else:
                        players[1 - who]["gp_saved"] += 1
    total = len(points)
    for pl in players:
        pl["points_pct"] = round(100 * pl["points_won"] / total, 1) if total else None

    # Por juego: duración, rachas, ventaja máxima, remontada y 10-10.
    offset = sum(summary.states[0].games) if summary.states else 0
    games_out = []
    numbers = sorted({p.game for p in points})
    for g in numbers:
        gp = [p for p in points if p.game == g]
        result = summary.games[g - offset] if 0 <= g - offset < len(summary.games) else None
        game_states = [s for s in summary.states if sum(s.games) == g]
        start = game_states[0].time_s if game_states else gp[0].time_s
        end = result.time_s if result else gp[-1].time_s
        runs, max_lead, deficit = [0, 0], [0, 0], [0, 0]
        run_who, run_len, run_from = None, 0, None
        deuce = False
        for p in gp:
            if p.before is None:  # orden desconocido: corta la racha
                run_who, run_len = None, 0
                continue
            a, b = p.before
            deuce = deuce or (a == b and a >= 10)
            deficit[0], deficit[1] = max(deficit[0], b - a), max(deficit[1], a - b)
            if p.winner == run_who:
                run_len += 1
            else:
                run_who, run_len, run_from = p.winner, 1, p.before
            if run_len > runs[p.winner]:
                runs[p.winner] = run_len
                after = (a + (p.winner == 0), b + (p.winner == 1))
                best = players[p.winner]["longest_run"]
                if best is None or run_len > best["length"]:
                    players[p.winner]["longest_run"] = {"length": run_len, "game": g + 1, "from": run_from,
                                                        "to": after, "t": p.time_s}
            after = (a + (p.winner == 0), b + (p.winner == 1))
            max_lead[0], max_lead[1] = max(max_lead[0], after[0] - after[1]), max(max_lead[1], after[1] - after[0])
        winner = result.winner if result else None
        comeback = deficit[winner] if winner is not None and deficit[winner] >= 2 else 0
        if winner is not None:
            if deuce:
                players[winner]["deuce_won"] += 1
            best = players[winner]["comeback"]
            if comeback and (best is None or comeback > best["deficit"]):
                players[winner]["comeback"] = {"deficit": comeback, "game": g + 1, "final": list(result.points)}
        games_out.append({
            "number": g + 1, "winner": winner, "points": list(result.points) if result else None,
            "finished": result is not None, "start": start, "end": end, "duration": end - start,
            "longest_run": runs, "max_lead": max_lead, "comeback": comeback, "deuce": deuce,
        })

    timed = [p for p in points if p.duration is not None]
    longest = max(timed, key=lambda p: p.duration, default=None)
    return {
        "first_server": first_server,
        "points": {"total": total, "unordered": sum(p.before is None for p in points),
                   "with_server": sum(p.server is not None for p in points)},
        "players": players,
        "deuce_games": sum(g["deuce"] for g in games_out),
        "point_time": {
            "avg": round(sum(p.duration for p in timed) / len(timed), 1) if timed else None,
            "max": round(longest.duration, 1) if longest else None,
            "max_t": longest.time_s if longest else None,
            "count": len(timed),
        },
        "games": games_out,
    }


# --------------------------------------------------------------------------- jugadores


def player_key(name: str) -> str:
    """Clave para agrupar el mismo jugador escrito de formas distintas (tildes, mayúsculas...)."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return "-".join(plain.lower().split())


def _chronological(match: dict[str, Any]) -> tuple[str, str]:
    """Por fecha del partido (o del análisis si no se indicó) y, a igual fecha, por orden de análisis."""
    return match["date"], match["created_at"]


def _side(match: dict[str, Any], p: int) -> dict[str, Any]:
    """Un partido visto desde el jugador ``p`` (0 = A, 1 = B)."""
    s = match["stats"]
    me, rival = s["players"][p], s["players"][1 - p]
    return {
        "id": match["id"], "date": match["date"], "title": match["title"],
        "rival": match["names"][1 - p], "rival_key": player_key(match["names"][1 - p]),
        "won": None if match["winner"] is None else match["winner"] == p,
        "games": [match["games_won"][p], match["games_won"][1 - p]],
        "points": [me["points_won"], rival["points_won"]], "points_pct": me["points_pct"],
        "serve": [me["serve_won"], me["serve_played"]], "return": [me["return_won"], me["return_played"]],
        "gp": [me["gp_converted"], me["gp_had"]], "gp_saved": [me["gp_saved"], me["gp_faced"]],
        "deuce": [me["deuce_won"], s["deuce_games"]],
    }


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def add(key: str) -> list[int]:
        return [sum(r[key][0] for r in rows), sum(r[key][1] for r in rows)]

    return {
        "matches": len(rows), "wins": sum(r["won"] is True for r in rows), "losses": sum(r["won"] is False for r in rows),
        "games": add("games"), "points": add("points"), "serve": add("serve"), "return": add("return"),
        "gp": add("gp"), "gp_saved": add("gp_saved"), "deuce": add("deuce"),
    }


def players_overview(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Todos los jugadores del historial con su balance. ``matches``: partidos con "stats"."""
    by_key: dict[str, dict[str, Any]] = {}
    for m in sorted(matches, key=_chronological):
        for p in (0, 1):
            key = player_key(m["names"][p])
            entry = by_key.setdefault(key, {"key": key, "name": m["names"][p], "rows": []})
            entry["name"] = m["names"][p]  # la grafía más reciente
            entry["rows"].append(_side(m, p))
    out = []
    for entry in by_key.values():
        out.append({"key": entry["key"], "name": entry["name"], **_totals(entry["rows"]),
                    "last": entry["rows"][-1]["date"]})
    return sorted(out, key=lambda e: (-e["matches"], e["name"].lower()))


def player_profile(key: str, matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows, name = [], None
    for m in sorted(matches, key=_chronological):
        for p in (0, 1):
            if player_key(m["names"][p]) == key:
                rows.append(_side(m, p))
                name = m["names"][p]
    if not rows:
        return None
    rivals: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        rivals.setdefault(r["rival_key"], []).append(r)
    head_to_head = sorted(
        ({"key": k, "name": rs[-1]["rival"], **_totals(rs)} for k, rs in rivals.items()),
        key=lambda h: (-h["matches"], h["name"].lower()),
    )
    return {"key": key, "name": name, "totals": _totals(rows), "matches": rows, "head_to_head": head_to_head}
