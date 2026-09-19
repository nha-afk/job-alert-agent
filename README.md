# Agent d'alertes emploi : Investment Analyst / Associate en infrastructure → Telegram

Toutes les 30 minutes, l'agent cherche les **nouvelles offres** d'Investment Analyst / Associate
**en investissement d'infrastructure**, publiées depuis moins de 3 h, à
**Paris, Londres, Casablanca, Lagos et Cape Town**. Il vous envoie chaque nouvelle offre sur Telegram,
une seule fois.

Structures visées : fonds de private equity infrastructure, équipes infrastructure des fonds souverains,
banques d'investissement (infrastructure, project finance, power & utilities), DFI et toute autre
structure dotée d'une équipe d'investissement en infrastructure.

**Comment l'agent décide qu'une offre est « infrastructure »** :
1. L'intitulé doit viser un poste d'analyst / associate. Sont aussi reconnus « chargé d'investissement »
   et « Investment Officer / Executive / Professional ». Les stages, les postes de VP / Director et plus,
   ainsi que les postes IT, ingénierie, audit, etc. sont exclus.
2. Il retient d'office les intitulés sans ambiguïté (« Infrastructure Investment Associate »,
   « Project Finance Analyst », « Associate, Infrastructure Credit »…), ainsi que les postes chez un investisseur
   spécialisé en infrastructure (Meridiam, Africa50, AIIM, InfraCo, Antin, Stonepeak…).
3. Si l'intitulé est ambigu (« Associate, Infrastructure », « Private Equity Associate », simple « Associate »…),
   l'agent **lit l'annonce complète**. Il ne la retient que si elle traite à la fois d'infrastructure et
   d'investissement. Cela écarte par exemple les postes « IT infrastructure » et les fonds de PE généralistes.

| Source | Accès | Villes couvertes |
|---|---|---|
| LinkedIn | recherche publique, sans compte | les 5 villes |
| Adzuna (agrège Indeed, Totaljobs, sites carrières…) | clé API gratuite, optionnelle | Paris, Londres, Cape Town |
| Jooble (agrège des centaines de job boards) | clé API gratuite, optionnelle | les 5 villes |

Exemple de message reçu :

```
🏗️ Associate, Infrastructure Credit
🏢 GIC
📍 London, England, United Kingdom
🌐 LinkedIn · publié le 2026-09-18
🔗 Voir l'offre
```

---

## 1. Créer le bot Telegram (5 min)

1. Dans Telegram, ouvrez **@BotFather**, envoyez `/newbot` et suivez les instructions.
   BotFather vous donne un **token** (du type `123456789:AA...`) : c'est votre `TELEGRAM_BOT_TOKEN`.
2. Ouvrez la conversation avec votre nouveau bot et envoyez-lui un message quelconque (« bonjour »).
3. Dans un navigateur, ouvrez `https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates`
   et repérez `"chat":{"id": 123456789 ...}`. Ce nombre est votre `TELEGRAM_CHAT_ID`.

## 2. (Optionnel) Ajouter des sources

- **Adzuna** : inscription gratuite sur <https://developer.adzuna.com>, qui fournit un `app_id` et une `app_key`.
- **Jooble** : demande de clé gratuite sur <https://jooble.org/api/about>.

Sans ces clés, l'agent fonctionne avec LinkedIn uniquement.

## 3. Déployer sur GitHub Actions (gratuit, tourne même PC éteint)

1. Créez un dépôt sur GitHub, **public de préférence**. Les minutes d'Actions sont illimitées
   pour un dépôt public. En privé, le quota gratuit (2 000 min/mois) ne suffit pas pour un passage
   toutes les 30 min. Le dépôt ne contient aucun secret : les clés restent dans les *Secrets*.
2. Envoyez-y le contenu de ce dossier :
   ```bash
   git init && git add . && git commit -m "Job alert agent" && git branch -M main
   ```
   ```bash
   git remote add origin https://github.com/<vous>/<depot>.git && git push -u origin main
   ```
3. Sur GitHub, allez dans **Settings → Secrets and variables → Actions → New repository secret**
   et ajoutez `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, ainsi que `ADZUNA_APP_ID`,
   `ADZUNA_APP_KEY` et `JOOBLE_API_KEY` si vous les avez.
4. Allez dans l'onglet **Actions**, activez les workflows si GitHub le demande, puis ouvrez
   **Job alerts → Run workflow** pour un premier passage manuel.

L'agent tourne ensuite automatiquement toutes les 30 minutes.

## 4. Alternative : lancer sur votre PC Windows

```bash
python -m pip install -r requirements.txt
```

Copiez `.env.example` en `.env`, remplissez vos clés, puis vérifiez la connexion Telegram :

```bash
python job_alert.py --test
```

Pour une exécution automatique toutes les 30 minutes (Planificateur de tâches Windows) :

```bash
schtasks /Create /SC MINUTE /MO 30 /TN "JobAlert" /TR "python C:\chemin\vers\job-alert-agent\job_alert.py"
```

## Commandes utiles

| Commande | Effet |
|---|---|
| `python job_alert.py --dry-run` | affiche les offres trouvées sans rien envoyer |
| `python job_alert.py --dry-run --hours 72 --verbose` | idem sur 72 h, avec la décision (et sa raison) pour chaque offre |
| `python job_alert.py --test` | envoie un message de test sur Telegram |

## Personnaliser : `config.yaml`

- `search_terms` : les recherches lancées dans chaque ville.
- `locations` : ajoutez ou retirez des villes, avec `extra_terms` pour des intitulés locaux (en français, par exemple).
- `job_filter` : les règles de décision décrites plus haut.
  - `infra_specialists` : ajoutez les fonds infra que vous ciblez. Tout poste d'analyst / associate chez eux sera retenu.
  - `exclude` : retirez par exemple `director` si vous visez aussi des postes plus seniors.
  - `min_description_mentions` : augmentez cette valeur pour être plus strict sur les annonces ambiguës.
- `max_age_hours` : l'ancienneté maximale d'une offre, en heures.

## Bon à savoir

- **Délai** : c'est un système de surveillance périodique, pas du temps réel. Comptez 30 à 45 min
  entre la publication et l'alerte, car GitHub décale parfois les exécutions planifiées.
  On peut descendre à 15 min dans `.github/workflows/job-alert.yml` (`*/15`),
  mais on s'expose davantage au blocage par LinkedIn.
- **Blocage LinkedIn** : LinkedIn limite parfois les requêtes venant des serveurs GitHub. Si aucune
  source ne répond, l'agent vous envoie un avertissement Telegram (au maximum une fois par 24 h).
  Si cela se répète, lancez l'agent depuis votre PC (section 4).
- **Anti-doublons** : chaque offre, retenue ou écartée, est mémorisée 30 jours (`seen_jobs.json`).
  Elle n'est donc évaluée qu'une fois, et une annonce n'est lue en entier qu'une seule fois. Une même
  offre repérée à la fois sur LinkedIn et sur Jooble n'est envoyée qu'une fois.
- **Inactivité** : GitHub met en pause les tâches planifiées d'un dépôt public sans activité depuis
  60 jours. Il suffit alors de cliquer sur « Enable workflow » dans l'onglet Actions.
- La lecture automatisée des pages LinkedIn n'est pas prévue par leurs conditions d'utilisation.
  L'agent reste volontairement discret (quelques requêtes espacées toutes les 30 min, sans compte),
  mais il est destiné à un usage personnel.
