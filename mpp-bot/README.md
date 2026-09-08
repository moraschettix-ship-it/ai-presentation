# Bot de pronostics Mon Petit Prono

Remplit tout seul les pronostics MPP, tous les jours : connexion, lecture des
cartes, décodage des cotes, choix d'un score, saisie, vérification.

Deux passes par match : la veille (filet de sécurité) et juste avant le coup
d'envoi (cotes fraîches).

---

## Mise en route

### Le dépôt doit être privé

Ce n'est pas une précaution de principe. Sur un dépôt **public** :

- le journal du bot est commité en clair, avec vos pronostics ;
- les **logs** de GitHub Actions sont lisibles par n'importe qui, et le résumé
  de job affiche la table complète des pronostics ;
- les **artefacts** (captures de diagnostic) sont téléchargeables par tous.

Dans une ligue entre amis, cela revient à publier vos choix avant le coup
d'envoi. Utilisez un dépôt privé.

### 1. Où mettre vos identifiants MPP

Dans les **secrets GitHub Actions**. Nulle part ailleurs.

`https://github.com/VOTRE-COMPTE/VOTRE-DEPOT/settings/secrets/actions`
→ bouton **New repository secret**, deux fois :

| Name | Secret |
|---|---|
| `MPP_EMAIL` | votre identifiant MPP (email ou pseudo) |
| `MPP_PASSWORD` | votre mot de passe MPP |

Le nom doit être écrit exactement comme ci-dessus, en majuscules. Une fois
enregistré, GitHub ne vous les réaffichera plus — c'est normal, ils sont
chiffrés au repos et injectés uniquement dans le job au moment de l'exécution.
Ils sont masqués automatiquement dans les logs (`***`).

Trois règles, dans l'ordre d'importance :

1. **Jamais dans un fichier du dépôt.** Ni dans `config.yaml`, ni dans un
   `.env`, ni dans un commentaire. Un secret commité reste dans l'historique
   git même après suppression.
2. **Jamais dans une conversation**, y compris avec moi. Je n'en ai pas besoin :
   le bot les lit depuis l'environnement du job.
3. **Un mot de passe dédié si possible.** Si vous réutilisez ailleurs le mot de
   passe de votre compte MPP, changez-en un des deux avant de continuer.

Secrets optionnels, pour être prévenu quand ça casse :

| Name | À quoi ça sert |
|---|---|
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | alerte Telegram |
| `ALERT_WEBHOOK_URL` | alerte vers n'importe quel webhook JSON |

Sans eux, un échec fait quand même rougir le job et GitHub vous envoie un mail.

### 2. Mettre ce code sur la branche par défaut

**Un workflow planifié ne s'exécute que depuis la branche par défaut du dépôt**
(`main`). Tant que ces fichiers sont ailleurs, rien ne se déclenche. C'est la
seule subtilité GitHub qui compte ici.

C'est tout. Le bot tourne ensuite tous les jours à 18h UTC (passe J-1) et toutes
les 15 minutes les mardis, mercredis et jeudis entre 15h et 21h UTC (passe juste
avant les coups d'envoi).

### Recommandé une fois : vérifier

`Actions` → **MPP - diagnostic** → `Run workflow`. Ça se connecte, liste tout ce
que le bot voit, **n'écrit rien** sur MPP, et joint une capture. Une minute pour
être sûr que la connexion passe.

> **Honnêteté sur ce point.** Ce code n'a jamais pu être exécuté contre le vrai
> monpetitprono.com : l'environnement où il a été écrit bloque ce domaine. La
> détection est validée en bout-en-bout dans un vrai navigateur sur deux faux
> sites MPP de structures totalement différentes (voir plus bas), mais la
> première exécution réelle reste la première. Si elle échoue, le diagnostic
> joint dit exactement ce qui a été vu, et l'ajustement est une ligne de
> `config.yaml`.

### Coût

Sur un dépôt privé, GitHub offre 2000 minutes d'Actions par mois. Ce bot en
consomme environ 460 : ~340 pour l'étape `gate` (qui s'arrête en quelques
secondes quand il n'y a aucun match dans la fenêtre, sans installer de
navigateur) et ~120 pour les exécutions réelles les jours de match.

---

## Le décodage des cotes

C'est le point de départ, et il change tout le reste.

Sur une carte MPP, trois nombres sont affichés sous les équipes (65 / 124 / 129
pour AEK - LASK) avec, en dessous, trois pourcentages (70 % / 21 % / 8 %). Ce ne
sont pas la même chose : les nombres sont les **points** de chaque issue, les
pourcentages la **répartition des autres joueurs**.

Vérification sur six cartes réelles : la relation est

```
cote_i = K / p_i          (K constant par match, observé entre 31.7 et 34.1)
```

d'où, en normalisant, une formule invariante d'échelle qui n'a jamais besoin de
connaître K :

```
p_i = (1 / cote_i) / Σ_j (1 / cote_j)
```

Les probabilités obtenues sont directement plausibles et **déjà sans marge
bookmaker** :

| Match | Cotes MPP | Probabilités décodées 1 / N / 2 |
|---|---|---|
| Real Madrid - Inter | 62 / 124 / 136 | 51 % / 26 % / 23 % |
| Bruges - Aston Villa | 100 / 117 / 93 | 34 % / 29 % / 37 % |
| Porto - Man City | 127 / 124 / 67 | 26 % / 26 % / 48 % |
| Dortmund - Villarreal | 73 / 119 / 122 | 45 % / 28 % / 27 % |
| LOSC - Betis | 88 / 121 / 103 | 39 % / 28 % / 33 % |
| AEK Athens - LASK | 65 / 124 / 129 | 49 % / 26 % / 25 % |

Un détail confirme la lecture : la cote du nul reste collée à 117-124 sur les
six cartes alors que le pourcentage de joueurs qui jouent le nul varie de 11 % à
25 %. Les deux séries sont indépendantes — les cotes ne suivent pas la foule.

## La conséquence : suivre la cote ne rapporte rien

Si les points gagnés valent la cote, alors l'espérance de points d'une issue vaut

```
E = p_i × cote_i = p_i × (K / p_i) = K
```

**identique pour les trois issues.** Vérifié numériquement :

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

Deux implications directes :

1. **Un bot qui « suit la cote » finit médian**, par construction.
2. **Le seul levier pour finir premier est l'écart à la foule.** Et comme
   l'espérance est plate, cet écart est *gratuit en points* : il ne coûte que de
   la variance.

`tests/test_strategy.py` verrouille cette propriété : l'espérance de points est
identique que le bot joue 0 ou 6 déviations.

## La stratégie : un seul réglage

Pour chaque match, avec `p` = probabilité marché et `foule` = part des joueurs :

```
U_i = p_i + kappa × (p_i − foule_i)
```

`kappa = 0` donne le favori du marché. `kappa` grand donne l'écart maximal à la
foule. Entre les deux, la propriété utile : **quand kappa monte depuis 0, les
déviations apparaissent dans l'ordre de leur efficacité** — d'abord celles qui
coûtent peu de probabilité et rapportent beaucoup de différenciation.

| Match | Proba sacrifiée | Différenciation gagnée |
|---|---|---|
| Bruges - Villa | 2.6 pts | 47 pts |
| LOSC - Betis | 5.6 pts | 50 pts |
| Dortmund - Villarreal | 18.1 pts | 77 pts |
| Real - Inter | 27.8 pts | 81 pts |

Bruges - Villa est le cas d'école : le marché dit 34/29/37 (quasi pile ou face)
et la foule dit 15/23/62. Jouer Bruges coûte 2.6 points de probabilité et vous
sépare de 47 % du terrain.

En pratique on ne règle pas `kappa` mais **le nombre de déviations par journée**
(`target_deviations` dans `config.yaml`), et `kappa` est recalibré à chaque
journée pour l'atteindre. Sur 18 matchs :

- `0` → suivre le marché, finir médian
- `2-3` → prudent, uniquement les déviations quasi gratuites
- `5` → agressif mais tenable *(défaut)*
- `8+` → dernier la plupart des semaines, premier de temps en temps

`catchup_kappa` s'ajoute à `kappa` pour monter la variance quand on court après
le leader en fin de saison.

Pour voir l'effet sans rien toucher :

```bash
python -m mpp plan fixtures/screenshot-j1.json --target-deviations 5
```

## Le score exact

Une fois l'issue choisie, il faut un score. Les cotes donnent trois nombres, pas
une distribution : on ajuste un **Poisson double** dont les probabilités d'issue
reproduisent *exactement* le triplet (deux paramètres, deux contraintes
indépendantes — l'ajustement est exact, pas approché), puis on prend le score le
plus probable compatible avec l'issue retenue.

Un piège, corrigé : un triplet 1/N/2 ne détermine le total de buts que via la
probabilité de nul, ce qui donne des totaux autour de **2.5**, alors qu'un match
de C1 tourne vers **3.1**. Sans correction, tout sort en 1-0 / 1-1 / 0-1 et les
bonus de score exact sont perdus. `total_shrink` tire le total vers
`total_prior` en re-résolvant la supériorité pour préserver exactement P1 − P2 :
**le choix 1/N/2 n'est jamais modifié**, seul le score l'est.

```
shrink = 0.00   total 2.50   scores 1-0 / 1-1 / 0-1
shrink = 0.50   total 2.78   scores 1-0 / 1-1 / 0-1
shrink = 0.75   total 2.91   scores 2-1 / 1-1 / 1-2   (défaut)
shrink = 1.00   total 3.05   scores 2-1 / 1-1 / 1-2
```

Le seuil de bascule 1-0 → 2-1 se situe entre 0.7 et 0.8 : **en dessous de 0.7 la
correction ne change rien**. Le défaut est donc 0.75, juste au-dessus du seuil.

`rho` (correction Dixon-Coles) est à 0 : aucune hypothèse non justifiée tant
qu'on n'a pas de données pour la calibrer.

## Comment il trouve les infos sans aucun sélecteur

Un scraper classique code en dur `div.match-card`, `.odds-value`… Ces
sélecteurs cassent au premier redesign, et surtout ils ne peuvent pas être
écrits sans avoir le site sous les yeux.

Ici une carte est reconnue à sa **signature numérique** :

- trois nombres à 2-4 chiffres → les cotes ;
- trois pourcentages → la répartition des joueurs ;
- une heure `18h45` ou `18:45` ;
- deux libellés textuels → les équipes.

Aucune autre partie d'une page de pronostics ne reproduit ça par accident.
L'ancrage se fait sur les **pourcentages**, non ambigus car ils portent un `%` ;
la carte est ensuite le plus grand ancêtre qui en contient encore exactement
trois, ce qui fait entrer les noms d'équipes et l'heure et s'arrête juste avant
d'absorber la carte voisine.

Trois garde-fous rendent l'approche sûre plutôt que fragile :

1. **Vérification finale par K.** Le triplet retenu doit donner un
   `K = 1/Σ(1/cote)` entre 20 et 80. Un triplet mal capturé échoue ce test, donc
   une carte mal lue est **rejetée** au lieu d'être pronostiquée de travers.
2. **Départage par K médian.** Quand plus de trois nombres ressemblent à des
   cotes (un classement affiché « 36 » est indiscernable d'une cote), on compare
   chaque fenêtre de trois au K médian des cartes non ambiguës *de la même
   page*. Une référence mesurée sur le site, pas une constante inventée. Sans
   référence disponible, le bot refuse au lieu de deviner.
3. **Relecture après écriture.** Un HTTP 200 ne prouve rien. La page est
   rechargée et chaque score confirmé.

La connexion suit la même logique : le champ mot de passe est trouvé par son
`type`, l'identifiant est le champ saisissable qui le précède, et le succès est
constaté par la **disparition** du champ mot de passe — pas par un libellé, pas
par une URL. Si aucun formulaire n'est sur la page d'accueil, le bot suit un
lien de connexion ; une fois connecté, il cherche la page de pronostics en
suivant les liens jusqu'à en trouver une qui contient des cartes.

### Ce qui le prouve

`tests/test_e2e_browser.py` et `tests/test_cli_end_to_end.py` lancent un vrai
Chromium sur deux faux sites MPP contenant **les mêmes données dans des DOM sans
rapport** :

| | `pronos.html` | `grille.html` |
|---|---|---|
| Structure | `<article>` imbriquées | un `<table>` |
| Classes | obfusquées (`x7f2a-*`) | sémantiques |
| Noms d'équipes | texte **et** attribut `alt` | texte seul |
| Identifiant de match | attribut `data-match-id` | aucun |
| Rangs | suffixés (`27e`) | **nus** (`27`), donc confondables avec une cote |
| Validation | un bouton par carte | un bouton global |

Aucun sélecteur n'est fourni. Le bot lit les six matchs correctement sur les
deux, trouve la page de pronostics tout seul depuis l'accueil, écrit, relit,
confirme, et échoue bruyamment sur une page sans cartes.

## Fiabilité

**Le cron GitHub Actions n'est pas ponctuel.** Les jobs planifiés sont
régulièrement retardés de 5 à 20 minutes aux heures chargées, et peuvent être
sautés. Un déclenchement « à T-30 pile » est donc impossible à garantir. La
parade est structurelle, pas optimiste :

- la passe **J-1** garantit qu'un pronostic existe toujours, même si toutes les
  passes tardives échouent ;
- la passe tardive accepte une fenêtre **T-90 → T-15** et tourne toutes les
  10 minutes dedans : un retard de cron reste rattrapé ;
- la soumission est idempotente et écrase, donc plusieurs passages sont sans
  conséquence ;
- **marge absolue** : jamais de soumission à moins de 6 minutes du coup d'envoi.

| Risque | Traitement |
|---|---|
| Redesign du site | Aucun sélecteur CSS. Détection par signature numérique. |
| Heure d'été / hiver | `zoneinfo` sur `Europe/Paris`, tout en UTC en interne. Testé sur les deux dimanches de bascule. |
| Page affichant J et J+1 | Date lue sur la carte si présente ; sinon, une heure passée depuis plus de 6 h est datée du lendemain. Le 31/12 lu le 1er janvier est correctement daté de l'année écoulée. |
| Une carte illisible | Collectée comme erreur, les 17 autres sont traitées. |
| Cotes aberrantes | Bornes sur les cotes, sur K et sur la somme des pourcentages. En cas d'échec : aucune soumission, alerte. |
| Saisie qui échoue en silence | Relecture systématique après envoi. |
| Deux exécutions simultanées | `concurrency: mpp-bot`. |
| Journal corrompu (job tué) | Lecture tolérante ligne à ligne, écritures JSON atomiques. |
| Panne silencieuse | Alerte Telegram / webhook, job rouge, capture jointe en artefact. |
| Fuite de jeton dans une capture | Masquage structurel des clés sensibles avant écriture (`redact`), testé. |
| Coût CI | Étape `gate` sans navigateur : les ~36 exécutions quotidiennes s'arrêtent en quelques secondes s'il n'y a rien à faire. |
| Cron désactivé après 60 j | Les commits du journal maintiennent le dépôt actif. |

Codes de sortie : `0` tout va bien, `1` échec partiel, `2` rien n'a pu être fait.

## Si ça casse

`Actions` → **MPP - diagnostic** → `Run workflow`. Le résumé du job liste tout
ce que le bot a vu ; l'artefact contient le HTML, une capture d'écran, les
réponses réseau (valeurs sensibles masquées) et le détail de ce qui a bloqué.

En local :

```bash
pip install -r requirements.txt && python -m playwright install chromium
export MPP_EMAIL=... MPP_PASSWORD=...
python -m mpp doctor --headed     # voir le navigateur travailler
python -m mpp run --dry-run       # ce qu'il enverrait, sans rien envoyer
python -m mpp report              # ce qu'il a fait
```

Deux réglages de secours dans `config.yaml`, à ne toucher que si le diagnostic
le demande : `site.login_url` et `site.matches_url`.

## Architecture

Python + cron GitHub Actions, **pas n8n**. Une seule source, une seule
destination, mais de la logique non triviale (ajustement Poisson, calibration de
kappa, détection de structure, fenêtres temporelles). Sur n8n ça devient une
dizaine de nœuds Function contenant le même code, sans tests, sans diff lisible,
sans exécution locale.

Le cœur mathématique n'a **aucune dépendance** (ni numpy ni scipy) : Python pur,
résolution par bissection imbriquée.

```
mpp/odds.py       cotes -> probabilités, validation stricte
mpp/poisson.py    probabilités d'issue -> distribution de scores
mpp/strategy.py   choix de l'issue et du score, calibration de kappa
mpp/detect.py     reconnaissance des cartes sans sélecteur CSS
mpp/site.py       Playwright : connexion, navigation, saisie, diagnostic
mpp/schedule.py   fuseaux, datation des cartes, fenêtres de tir
mpp/state.py      journal append-only, cache calendrier, écritures atomiques
mpp/cli.py        run / gate / doctor / plan / report
```

62 tests, dont 15 dans un vrai navigateur.

## Ce qui n'est pas encore établi

1. **Le barème exact de la ligue.** On sait que la bonne issue rapporte la cote.
   On ne sait pas ce que vaut un score exact. `exact_bonus` est à 0 : ça
   n'affecte pas le choix de l'issue (l'espérance est plate de toute façon) mais
   si le bonus est gros, il faudrait le prendre en compte pour choisir le score.
2. **L'existence d'une API interne.** `doctor` liste les réponses JSON de la
   page. Si le calendrier et les cotes y figurent, y basculer supprimerait
   Chromium du pipeline et diviserait le temps d'exécution par dix.
3. **La compétition.** La capture de référence mélangeait des clubs de C1 (Real,
   Inter, City, Dortmund, Villarreal, Bruges, Aston Villa) et de C3 (AEK, LASK,
   LOSC, Betis, Porto). Si les deux coexistent sur la même page,
   `competition_filter` n'est pas cosmétique.
4. **La stabilité de K** (31.7 - 34.1). La normalisation le neutralise, mais si
   MPP compressait légèrement l'échelle, les probabilités seraient tirées vers
   les extrêmes. Le journal permet de le vérifier après quelques journées.

## Risques assumés

L'accès automatisé n'est vraisemblablement pas prévu par les CGU de MPP. Le
risque réel est la perte du compte, et il est accepté explicitement. D'où une
hygiène délibérée : un seul navigateur, temporisation aléatoire entre chaque
action, aucune requête en parallèle, User-Agent stable, deux passes par match au
lieu d'un polling permanent.

Sur un dépôt **public**, les artefacts GitHub Actions sont téléchargeables par
n'importe qui. Les valeurs sensibles sont masquées avant écriture et la
rétention est de 3 jours, mais un dépôt privé reste le choix prudent.
