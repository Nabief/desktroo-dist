# Notes de version — Desktroo

Ce qui change à chaque version. Chaque point est écrit pour ce qu'il vous apporte ;
la ligne grise en dessous donne le détail technique, quand il y en a un.

## 2.60.40 — 22 octobre 2026

### Corrigé
- **Musique : la lecture d'un titre fonctionne** (l'adresse du flux audio était incomplète). Une erreur de lecture n'enchaîne plus une avalanche de messages.

## 2.60.39 — 22 octobre 2026

### Nouveau
- **Nouvelle app du Magasin : Musique (Navidrome)**, en canal test. Vos dossiers de musique deviennent une bibliothèque : accueil (récents, plus écoutés, à découvrir), artistes, albums, listes de lecture, favoris, recherche, avec pochettes.
- **Fenêtre Musique de Desktroo** : lecteur intégré (lecture aléatoire, répétition, file d'attente, volume, touches multimédia du clavier), sans ouvrir l'interface d'origine.
- **Applications mobiles** : compatible Subsonic (Symfonium, DSub, Feishin…) sur le réseau local ; adresse et compte indiqués dans les réglages de la fenêtre.
  Lecture seule des dossiers choisis ; compte administrateur créé automatiquement ; non testé en conditions réelles.

## 2.60.38 — 22 octobre 2026

### Corrigé
- **Caméras : la nouvelle icône est aussi utilisée pour l'application installée / le raccourci Windows** (l'ancienne image restait mémorisée sur le NAS ; elle est régénérée une fois à l'ouverture du bureau).

## 2.60.37 — 22 octobre 2026

### Corrigé
- **Caméras : alignement des champs du formulaire** (section PTZ, enregistrement) : colonnes régulières, champs de même hauteur.

## 2.60.36 — 22 octobre 2026

### Amélioré
- **Fenêtres autonomes des apps : le message explique la règle de Chrome** — installez d'abord les apps voulues, puis Desktroo lui-même ; pour en ajouter une plus tard, désinstallez Desktroo (chrome://apps), installez l'app, puis réinstallez Desktroo. Le raccourci « Ouvrir dans une fenêtre » reste possible sans désinstaller.

## 2.60.35 — 22 octobre 2026

### Corrigé
- **Installer une app du Magasin dans sa propre fenêtre Windows : Chrome n'offre pas « Installer » quand Desktroo est déjà installé** (il ne propose que « Ouvrir dans l'appli »). Le message indique la solution : menu ⋮ → « Créer un raccourci… » → « Ouvrir dans une fenêtre ». Le titre de l'onglet affiche « Caméras ».

## 2.60.34 — 22 octobre 2026

### Nouveau
- **Caméras : plein écran.** Bouton « Plein écran » dans « En direct » (toute la fenêtre des caméras, fenêtres d'image comprises) et bouton ⛶ dans la vue agrandie (image, zoom et commandes PTZ en plein écran). Échap pour quitter.

## 2.60.33 — 22 octobre 2026

### Amélioré
- **Installer une app du Magasin comme application Windows : le message explique maintenant le bon chemin** quand le navigateur n'affiche que « Ouvrir dans l'appli » (qui ouvre Desktroo en entier) : menu ⋮ → « Diffuser, enregistrer et partager » → « Installer Desktroo … ». Le nom de l'app s'affiche aussi correctement (« Caméras »).

## 2.60.32 — 22 octobre 2026

### Nouveau
- **Caméras : prise en charge des caméras motorisées (PTZ)**. Case « Cette caméra est motorisée » dans le formulaire (port ONVIF, identifiants facultatifs : ceux de l'adresse RTSP sont repris par défaut). Dans la vue agrandie : flèches (maintenir pour bouger, relâcher pour arrêter), zoom optique et positions mémorisées de la caméra.
  Contrôle via ONVIF par le moteur Frigate (WebSocket interne). Non testé sur une caméra motorisée réelle ; ne concerne pas les Tapo sur batterie.

## 2.60.31 — 22 octobre 2026

### Amélioré
- **Caméras : les listes déroulantes du formulaire sont remplacées par de vrais boutons de choix** (type de caméra, enregistrement, sensibilité), plus lisibles.

## 2.60.30 — 22 octobre 2026

### Nouveau
- **Caméras : fenêtre d'ajout refaite, plus aérée**, organisée en sections (identité, connexion, enregistrement, détection).
- **Caméras : réglages de détection par caméra** — choix des objets à repérer (personne, voiture, chien, chat, camion, vélo, moto, oiseau, colis) et sensibilité basse / normale / haute.
- **Caméras : zoom dans la vue agrandie** (molette, boutons, pincement, double-clic, glisser pour déplacer).
- **Caméras : SMS quand une personne est détectée** (caméras RTSP, option par caméra, au plus un SMS toutes les 5 minutes par caméra), via la passerelle SMS de Desktroo.
  Les Tapo sur batterie n'ont ni détection ni PTZ côté Desktroo ; le PTZ n'est pas encore pris en charge.

## 2.60.29 — 22 octobre 2026

### Corrigé
- **Caméras : le flux vidéo de la vue agrandie fonctionne sans multiplier les connexions à la caméra** (une seule connexion continue au lieu d'une image redemandée toutes les deux secondes, que les Tapo finissaient par refuser). La configuration des caméras se met à jour toute seule après une mise à jour de Desktroo.

## 2.60.28 — 22 octobre 2026

### Nouveau
- **Caméras : vraie vidéo dans la vue agrandie.** Un clic sur une caméra affiche maintenant son flux vidéo en continu, au lieu d'une image fixe rafraîchie. Si le flux ne démarre pas, la fenêtre retombe sur les images fixes et en donne la raison.

## 2.60.27 — 22 octobre 2026

### Corrigé
- **Caméras : les Tapo dont le mot de passe était refusé (« 401 Unauthorized »)** sont maintenant essayées avec les trois formes d'identification acceptées selon les micrologiciels (mot de passe, empreinte MD5, empreinte SHA-256).

## 2.60.26 — 22 octobre 2026

### Amélioré
- **Caméras : message d'erreur plus précis quand l'image n'arrive pas.**

## 2.60.25 — 22 octobre 2026

### Amélioré
- **Caméras : quand l'image n'arrive pas, la fenêtre affiche la raison donnée par le moteur** (mot de passe refusé, caméra injoignable…), au lieu de rester vide.

## 2.60.24 — 22 octobre 2026

### Nouveau
- **Caméras : recherche des caméras sur le réseau.** Le bouton « Chercher les caméras du réseau » repère les caméras RTSP et les Tapo, et préremplit le formulaire d'ajout.
  > Balayage du réseau local du NAS (ports 554, 8554, 8800, 2020), lancé depuis le NAS.

## 2.60.23 — 22 octobre 2026

### Nouveau
- **Caméras : les Tapo sur batterie ou solaires sont prises en charge** (en test). Elles se branchent avec leur adresse IP et le mot de passe du compte Tapo, sans RTSP. Pour ménager la batterie, l'image n'est demandée que lorsque vous cliquez sur la caméra.

## 2.60.22 — 22 octobre 2026

### Nouveau
- **Caméras : une nouvelle app dans Desktroo Apps.** Vos caméras IP en direct, enregistrement continu ou sur détection de mouvement, reconnaissance des personnes, véhicules et animaux. Tout reste sur le NAS, sans compte ni cloud. Sa fenêtre Desktroo permet d'ajouter une caméra par son adresse RTSP (avec test), de voir toutes les caméras en direct, de parcourir les événements détectés et de regarder les enregistrements.
  > Moteur Frigate 0.18.2 (image figée par empreinte) ; l'installeur du Magasin sait maintenant réserver de la mémoire partagée à une app (`resources.shm_mb`).

## 2.60.20 — 22 octobre 2026

### Nouveau
- **Jellyfin : le reste des fonctions de Jellyfin est maintenant dans la fenêtre Desktroo.** Tâches planifiées (lancer, arrêter, horaires), plugins (installer, activer, désinstaller, dépôts), transcodage (accélération matérielle), serveur et réseau (avec redémarrage de Jellyfin), clés d'API et Quick Connect, et télévision en direct (tuners, guide, chaînes, enregistrements).
- **Jellyfin : sur la fiche d'un titre**, modification des informations, choix d'une autre affiche, recherche de sous-titres, création d'une collection et suppression définitive.
- **Jellyfin : options de chaque bibliothèque, droits détaillés de chaque compte, message et pause sur les lectures en cours.**
  > Nouvelle action `adm` de `/store/jellyfin/api` (`_jf_adm`) ; champs validés, réglages fusionnés avec l'existant avant envoi.

## 2.60.19 — 22 octobre 2026

### Corrigé
- **Jellyfin : les compteurs de l'accueil (films, séries, épisodes, morceaux, albums) sont justes.** Ils pouvaient afficher bien plus que le contenu réel (840 films pour 506 dans la bibliothèque). Ils comptent maintenant comme les bibliothèques.
  > `/Items/Counts` de Jellyfin additionne des éléments en double ; remplacé par `/Items?Recursive=true&Limit=0&IncludeItemTypes=…` (`TotalRecordCount`), `/Items/Counts` restant le repli.

## 2.60.18 — 22 octobre 2026

### Amélioré
- **Jellyfin : tous les types de bibliothèque sont proposés.** À « Clips musicaux » et « Films et séries mélangés » près, la liste de Desktroo était déjà là : elle s'enrichit de ces deux types, et « Vidéos personnelles » devient « Vidéos et photos personnelles », comme dans Jellyfin.
  > `_JF_TYPES` + `musicvideos` (`MusicVideo`) et `mixed` (création sans `collectionType`).

## 2.60.17 — 22 octobre 2026

### Corrigé
- **Une opération lancée depuis cet appareil ne s'affiche plus deux fois.** Son indicateur de progression suffit ; la fenêtre « Opérations en cours sur le NAS » ne montre que celles lancées depuis un autre appareil.

## 2.60.16 — 22 octobre 2026

### Corrigé
- **Annuler un déplacement ou une copie arrête vraiment toute l'opération.** Quand on déplace une sélection de fichiers, Desktroo les envoie un par un : annuler le fichier en cours laissait le suivant partir. L'opération porte maintenant un identifiant commun ; « Annuler » (dans la fenêtre « Opérations en cours sur le NAS », ou sur l'indicateur de progression de l'appareil qui l'a lancée) arrête le fichier en cours et refuse tous les suivants, même si l'opération a été lancée depuis un autre appareil.
  > Champ `bid` envoyé avec chaque `/move` et `/copy`, registre `_FS_BATCH_CANCELLED` (1 h) côté serveur, `POST /fs/jobs/cancel` accepte `id` ou `bid`. Une page ouverte avant cette version n'envoie pas `bid` : la fermer suffit à arrêter sa boucle.

## 2.60.15 — 22 octobre 2026

### Nouveau
- **Les déplacements et copies longs sont visibles et annulables depuis n'importe quel appareil.** Une fenêtre « Opérations en cours sur le NAS » liste ce qui tourne (nom, destination, durée) avec un bouton « Annuler », même si l'opération a été lancée depuis un autre ordinateur ou téléphone du même utilisateur, ou si la page a été fermée.
  > Registre `_FS_JOBS` côté serveur (`_fs_job_ssh`), routes `GET /fs/jobs` et `POST /fs/jobs/cancel` (arrêt du `cp`/`mv` sur le NAS par un motif propre à la tâche), sondage toutes les 8 s par `fsJobsPoll`. Ne concerne que les opérations confiées à l'hôte (entre datasets, ACL) ; les opérations rapides ne s'affichent pas.

## 2.60.14 — 22 octobre 2026

### Nouveau
- **Un SMS quand une mise à jour est disponible.** Avec l'app SMS installée, Desktroo vous envoie un SMS pour une mise à jour de Desktroo, d'une app de Desktroo Apps, d'une app TrueNAS ou de TrueNAS lui-même : un seul SMS par vérification (toutes les 6 heures), et seulement pour ce qui est nouveau.
  > `_upd_sms_loop` : sources `_version_status`, `store_view`, `midclt app.query` (`upgrade_available`) et `update.check_available` ; mémoire dans `store/update-notified.json` ; envoi au webhook de l'app SMS. Pour l'éteindre, créer le fichier `store/update-sms-off`.

## 2.60.13 — 22 octobre 2026

### Amélioré
- **Déplacer des fichiers d'un dataset à un autre du même pool est maintenant quasi instantané.** Avant, Desktroo recopiait chaque fichier puis supprimait l'original (plusieurs heures pour une bibliothèque de films). Il utilise désormais le clonage de blocs de ZFS ; si le pool ne le permet pas, il fait comme avant.
  > `/move` : repli hôte `cp -a --reflink=always` puis `rm -rf` de la source, sinon `mv -f` (nettoyage de la destination partielle entre les deux).

## 2.60.12 — 21 octobre 2026

### Amélioré
- **Bouton « Tout sélectionner » dans Fichiers.** Un clic sélectionne tout le dossier (un second clic désélectionne) ; Ctrl+A fonctionnait déjà. Sur téléphone, une fois une sélection commencée, un appui sur un fichier le coche ou le décoche au lieu de l'ouvrir.
  > `fsSelectAllToggle()`, même sélection que Ctrl+A.
- **Configuration des alertes en français.** Les catégories et les types d'alerte de TrueNAS (« Certificate Is Expiring », « Pool Status Is Not Healthy »…) s'affichent maintenant traduits.
  > Table `AL_FR` (≈250 titres du dépôt TrueNAS) appliquée par `alTr()` ; un titre inconnu reste en anglais.

## 2.60.11 — 21 octobre 2026

### Corrigé
- **Installer NPMplus, AdGuard Home ou Pi-hole « reliés » comme application de l'ordinateur.** Une app reliée (installée ailleurs que par Desktroo Apps, par exemple NPMplus sur une autre machine) s'installait avec l'icône de Desktroo, et le navigateur proposait seulement « Ouvrir dans l'application Desktroo » au lieu d'ajouter une nouvelle application. Elle a maintenant son propre nom et sa propre icône.
  > `store_kiosk_manifest` et `store_kiosk_icon_put` ne connaissaient que les apps du registre local : les apps reliées, injectées seulement dans la liste affichée, n'avaient ni manifeste (404) ni icône PNG. Nouvelle fonction `_store_kiosk_inst`.

## 2.60.10 — 19 octobre 2026

### Nouveau
- **Pi-hole dans Desktroo Apps.** Un filtre de publicités et de traqueurs pour tout le réseau, comme AdGuard Home, avec sa propre fenêtre Desktroo : vue d'ensemble avec graphique, journal des requêtes (bloquer ou autoriser un site en un clic), listes de blocage, sites autorisés ou bloqués à la main, pause de la protection, serveurs DNS en amont.
  > Entrée `pihole` du catalogue (canal test, image `pihole/pihole`, port 53, `FTLCONF_*`, mot de passe d'API dérivé). La fenêtre `dkPh*` passe par `/store/pihole/api` : le NAS ouvre la session de l'API v6 (sid, renouvelé à l'expiration) et ne relaie que les chemins d'une liste blanche ; seul le réglage `dns.upstreams` peut être modifié.
- **Pi-hole sur une autre machine.** Si vous avez déjà un Pi-hole (Raspberry Pi, machine virtuelle…), « Relier un Pi-hole existant » le pilote depuis la même fenêtre. Le mot de passe reste sur le NAS.
  > `/store/pihole/link`, `/unlink` ; fichier `data/store/pihole.json` en 0600 ; version 6 exigée ; entrée injectée dans la liste des apps installées comme pour AdGuard.

### Amélioré
- **Pi-hole et AdGuard Home : un message clair au lieu d'une erreur de port.** Les deux utilisent le port 53 et ne peuvent pas tourner ensemble sur le même NAS. Si l'un est installé, l'installation de l'autre est refusée avec une explication et la piste à suivre (désinstaller le premier, ou relier un serveur sur une autre machine).
  > `_store_dns_conflict` avant le message générique « port déjà utilisé » ; un app dont l'empreinte d'image n'est pas encore figée est refusée proprement.

## 2.60.9 — 19 octobre 2026

### Corrigé
- **« Relier un AdGuard Home existant » échouait sur « Operation not permitted ».** Desktroo enregistrait bien la liaison, puis tentait de restreindre les droits du fichier, ce que les dossiers du NAS à ACL refusent. Il laisse maintenant ce réglage de côté : la liaison s'enregistre. Même correction pour la mise en quarantaine d'un fichier par l'antivirus.
  > `os.chmod` rendu facultatif après création en 0600 (`store_ag_link`, quarantaine `isolate`).

## 2.60.8 — 19 octobre 2026

### Corrigé
- **« Réglages avancés » d'un NPMplus installé sur une autre machine.** Dans Desktroo Apps, le bouton ouvrait l'administration à l'adresse du NAS (port 81) au lieu de celle du NPMplus relié ; il ouvre maintenant la bonne adresse, comme la fenêtre Domaines.
  > dkStoreNpmOpen prend l'adresse du NPMplus relié (installed.remote, sinon npm.linked.url) avant de retomber sur l'adresse du NAS et le port d'administration.

## 2.60.7 — 19 octobre 2026

### Amélioré
- **Un seul réglage pour l'accès aux apps.** « Droits d'accès » et « Qui peut l'utiliser » (fiche de l'app dans Desktroo Apps) sont maintenant le même réglage, et c'est le NAS qui l'applique : un compte refusé ne peut pas ouvrir l'app, même par son adresse. Un nouveau compte a accès tant qu'on ne le lui refuse pas ; un refus peut aussi venir d'un groupe. Les listes enregistrées jusqu'ici sont reprises telles quelles à la mise à jour : personne ne gagne ni ne perd d'accès.
  > Contrôle /store/auth fondé sur data/access-policy.json (même règle que acEffectiveApplication : compte, puis groupes, refus prioritaire, sinon autorisé ; root et truenas_admin toujours autorisés). Reprise unique de data/store/access.json en refus explicites pour les administrateurs absents des listes, fichier renommé access.json.repris. Comptes du NAS relus au plus une fois par minute.

## 2.60.6 — 19 octobre 2026

### Amélioré
- **Utilisateurs plus simples.** Un interrupteur « Administrateur du NAS » dans la fiche du compte donne ou retire les droits d'administration, nécessaires pour ouvrir Desktroo ; la liste et la fiche des comptes affichent ce rôle. Les groupes personnels que TrueNAS crée avec chaque compte, sous le même nom (compte fabien, groupe fabien), ne sont plus montrés : un groupe est maintenant toujours un ensemble de personnes, dans Utilisateurs comme dans Droits d'accès.
  > Rôle FULL_ADMIN porté par le groupe système builtin_administrators, masqué jusqu'ici. Groupe personnel : groupe principal d'un compte, de même nom ; ses appartenances sont conservées à l'enregistrement. On ne peut pas retirer ses propres droits.

### Corrigé
- **Connexion d'un compte sans droits d'administration.** Le message disait « Identifiants TrueNAS incorrects » alors que le mot de passe était bon : il indique maintenant que le compte n'a peut-être pas les droits d'administration du NAS.
  > TrueNAS refuse la connexion à son API (AUTH_ERR) à un compte sans aucun privilège, comme pour un mauvais mot de passe.

## 2.60.5 — 19 octobre 2026

### Nouveau
- **Alertes du NAS par SMS.** Avec la nouvelle app SMS de Desktroo Apps, « SMS » devient une sorte de service d'alerte dans Configuration des alertes : le NAS vous prévient par SMS, par Free Mobile (gratuit, pour les abonnés, vers votre propre numéro) ou par OVHcloud (vers les numéros de votre choix). Le canal et les numéros se règlent dans l'app SMS, qui sert aussi à envoyer des SMS avec un compte OVHcloud.
  > Pour TrueNAS, un service « Slack » dont l'adresse est celle où l'app SMS reçoit les alertes : port 8135 ouvert au réseau local seulement, jeton dérivé propre à l'installation (DESKTROO_HOOK_TOKEN). Adresse rendue par GET /store/sms/hook.

## 2.60.4 — 19 octobre 2026

### Nouveau
- **Veille des apps : la mémoire du NAS libérée quand une app ne sert pas.** OnlyOffice s'arrête de lui-même après 15 minutes sans activité, et redémarre tout seul quand on l'ouvre : depuis son icône, en ouvrant un document dans Fichiers ou par son adresse. On attend alors quelques dizaines de secondes. Aucun document n'est perdu : la veille attend que tous les documents soient fermés et enregistrés. Le délai (5 min à 2 h) se règle, ou la veille se coupe, dans la fiche de l'app dans Desktroo Apps, où l'app apparaît « En veille ».
  > Activité relevée à chaque requête vers /a/<app>/ (vérification /store/auth) et, pour la bureautique, à chaque échange avec le serveur de documents ; pas d'arrêt tant qu'une séance OnlyOffice est ouverte. Vérification chaque minute, arrêt par app.stop ; réveil POST /store/wake (état GET /store/wake), et depuis la page « l'app ne répond pas » (bloc nginx V6, qui transmet l'app). Capacité déclarée par « idle_stop » dans la fiche du catalogue ; réglage dans data/store/idle.json.

## 2.60.3 — 18 octobre 2026

### Amélioré
- **OnlyOffice pour tout le monde.** L'app OnlyOffice quitte le canal test : elle apparaît dans Desktroo Apps sur tous les NAS, inscrits ou non au programme bêta.

### Corrigé
- **Icônes des apps installées comme application.** Desktroo Apps et Droits d'accès ont maintenant leur icône quand on les installe sur l'ordinateur ou le téléphone, et la console du navigateur n'affiche plus une série d'erreurs 404 à chaque ouverture du bureau.
  > La préparation des icônes PNG demandait les apps du Magasin sous leur identifiant interne (dkapp-…) et essayait deux adresses pour chaque app : elle demande maintenant le nom court, à la seule bonne adresse, et retient les apps sans icône jusqu'à la version suivante. Icônes système ajoutées pour store et access.

## 2.60.2 — 18 octobre 2026

### Corrigé
- **Les documents OnlyOffice restaient sur l'écran de chargement.** L'éditeur s'ouvre maintenant jusqu'au bout. Si un document ne s'ouvre toujours pas au bout d'une minute et demie, un bandeau le dit et propose de réessayer.
  > Le service worker d'OnlyOffice relisait les fichiers statiques de l'éditeur sans le cookie de session des apps : nginx les refusait. Ces fichiers (web-apps, sdkjs, polices, dictionnaires) sont servis sans la session, en lecture seule (bloc nginx V5) ; document, enregistrement et liaison en direct restent derrière la session. « Réessayer » retire aussi le service worker d'OnlyOffice.

## 2.60.1 — 18 octobre 2026

### Nouveau
- **Des favoris dans Fichiers.** Une section « Favoris » entre Emplacements et Pools garde vos datasets et dossiers préférés sous la main : le bouton + permet de cocher des datasets dans la liste de tous ceux du NAS, et un clic droit sur un dossier l'ajoute ou le retire. Glissez un favori pour le ranger ; déposez-y des fichiers pour les y déplacer. Les favoris suivent votre compte sur tous les navigateurs.
  > Gardés dans le profil du compte (data/profiles, champ fs_favorites, 60 au plus) ; route POST /profile/favorites. Datasets internes (.system, ix-apps) écartés de la liste.

## 2.60.0 — 18 octobre 2026

### Nouveau
- **Vos documents Word, Excel et PowerPoint s'ouvrent dans le bureau.** Installez OnlyOffice depuis Desktroo Apps (canal test pour l'instant) : un double-clic sur un document dans Fichiers l'ouvre dans une fenêtre de Desktroo, sans autre onglet. Il s'enregistre tout seul sur le NAS, à sa place, et plusieurs personnes peuvent le modifier en même temps. Les formats OpenDocument (odt, ods, odp) s'ouvrent aussi ; les anciens formats (doc, xls, ppt) en lecture seule.
  > Le serveur de documents tourne dans le conteneur dt-onlyoffice (réseau des apps, port 80), servi sous /a/onlyoffice/ derrière la session du bureau (bloc nginx V4, X-Forwarded-Host avec le chemin). fileops signe la configuration de l'éditeur (JWT HS256, secret propre à l'installation), sert le fichier sur une adresse signée et reçoit l'enregistrement (/office/file, /office/save), remplacé de façon atomique en gardant propriétaire et droits. Deux ouvertures du même fichier rejoignent la même séance (coédition).
- **Créer un document, un classeur ou une présentation.** L'app OnlyOffice du bureau propose trois boutons et la liste de vos documents récents ; dans Fichiers, un clic droit dans un dossier ajoute « Nouveau document Word », « Nouveau classeur Excel » et « Nouvelle présentation PowerPoint ».
  > Modèles vides générés par fileops (Office Open XML, A4), donnés au propriétaire du dossier. Route POST /office/new.

### Amélioré
- **On voit la corbeille se vider.** Les éléments sont supprimés l'un après l'autre : celui en cours est signalé, la liste se vide au fur et à mesure, une barre et l'en-tête donnent l'avancement (« Suppression : 2 sur 7, 11,2 Go libérés »). À la fin, la place libérée est annoncée.
  > Un appel /trash/purge par élément au lieu d'un seul pour tout : un gros dossier peut demander plusieurs secondes, l'utilisateur sait lequel.

### Corrigé
- **Dates et nombres sur un navigateur à la langue mal décrite.** Une langue de navigateur que le système de dates ne reconnaît pas (« en-US@posix ») faisait échouer l'affichage de certaines fenêtres ; le format en-GB est alors utilisé.

## 2.59.0 — 18 octobre 2026

### Nouveau
- **Une photo ou un avatar pour votre compte.** Cliquez sur votre nom en haut à droite, puis « Photo du profil » : choisissez l'un des 18 avatars proposés ou une photo à vous. Elle s'affiche dans la barre des tâches, pour ce compte, sur tous les navigateurs.
  > La photo est recadrée au centre et réduite à 256 px dans le navigateur, puis gardée sur le NAS (data/profiles, compris dans la sauvegarde de Desktroo). Routes GET /profile, GET /profile/photo, POST /profile/avatar.

### Amélioré
- **Menu du compte.** Il affiche maintenant le vrai nom et l'initiale du compte connecté, et son rôle lu dans TrueNAS (administrateur, lecture seule…) au lieu d'un « R … Administrateur » figé. « Langue » ouvre le choix des langues (Configuration système → Langue et fuseau horaire) au lieu de basculer directement en anglais.

### Corrigé
- **Desktroo Apps dans la langue du bureau.** Le nom et la description des apps du Magasin restaient en français pour l'espagnol : ils sont maintenant traduits, et une langue ajoutée affiche l'anglais plutôt que le français quand l'app n'a pas sa traduction.
  > Catalogue (store/desktroo-store/catalog.json) : entrée « es » ajoutée à chaque texte (nom, accroche, description, ports, notes d'installation). dkStoreName : langue du bureau, sinon anglais, sinon français. À publier avec publish-store.ps1.
- **Plus de « à paraître » à côté de « Installée ».** Une version publiée garde désormais sa date dans les notes de version.
  > publish-dist.ps1 date l'entrée de la version publiée (« à paraître » / « unreleased » remplacés par la date du jour, dans le dépôt source comme dans le dist). Le bureau n'affiche plus ces mentions pour les versions déjà publiées sans date.
- **La corbeille annonce sa vraie taille.** Elle affichait moins que la page de désinstallation (7,6 Go contre 11,7 Go) : un gros dossier n'était mesuré qu'en partie au moment de sa suppression (« ≥ 5,2 Go »). Sa taille exacte se calcule maintenant juste après, et l'en-tête donne la place occupée sur le disque, celle que « Vider la corbeille » libère.
  > Taille complète d'un élément mesuré en partie calculée en arrière-plan et notée dans sa fiche ; place sur le disque par du -sk de chaque dossier .desktroo-corbeille (même mesure que uninstall.sh), recalculée quand le contenu change. La liste se relit toute seule pendant le calcul.

## 2.58.1 — 18 octobre 2026

### Corrigé
- **« Voir ce qu'apporte la mise à jour » montre bien la nouvelle version.** Les notes de version affichées dataient parfois d'avant sa publication : elles s'arrêtaient à la version installée.
  > Les notes sont relues dès qu'elles ne contiennent pas la dernière version annoncée par version.txt, côté bureau (cache de la page) comme côté service (cache de 15 minutes) ; une copie encore en cache chez GitHub est contournée.

## 2.58.0 — 18 octobre 2026

### Nouveau
- **Programme bêta.** Ceux qui le souhaitent reçoivent les nouvelles versions de Desktroo quelques jours avant tout le monde, et aident à les mettre au point. L'invitation apparaît une fois dans les alertes ; on s'inscrit ou on se désinscrit à tout moment dans À propos → Programme bêta.
  > S'inscrire crée data/store/beta : les mises à jour viennent alors de la branche « beta » du dépôt de distribution (publish-dist.ps1 -Beta), et les apps en test de Desktroo Apps deviennent visibles. En quittant le programme, le NAS garde sa version jusqu'à la prochaine version stable plus récente. La réponse à l'invitation est notée dans data/store/beta-offer. Les bêtas se numérotent 2.58.1b1, 2.58.1b2…, avant la 2.58.1 stable : le numéro de bêta est désormais bien comparé (une 2.58.1b1 passait devant la 2.58.1).

## 2.57.1 — 18 octobre 2026

### Nouveau
- **Desktroo en espagnol.** L'espagnol rejoint le français et l'anglais : tout le bureau et l'assistant d'installation, choisi automatiquement pour un navigateur réglé en espagnol, ou depuis l'écran de connexion et Configuration système → Langue et fuseau horaire.
  > Dictionnaire embarqué (dk-i18n-es, I18N_ES dans l'assistant), qui complète l'anglais. Un fichier « es » importé reste prioritaire, pour corriger un texte. Dates et nombres au format es-ES (ou celui du navigateur s'il est hispanophone).

## 2.57.0 — 18 octobre 2026

### Nouveau
- **Traduire Desktroo devient faisable sans outil spécial.** Le modèle à traduire se télécharge maintenant en tableur (.xlsx, pour Excel, LibreOffice ou Google Sheets), en XLIFF (.xlf, le format des outils de traduction comme OmegaT, Poedit, Weblate ou Crowdin) ou en JSON. Il se télécharge aussi par lots de 500 textes, assez petits pour un service de traduction en ligne, qui refuse les gros envois.
  > Configuration système → Langue et fuseau horaire → « Traduire » : choisissez la langue, les textes (les 500 suivants, un lot ou tout) et le format. Dans Google Sheets, la formule =GOOGLETRANSLATE() propose une première traduction de la colonne à relire. L'import accepte .json, .xlsx, .csv et .xlf.
- **Un import complète la langue au lieu de la remplacer.** Les lots s'importent les uns après les autres ; « Remplacer toute la langue » repart de zéro.
- **Les notes de version ont leur fenêtre.** À gauche, la liste des versions mène directement à chacune ; un tri n'affiche que les nouveautés, les améliorations ou les corrections, et les détails techniques (les lignes grises) se masquent d'un clic. Un bouton « Revenir au début » apparaît dès qu'on descend.
  > À propos → Nouveautés. La version en cours de lecture est surlignée dans la colonne de gauche. Dans une langue ajoutée, les notes s'affichent en anglais.

## 2.56.1 — 18 octobre 2026

### Corrigé
- **Desktroo Pass : « Renvoyer l'invitation », « Désactiver », « Réactiver », « Retirer la 2FA » et « Supprimer » fonctionnent.** Le coffre répondait « 404 » avec une page HTML.
  > Vaultwarden n'accepte ces actions qu'avec un corps JSON (Content-Type: application/json) ; Desktroo n'en envoyait pas. Une page d'erreur HTML n'est plus affichée telle quelle dans le message.

## 2.56.0 — 18 octobre 2026

### Nouveau
- **Ajoutez votre langue à Desktroo.** Desktroo existe en français et en anglais ; chacun peut maintenant le traduire dans sa langue, sans attendre une mise à jour. Configuration système → Langue et fuseau horaire → « Télécharger le modèle à traduire », traduisez le fichier, puis « Importer un fichier de langue… ». La langue est proposée à l'écran de connexion et dans les réglages, pour tous ceux qui ouvrent ce bureau ; un navigateur réglé dans cette langue l'affiche tout seul.
  > Le fichier est <install>/lang/<code>.json : les textes français en clés, la traduction en valeurs. Ce qui n'est pas traduit reste en anglais, et « Modèle à jour » redonne le fichier complété des textes ajoutés par les versions suivantes. Le fichier est vérifié et nettoyé à l'import (balises de mise en forme seulement). Les langues ajoutées font partie de la sauvegarde de Desktroo et restent en place aux mises à jour.
- **Licence d'utilisation 1.1.** Elle permet désormais de créer, d'utiliser et de partager des fichiers de langue. Le bureau vous demande de l'accepter une nouvelle fois.
- **Un lien vers Desktroo Apps** dans « Voir aussi » du Catalogue d'applications et du gestionnaire d'Applications.

### Corrigé
- **Une fenêtre ne se réduit plus au point de couper son contenu.** Les gestionnaires à deux volets qui n'avaient pas de taille minimale (Desktroo Apps et les fenêtres de ses apps comme Desktroo Pass, Mises à jour TrueNAS, Journal d'activité, Bilan de santé, Sites Web, Téléchargements) s'arrêtent à 640 × 420, jamais plus grand que l'écran.
  > La poignée du coin bas-droit respecte désormais la taille minimale de chaque fenêtre, comme les bords.
- **Desktroo Pass dit clairement quand les invitations ne partent pas par e-mail.** Le bandeau explique que la personne invitée ne reçoit rien tant que l'envoi n'est pas activé, et le bouton s'appelle « Activer l'envoi des e-mails ». Le coffre signale aussi quand la messagerie du NAS a changé depuis qu'il l'a reprise, et un bouton « Envoyer un e-mail de test » vérifie l'envoi par le même chemin que les invitations.
  > Le test passe par Vaultwarden (/admin/test/smtp) ; son message d'erreur est affiché tel quel. Après l'activation, « Renvoyer l'invitation » envoie l'e-mail aux personnes déjà invitées.
- **La liste des applications montre leurs icônes.** Les apps de Desktroo Apps prennent l'icône du Magasin, et une application personnalisée celle de l'app du même nom du catalogue TrueNAS (Nextcloud, n8n, OnlyOffice…). Les autres gardent leur initiale.

## 2.55.0 — 18 octobre 2026

### Nouveau
- **Les widgets se posent où vous voulez sur le bureau.** Faites glisser un widget par son titre et lâchez-le n'importe où : il s'aimante aux bords et aux autres widgets. Tirez son coin inférieur droit pour l'élargir ou le rétrécir. Les icônes du bureau s'écartent pour ne jamais passer dessous.
  > Un widget posé près d'un bord le reste quand la fenêtre change de taille, et il est ramené dans l'écran s'il ne tient plus. Panneau de configuration → Bureau Desktroo → Widgets → « Ranger les widgets » les remet en colonnes contre les bords. À la mise à jour, les widgets partent de leur place actuelle. Sur téléphone, ils restent empilés comme avant.

## 2.54.51 — 18 octobre 2026

### Corrigé
- **Supprimer un dataset qui en contient d'autres.** TrueNAS répondait « filesystem has children ». Desktroo liste maintenant les sous-datasets qui partiront avec lui, et demande de retaper le nom du dataset pour confirmer avant de tout supprimer.
- **La sauvegarde de Desktroo se fait aussi sur un dossier du NAS aux droits restrictifs.** L'archive se prépare désormais ailleurs, puis se copie dans le dossier choisi : plus besoin de renommer ni de changer les droits d'un fichier, ce que certains dossiers (ACL) refusent. Si le dossier n'accepte vraiment pas l'écriture, le message dit lequel et que faire.

## 2.54.50 — 18 octobre 2026

### Amélioré
- **Le choix d'un fichier prend les couleurs du bureau.** Le bouton gris d'origine du navigateur, sur les pages Restauration et Mise à jour, devient un bouton arrondi comme les autres.

### Corrigé
- **Noyau et matériel : plus d'erreur « NotImplemented.service ».** Le réglage « Économie d'énergie du processeur » (un service qui n'existe pas sous Linux) est retiré de la page, et si TrueNAS enregistre un réglage mais ne peut pas relancer un service absent, Desktroo l'indique au lieu d'afficher une erreur.
- **La sauvegarde de Desktroo échouait sur un dossier de NAS à ACL** (« Operation not permitted » sur le fichier en cours d'écriture). Desktroo n'impose plus les droits du fichier quand le dossier ne l'autorise pas : la sauvegarde se fait.

## 2.54.49 — 18 octobre 2026

### Nouveau
- **Sauvegarde et restauration de Desktroo, au même endroit que celles de TrueNAS.** Dans Configuration système, les pages « Sauvegarde » et « Restauration » ont une section Desktroo. Un clic sur « Sauvegarder Desktroo maintenant » fait un fichier avec vos réglages (partages, sites, accès et secrets des apps, fonds d'écran, liste des apps installées), rangé dans un dossier du NAS que vous choisissez et téléchargeable. Chaque sauvegarde prend aussi un instantané ZFS des données de chaque app, auquel on peut revenir en un clic.
- **Sauvegarde automatique chaque semaine**, avec le nombre de copies à garder (4 par défaut). Desktroo la fait lui-même : aucune tâche à créer dans TrueNAS.
- **Restauration sûre.** Une copie de sécurité de l'état actuel est faite juste avant, puis Desktroo redémarre. La licence et l'identité de l'installation n'en font pas partie : elles restent celles du NAS.
  > Archive `.tar.gz` (`data/` + `wallpapers/`, licence, `install-id`, essai et caches exclus), instantanés `dkbak-AAAAMMJJ-HHMMSS` sur `apps/desktroo/<app>`, retour par `zfs rollback` app arrêtée. Routes `/dkbackup/*`.

### Corrigé
- **L'installation d'une app échouait sur un NAS dont le dataset « apps » est en ACL POSIX** (« aclmode: Must be set to DISCARD when acltype is POSIX or OFF »), par exemple VPN (WireGuard). Desktroo crée désormais ses datasets avec la bonne valeur.

## 2.54.48 — 18 octobre 2026

### Corrigé
- **L'essai de 30 jours ne peut plus être prolongé.** Le début de l'essai est désormais enregistré sur desktroo.fr, lié à une empreinte irréversible de votre NAS (un hachage : aucun identifiant matériel n'est transmis en clair). Supprimer un fichier ou reculer l'horloge ne rend plus de jours. Si desktroo.fr est injoignable au premier lancement, Desktroo vous laisse 3 jours provisoires, le temps de joindre le serveur ; l'essai passe ensuite à 30 jours à compter du premier lancement. Les installations existantes gardent leur date d'essai.
  > Serveur : route `/trial` (plugin Desktroo Licenses 1.4.0), table `desktroo_trials`, jeton signé RSA `kind: trial`. NAS : `_lic_fingerprint` (UUID DMI, sinon GUID `boot-pool` + machine-id hôte), `_lic_trial_state` (jeton, mode provisoire, témoin `.dk-t0`, garde anti-recul d'horloge), `license_trial_sync`.

## 2.54.47 — 18 octobre 2026

### Amélioré
- **Les données des nouvelles apps vont dans votre dataset « apps ».** Au lieu d'un dataset à part (`desktroo-apps`), une app installée depuis Desktroo Apps range ses données dans `<pool>/apps/desktroo/<app>`, avec vos autres apps. Les apps déjà installées ne bougent pas, et des données gardées à la désinstallation sont retrouvées là où elles sont. Sans dataset « apps » sur le pool, l'ancien emplacement est conservé.
  > `_store_dataset` : le registre d'installation fait foi pour une app existante ; sinon `apps/desktroo/<app>` si `<pool>/apps` est un dataset ZFS. Création en cascade (`apps/desktroo`, puis l'app).
- **Les données des apps ne sont jamais proposées à une autre app.** Le dossier `apps/desktroo` et le dataset `apps` qui le contient sont retirés du choix des dossiers du NAS : une app qui lit « tout le NAS » ne voit pas les coffres et certificats des autres.

## 2.54.46 — 18 octobre 2026

### Corrigé
- **Connexion depuis un téléphone Android.** L'autoremplissage du téléphone pouvait recopier le mot de passe dans le champ « Clé API » : la clé, prioritaire, faisait refuser la connexion (« Identifiants TrueNAS incorrects »). Ce champ n'est plus rempli automatiquement, il est ignoré s'il reproduit le mot de passe, et une clé refusée alors qu'un mot de passe est saisi est remplacée par un second essai avec le mot de passe.
  > Champ `login-apikey` : `type="text"` masqué par `-webkit-text-security`, `autocomplete="off"` et attributs d'exclusion des gestionnaires de mots de passe.
- **Collage d'identifiants plus tolérant.** Les caractères invisibles et les espaces en bout de mot de passe, fréquents après un copier-coller, sont retirés ; si TrueNAS refuse, Desktroo retente une fois avec le texte exactement collé. Le clavier n'ajoute plus de majuscule ni de correction automatique dans ces champs.

### Amélioré
- **Desktroo Apps affiche sa version** : la version de Desktroo et la date du catalogue, en bas de la colonne de gauche.
- **Installer une app seule : une explication au lieu d'une page d'erreur** quand Desktroo est lui-même ouvert comme une application installée. Chrome ne permet pas l'installation depuis ce type de fenêtre : il faut ouvrir Desktroo dans le navigateur.

## 2.54.44 — 17 octobre 2026

### Nouveau
- **Chaque application Desktroo s'installe comme une vraie application de l'ordinateur.** « Desktroo Fichiers », « Desktroo Jellyfin », « Desktroo Antivirus »… : une fenêtre à elle, son icône sur le bureau Windows et dans la barre des tâches, sans onglet ni barre d'adresse autour. Un clic droit sur une application du tiroir, le bouton d'installation de la barre d'adresse de Fichiers, ou la liste « Une application seule » du Panneau de configuration, ouvre la page d'installation.
  > Adresse `/k/<app>/` avec son propre manifeste (`/fileops/store/kiosk-manifest/<app>`) : l'`id` et la portée sont propres à chaque application, ce qui évite qu'une application installée « capture » les autres. Les applications du tiroir (`dkapp-<id>`) sont ramenées à `<id>`.
- **De belles icônes d'application.** Windows ne sait pas fabriquer un raccourci à partir d'une icône SVG (il affichait un carré gris avec une lettre) : Desktroo dessine des icônes PNG nettes à partir de celles de chaque application et le NAS les range pour les manifestes.
  > PNG 192 et 512 générés par le navigateur (canvas) et envoyés à `/store/kiosk-icon`, cache `kiosk-icons`.
- **En mode application, rien d'autre que l'application.** Le bureau, ses icônes et ses widgets sont masqués ; un petit rond de chargement s'affiche le temps de la connexion, puis l'application s'ouvre seule.
- **Tâches : « Effacer la liste ».** Dans la fenêtre des tâches (onglets Échecs et Terminées), un bouton masque les tâches déjà vues et remet le compteur à zéro. Un nouvel échec réapparaît normalement.
  > TrueNAS ne permet pas de supprimer l'historique des tâches : le masquage est mémorisé dans ce navigateur.
- **Adresse directe des applications.** Les applications qui s'ouvrent dans leur propre adresse affichent une ligne « Adresse directe » avec un bouton pour la copier.
- **Antivirus : quarantaine plus lisible.** La liste montre la taille et le dossier d'origine de chaque fichier, et la confirmation se fait sur place (nouvelle version 1.0.3 de l'application).

### Corrigé
- **Fichiers s'ouvre bien seul en mode application.** La fenêtre était demandée avant d'être construite et restait fermée.
- **Jellyfin : texte illisible sur le thème clair** dans la fiche d'un titre.

## 2.54.38 — 17 octobre 2026

### Nouveau
- **Jellyfin : votre médiathèque familiale, depuis Desktroo Apps.** Jellyfin classe vos films, séries, musiques et vidéos du NAS, récupère les affiches et les résumés, et les diffuse sur la télé, le téléphone, la tablette ou le navigateur. Une fenêtre Desktroo remplace son interface d'administration : vos bibliothèques dans la colonne de gauche, un accueil avec les derniers ajouts de chacune, la recherche, la fiche détaillée de chaque titre (résumé, acteurs, saisons et épisodes) et vos favoris.
  > Fiche `jellyfin` (image officielle fixée par empreinte, Jellyfin 12.2). Le NAS crée lui-même le compte administrateur au premier démarrage et pilote l'API de Jellyfin ; le mot de passe reste sur le NAS (`/store/jellyfin/api`).
- **Regardez vos films dans la fenêtre Desktroo, sans ouvrir Jellyfin.** La lecture reprend là où vous vous étiez arrêté (même depuis un autre appareil), les épisodes s'enchaînent, et « Reprendre la lecture » et « Prochains épisodes » apparaissent sur l'accueil. Les formats que le navigateur ne lit pas (MKV, x265, son 5.1…) sont convertis à la volée.
  > Flux relayé par une adresse signée et temporaire (`/store/jellyfin/stream/<billet>`, Range pris en charge) : le navigateur n'a ni jeton Jellyfin ni accès direct au réseau local. Lecture directe pour mp4/webm en h264/vp9/av1 + aac/mp3/opus, sinon `stream.mp4` transcodé (h264 + aac). Position enregistrée par `/Sessions/Playing*`.
- **Comptes de la famille avec limite d'âge**, créés et réglés depuis la fenêtre : un compte enfant ne voit que les titres adaptés.
- **Reprendre un ancien serveur Jellyfin.** Après une restauration de sauvegarde, la fenêtre repère les dossiers de bibliothèque devenus introuvables (ex. `/media/series`) et propose, en un clic, le dossier du NAS qui leur correspond.
  > Action `repair` : correspondance par nom avec les dossiers `/nas/…` ; remplacement par `POST/DELETE /Library/VirtualFolders/Paths`, puis analyse.
- **Sous-titres et pistes audio** dans le lecteur : choix de la langue du son et des sous-titres en cours de lecture, et Desktroo retient votre langue préférée. Les sous-titres image (PGS, VobSub) sont incrustés dans la vidéo.
  > Sous-titres texte relayés en WebVTT (`/store/jellyfin/sub/<billet>`) ; piste audio ou sous-titre image : relance en conversion à la même position.
- **Lecteur plus complet** : vitesse de lecture, bouton « Épisode suivant », « Passer le générique » et « Passer le résumé » quand Jellyfin les a repérés, raccourcis clavier (espace, flèches, F, M, N, Échap) et, pour la musique, la pochette de l'album.
- **Favoris, Historique, Listes de lecture et recherche partout** dans la colonne de gauche ; dans chaque bibliothèque, tri, filtre (vus, non vus, favoris) et filtre par genre.
- **Fiche d'un titre** : « Ajouter à une liste », « Corriger l'identification » (choix du bon titre parmi les résultats) et « Actualiser » les informations.
- **Sauvegardes de Jellyfin depuis la fenêtre** : créer une sauvegarde à la demande, voir la liste et restaurer (comptes, réglages, historique).
  > Sauvegarde intégrée de Jellyfin 10.11+ (`/Backup`).
- **Activité** : séances en cours (avec arrêt de la lecture), appareils connectés (révocation), journal d'activité et dernières erreurs du serveur.
- **Comptes** : choix des bibliothèques visibles par chaque compte et changement de mot de passe.
- **Publier Jellyfin sous un nom de domaine** depuis la fenêtre (onglet « Domaine »), avec NPMplus.

### Corrigé
- **Les liens « Ouvrir » des apps de Desktroo Apps mènent à l'adresse du NAS sur votre réseau**, et non plus au nom de domaine par lequel vous ouvrez Desktroo. C'est le cas de l'administration de NPMplus (port 8181), qui ne répondait pas par le domaine.
  > Nouvel assistant `dkNasHost` : IP du NAS donnée par `nas_ip`, sinon `truenas_ip` ; le nom de domaine n'est qu'un dernier recours.

## 2.54.19 — 17 octobre 2026

### Corrigé
- **La pastille ✓ « Sur le bureau » n'est plus coupée** sur les icônes des apps de Desktroo Apps (Domaines, Authenticator, Desktroo Pass, Antivirus, Uptime Kuma…), dans le menu des applications.
- **Les icônes de Desktroo Apps ont le même poids que celles du système** : dessins un peu plus petits et traits plus fins, au lieu d'icônes visiblement plus grosses et plus épaisses.
  > Les icônes du magasin sont retravaillées (dessin réduit à 78 %, traits ×0,7) ; elles arrivent avec la publication du magasin.

### Amélioré
- **Desktroo Apps sait accueillir de nouveaux types d'apps** qui lisent vos dossiers du NAS en lecture seule tout en gardant leurs propres droits internes.
  > Nouveau champ de fiche `nas_user_env` : le compte emprunté est transmis à l'app en variables d'environnement (UID/GID) au lieu de remplacer l'utilisateur du conteneur.

## 2.54.15 — 16 octobre 2026

### Nouveau
- **Uptime Kuma : surveillez vos sites et services depuis Desktroo Apps.** Uptime Kuma vérifie en continu que vos sites, serveurs et appareils répondent, et vous prévient par e-mail, Telegram, SMS ou une centaine d'autres moyens dès qu'un service tombe. Une fenêtre Desktroo montre l'état de tous vos services d'un coup d'œil : en ligne, hors ligne, temps de réponse, disponibilité sur 30 jours, jours restants avant l'expiration du certificat. Créer ou modifier un service se fait dans Uptime Kuma (« Administration complète »).
  > Fiche `uptime-kuma` (image officielle fixée par empreinte, base SQLite imposée). La fenêtre lit l'état au format Prometheus (`/metrics`) avec une clé API en lecture créée par l'utilisateur, gardée sur le NAS et jamais renvoyée au navigateur (`/store/uptime-kuma/api`).
- **Une pastille « Mise à jour disponible » pour les apps de Desktroo Apps, dans le gestionnaire d'applications.** Elle apparaît dans la liste, dans la colonne de gauche et sur la fiche de l'app, avec un bouton qui ouvre directement les mises à jour de Desktroo Apps.

### Amélioré
- **Fiches des apps plus lisibles.** Les descriptions sont présentées en paragraphes, en français comme en anglais.
- **Desktroo Apps se met à jour tout seul.** Les états (en marche, arrêtée, installée…) se rafraîchissent d'eux-mêmes : plus besoin de fermer et rouvrir la fenêtre pour voir un changement.
- **Compatibilité élargie des fiches.** Desktroo Apps accepte de nouveaux droits de conteneur (CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID) pour les apps qui en ont besoin.

### Corrigé
- **Installer une app sur un dossier de données déjà présent n'échoue plus** (erreur « Path … already exists » après une installation interrompue).
- **« Installée, mais elle ne répond pas encore » ne reste plus affiché** quand l'app répond bien.

## 2.54.0 — 16 octobre 2026

### Nouveau
- **VPN (WireGuard) : une fenêtre Desktroo dédiée.** Ajoutez un téléphone ou un ordinateur en quelques clics, affichez son QR code à scanner dans l'application WireGuard, récupérez ou copiez son fichier de configuration, activez, désactivez ou supprimez un appareil. La liste montre qui est connecté et le trafic de chacun. « Ouvrir » ouvre cette fenêtre ; l'administration complète de wg-easy reste accessible depuis son bouton.
  > Le NAS relaie les requêtes vers l'API de wg-easy (`/store/wireguard/api`, routes limitées aux appareils) avec le compte administrateur de la fiche : le navigateur ne le voit jamais.

## 2.53.4 — 16 octobre 2026

### Corrigé
- **VPN (WireGuard) : « Ouvrir » mène à l'administration** (adresse directe sur votre réseau local) au lieu de revenir à l'accueil du bureau.

## 2.53.3 — 16 octobre 2026

### Corrigé
- **VPN (WireGuard) : administration accessible** : l'interface de wg-easy ne peut pas s'afficher dans le bureau. Elle est maintenant joignable depuis votre réseau local, à l'adresse indiquée dans la fiche (port 51821, jamais ouvert sur Internet). Une fenêtre Desktroo dédiée la remplacera.
  > Un port publié peut désormais viser un autre port dans le conteneur (`to` dans le catalogue).

## 2.53.2 — 16 octobre 2026

### Corrigé
- **VPN (WireGuard) ne démarrait pas** : son interface réseau ne pouvait pas régler le pare-feu du conteneur. L'app reçoit maintenant le droit réseau qui lui manquait (NET_RAW), en plus de ceux déjà prévus. Si vous l'aviez installée en test, « Mettre à jour » corrige l'installation.

## 2.53.1 — 16 octobre 2026

### Amélioré
- **Test du canal bêta** : cette version ne change rien d'autre que son numéro. Elle sert à vérifier qu'un NAS de test reçoit bien les versions avant tout le monde.

## 2.53.0 — 16 octobre 2026

### Nouveau
- **VPN (WireGuard), en test** : rejoignez votre NAS et votre réseau depuis l'extérieur. À l'installation, Desktroo demande l'adresse publique de votre box, puis génère le compte administrateur (visible dans la fiche). Une fenêtre Desktroo dédiée suivra.
  > Moteur wg-easy (AGPL-3.0, image officielle figée par empreinte). Droits réseau étendus (NET_ADMIN, SYS_MODULE), transfert IP et dossier des modules du noyau en lecture seule ; port 51820 en UDP à rediriger vers le NAS.
- **Canal bêta pour les mises à jour** : un NAS de test (celui qui a déjà le fichier `data/store/beta`) reçoit les versions de la branche `beta` du dépôt de distribution, avant qu'elles soient publiées pour tout le monde. Les autres NAS ne voient rien.
  > `publish-dist.ps1 -Beta` publie sur la branche `beta` seulement ; la publication normale met à jour `main` puis réaligne `beta` dessus. Sans branche `beta`, le NAS de test suit le stable.
- **Réglages demandés avant l'installation** : une app du Magasin peut maintenant demander une valeur (adresse publique, par exemple), vérifiée avant de démarrer.
  > Champs `install_fields` du catalogue ; valeurs gardées sur le NAS ; secrets dérivés propres à chaque installation (`@derive:`).

### Amélioré
- **Fichiers : « Restaurer » dans la corbeille ouverte comme un dossier** : quand on parcourt `.desktroo-corbeille` directement, le clic droit propose maintenant de remettre l'élément à sa place d'origine.
  > Même restauration que la fenêtre Corbeille (emplacement d'origine vérifié, nom unique si besoin).

## 2.52.0 — 16 octobre 2026

### Nouveau
- **Relier un AdGuard Home existant** (sur une box, une machine virtuelle, un autre ordinateur) : la même fenêtre Desktroo le pilote à distance. Adresse, identifiant et mot de passe se saisissent dans la fiche d'AdGuard Home ; une icône apparaît au bureau.
  > Les identifiants restent sur le NAS (fichier en lecture réservée) ; le navigateur n'y a jamais accès. Seuls les chemins `/control/…` d'AdGuard sont relayés ; adresses du NAS lui-même, boucle locale et adresses internes refusées ; aucune redirection suivie.

### Corrigé
- **Desktroo Pass avec un domaine déjà configuré dans NPMplus** : « Ce domaine est déjà publié » bloquait l'étape 1. Le domaine est maintenant repris (certificat conservé). S'il pointe vers autre chose, Desktroo demande confirmation avant de le rediriger vers le coffre.

## 2.51.0 — 16 octobre 2026

### Nouveau
- **AdGuard Home a sa propre fenêtre Desktroo**, au même gabarit que les autres : Vue d'ensemble (requêtes, blocages, graphique, sites et appareils les plus actifs, pause de la protection), Journal des requêtes (avec « Bloquer » et « Autoriser » en un clic), Listes de blocage (ajout, activation, règles personnelles) et Réglages (serveurs DNS testés avant enregistrement, durée de conservation).
  > La fenêtre parle à l'API d'AdGuard par le proxy de Desktroo (session du bureau) : aucun mot de passe, aucun port ouvert. L'interface complète d'AdGuard reste accessible depuis les Réglages.

## 2.50.1 — 16 octobre 2026

### Corrigé
- **Une app qui offre un service au réseau (AdGuard Home) ne se heurte plus au réseau interne des machines virtuelles.** Son port DNS est ouvert sur l'adresse du NAS seulement, et non plus sur toutes les adresses : il cohabite avec le serveur DNS des machines virtuelles de TrueNAS.
- **Si un port demandé est vraiment pris, Desktroo dit par quel programme** (par exemple « 53 (pihole-FTL) »).

## 2.50.0 — 16 octobre 2026

### Nouveau
- **AdGuard Home dans Desktroo Apps (en test).** Un serveur DNS qui bloque publicités, traqueurs et sites dangereux pour tous les appareils du réseau : il suffit d'indiquer l'adresse du NAS comme serveur DNS dans votre box. Installé en un clic, sans assistant de démarrage ni seconde connexion.
  > Image officielle `adguard/adguardhome` non modifiée (GPL-3.0). Nouveau champ de catalogue `host_ports` : une app peut ouvrir des ports sur le réseau local (ici 53, TCP et UDP) ; Desktroo vérifie avant d'installer que le port est libre et le dit clairement sinon. Autres champs : `cap_add` (NET_BIND_SERVICE seulement) et `seed` (fichier de configuration de départ, écrit une seule fois).
- **Les fiches d'apps indiquent les ports ouverts sur le réseau et le logiciel libre utilisé**, avec un mot d'explication avant l'installation.

## 2.49.1 — 16 octobre 2026

### Amélioré
- **La liste des domaines publiés est plus claire** : un domaine par ligne, sa destination dessous, l'état du certificat à droite. Les boutons Renouveler, Désactiver et Retirer apparaissent quand on survole la ligne (toujours visibles sur tablette et téléphone).

## 2.49.0 — 16 octobre 2026

### Amélioré
- **L'Antivirus ne demande plus aucun réglage.** Installez-le : il analyse tous les dossiers du NAS, sans compte à choisir ni droits à vérifier. Il lit vos fichiers mais ne peut rien y modifier ; quand il trouve un fichier dangereux, c'est Desktroo qui le met en quarantaine, et qui le remet en place si vous le demandez.
  > L'app tourne sans aucun privilège sauf celui de lire, chaque dossier monté en lecture seule. Elle demande les déplacements à Desktroo, qui vérifie son identité et le chemin avant d'agir.
- **Un NPMplus installé sur une autre machine peut être relié à Desktroo** (box, machine virtuelle, autre ordinateur du réseau). Dans Desktroo Apps, fiche NPMplus, « Relier un NPMplus distant » : adresse de son administration et un compte administrateur. Vos domaines et certificats se gèrent ensuite dans la fenêtre Domaines, comme pour un NPMplus local ; les sites sont envoyés vers l'adresse de ce NAS.
  > Desktroo ne parle qu'à l'API de ce NPMplus, sans toucher à son installation. Le compte ne doit pas avoir la double authentification, et la machine doit pouvoir joindre le NAS.
- **Plus besoin de passer par TrueNAS pour gérer un compte.** Depuis Droits d'accès, vous modifiez le compte (nom, e-mail, groupes, dossier personnel), changez son mot de passe et gérez les membres d'un groupe, dans la fenêtre Utilisateurs de Desktroo.
- **Le choix « lecture seule » ne se coche plus par mégarde**, pour les apps qui demandent à modifier vos dossiers : l'accès se choisit dans une liste, juste sous le compte.
- **L'ombre des widgets se fond dans le bureau** au lieu de s'arrêter en lignes droites sur les bords des zones de widgets.
- **Les dossiers cachés du NAS ne sont plus proposés aux apps** (disques des conteneurs et des machines virtuelles).

## 2.48.0 — 15 octobre 2026

### Nouveau
- **Les apps de Desktroo Apps peuvent travailler sur vos dossiers du NAS**, quand elles en ont besoin. À l'installation, vous choisissez le compte du NAS dont l'app prend les droits et les dossiers qu'elle voit ; elle ne peut ouvrir que ce que ce compte peut ouvrir. Vous pouvez changer ce choix ensuite depuis sa fiche.
  > Le conteneur tourne avec l'identifiant et les groupes du compte choisi ; chaque dossier est monté sous /nas, en lecture seule si vous le demandez.

## 2.47.1 — 15 octobre 2026

### Amélioré
- **Desktroo Pass envoie les invitations par e-mail**, avec la messagerie déjà réglée pour les alertes du NAS. La personne reçoit un lien pour créer son compte ; une invitation peut être renvoyée.
  > Les messageries Gmail ou Outlook connectées par OAuth ne peuvent pas servir au coffre : il faut un serveur SMTP avec mot de passe. Sans messagerie, l'invitation reste possible : la personne crée son compte elle-même sur l'adresse du coffre.

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
