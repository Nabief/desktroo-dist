# Notes de version — Desktroo

Ce qui change à chaque version. Chaque point est écrit pour ce qu'il vous apporte ;
la ligne grise en dessous donne le détail technique, quand il y en a un.

## 2.44.7 — à paraître

### Nouveau
- **Exclure des fichiers d'une sauvegarde cloud.** Un champ « Ne pas copier » dans le formulaire : un motif par ligne, et ce qui correspond n'est pas copié.
  > Alimente le champ `exclude` de TrueNAS, que Desktroo n'envoyait pas : il fallait passer par l'interface de TrueNAS. Utile quand la destination refuse certains noms de fichiers — un Nextcloud qui rejette le signe égal, par exemple.

## 2.44.6 — 13 octobre 2026

### Nouveau
- **Les notes de version sont dans l'application.** À propos → Nouveautés : vous voyez ce qu'apporte la version installée, et ce que vous gagnerez en mettant à jour.
  > Lues depuis `RELEASES.md` du dépôt de distribution, avec repli sur la copie locale si le NAS n'a pas Internet.

## 2.44.5 — 13 octobre 2026

### Nouveau
- **Choisir où une application range ses données.** À l'installation, pour chaque stockage : le volume géré par TrueNAS, ou un dossier de votre choix sur le NAS.
  > Expose le champ `storage.*.type` (ix_volume / host_path) du catalogue TrueNAS, que l'application imposait jusqu'ici. Un dossier vide ou relatif bloque avant l'installation plutôt qu'à l'exécution.
- **Créer un dossier depuis le sélecteur**, sans quitter le formulaire en cours. Bouton « + Dossier », partout où le sélecteur s'ouvre.

### Corrigé
- **Changer l'adresse de Home Assistant sans ressaisir le jeton.** Laissez le champ vide : le jeton déjà enregistré est repris, et votre sélection d'appareils est conservée.
  > Le jeton n'était reconduit que si l'adresse restait identique, alors que le champ promettait l'inverse. La liaison est maintenant vérifiée avant d'être enregistrée : une adresse qui désigne un autre serveur est refusée par lui, et rien n'est écrit.
- **Un message clair quand le jeton est mal collé** (espace ou retour à la ligne), au lieu du message qui vous demandait un jeton que vous aviez déjà.

### Amélioré
- **Le widget Stockage réagit plus vite et travaille moins.** Un pool qui se dégrade passe au rouge sans attendre, et les widgets ne tournent plus quand l'onglet est en arrière-plan.
  > Un seul `pool.dataset.query` au lieu d'un appel par pool ; abonnement à `pool.query` pour la santé, plafonné à une relecture toutes les 10 s ; verrou contre deux parcours simultanés.

## 2.44.4 — 13 octobre 2026

### Nouveau
- **Arrêter une tâche en cours depuis la barre système.** Le panneau reprend les quatre listes de TrueNAS — en cours, en attente, échecs, réussies — avec un bouton d'arrêt sur chaque tâche qui l'accepte.
  > `core.job_abort`, proposé seulement si TrueNAS marque le job comme interruptible. Les tâches vivantes sont relues à chaque tour, l'historique au plus toutes les 150 secondes.
- **L'indicateur signale aussi les échecs.** Une tâche en échec de moins de sept jours que vous n'avez pas encore vue allume l'indicateur, même si plus rien ne tourne.
- **Arrêter une sauvegarde ou une réplication** depuis sa fiche, dans Protection des données.
- **« Servir sur le web » déplace le dossier dans le dataset Web**, avec une confirmation qui nomme la destination exacte et prévient si le dossier change de pool.
  > Un site doit vivre dans le dataset Web : c'est la seule zone que Desktroo sait retrouver et la seule dont les droits sont garantis.

### Corrigé
- **Une sauvegarde cloud encore en cours n'est plus annoncée comme terminée.** Au bout d'une heure d'attente, Desktroo affichait « terminée » sans avoir vérifié.
  > L'attente d'un job rend la même valeur vide en cas de dépassement de délai et en cas de réussite sans résultat. L'état du job est désormais relu pour les distinguer.
- Une tâche que vous venez d'arrêter n'est plus signalée comme une erreur.

## 2.44.3 — 12 octobre 2026

### Corrigé
- **Le compte affiché dans la barre système est bien le vôtre.** Desktroo pouvait afficher « root », en particulier à la réouverture d'un onglet ou du navigateur.
  > Le nom vient maintenant de TrueNAS lui-même. Il alimentait le journal d'activité, la corbeille, la présélection de compte pour une clé d'API et l'acceptation des conditions.
- **La mise à jour n'est plus proposée quand elle est déjà installée.**

## 2.44.2 — 12 octobre 2026

### Corrigé
- **Les informations système ne sont plus coupées.** Les sous-titres des tuiles se replient sur deux lignes au lieu d'être tronqués sans recours.

## 2.44.1 — 12 octobre 2026

### Corrigé
- **Les noms de disques sont lisibles dans Rapports**, sans l'étiquette technique ni le numéro de série qui débordaient de la légende.
- **Les fenêtres retrouvent leur taille** au lieu de rouvrir vides.
- Les libellés tronqués de la navigation latérale affichent leur nom complet au survol.
- Plusieurs textes restés en français en mode anglais, et un débordement sur la mémoire avec cache ZFS.

## 2.44.0 — 12 octobre 2026

### Nouveau
- **Desktroo s'utilise depuis un téléphone.** Mise en page dédiée : barre du bas, navigation par onglets, bouton retour et menus adaptés au tactile.

---

_Les versions antérieures à la 2.44.0 n'ont pas de notes publiées._
