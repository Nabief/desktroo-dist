# Notes de version — Desktroo

Ce qui change à chaque version. Chaque point est écrit pour ce qu'il vous apporte ;
la ligne grise en dessous donne le détail technique, quand il y en a un.

## 2.47.0 — 15 octobre 2026

### Nouveau
- **Desktroo Pass, le coffre de mots de passe du NAS** (Desktroo Apps). Il fonctionne avec les applis Bitwarden officielles et gratuites : remplissage automatique dans le navigateur, sur le téléphone et sur le PC. Chaque personne a son coffre, chiffré avec son propre mot de passe maître.
  > Basé sur Vaultwarden. Desktroo l'installe, le publie en https sous votre domaine grâce à NPMplus, et l'administre depuis sa fenêtre : comptes sur invitation seulement, page d'administration jamais servie sur Internet.

## 2.46.3 — 15 octobre 2026

### Corrigé
- **Une app installée depuis Desktroo Apps a toujours son icône sur le bureau**, même si la fenêtre Desktroo Apps a été fermée pendant l'installation. Retirée ensuite, elle n'y revient pas.

## 2.46.2 — 15 octobre 2026

### Amélioré
- **Suivi de « Tout mettre à jour ».** La fenêtre Alertes reste ouverte avec une ligne par application : en attente, en cours, à jour, ou la raison d'un échec, et une barre d'avancement.
- **« Tout mettre à jour » ne bute plus sur une application arrêtée** : TrueNAS refuse de la mettre à jour, Desktroo la laisse de côté et vous le dit.
- **Le reverse-proxy est mis à jour en dernier.** Si vous ouvrez Desktroo par lui (NPMplus, par exemple), la connexion coupée pendant sa mise à jour n'interrompt plus les autres : la page se reconnecte et continue.
  > Une mise à jour lancée continue côté TrueNAS même quand la page perd la connexion ; Desktroo l'indique au lieu d'afficher « WS_UNAVAILABLE ».

## 2.46.1 — 15 octobre 2026

### Nouveau
- **Tout mettre à jour.** Dans Alertes, un bouton met à jour d'un coup toutes les applications qui l'attendent, l'une après l'autre, avec l'avancement affiché.
  > Nouvelle version du catalogue TrueNAS : `app.upgrade` ; nouvelles images d'une application personnalisée : récupération puis redéploiement.

### Amélioré
- **Les apps de Desktroo Apps portent leur vrai nom** dans les alertes et la liste des applications : « Serveur web » au lieu de « Dt-webserver ».

## 2.46.0 — 15 octobre 2026

### Nouveau
- **Desktroo Apps : des applications pour votre NAS, comprises dans l'abonnement.** Une nouvelle fenêtre liste les apps de Mdm-services.fr. Elles s'installent en un clic, sans configuration ni port à choisir, s'ouvrent dans une fenêtre du bureau avec votre session, et se mettent à jour depuis Desktroo. Vous choisissez quels comptes y ont accès.
  > Catalogue signé, vérifié par le NAS avant tout usage. Chaque app tourne dans son propre conteneur, isolé, avec ses données dans `<pool>/desktroo-apps/<app>` et un instantané ZFS avant chaque mise à jour. Abonnement expiré : les apps installées continuent de fonctionner ; seules l'installation et la mise à jour demandent un abonnement.
- **Certaines apps s'ouvrent aussi sans le bureau**, dans un onglet ou depuis l'écran d'accueil du téléphone, à l'adresse de Desktroo suivie de `/a/<app>/`. Connexion avec votre compte TrueNAS, code de double authentification compris, et « Rester connecté » pendant 30 jours.
- **Le serveur web devient un module facultatif.** Si vous n'hébergez rien sur le NAS, il ne tourne plus pour rien ; sinon, il s'installe depuis Desktroo Apps et la fenêtre Sites Web apparaît.
  > Une installation existante est déplacée automatiquement, une seule fois, au premier démarrage de cette version : mêmes conteneurs, mêmes sites, mêmes bases. Les sites sont coupés une dizaine de secondes. En cas d'échec, tout est remis comme avant. Une installation faite comme application TrueNAS (Custom App) n'est pas déplacée.
- **NPMplus en module.** Le reverse-proxy qui publie vos sites sous vos domaines, en https, s'installe en un clic (ports 8090, 8453 et 8181, pour laisser 80 et 443 à TrueNAS). Un NPMplus installé à la main peut être repris, avec ses domaines et ses certificats ; un NPMplus installé comme app TrueNAS peut être relié.
- **Fenêtre Domaines.** La liste de vos domaines publiés, vers quel site ils mènent et l'état de leur certificat. Publier un domaine, demander son certificat Let's Encrypt, le renouveler ou le retirer se fait sans quitter Desktroo. La page de chaque site propose « Publier ce site avec NPMplus ».
  > Desktroo pilote NPMplus par son API, depuis le NAS ; l'interface de NPMplus reste accessible pour les réglages avancés.
- **Supprimer plusieurs snapshots d'un coup.** Une case par ligne dans Snapshots, « Tout cocher » pour ce qui est affiché, et Maj + clic pour cocher une plage. Une barre indique combien sont cochés et la place libérée, avec le bouton de suppression.
  > Astuce : filtrez d'abord (par exemple `auto-2026-09`), puis « Tout cocher » ne prend que les lignes visibles. Un snapshot que TrueNAS refuse de supprimer (retenu, ou à l'origine d'un clone) reste coché, avec la raison. La place affichée est un minimum : ce que plusieurs snapshots partagent n'est libéré que quand ils partent tous.

### Amélioré
- **L'état d'une app se lit en clair** dans Desktroo Apps : « Démarrage… », « En marche », « Arrêtée »…
- **Installer une application dans Sites Web ne crée plus de doublon** : une fois le site prêt, le bouton « Installer » devient « Voir le site ».
- **Désinstaller Desktroo retire aussi les apps de Desktroo Apps**, leurs données restant sur le NAS.
- **Le bureau ne perd plus le contact avec son service** quand celui-ci redémarre avec une autre adresse sur le réseau de Docker.
  > nginx est rechargé à chaque démarrage de fileops : il relit l'adresse au lieu de répondre 502.
- **Le message d'essai de la messagerie part à l'adresse de votre choix.** Desktroo propose celle du compte administrateur, et le test marche même quand aucun compte n'en a. Il vous prévient alors que les alertes de TrueNAS n'ont nulle part où arriver.
  > Avant, l'essai échouait avec « None of the local administrators has an e-mail address configured ». Les erreurs d'envoi les plus courantes (identifiants refusés, chiffrement, serveur muet) sont maintenant expliquées en clair.
- **Port et chiffrement accordés.** Choisir un chiffrement propose le port qui va avec (587 pour STARTTLS, 465 pour SSL/TLS direct, 25 sans chiffrement), et une combinaison qui ne peut pas marcher est signalée avant d'enregistrer.

## 2.45.0 — 14 octobre 2026

### Nouveau
- **Désinstaller Desktroo depuis l'application.** À propos → Désinstaller Desktroo : la fenêtre montre ce qui sera supprimé, ce qui reste, et la place que vous récupérez. Il faut taper le nom du NAS pour confirmer.
  > Supprime les conteneurs, leurs images Docker (sauf celles qu'une autre application utilise), la tâche de démarrage automatique et les fichiers de l'application. Les bases des sites, les réglages et la corbeille ne partent que si vous cochez leur case. Les machines virtuelles, les fichiers des sites et tout ce que Desktroo a créé dans TrueNAS restent en place.
- **La licence est libérée à la désinstallation** : vous pouvez l'activer sur un autre NAS sans nous écrire.
  > L'abonnement, lui, continue : il se gère depuis le portail client.
- **Le même désinstalleur en ligne de commande**, si l'interface ne s'ouvre plus : `sudo bash uninstall.sh`, dans le dossier d'installation.
  > `--plan` fait l'état des lieux sans rien modifier. Fonctionne aussi pour une installation en Custom App.

## 2.44.7 — 14 octobre 2026

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
