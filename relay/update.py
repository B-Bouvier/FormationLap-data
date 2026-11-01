#!/usr/bin/env python3
"""Relais Formation Lap : copie de jolpica-f1 en fichiers statiques.

Chaque passage (toutes les 15 minutes, via GitHub Actions) décide quels
fichiers ont besoin d'être rafraîchis, les télécharge chez jolpica et les
écrit dans `public/`, publié ensuite sur Cloudflare Pages. Les apps lisent
ces fichiers au lieu d'appeler jolpica : la charge sur jolpica ne dépend plus
du nombre d'utilisateurs.

Les réponses sont gardées telles quelles (format Ergast), sous des chemins
fixes et sans paramètres d'URL :

    {saison}.json                                calendrier
    {saison}/last/results.json                   dernière course courue
    {saison}/{manche}/results.json               résultat d'une course
    {saison}/{manche}/sprint.json                résultat d'un sprint
    {saison}/{manche}/qualifying.json            qualifications
    {saison}/{table}/offset-{n}.json             pages de toute la saison
    {saison}/driverstandings.json                classement pilotes
    {saison}/constructorstandings.json           classement constructeurs

Python 3.9+, bibliothèque standard seulement.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

SOURCE = "https://api.jolpi.ca/ergast/f1/"
USER_AGENT = "FormationLap-data relay (+https://github.com/B-Bouvier/FormationLap-data)"

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public" / "ergast" / "f1"
STATE_FILE = ROOT / "state.json"

PAGE_SIZE = 100
SEASON_TABLES = ("results", "sprint", "qualifying")

# Quotas jolpica : 4 req/s et 500 req/h. On reste nettement en dessous.
MIN_INTERVAL = 0.35          # secondes entre deux requêtes
MAX_PER_RUN = 100
MAX_PER_HOUR = 400

# Durée estimée des séances qui comptent pour l'app (en minutes).
DURATIONS = {"Qualifying": 75, "SprintQualifying": 60, "SprintShootout": 60, "Sprint": 60, "Race": 150}

# Fenêtres après la fin d'une séance.
HOT_WINDOW = timedelta(hours=6)       # résultats et classements en train d'arriver
RECENT_WINDOW = timedelta(days=3)     # décisions des commissaires possibles

# Durées de validité.
TTL_HOT = timedelta(minutes=10)       # un peu moins que l'intervalle du cron
TTL_HOT_PAGES = timedelta(minutes=55)
TTL_RECENT = timedelta(hours=2)
TTL_COLD = timedelta(hours=24)
TTL_SCHEDULE = timedelta(hours=6)
TTL_PAST = timedelta(days=7)
TTL_PREVIOUS_SEASON = timedelta(days=14)


# MARK: - Calendrier


@dataclass(frozen=True)
class SessionTime:
    kind: str
    start: datetime

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=DURATIONS.get(self.kind, 60))


@dataclass(frozen=True)
class Round:
    number: int
    sessions: List[SessionTime]

    def session(self, kind: str) -> Optional[SessionTime]:
        return next((s for s in self.sessions if s.kind == kind), None)

    @property
    def has_sprint(self) -> bool:
        return self.session("Sprint") is not None

    def ended(self, kind: str, now: datetime) -> bool:
        session = self.session(kind)
        return session is not None and session.end <= now

    def last_end(self, now: datetime) -> Optional[datetime]:
        """Fin de la dernière séance terminée de la manche."""
        ends = [s.end for s in self.sessions if s.end <= now]
        return max(ends) if ends else None


def parse_datetime(date: str, clock: Optional[str]) -> datetime:
    # Sans heure (vieilles saisons), on prend la fin de journée : prudent.
    clock = (clock or "23:59:00Z").rstrip("Z")
    return datetime.fromisoformat(f"{date}T{clock}").replace(tzinfo=timezone.utc)


def parse_schedule(payload: dict) -> List[Round]:
    races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    rounds = []
    for race in races:
        sessions = [SessionTime("Race", parse_datetime(race["date"], race.get("time")))]
        for kind in DURATIONS:
            if kind != "Race" and kind in race:
                block = race[kind]
                sessions.append(SessionTime(kind, parse_datetime(block["date"], block.get("time"))))
        rounds.append(Round(int(race["round"]), sessions))
    return rounds


# MARK: - Plan de rafraîchissement


@dataclass(frozen=True)
class Target:
    path: str        # chemin publié, relatif à PUBLIC
    source: str      # chemin jolpica, sans la base
    ttl: timedelta
    paged: bool = False


def season_phase(rounds: List[Round], now: datetime) -> str:
    """'hot', 'recent' ou 'cold' selon la dernière séance terminée."""
    ends = [end for r in rounds for end in [r.last_end(now)] if end is not None]
    if not ends:
        return "cold"
    since = now - max(ends)
    if since <= HOT_WINDOW:
        return "hot"
    if since <= RECENT_WINDOW:
        return "recent"
    return "cold"


def dynamic_ttl(phase: str, previous_season: bool, pages: bool = False) -> timedelta:
    if previous_season:
        return TTL_PREVIOUS_SEASON
    if phase == "hot":
        return TTL_HOT_PAGES if pages else TTL_HOT
    if phase == "recent":
        return TTL_RECENT
    return TTL_COLD


def plan_season(season: int, rounds: List[Round], now: datetime, previous_season: bool) -> List[Target]:
    """Fichiers à tenir à jour pour une saison, avec leur durée de validité."""
    phase = season_phase(rounds, now)
    ttl = dynamic_ttl(phase, previous_season)
    page_ttl = dynamic_ttl(phase, previous_season, pages=True)
    targets: List[Target] = []

    raced = any(r.ended("Race", now) for r in rounds)
    if raced:
        targets.append(Target(f"{season}/last/results.json", f"{season}/last/results.json", ttl))
        targets.append(Target(f"{season}/driverstandings.json", f"{season}/driverstandings.json", ttl))
        targets.append(Target(f"{season}/constructorstandings.json", f"{season}/constructorstandings.json", ttl))

    for table, kind in (("results", "Race"), ("sprint", "Sprint"), ("qualifying", "Qualifying")):
        if any(r.ended(kind, now) for r in rounds):
            targets.append(Target(f"{season}/{table}", f"{season}/{table}.json", page_ttl, paged=True))

    for r in rounds:
        last_end = r.last_end(now)
        if last_end is None:
            continue
        # La manche en cours ou tout juste finie bouge encore ; les autres non.
        current = not previous_season and now - last_end <= RECENT_WINDOW
        round_ttl = ttl if current else (TTL_PREVIOUS_SEASON if previous_season else TTL_PAST)
        if r.ended("Qualifying", now):
            targets.append(Target(f"{season}/{r.number}/qualifying.json", f"{season}/{r.number}/qualifying.json", round_ttl))
        if r.has_sprint and r.ended("Sprint", now):
            targets.append(Target(f"{season}/{r.number}/sprint.json", f"{season}/{r.number}/sprint.json", round_ttl))
        if r.ended("Race", now):
            targets.append(Target(f"{season}/{r.number}/results.json", f"{season}/{r.number}/results.json", round_ttl))

    return targets


# MARK: - Réseau


class BudgetExhausted(Exception):
    pass


class Client:
    def __init__(self, state: dict, now: datetime):
        self.now = now
        cutoff = (now - timedelta(hours=1)).timestamp()
        self.history: List[float] = [t for t in state.get("requests", []) if t >= cutoff]
        self.count = 0
        self.last_request = 0.0

    def get(self, source: str, offset: Optional[int] = None) -> dict:
        if self.count >= MAX_PER_RUN or len(self.history) >= MAX_PER_HOUR:
            raise BudgetExhausted()

        query = f"?limit={PAGE_SIZE}" + (f"&offset={offset}" if offset is not None else "")
        url = SOURCE + source + query
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})

        for attempt in range(4):
            wait = MIN_INTERVAL - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.monotonic()
            self.count += 1
            self.history.append(time.time())
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 429 or error.code >= 500:
                    retry_after = error.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
                    time.sleep(min(delay, 60))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(2 ** (attempt + 1))
                continue
            if not isinstance(payload, dict) or "MRData" not in payload:
                raise ValueError(f"réponse inattendue pour {url}")
            return payload
        raise RuntimeError(f"échec après plusieurs tentatives : {url}")


# MARK: - Fichiers


def write_json(path: Path, payload: dict) -> bool:
    """Écrit seulement si le contenu change. Retourne True s'il a changé."""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def is_due(state: dict, path: str, ttl: timedelta, now: datetime) -> bool:
    fetched = state.get("fetched", {}).get(path)
    # Un fichier (ou un dossier de pages) absent est toujours à télécharger.
    if fetched is None or not (PUBLIC / path).exists():
        return True
    age = now - datetime.fromisoformat(fetched)
    return age < timedelta(0) or age >= ttl


def fetch_target(client: Client, target: Target) -> bool:
    """Télécharge une cible. Retourne True si un fichier publié a changé."""
    if not target.paged:
        return write_json(PUBLIC / target.path, client.get(target.source))

    changed = False
    offset, total = 0, 1
    kept = set()
    while offset < total and offset < 10 * PAGE_SIZE:
        payload = client.get(target.source, offset=offset)
        total = int(payload["MRData"].get("total", "0"))
        name = f"offset-{offset}.json"
        kept.add(name)
        changed |= write_json(PUBLIC / target.path / name, payload)
        offset += PAGE_SIZE
    # Une page qui n'existe plus (total en baisse) ne doit pas rester publiée.
    directory = PUBLIC / target.path
    for stale in directory.glob("offset-*.json"):
        if stale.name not in kept:
            stale.unlink()
            changed = True
    return changed


# MARK: - Passage


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def run(now: datetime) -> bool:
    state = load_state()
    state.setdefault("fetched", {})
    client = Client(state, now)
    changed = False
    errors = 0

    def stamp(path: str) -> None:
        state["fetched"][path] = now.isoformat(timespec="seconds")

    try:
        for season in (now.year, now.year - 1):
            previous_season = season != now.year
            schedule_path = f"{season}.json"
            schedule_ttl = TTL_PREVIOUS_SEASON if previous_season else TTL_SCHEDULE
            if is_due(state, schedule_path, schedule_ttl, now):
                try:
                    changed |= write_json(PUBLIC / schedule_path, client.get(schedule_path))
                    stamp(schedule_path)
                    print(f"calendrier {season} rafraîchi")
                except BudgetExhausted:
                    raise
                except Exception as error:  # noqa: BLE001 — on garde l'ancienne version
                    errors += 1
                    print(f"échec calendrier {season} : {error}", file=sys.stderr)

            schedule_file = PUBLIC / schedule_path
            if not schedule_file.exists():
                continue
            rounds = parse_schedule(json.loads(schedule_file.read_text()))

            for target in plan_season(season, rounds, now, previous_season):
                if not is_due(state, target.path, target.ttl, now):
                    continue
                try:
                    changed |= fetch_target(client, target)
                    stamp(target.path)
                    print(f"{target.path} rafraîchi")
                except BudgetExhausted:
                    raise
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    print(f"échec {target.path} : {error}", file=sys.stderr)
    except BudgetExhausted:
        print("quota du passage atteint : la suite au prochain passage")

    state["requests"] = client.history
    save_state(state)
    print(f"{client.count} requête(s), {errors} échec(s), fichiers publiés {'modifiés' if changed else 'inchangés'}")
    return changed


def main() -> None:
    changed = run(datetime.now(timezone.utc))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
