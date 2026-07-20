# Veille Chartreuse

Vérifie chaque jour à 21h (heure de Paris) si la Chartreuse Jaune VEP, la
Reine des Liqueurs ou un Jéroboam (toute couleur) sont en stock chez une
liste de distributeurs européens, et affiche le résultat sur un petit
dashboard.

100% gratuit : le check tourne sur **GitHub Actions** (cron cloud), le
résultat est publié sur **GitHub Pages**. Rien à installer, rien à laisser
allumé sur ton PC.

## Mise en place (une seule fois, ~5 min)

1. Crée un repo GitHub (public ou privé, les deux marchent avec Pages sur
   un compte gratuit) et pousse ce dossier dedans :

   ```bash
   git init
   git add .
   git commit -m "Initial commit: veille Chartreuse"
   git branch -M main
   git remote add origin https://github.com/<ton-user>/veille-chartreuse.git
   git push -u origin main
   ```

2. Dans le repo GitHub : **Settings → Pages** → Source = `Deploy from a
   branch` → Branch = `main`, dossier = `/docs` → Save.
   Ton dashboard sera visible à une adresse du type
   `https://<ton-user>.github.io/veille-chartreuse/` (peut prendre 1-2 min
   la première fois).

3. Dans **Settings → Actions → General → Workflow permissions**, coche
   `Read and write permissions` (nécessaire pour que le workflow puisse
   committer `status.json` tout seul).

4. Pour tester tout de suite sans attendre 21h : onglet **Actions** →
   `Verification disponibilite Chartreuse` → `Run workflow` (le bouton
   force l'exécution même si ce n'est pas l'heure).

C'est tout : le workflow tourne ensuite automatiquement chaque jour.

## Comment ça marche

- `.github/workflows/check.yml` se déclenche à 19h **et** 20h UTC (pour
  couvrir 21h heure de Paris été comme hiver, GitHub Actions ne gère pas
  le changement d'heure). Le script vérifie l'heure réelle à Paris et ne
  fait le check "pour de vrai" que sur le créneau ~21h.
- `scripts/check_availability.py` va chercher la page de recherche de
  chaque site listé dans `config/sites.json`, cherche le nom du produit
  puis regarde le texte autour pour deviner si c'est en stock ou en
  rupture (mots-clés FR/EN/ES/DE).
- Le résultat est écrit dans `docs/status.json` (état courant) et ajouté
  à `docs/history.json` (historique, 120 jours glissants), puis commité
  automatiquement par le bot.
- `docs/index.html` est le dashboard : il lit juste ces deux fichiers
  JSON, aucun serveur derrière.

## Limites à connaître (important)

- **La détection est heuristique**, pas un scraping structuré site par
  site. Certains sites changent souvent leur HTML, ou chargent leur
  contenu produit en JavaScript (dans ce cas le script ne verra pas grand
  chose et affichera "Non trouvé" / "À vérifier") — c'est normal, regarde
  les logs du run dans l'onglet Actions pour voir ce qui a été récupéré.
- Les URLs de recherche dans `config/sites.json` sont un point de départ
  raisonnable mais pas garanties à 100% (certains sites changent leurs
  paramètres d'URL). Si un site remonte toujours "Non trouvé", ouvre son
  `search_url` dans un navigateur pour vérifier qu'il pointe bien vers des
  résultats, et ajuste si besoin.
- Pas de notification push pour l'instant (tu as choisi "juste le
  dashboard") : il faut aller consulter la page. Si tu changes d'avis,
  ajouter un ping Telegram/Discord ne prend que quelques lignes en plus
  dans le script — dis-le moi quand tu veux.
- Ajouter un site ou un produit : édite `config/sites.json`, pas besoin de
  toucher au reste.
