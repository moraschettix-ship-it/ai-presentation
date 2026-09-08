"""Detection de structure sans selecteur CSS.

Pourquoi ce module existe
-------------------------
Un bot de scraping classique code en dur des selecteurs (`div.match-card`,
`.odds-value`...). Ils cassent au premier redesign, et ils ne peuvent pas etre
ecrits sans avoir le site sous les yeux.

Ici on identifie une carte de match par sa SIGNATURE NUMERIQUE, qui elle ne
depend d'aucune classe CSS :

    - trois nombres a 2-4 chiffres  -> les cotes
    - trois pourcentages sommant a ~100 -> la repartition des joueurs
    - une heure au format 18h45 ou 18:45
    - deux libelles textuels -> les equipes

Cette signature est extremement discriminante : aucune autre partie d'une page
de pronostics ne la reproduit par accident. Et surtout, elle survit a un
changement de classes, de balises ou d'imbrication.

Verification finale, decisive : le triplet doit donner un K = 1/somme(1/cote)
plausible (cf odds.py). Un triplet mal capture echoue ce test, donc une carte
mal lue est REJETEE au lieu d'etre pronostiquee de travers.

Des selecteurs CSS explicites restent acceptes dans config.yaml et prennent le
pas sur l'heuristique : si un jour le site devient hostile, on repasse en
manuel sans changer une ligne de code.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any

# Le script est evalue dans la page. Il pose un attribut data-mpp-card sur
# chaque carte detectee pour que Python puisse recuperer un handle ensuite.
CARD_SCAN_JS = r"""
() => {
  const RE_COTE = /^\d{2,4}$/;
  const RE_PCT  = /^(\d{1,3})\s*%$/;
  // Sur un noeud feuille l'heure est isolee, donc ancree. Le repli sur
  // innerText ne peut PAS etre ancre, mais il ne peut pas non plus utiliser \b :
  // deux spans voisins ("J.1" et "18h45") se concatenent sans espace dans
  // innerText, ce qui donne "J.118h45" ou aucune frontiere de mot ne separe le
  // parasite de l'heure. On exige donc explicitement un non-chiffre avant.
  const RE_TIME = /^(\d{1,2})\s*[hH:]\s*(\d{2})$/;
  const RE_TIME_LOOSE = /(?:^|[^\d])((?:[01]?\d|2[0-3])\s*[hH:]\s*[0-5]\d)(?!\d)/;
  const RE_ORDINAL = /^\d{1,3}\s*(?:e|er|ere|eme|ème)$/i;
  const RE_MATCHDAY = /^J\.?\s*\d{1,2}$/i;
  const RE_SINGLE = /^\d$/;
  // Date compacte affichee par la plupart des grilles : "15/09", "15.09",
  // "15/09/2026". Sans elle, une carte de demain serait datee d'aujourd'hui.
  const RE_DATE = /^(\d{1,2})[\/.](\d{1,2})(?:[\/.](\d{2,4}))?$/;
  const NOISE = /^(valider|validé|valide|pronostic|pronostics|score|vs|-|—|:|match|terminé|termine|en cours|live|bonus)$/i;

  const leafText = (el) => {
    if (el.children.length > 0) return null;
    const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
    return t.length > 0 && t.length <= 60 ? t : null;
  };

  const all = Array.from(document.querySelectorAll('body *'));
  const coteCount = new Map();
  const pctCount = new Map();

  const bump = (map, el) => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      map.set(n, (map.get(n) || 0) + 1);
    }
  };

  for (const el of all) {
    const t = leafText(el);
    if (t === null) continue;
    if (RE_COTE.test(t)) bump(coteCount, el);
    else if (RE_PCT.test(t)) bump(pctCount, el);
  }

  // L'ancrage se fait sur les POURCENTAGES, pas sur les cotes : un pourcentage
  // est non ambigu (il porte un %), alors qu'un classement affiche "36" est
  // indiscernable d'une cote par sa seule forme. Une carte contient donc
  // exactement 3 pourcentages, mais peut exposer PLUS de trois nombres
  // ressemblant a des cotes (le tri se fait ensuite cote Python, cf
  // _pick_cote_triplet).
  //
  // Partir du plus petit ancetre ne suffit pas : ce serait le bloc de
  // statistiques, sans les noms d'equipes ni l'heure. On remonte donc tant que
  // le compte de pourcentages reste a 3, ce qui fait entrer les equipes et
  // l'heure, et s'arrete juste avant d'absorber la carte voisine.
  const seeds = all.filter(el => pctCount.get(el) === 3 && (coteCount.get(el) || 0) >= 3);
  const cards = new Set();
  for (const el of seeds) {
    let best = el;
    for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
      if (pctCount.get(n) === 3) best = n;
      else break;
    }
    cards.add(best);
  }

  const idAttrs = ['data-match-id', 'data-matchid', 'data-id', 'data-game-id', 'id'];
  const out = [];
  let idx = 0;

  for (const card of cards) {
    // Ne garder que les cartes maximales : si une carte detectee en contient
    // une autre, c'est un conteneur, pas une carte.
    if ([...cards].some(other => other !== card && card.contains(other))) continue;

    const cotes = [], pcts = [], names = [], singles = [];
    let firstPctOrder = Infinity, order = 0, timeLeaf = null, dateLeaf = null;

    for (const el of card.querySelectorAll('*')) {
      const t = leafText(el);
      if (t === null) continue;
      order++;
      if (RE_COTE.test(t)) { cotes.push({ text: t, order }); }
      else if (RE_PCT.test(t)) { pcts.push({ text: t, order }); firstPctOrder = Math.min(firstPctOrder, order); }
      else if (RE_SINGLE.test(t)) { singles.push({ text: t, order }); }
      else if (RE_TIME.test(t)) { if (timeLeaf === null) timeLeaf = t; }
      else if (RE_DATE.test(t)) { if (dateLeaf === null) dateLeaf = t; }
      else if (RE_ORDINAL.test(t) || RE_MATCHDAY.test(t) || NOISE.test(t)) { /* bruit connu */ }
      else if (/[A-Za-zÀ-ÿ]{2}/.test(t)) { names.push({ text: t, order }); }
    }

    // Les alt d'images de logo sont une source de noms plus fiable que le texte
    // quand le site n'affiche que des ecussons.
    const alts = Array.from(card.querySelectorAll('img[alt]'))
      .map(i => (i.getAttribute('alt') || '').trim())
      .filter(a => a.length >= 2 && a.length <= 40 && /[A-Za-zÀ-ÿ]{2}/.test(a));

    // Priorite au noeud feuille : c'est la lecture non ambigue. Le repli sur
    // innerText couvre les mises en page ou l'heure est collee a du texte.
    const loose = timeLeaf === null ? (card.innerText || '').match(RE_TIME_LOOSE) : null;
    const timeMatch = timeLeaf !== null ? timeLeaf : (loose ? loose[1] : null);

    let matchId = null;
    for (const a of idAttrs) {
      const v = card.getAttribute(a);
      if (v && /\d/.test(v)) { matchId = v; break; }
    }
    if (!matchId) {
      const link = card.querySelector('a[href]');
      if (link) {
        const m = (link.getAttribute('href') || '').match(/(\d{3,})/);
        if (m) matchId = m[1];
      }
    }

    const inputs = Array.from(card.querySelectorAll('input')).filter(i => {
      const t = (i.getAttribute('type') || 'text').toLowerCase();
      return ['text', 'number', 'tel', ''].includes(t);
    });

    card.setAttribute('data-mpp-card', String(idx));
    out.push({
      idx,
      match_id: matchId,
      cotes: cotes.map(c => c.text),
      cote_orders: cotes.map(c => c.order),
      pcts: pcts.map(p => p.text),
      first_pct_order: firstPctOrder === Infinity ? null : firstPctOrder,
      names: names.map(n => n.text),
      img_alts: alts,
      singles: singles.map(s => s.text),
      time: timeMatch,
      date: dateLeaf,
      input_count: inputs.length,
      input_values: inputs.map(i => i.value || ''),
      text: (card.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300),
    });
    idx++;
  }
  return out;
}
"""

# Recherche des champs de connexion, sans selecteur non plus : un champ mot de
# passe est identifiable par son type, et l'identifiant est le champ saisissable
# qui le precede immediatement dans le formulaire.
LOGIN_SCAN_JS = r"""
() => {
  const pw = document.querySelector('input[type="password"]');
  if (!pw) return { found: false };
  pw.setAttribute('data-mpp-pass', '1');

  const form = pw.closest('form') || document.body;
  const candidates = Array.from(form.querySelectorAll('input')).filter(i => {
    const t = (i.getAttribute('type') || 'text').toLowerCase();
    return ['text', 'email', 'tel', ''].includes(t);
  });
  const before = candidates.filter(i => i.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING);
  const user = (before.length ? before[before.length - 1] : candidates[0]) || null;
  if (user) user.setAttribute('data-mpp-user', '1');

  let submit = form.querySelector('button[type="submit"], input[type="submit"]');
  if (!submit) {
    submit = Array.from(form.querySelectorAll('button, a[role="button"]')).find(b =>
      /connexion|connecter|login|sign in|valider|entrer/i.test(b.innerText || b.value || ''));
  }
  if (submit) submit.setAttribute('data-mpp-submit', '1');

  return { found: true, has_user: !!user, has_submit: !!submit };
}
"""

CONSENT_JS = r"""
() => {
  const RE = /^(tout accepter|accepter( & fermer| et fermer)?|j'accepte|accepter tout|accept all|accept|ok, j'ai compris|continuer sans accepter|tout refuser)$/i;
  const btns = Array.from(document.querySelectorAll('button, a[role="button"], [class*="consent"] button, [id*="consent"] button'));
  const hit = btns.find(b => RE.test(((b.innerText || '').replace(/\s+/g, ' ').trim())));
  if (!hit) return false;
  hit.click();
  return true;
}
"""

# Bouton de validation d'un pronostic, cherche d'abord dans la carte puis dans
# la page (certains sites ont un bouton global "enregistrer mes pronos").
SUBMIT_SCAN_JS = r"""
(idx) => {
  const RE = /^(valider|enregistrer|envoyer|confirmer|je valide|valider mon prono|valider mes pronos)/i;
  const card = document.querySelector(`[data-mpp-card="${idx}"]`);
  const scopes = card ? [card, document.body] : [document.body];
  for (const scope of scopes) {
    const hit = Array.from(scope.querySelectorAll('button, input[type="submit"], a[role="button"]'))
      .find(b => RE.test(((b.innerText || b.value || '').replace(/\s+/g, ' ').trim())));
    if (hit) {
      hit.setAttribute('data-mpp-validate', String(idx));
      return scope === card ? 'card' : 'page';
    }
  }
  return null;
}
"""

_PCT_RE = re.compile(r"^(\d{1,3})\s*%$")


class DetectionError(ValueError):
    """La page ne ressemble pas a ce qu'on attend. Jamais silencieux."""


@dataclass
class RawCard:
    """Sortie brute du scan, avant validation metier."""

    idx: int
    match_id: str | None
    cotes: tuple[float, float, float]
    crowd_pct: tuple[float, float, float]
    home: str
    away: str
    time: str
    date: str | None
    input_count: int
    input_values: list[str]
    text: str


def _k_of(window: list[float]) -> float | None:
    try:
        return 1.0 / sum(1.0 / v for v in window)
    except ZeroDivisionError:
        return None


def _pick_cote_triplet(
    cotes: list[str],
    orders: list[int],
    first_pct_order: int | None,
    reference_k: float | None = None,
) -> tuple[float, float, float]:
    """Choisit les 3 cotes quand la carte en expose plus de trois.

    Cas reel : un classement affiche "36" a cote d'une equipe est indiscernable
    d'une cote par sa seule forme.

    Le depart se fait sur K = 1/somme(1/cote). Ce n'est pas un critere
    arbitraire : K varie tres peu d'un match a l'autre (31.7 a 34.1 sur
    l'echantillon de reference), alors qu'un triplet contenant un intrus donne
    un K completement different. On compare donc chaque fenetre de trois
    valeurs consecutives au K MEDIAN des cartes non ambigues de la meme page -
    une reference mesuree sur le site lui-meme, pas une constante codee en dur.
    L'ordre dans le DOM ne sert que de departage.
    """
    from .odds import K_MAX, K_MIN

    values = [float(c) for c in cotes]
    if len(values) == 3:
        return (values[0], values[1], values[2])
    if len(values) < 3:
        raise DetectionError(f"seulement {len(values)} cotes candidates")

    scored = []
    for i in range(len(values) - 2):
        window = values[i : i + 3]
        k = _k_of(window)
        if k is None or not (K_MIN <= k <= K_MAX):
            continue
        deviation = abs(k - reference_k) if reference_k else 0.0
        distance = (
            abs(orders[i + 2] - first_pct_order) if first_pct_order is not None else i
        )
        scored.append((deviation, distance, window))

    if not scored:
        raise DetectionError(
            f"{len(values)} cotes candidates, aucune fenetre de 3 plausible : {values}"
        )
    if reference_k is None and len(scored) > 1:
        raise DetectionError(
            f"{len(values)} cotes candidates et aucune carte de reference sur la "
            f"page pour departager : {values}"
        )
    scored.sort(key=lambda w: (w[0], w[1]))
    return tuple(scored[0][2])  # type: ignore[return-value]


def _pick_names(names: list[str], img_alts: list[str]) -> tuple[str, str]:
    """Deux equipes : les alt d'images d'abord, sinon les libelles textuels."""
    for source in (img_alts, names):
        cleaned = [n.strip() for n in source if 2 <= len(n.strip()) <= 40]
        # Deduplication en preservant l'ordre : certains sites repetent le nom
        # dans l'alt du logo et sous le logo.
        seen: list[str] = []
        for n in cleaned:
            if n.lower() not in {s.lower() for s in seen}:
                seen.append(n)
        if len(seen) >= 2:
            return seen[0], seen[-1]
    raise DetectionError(f"noms d'equipes introuvables (texte={names}, alt={img_alts})")


def parse_raw_cards(payload: list[dict[str, Any]]) -> tuple[list[RawCard], list[str]]:
    """Valide la sortie JS. Renvoie (cartes exploitables, erreurs par carte)."""
    if not payload:
        raise DetectionError(
            "aucune carte detectee : la page ne contient aucun bloc combinant "
            "3 cotes, 3 pourcentages et une heure"
        )

    # Passe 1 : K median des cartes non ambigues, qui sert de reference pour
    # departager les cartes ou plus de trois nombres ressemblent a des cotes.
    reference_ks = []
    for item in payload:
        if len(item.get("cotes", [])) == 3:
            k = _k_of([float(c) for c in item["cotes"]])
            if k is not None:
                reference_ks.append(k)
    reference_k = statistics.median(reference_ks) if reference_ks else None

    cards: list[RawCard] = []
    errors: list[str] = []

    for item in payload:
        label = item.get("text", "")[:60]
        try:
            pcts = []
            for p in item["pcts"]:
                m = _PCT_RE.match(p)
                if not m:
                    raise DetectionError(f"pourcentage illisible : {p!r}")
                pcts.append(float(m.group(1)))
            if len(pcts) != 3:
                raise DetectionError(f"{len(pcts)} pourcentages au lieu de 3")

            cotes = _pick_cote_triplet(
                item["cotes"],
                item["cote_orders"],
                item.get("first_pct_order"),
                reference_k,
            )
            if not item.get("time"):
                raise DetectionError("heure de coup d'envoi absente")

            home, away = _pick_names(item["names"], item["img_alts"])
            cards.append(
                RawCard(
                    idx=item["idx"],
                    match_id=item.get("match_id"),
                    cotes=cotes,
                    crowd_pct=(pcts[0], pcts[1], pcts[2]),
                    home=home,
                    away=away,
                    time=item["time"],
                    date=item.get("date"),
                    input_count=item.get("input_count", 0),
                    input_values=item.get("input_values", []),
                    text=item.get("text", ""),
                )
            )
        except (DetectionError, KeyError, ValueError) as exc:
            errors.append(f"carte #{item.get('idx', '?')} ({label}...) : {exc}")

    if not cards:
        raise DetectionError(
            "aucune carte exploitable sur "
            f"{len(payload)} candidates. Details : " + " | ".join(errors[:5])
        )
    return cards, errors
