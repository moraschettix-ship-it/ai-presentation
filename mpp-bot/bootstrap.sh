#!/usr/bin/env bash
# Installe le bot dans un depot vide, sur la branche par defaut.
#
#   ./bootstrap.sh git@github.com:VOTRE-COMPTE/mpp-bot.git
#   ./bootstrap.sh https://github.com/VOTRE-COMPTE/mpp-bot.git
#
# A lancer depuis ce dossier. Le depot cible doit exister et etre VIDE (aucun
# README, aucun .gitignore a la creation) : le script pousse un premier commit
# complet, il ne fusionne rien.
#
# Rappel : le depot doit etre PRIVE. Sur un depot public, les logs et artefacts
# GitHub Actions sont lisibles par tous, et le journal du bot contient vos
# pronostics.

set -euo pipefail

REMOTE="${1:-}"
if [ -z "$REMOTE" ]; then
  echo "usage: $0 <url-du-depot-git>" >&2
  exit 2
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "Preparation dans $STAGE"
mkdir -p "$STAGE/.github/workflows" "$STAGE/mpp-bot"

# Les workflows vivent a la racine du depot, le code dans mpp-bot/ (les
# workflows declarent working-directory: mpp-bot).
cp "$ROOT"/.github/workflows/mpp-*.yml "$STAGE/.github/workflows/"

# Tout sauf les artefacts locaux : etat, captures, caches Python.
tar -C "$HERE" \
    --exclude='state/captures' \
    --exclude='state/journal.jsonl' \
    --exclude='state/last_run.json' \
    --exclude='state/fixtures.json' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='run.log' \
    -cf - . | tar -C "$STAGE/mpp-bot" -xf -

cat > "$STAGE/README.md" <<'EOF'
# mpp-bot

Remplissage automatique des pronostics Mon Petit Prono.

Documentation, mise en route et explication du décodage des cotes :
[`mpp-bot/README.md`](mpp-bot/README.md).

Deux choses à faire pour démarrer : ajouter les secrets `MPP_EMAIL` et
`MPP_PASSWORD` dans `Settings → Secrets and variables → Actions`, et vérifier
que ce dépôt est **privé**.
EOF

cd "$STAGE"
git init -q -b main
git add -A
git -c user.name="mpp-bot" -c user.email="mpp-bot@users.noreply.github.com" \
    commit -q -m "Bot de pronostics Mon Petit Prono"
git remote add origin "$REMOTE"

echo "Envoi vers $REMOTE"
for attempt in 1 2 3 4; do
  if git push -u origin main; then
    echo
    echo "Termine."
    echo "Reste a faire :"
    echo "  1. verifier que le depot est prive"
    echo "  2. ajouter les secrets MPP_EMAIL et MPP_PASSWORD"
    echo "  3. Actions -> 'MPP - diagnostic' -> Run workflow, pour verifier"
    exit 0
  fi
  echo "echec de l'envoi, nouvelle tentative dans $((2 ** attempt))s" >&2
  sleep $((2 ** attempt))
done

echo "envoi impossible apres 4 tentatives" >&2
exit 1
