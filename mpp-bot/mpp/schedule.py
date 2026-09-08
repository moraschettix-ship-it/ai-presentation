"""Fenetres de tir et gestion des horaires.

Contraintes reelles a absorber :

1. MPP affiche les heures en heure de Paris ("18h45", "21h00"). On les convertit
   en UTC des la lecture ; tout le reste du code ne manipule que de l'UTC
   timezone-aware. Le passage heure d'ete / heure d'hiver est gere par zoneinfo,
   pas par un decalage code en dur.

2. Le cron de GitHub Actions n'est PAS ponctuel : les jobs planifies sont
   regulierement retardes de 5 a 20 minutes aux heures chargees, et peuvent
   etre purement sautes. Un declenchement "a T-30 pile" est donc impossible a
   garantir. La parade est structurelle :
     - la passe J-1 garantit qu'un pronostic existe TOUJOURS, meme si toutes
       les passes tardives sautent ;
     - la passe tardive accepte une fenetre large (T-90 a T-15) et le workflow
       tourne toutes les 10 minutes dedans, donc un retard de cron reste
       rattrape ;
     - la soumission est idempotente et ecrase, donc plusieurs passages dans la
       fenetre sont sans consequence.

3. Marge de securite : on ne soumet jamais a moins de `deadline_margin` du coup
   d'envoi, pour ne pas courir apres la fermeture de la saisie cote MPP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Card

SITE_TZ = ZoneInfo("Europe/Paris")

_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[hH:]\s*(\d{2})?\s*$")
_DATE_RE = re.compile(r"^\s*(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\s*$")

# Tolerance avant de considerer qu'une heure affichee concerne le lendemain.
# Un match affiche a 18h45 alors qu'il est 23h00 est celui de demain ; un match
# affiche a 21h00 alors qu'il est 22h30 est celui d'il y a une heure et demie,
# et doit rester date d'aujourd'hui pour etre correctement ecarte comme passe.
PAST_GRACE_HOURS = 6


class ScheduleError(ValueError):
    pass


@dataclass
class Windows:
    """Bornes des deux passes, en minutes avant le coup d'envoi."""

    early_min: int = 6 * 60        # passe J-1 : de T-48h ...
    early_max: int = 48 * 60       # ... a T-6h
    late_min: int = 15             # passe tardive : de T-90min ...
    late_max: int = 90             # ... a T-15min
    deadline_margin: int = 6       # jamais de soumission a moins de T-6min


def parse_site_time(day: date, hhmm: str, tz: ZoneInfo = SITE_TZ) -> datetime:
    """Convertit une heure affichee par MPP ("18h45") en datetime UTC.

    `day` est la date locale du match, pas la date UTC : un match a 21h00 CEST
    tombe le meme jour local mais a 19:00 UTC, et un hypothetique 00h30 tombe
    le lendemain en UTC. La conversion via zoneinfo gere ce cas et les
    changements d'heure.
    """
    m = _TIME_RE.match(hhmm)
    if not m:
        raise ScheduleError(f"heure illisible : {hhmm!r}")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"heure hors bornes : {hhmm!r}")
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return local.astimezone(timezone.utc)


def resolve_kickoff(
    time_text: str,
    date_text: str | None,
    now: datetime,
    tz: ZoneInfo = SITE_TZ,
) -> datetime:
    """Datetime UTC d'un coup d'envoi, a partir de ce qui est affiche.

    Trois cas, du plus sur au moins sur :

    1. La carte affiche une date ("15/09") : on l'utilise. L'annee n'etant
       presque jamais affichee, on choisit celle qui place le match au plus
       pres de maintenant - sinon un match du 3 janvier lu le 28 decembre
       serait date de l'annee ecoulee.
    2. Pas de date, et l'heure tombe dans le futur ou le passe recent : c'est
       aujourd'hui.
    3. Pas de date, et l'heure est passee depuis plus de PAST_GRACE_HOURS :
       c'est le match de demain. Sans cette regle, une page consultee le soir
       daterait les matchs du lendemain de la veille, et le bot les ignorerait.
    """
    local_now = now.astimezone(tz)

    if date_text:
        m = _DATE_RE.match(date_text)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            if m.group(3):
                year = int(m.group(3))
                year += 2000 if year < 100 else 0
                return parse_site_time(date(year, month, day), time_text, tz)
            candidates = []
            for year in (local_now.year - 1, local_now.year, local_now.year + 1):
                try:
                    candidates.append(parse_site_time(date(year, month, day), time_text, tz))
                except ValueError:
                    continue    # 29 fevrier hors annee bissextile
            if candidates:
                return min(candidates, key=lambda d: abs((d - now).total_seconds()))

    today = parse_site_time(local_now.date(), time_text, tz)
    if today < now - timedelta(hours=PAST_GRACE_HOURS):
        return parse_site_time(local_now.date() + timedelta(days=1), time_text, tz)
    return today


def to_site_local(dt: datetime, tz: ZoneInfo = SITE_TZ) -> datetime:
    return dt.astimezone(tz)


def minutes_to_kickoff(card: Card, now: datetime) -> float:
    return (card.kickoff - now).total_seconds() / 60.0


def is_due(card: Card, now: datetime, windows: Windows, phase: str) -> bool:
    """Le match doit-il etre (re)soumis lors de cette passe ?"""
    if card.locked:
        return False
    delta = minutes_to_kickoff(card, now)
    if delta < windows.deadline_margin:
        return False   # trop tard, ou match deja commence / termine
    if phase == "early":
        return windows.early_min <= delta <= windows.early_max
    if phase == "late":
        return windows.late_min <= delta <= windows.late_max
    if phase == "any":
        return delta <= windows.early_max
    raise ScheduleError(f"phase inconnue : {phase!r}")


def due_cards(
    cards: list[Card], now: datetime, windows: Windows, phase: str
) -> list[Card]:
    return sorted(
        (c for c in cards if is_due(c, now, windows, phase)),
        key=lambda c: c.kickoff,
    )


def next_wake_up(cards: list[Card], now: datetime, windows: Windows) -> datetime | None:
    """Prochain instant ou au moins un match entre en fenetre tardive.

    Sert au mode `gate` : si rien n'est du avant longtemps, le workflow s'arrete
    avant meme d'installer Chromium.
    """
    candidates = [
        c.kickoff - timedelta(minutes=windows.late_max)
        for c in cards
        if not c.locked and c.kickoff > now
    ]
    future = [t for t in candidates if t > now]
    return min(future) if future else None
