# Bot de pronostics Mon Petit Prono

Remplit automatiquement les pronostics MPP : lecture des cartes de match,
décodage des cotes, choix d'un score, saisie et vérification.

Deux passes par match : une la veille (filet de sécurité) et une juste avant
le coup d'envoi (cotes fraîches).

---

## 1. Le décodage des cotes

C'est le point de départ, et il change tout le reste.

Sur une carte MPP, trois nombres sont affichés sous les équipes (65 / 124 / 129
pour AEK - LASK) avec, en dessous, trois pourcentages (70 % / 21 % / 8 %). Ce
ne sont pas la même chose :

| Affichage | Ce que c'est |
|---|---|
| Les trois nombres | Les **points** de chaque issue, dérivés du marché |
| Les trois pourcentages | La **répartition des autres joueurs** |

Vérification sur six cartes réelles : la relation est

```
cote_i = K / p_i          (K constant par match, observé entre 31.7 et 34.1)
```

d'où, en normalisant, une formule invariante d'échelle qui n'a jamais besoin
de connaître K :

```
p_i = (1 / cote_i) / Σ_j (1 / cote_j)
```

Les probabilités obtenues sont directement plausibles et **déjà sans marge
bookmaker** (elles somment à 1 par construction) :

| Match | Cotes MPP | Probabilités décodées 1 / N / 2 |
|---|---|---|
| Real Madrid - Inter | 62 / 124 / 136 | 51 % / 26 % / 23 % |
| Bruges - Aston Villa | 100 / 117 / 93 | 34 % / 29 % / 37 % |
| Porto - Man City | 127 / 124 / 67 | 26 % / 26 % / 48 % |
| Dortmund - Villarreal | 73 / 119 / 122 | 45 % / 28 % / 27 % |
| LOSC - Betis | 88 / 121 / 103 | 39 % / 28 % / 33 % |
| AEK Athens - LASK | 65 / 124 / 129 | 49 % / 26 % / 25 % |

Un détail confirme la lecture : la cote du nul reste collée à 117-124 sur les
six cartes alors que le pourcentage de joueurs qui jouent le nul varie de 11 %
à 25 %. Les deux séries sont indépendantes — les cotes ne suivent pas la foule.

## 2. La conséquence : suivre la cote ne rapporte rien

Si les points gagnés valent la cote, alors l'espérance de points d'une issue
vaut

```
E = p_i × cote_i = p_i × (K / p_i) = K
```

**identique pour les trois issues.** Vérifié numériquement sur les six cartes :

| Match | E(jouer 1) | E(jouer N) | E(jouer 2) |
|---|---|---|---|
| AEK - LASK | 32.05 | 32.05 | 32.05 |
| Bruges - Villa | 34.13 | 34.13 | 34.13 |
| Real - Inter | 31.70 | 31.70 | 31.70 |
| LOSC - Betis | 34.09 | 34.09 | 34.09 |
| Dortmund - Villarreal | 33.00 | 33.00 | 33.00 |
| Porto - City | 32.40 | 32.40 | 32.40 |

Le barème ne laisse aucun arbitrage. Aucun modèle de football, aussi bon
soit-il, ne peut battre ce système en espérance de points sur l'issue 1/N/2.

Ce qui a deux implications directes :

1. **Un bot qui « suit la cote » finit médian**, par construction. Il joue le
   favori, comme tout le monde, pour une espérance strictement identique à
   n'importe quel autre choix.
2. **Le seul levier pour finir premier est l'écart à la foule.** Et comme
   l'espérance est plate, cet écart est *gratuit en points* : il ne coûte que
   de la variance.

C'est exactement ce que fait le bot. `tests/test_strategy.py` verrouille cette
propriété : l'espérance de points est identique que le bot joue 0 ou 6
déviations.

## 3. La stratégie : un seul paramètre

Pour chaque match, avec `p` = probabilité marché et `foule` = part des joueurs :

```
U_i = p_i + kappa × (p_i − foule_i)
```

`kappa = 0` donne le favori du marché. `kappa` grand donne l'écart maximal à la
foule. Entre les deux, la propriété utile : **quand kappa monte depuis 0, les
déviations apparaissent dans l'ordre de leur efficacité** — d'abord celles qui
coûtent peu de probabilité et rapportent beaucoup de différenciation.

Sur la journée de référence, les deux premières déviations sont Bruges et LOSC :

| Match | Proba sacrifiée | Différenciation gagnée |
|---|---|---|
| Bruges - Villa | 2.6 pts | 47 pts |
| LOSC - Betis | 5.6 pts | 50 pts |
| Dortmund - Villarreal | 18.1 pts | 77 pts |
| Real - Inter | 27.8 pts | 81 pts |

Bruges - Villa est le cas d'école : le marché dit 34/29/37 (quasi pile ou face)
et la foule dit 15/23/62. Jouer Bruges coûte 2.6 points de probabilité et vous
sépare de 47 % du terrain. C'est la déviation qu'un joueur affûté ferait à la
main.

En pratique on ne règle pas `kappa` mais **le nombre de déviations par
journée** (`target_deviations`), et `kappa` est recalibré à chaque journée pour
l'atteindre. Sur 18 matchs :

- `0` → suivre le marché, finir médian
- `2-3` → prudent, on ne dévie que sur les cas quasi gratuits
- `5-6` → agressif mais tenable *(défaut)*
- `>8` → dernier la plupart des semaines, premier de temps en temps

`catchup_kappa` s'ajoute à `kappa` pour monter la variance quand on court après
le leader en fin de saison.

### Ce que ça donne sur la journée de référence

```
target = 0  (kappa 0.000)     6 fois le favori du marché — le bot médian
target = 2  (kappa 0.129)     dévie sur Bruges et LOSC uniquement
target = 5  (kappa 0.523)     dévie partout sauf AEK - LASK
```

Sortie complète : `python -m mpp plan fixtures/screenshot-j1.json --target-deviations 2`

## 4. Le score exact

Une fois l'issue choisie, il faut un score. Les cotes donnent trois nombres,
pas une distribution : on ajuste un **Poisson double** dont les probabilités
d'issue reproduisent *exactement* le triplet (deux paramètres, deux contraintes
indépendantes — l'ajustement est exact, pas approché), puis on prend le score
le plus probable compatible avec l'issue retenue.

Un piège, corrigé : un triplet 1/N/2 ne détermine le total de buts que via la
probabilité de nul, ce qui donne des totaux ajustés autour de **2.2 - 2.5**,
alors qu'un match de C1 tourne vers **3.0 - 3.2**. Sans correction, tous les
pronostics sortent 1-0 / 1-1 / 0-1 et les bonus de score exact sont perdus.
`total_shrink` tire le total vers `total_prior`, en re-résolvant la supériorité
pour préserver exactement P1 − P2 : **le choix 1/N/2 n'est jamais modifié**,
seul le score l'est.

```
shrink = 0.00   total 2.50   scores 1-0 / 1-1 / 0-1
shrink = 0.50   total 2.78   scores 1-0 / 1-1 / 0-1
shrink = 0.75   total 2.91   scores 2-1 / 1-1 / 1-2   (défaut)
shrink = 1.00   total 3.05   scores 2-1 / 1-1 / 1-2
```

Le seuil de bascule 1-0 → 2-1 se situe entre 0.7 et 0.8 : **en dessous de 0.7
la correction ne change rien**, c'est du réglage pour rien. Le défaut est donc
0.75, juste au-dessus du seuil, ce qui corrige l'essentiel du biais tout en
laissant 25 % de variation propre au match. Justification du prior : la phase
de ligue de C1 tourne autour de 3.1 buts par match, alors que l'ajustement issu
des seules cotes donne 2.5. L'écart n'est pas du bruit, c'est un biais
systématique du triplet 1/N/2, et le journal permettra de le recalibrer sur
données réelles après quelques journées.

`rho` (correction Dixon-Coles sur les scores à 0 et 1 but) est à 0 par défaut :
aucune hypothèse non justifiée tant qu'on n'a pas de données pour la calibrer.

## 5. Architecture

Python + cron GitHub Actions, **pas n8n**. Raison : une seule source, une seule
destination, mais de la logique métier non triviale (ajustement Poisson,
calibration de kappa, fenêtres temporelles). Sur n8n ça devient une dizaine de
nœuds Function contenant le même code, sans tests, sans diff lisible, sans
exécution locale. Ici : 39 tests unitaires, `git diff`, et `python -m mpp plan`
qui tourne hors ligne en une seconde.

Le cœur mathématique n'a **aucune dépendance** (pas de numpy, pas de scipy) :
Python pur, résolution par bissection imbriquée. Le job de test démarre en
quelques secondes.

```
mpp/odds.py       cotes -> probabilités, et validation stricte des entrées
mpp/poisson.py    probabilités d'issue -> distribution de scores
mpp/strategy.py   choix de l'issue et du score, calibration de kappa
mpp/schedule.py   fuseaux, fenêtres de tir, absorption des retards de cron
mpp/site.py       Playwright : connexion, lecture des cartes, saisie
mpp/state.py      journal append-only, cache calendrier, écritures atomiques
mpp/cli.py        run / gate / plan / discover / report
```

### Fiabilité

**Le cron GitHub Actions n'est pas ponctuel.** Les jobs planifiés sont
régulièrement retardés de 5 à 20 minutes aux heures chargées, et peuvent être
purement sautés. Un déclenchement « à T-30 pile » est donc impossible à
garantir. La parade est structurelle, pas optimiste :

- la passe **J-1** garantit qu'un pronostic existe toujours, même si toutes les
  passes tardives échouent ;
- la passe tardive accepte une fenêtre **T-90 → T-15** et tourne toutes les
  10 minutes dedans : un retard de cron reste rattrapé ;
- la soumission est idempotente et écrase, donc plusieurs passages sont sans
  conséquence ;
- **marge absolue** : jamais de soumission à moins de 6 minutes du coup
  d'envoi, pour ne pas courir après la fermeture de la saisie.

Les autres garde-fous :

| Risque | Traitement |
|---|---|
| Heure d'été / heure d'hiver | `zoneinfo` sur `Europe/Paris`, tout converti en UTC dès la lecture. Testé sur les deux dimanches de bascule. |
| Une carte illisible | Collectée comme erreur, les 17 autres sont traitées. Un pronostic manquant vaut mieux que zéro. |
| Cotes aberrantes | Bornes de plausibilité sur les cotes, sur K et sur la somme des pourcentages. En cas d'échec : aucune soumission, alerte. |
| Saisie qui échoue en silence | Relecture systématique de la carte après envoi. Un HTTP 200 ne prouve rien. |
| Deux exécutions simultanées | `concurrency: mpp-bot` côté workflow. |
| Journal corrompu (job tué) | Lecture tolérante ligne à ligne, écritures JSON atomiques. |
| Panne silencieuse | Alerte Telegram / webhook **et** job rouge. Le journal est commité même en échec. |
| Coût CI | Étape `gate` sans navigateur : les ~36 exécutions quotidiennes s'arrêtent en quelques secondes s'il n'y a aucun match dans la fenêtre. |
| Cron désactivé après 60 j | Les commits du journal maintiennent le dépôt actif. |

Codes de sortie : `0` tout va bien, `1` échec partiel, `2` rien n'a pu être
fait.

## 6. Mise en route

Trois étapes. La première est bloquante : **les sélecteurs CSS sont vides**,
parce qu'ils ne s'inventent pas.

### a. Capturer le site

```bash
cd mpp-bot
pip install -r requirements.txt
python -m playwright install chromium
export MPP_EMAIL=...
export MPP_PASSWORD=...
python -m mpp discover --headed
```

Écrit dans `state/discovery/` le HTML des pages, une capture d'écran, et
surtout `*.network.json` : **toutes les réponses JSON de la page**. Si le
calendrier et les cotes y figurent, une API interne existe — auquel cas on
bascule dessus et Chromium disparaît du pipeline.

*(`discover` a besoin d'au moins `login_url` et `matches_url` dans
`config.yaml` pour savoir où aller.)*

### b. Remplir `config.yaml`

Section `site.selectors`. Deux contraintes fortes : `cote_cells` et
`crowd_cells` doivent renvoyer **exactement 3 éléments par carte**, sinon la
carte est rejetée plutôt que mal lue.

Puis vérifier hors ligne :

```bash
python -m unittest discover -s tests -t .
python -m mpp plan fixtures/screenshot-j1.json --target-deviations 5
```

### c. Brancher

Secrets GitHub (`Settings → Secrets and variables → Actions`) :

| Secret | Nécessaire | Rôle |
|---|---|---|
| `MPP_EMAIL` | oui | Identifiant MPP |
| `MPP_PASSWORD` | oui | Mot de passe MPP |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | non | Alertes |
| `ALERT_WEBHOOK_URL` | non | Alertes (autre canal) |

Puis **`workflow_dispatch` avec `dry_run: true`** pendant au moins une journée
complète avant de laisser écrire. La commande affiche exactement ce qu'elle
aurait envoyé.

### Au quotidien

```bash
python -m mpp run --phase any --dry-run   # ce qu'il ferait maintenant
python -m mpp report                      # ce qu'il a fait
python -m mpp gate --phase late           # y a-t-il un match dans la fenêtre ?
```

## 7. Ce qui n'est pas encore établi

Par ordre d'impact :

1. **Le barème exact de la ligue.** On sait que jouer la bonne issue rapporte
   la cote. On ne sait pas ce que vaut un score exact. `exact_bonus` est à 0 :
   ça n'affecte pas le choix de l'issue (l'espérance est plate de toute façon)
   mais ça fausse l'espérance affichée, et si le bonus est gros il faudrait le
   prendre en compte pour choisir le score.
2. **Les sélecteurs, et l'existence d'une API.** Bloquant. `discover` tranche.
3. **La compétition.** La capture de référence mélange des clubs de C1 (Real,
   Inter, City, Dortmund, Villarreal, Bruges, Aston Villa) et de C3 (AEK, LASK,
   LOSC, Betis, Porto). Si les deux compétitions coexistent dans la même page,
   `competition_filter` n'est pas cosmétique — sans lui, le bot pronostique
   l'Europa League en croyant faire la C1.
4. **La signification des rangs affichés** (`1e`, `36e` à côté des équipes).
   Non utilisés aujourd'hui.
5. **La stabilité de K** (31.7 - 34.1 selon les cartes). La normalisation le
   neutralise entièrement, mais si MPP compressait légèrement l'échelle, les
   probabilités seraient tirées vers les extrêmes. Le journal permet de le
   vérifier empiriquement après quelques journées : comparer la fréquence
   réelle des issues aux probabilités décodées.

## 8. Risques assumés

L'accès automatisé n'est vraisemblablement pas prévu par les CGU de MPP. Le
risque réel est la perte du compte, et il est **accepté explicitement**. D'où
une hygiène délibérée, qui n'est pas de la dissimulation mais de la simple
politesse technique : un seul navigateur, temporisation aléatoire entre chaque
action, aucune requête en parallèle, User-Agent stable, et deux passes par
match au lieu d'un polling permanent.

Les captures de `discover` contiennent le DOM complet et les réponses JSON,
donc potentiellement des jetons de session : `state/discovery/` est dans le
`.gitignore`, et doit y rester.
