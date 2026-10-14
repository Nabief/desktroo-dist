#!/bin/bash
# Desktroo — © 2026 Mdm-services.fr. Tous droits réservés.
# Logiciel propriétaire, soumis à la licence d'utilisation de Desktroo (LICENSE.md, https://desktroo.fr).
# Copie, modification et redistribution interdites sans accord écrit. Cette mention ne doit pas être retirée.
# ============================================================
#  Desktroo — désinstallation
#  Retire Desktroo du NAS, quelle que soit la façon dont il a été installé
#  (install.sh, assistant web, ou Custom App de TrueNAS).
#
#  Usage, depuis le shell TrueNAS :
#    sudo bash uninstall.sh            questions puis confirmation
#    sudo bash uninstall.sh --plan     état des lieux, rien n'est modifié
#
#  Options :
#    --purge-db         supprime aussi les bases MariaDB et les réglages des sites web
#    --purge-settings   supprime aussi les réglages (comptes, préférences, 2FA, licence)
#    --purge-trash      vide aussi la corbeille Desktroo de chaque jeu de données
#    --purge-all        les trois
#    --yes              pas de question (pour un lancement automatique)
#    --no-release       ne libère pas la licence sur le serveur
#    --dir DOSSIER      dossier d'installation, s'il n'est pas détecté
#    --lang en          messages en anglais
#
#  Jamais touchés : le dossier des VM, les fichiers des sites web, les téléchargements,
#  et tout ce que Desktroo a créé DANS TrueNAS (sauvegardes, snapshots, partages,
#  applications du catalogue) : ces éléments appartiennent à TrueNAS et continuent
#  de fonctionner sans Desktroo.
# ============================================================
set -u

UNINSTALL_VERSION="1.0"

L="fr"
case "${DESKTROO_LANG:-${LANG:-}}" in en*) L="en" ;; esac

PLAN=0; YES=0; PURGE_DB=0; PURGE_TRASH=0; PURGE_SETTINGS=0; RELEASE=1; DELAY=0; DIR_ARG=""
ASKED_DB=0; ASKED_TRASH=0; ASKED_SETTINGS=0

usage() { sed -n '5,28p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --plan) PLAN=1 ;;
    --yes) YES=1 ;;
    --purge-db) PURGE_DB=1; ASKED_DB=1 ;;
    --purge-trash) PURGE_TRASH=1; ASKED_TRASH=1 ;;
    --purge-settings) PURGE_SETTINGS=1; ASKED_SETTINGS=1 ;;
    --purge-all) PURGE_DB=1; PURGE_TRASH=1; PURGE_SETTINGS=1; ASKED_DB=1; ASKED_TRASH=1; ASKED_SETTINGS=1 ;;
    --no-release) RELEASE=0 ;;
    --dir) shift; DIR_ARG="${1:-}" ;;
    --delay) shift; DELAY="${1:-0}" ;;
    --lang) shift; case "${1:-}" in en*) L="en" ;; *) L="fr" ;; esac ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Option inconnue / unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$DELAY" in ''|*[!0-9]*) DELAY=0 ;; esac
[ "$DELAY" -gt 120 ] && DELAY=120

say()  { if [ "$L" = "en" ]; then printf '%s\n' "$2"; else printf '%s\n' "$1"; fi; }
step() { if [ "$L" = "en" ]; then printf '▸ %s\n' "$2"; else printf '▸ %s\n' "$1"; fi; }
ok()   { if [ "$L" = "en" ]; then printf '  ✓ %s\n' "$2"; else printf '  ✓ %s\n' "$1"; fi; }
warn() { if [ "$L" = "en" ]; then printf '  ⚠ %s\n' "$2"; else printf '  ⚠ %s\n' "$1"; fi; }

hsize() {
  awk -v b="${1:-0}" -v l="$L" 'BEGIN {
    if (l == "en") split("B KB MB GB TB", u, " "); else split("o Ko Mo Go To", u, " ");
    i = 1; while (b >= 1024 && i < 5) { b /= 1024; i++ }
    if (i == 1) s = sprintf("%d %s", b, u[i]); else s = sprintf("%.1f %s", b, u[i]);
    if (l != "en") sub(/\./, ",", s);
    print s }'
}

# Espace réellement occupé sur le disque (compression ZFS comprise), en octets.
size_of() {
  local p total=0 k
  for p in "$@"; do
    [ -e "$p" ] || continue
    k="$(du -sk "$p" 2>/dev/null | awk '{print $1}')"
    total=$(( total + ${k:-0} * 1024 ))
  done
  echo "$total"
}

if [ "$(id -u)" != "0" ]; then
  say "Lancez ce script en administrateur : sudo bash $0" "Run this script as administrator: sudo bash $0"
  exit 1
fi

# ── Ce que Desktroo installe ─────────────────────────────────
KNOWN_CONTAINERS="desktroo desktroo-fileops desktroo-websites desktroo-php83 desktroo-php82 desktroo-php81 desktroo-php74 desktroo-mariadb desktroo-authelia desktroo-init"
APP_FILES="fileops.py desktroo.html vnc-viewer.html setup-truenas-host.sh update.sh update-web.sh setup-wizard.py app-init.sh install.sh uninstall.sh LICENSE.md RELEASES.md RELEASES.en.md version.txt docker-compose.yml docker-compose.authelia.yml nginx.conf nginx-desktop-plain.conf autostart.sh autostart.log .htpasswd desktroo-dl-extension.zip __pycache__"
DB_ITEMS="mariadb websites"
SETTINGS_ITEMS="data authelia wallpapers"
AUTOSTART_COMMENTS='("Desktroo autostart", "TrueNAS Desktop autostart")'

HAVE_DOCKER=0
command -v docker >/dev/null 2>&1 && HAVE_DOCKER=1

cenv() {   # variable d'environnement d'un conteneur
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null | sed -n "s/^$2=//p" | head -n 1
}
clabel() { # étiquette d'un conteneur (vide si absente)
  local v
  v="$(docker inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null)"
  [ "$v" = "<no value>" ] && v=""
  printf '%s' "$v"
}
has_word() { case " $1 " in *" $2 "*) return 0 ;; esac; return 1; }

# ── Détection ────────────────────────────────────────────────
DIR=""; VM_DIR=""; PROJECT=""; WORKDIR=""; MODE="none"; APP=""; LIC_SERVER=""
if [ "$HAVE_DOCKER" = 1 ] && docker inspect desktroo-fileops >/dev/null 2>&1; then
  DIR="$(cenv desktroo-fileops APP_DIR)"
  VM_DIR="$(cenv desktroo-fileops VM_DIR)"
  LIC_SERVER="$(cenv desktroo-fileops LICENSE_SERVER)"
  PROJECT="$(clabel desktroo-fileops com.docker.compose.project)"
  WORKDIR="$(clabel desktroo-fileops com.docker.compose.project.working_dir)"
fi
CONF=""
for c in /etc/desktroo/config.env /etc/truenas-desktop/config.env; do
  if [ -f "$c" ]; then CONF="$c"; break; fi
done
conf_get() { [ -n "$CONF" ] && sed -n "s/^$1=//p" "$CONF" | head -n 1; }
[ -z "$DIR" ] && DIR="$(conf_get INSTALL_DIR)"
[ -z "$VM_DIR" ] && VM_DIR="$(conf_get VM_DIR)"
[ -n "$DIR_ARG" ] && DIR="$DIR_ARG"
DIR="${DIR%/}"
LIC_SERVER="${LIC_SERVER:-https://desktroo.fr/wp-json/desktroo/v1}"

case "$PROJECT" in
  ix-*) MODE="app"; APP="${PROJECT#ix-}" ;;
  "")   if [ -n "$CONF" ] || { [ -n "$DIR" ] && [ -f "$DIR/docker-compose.yml" ]; }; then MODE="compose"; fi ;;
  *)    MODE="compose" ;;
esac

# Conteneurs : les noms connus, plus ceux du même projet compose (même dossier de travail).
CONTAINERS=""
if [ "$HAVE_DOCKER" = 1 ]; then
  for n in $KNOWN_CONTAINERS; do
    docker inspect "$n" >/dev/null 2>&1 && CONTAINERS="$CONTAINERS $n"
  done
  if [ -n "$PROJECT" ]; then
    for n in $(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}}' 2>/dev/null); do
      has_word "$CONTAINERS" "$n" && continue
      [ "$(clabel "$n" com.docker.compose.project.working_dir)" = "$WORKDIR" ] && CONTAINERS="$CONTAINERS $n"
    done
  fi
  # Authelia d'avant le renommage, installée dans le dossier du bureau.
  if [ -n "$DIR" ] && docker inspect truenas-authelia >/dev/null 2>&1; then
    case "$(clabel truenas-authelia com.docker.compose.project.working_dir)" in
      "$DIR"|"$DIR"/*) CONTAINERS="$CONTAINERS truenas-authelia" ;;
    esac
  fi
fi
CONTAINERS="${CONTAINERS# }"

# Images utilisées par ces conteneurs, et celles qu'un autre conteneur utilise aussi (gardées).
IMAGES=""; SHARED_IMAGES=""
if [ "$HAVE_DOCKER" = 1 ] && [ -n "$CONTAINERS" ]; then
  for n in $CONTAINERS; do
    id="$(docker inspect -f '{{.Image}}' "$n" 2>/dev/null)"
    [ -n "$id" ] && ! has_word "$IMAGES" "$id" && IMAGES="$IMAGES $id"
  done
  all="$(docker ps -aq 2>/dev/null)"
  if [ -n "$all" ]; then
    while read -r name id; do
      name="${name#/}"
      has_word "$CONTAINERS" "$name" && continue
      has_word "$IMAGES" "$id" && ! has_word "$SHARED_IMAGES" "$id" && SHARED_IMAGES="$SHARED_IMAGES $id"
    done < <(docker inspect -f '{{.Name}} {{.Image}}' $all 2>/dev/null)
  fi
fi

AUTOSTART_IDS=""
if command -v midclt >/dev/null 2>&1; then
  AUTOSTART_IDS="$(midclt call initshutdownscript.query 2>/dev/null | python3 -c "import sys, json
try:
    for e in json.load(sys.stdin):
        if e.get('comment') in $AUTOSTART_COMMENTS: print(e['id'])
except Exception:
    pass" 2>/dev/null | tr '\n' ' ')"
fi

ETC_DIRS=""
for e in /etc/desktroo /etc/truenas-desktop; do [ -e "$e" ] && ETC_DIRS="$ETC_DIRS $e"; done

# Le dossier d'installation n'est touché que s'il ressemble vraiment à une installation de Desktroo.
DIR_OK=0
if [ -n "$DIR" ] && [ -d "$DIR" ]; then
  case "$DIR" in
    /mnt/*/*)
      case "$DIR" in
        *'/../'*|*'/..'|*'/./'*) ;;
        *) if [ -f "$DIR/fileops.py" ] || [ -f "$DIR/desktroo.html" ] || [ -f "$DIR/data/install-id" ]; then DIR_OK=1; fi ;;
      esac ;;
  esac
fi

DATASET=""
if command -v zfs >/dev/null 2>&1 && [ -n "$DIR" ]; then
  DATASET="$(zfs list -H -o name,mountpoint -t filesystem 2>/dev/null | awk -F'\t' -v d="$DIR" '$2 == d { print $1; exit }')"
fi

size_in_dir() { local i total=0; for i in $1; do [ -e "$DIR/$i" ] && total=$(( total + $(size_of "$DIR/$i") )); done; echo "$total"; }
APPFILES_BYTES=0; DB_BYTES=0; SETTINGS_BYTES=0
if [ "$DIR_OK" = 1 ]; then
  APPFILES_BYTES="$(size_in_dir "$APP_FILES")"
  DB_BYTES="$(size_in_dir "$DB_ITEMS")"
  SETTINGS_BYTES="$(size_in_dir "$SETTINGS_ITEMS")"
fi

# Corbeille : un dossier caché à la racine de chaque jeu de données monté sous /mnt.
TRASH_LIST=""
if command -v zfs >/dev/null 2>&1; then
  while IFS= read -r mp; do
    case "$mp" in /mnt/*) [ -d "$mp/.desktroo-corbeille" ] && TRASH_LIST="$TRASH_LIST$mp/.desktroo-corbeille"$'\n' ;; esac
  done < <(zfs list -H -o mountpoint -t filesystem 2>/dev/null)
fi

ACTIVATION=0
[ "$DIR_OK" = 1 ] && [ -s "$DIR/data/activation-key" ] && [ -s "$DIR/data/install-id" ] && ACTIVATION=1

image_tag()  { docker image inspect -f '{{if .RepoTags}}{{index .RepoTags 0}}{{end}}' "$1" 2>/dev/null; }
image_size() { docker image inspect -f '{{.Size}}' "$1" 2>/dev/null; }

# ── --plan : état des lieux lisible par une machine, une valeur par ligne ──
if [ "$PLAN" = 1 ]; then
  echo "VERSION=$UNINSTALL_VERSION"
  echo "MODE=$MODE"
  echo "APP=$APP"
  echo "PROJECT=$PROJECT"
  echo "DIR=$DIR"
  echo "DIR_OK=$DIR_OK"
  echo "DATASET=$DATASET"
  echo "VM_DIR=$VM_DIR"
  echo "HOSTNAME=$(hostname 2>/dev/null)"
  for n in $CONTAINERS; do echo "CONTAINER=$n|$(docker inspect -f '{{.State.Status}}' "$n" 2>/dev/null)"; done
  for id in $IMAGES; do
    s=0; has_word "$SHARED_IMAGES" "$id" && s=1
    echo "IMAGE=$id|$(image_size "$id")|$s|$(image_tag "$id")"
  done
  for i in $AUTOSTART_IDS; do echo "AUTOSTART=$i"; done
  for e in $ETC_DIRS; do echo "ETC=$e"; done
  echo "APPFILES_BYTES=$APPFILES_BYTES"
  echo "DB_BYTES=$DB_BYTES"
  echo "SETTINGS_BYTES=$SETTINGS_BYTES"
  if [ -n "$TRASH_LIST" ]; then
    while IFS= read -r t; do [ -n "$t" ] && echo "TRASH=$(size_of "$t")|$t"; done <<< "$TRASH_LIST"
  fi
  echo "ACTIVATION=$ACTIVATION"
  echo "END=1"
  exit 0
fi

if [ "$MODE" = "none" ] && [ -z "$CONTAINERS" ] && [ "$DIR_OK" = 0 ] && [ -z "$AUTOSTART_IDS" ] && [ -z "$ETC_DIRS" ]; then
  say "Desktroo n'est pas installé sur ce NAS (rien trouvé)." "Desktroo is not installed on this NAS (nothing found)."
  [ -z "$DIR_ARG" ] && say "S'il est dans un dossier particulier : --dir /mnt/pool/apps/desktop" "If it lives in a specific folder: --dir /mnt/pool/apps/desktop"
  exit 3
fi

# ── Mode interactif : résumé, choix, confirmation ────────────
if [ "$YES" = 0 ]; then
  echo ""
  say "Désinstallation de Desktroo" "Uninstalling Desktroo"
  echo ""
  case "$MODE" in
    app)     say "  Installé comme application TrueNAS : $APP" "  Installed as a TrueNAS app: $APP" ;;
    compose) say "  Installé par le script d'installation" "  Installed with the install script" ;;
  esac
  [ -n "$DIR" ] && say "  Dossier : $DIR" "  Folder: $DIR"
  [ -n "$CONTAINERS" ] && say "  Conteneurs : $CONTAINERS" "  Containers: $CONTAINERS"
  echo ""
  ask_yn() { # $1 question FR, $2 question EN → 0 si oui
    local a
    if [ "$L" = "en" ]; then printf '%s [y/N] ' "$2"; else printf '%s [o/N] ' "$1"; fi
    read -r a || a=""
    case "$(printf '%s' "$a" | tr 'A-Z' 'a-z')" in o|oui|y|yes) return 0 ;; esac
    return 1
  }
  if [ "$ASKED_DB" = 0 ] && [ "$DIR_OK" = 1 ] && [ "$DB_BYTES" -gt 0 ]; then
    ask_yn "Supprimer aussi les bases MariaDB et les réglages des sites web ($(hsize "$DB_BYTES")) ?" \
           "Also delete the MariaDB databases and website settings ($(hsize "$DB_BYTES"))?" && PURGE_DB=1
  fi
  if [ "$ASKED_SETTINGS" = 0 ] && [ "$DIR_OK" = 1 ] && [ "$SETTINGS_BYTES" -gt 0 ]; then
    ask_yn "Supprimer aussi les réglages (comptes, préférences, 2FA, licence) ($(hsize "$SETTINGS_BYTES")) ?" \
           "Also delete the settings (accounts, preferences, 2FA, licence) ($(hsize "$SETTINGS_BYTES"))?" && PURGE_SETTINGS=1
  fi
  if [ "$ASKED_TRASH" = 0 ] && [ -n "$TRASH_LIST" ]; then
    tb=0
    while IFS= read -r t; do [ -n "$t" ] && tb=$(( tb + $(size_of "$t") )); done <<< "$TRASH_LIST"
    ask_yn "Vider aussi la corbeille Desktroo ($(hsize "$tb")) ? Ces fichiers ne seront plus récupérables." \
           "Also empty the Desktroo bin ($(hsize "$tb"))? Those files can no longer be recovered." && PURGE_TRASH=1
  fi
  echo ""
  HN="$(hostname 2>/dev/null)"
  if [ "$L" = "en" ]; then printf 'To confirm, type the name of this NAS (%s): ' "$HN"; else printf 'Pour confirmer, tapez le nom de ce NAS (%s) : ' "$HN"; fi
  read -r typed || typed=""
  if [ -z "$typed" ] || { [ "$typed" != "$HN" ] && [ "$typed" != "${HN%%.*}" ]; }; then
    say "Nom différent : rien n'a été modifié." "Name does not match: nothing was changed."
    exit 1
  fi
fi

[ "$DELAY" -gt 0 ] && sleep "$DELAY"

echo ""
say "Désinstallation de Desktroo — $(date '+%Y-%m-%d %H:%M:%S')" "Uninstalling Desktroo — $(date '+%Y-%m-%d %H:%M:%S')"

# ── 1. Licence : libère le poste pour pouvoir réactiver ailleurs ──
if [ "$RELEASE" = 1 ] && [ "$ACTIVATION" = 1 ]; then
  step "Licence" "Licence"
  key="$(tr -d ' \r\n' < "$DIR/data/activation-key")"
  iid="$(tr -d ' \r\n' < "$DIR/data/install-id")"
  if ! printf '%s' "$key$iid" | grep -Eq '^[A-Za-z0-9-]+$'; then
    warn "clé ou identifiant illisible : licence non libérée" "unreadable key or id: licence not released"
  elif command -v curl >/dev/null 2>&1 && \
       curl -fsS -m 20 -H 'Content-Type: application/json' \
            -d "{\"activation_key\":\"$key\",\"install_id\":\"$iid\"}" "$LIC_SERVER/deactivate" >/dev/null 2>&1; then
    ok "licence libérée : elle peut être activée sur un autre NAS" "licence released: it can be activated on another NAS"
    rm -f "$DIR/data/license.json" "$DIR/data/license-refresh.json" "$DIR/data/activation-key"
  else
    warn "serveur de licence injoignable : écrivez à support@desktroo.fr pour libérer la licence" \
         "licence server unreachable: write to support@desktroo.fr to release the licence"
  fi
fi

# ── 2. Conteneurs ────────────────────────────────────────────
if [ "$MODE" = "app" ] && [ -n "$APP" ] && command -v midclt >/dev/null 2>&1; then
  step "Application TrueNAS « $APP »" "TrueNAS app “$APP”"
  if timeout 900 midclt call -j app.delete "$APP" '{"remove_images": false, "remove_ix_volumes": false}' >/dev/null 2>&1; then
    ok "application supprimée" "app deleted"
  else
    warn "suppression par TrueNAS en échec, les conteneurs sont retirés un par un" "TrueNAS could not delete it, removing containers one by one"
  fi
elif [ "$MODE" = "compose" ] && [ "$HAVE_DOCKER" = 1 ] && [ -n "$DIR" ] && [ -f "$DIR/docker-compose.yml" ]; then
  step "Arrêt des services" "Stopping services"
  ( cd "$DIR" && docker compose down --remove-orphans >/dev/null 2>&1 ) && ok "services arrêtés" "services stopped"
fi
if [ "$HAVE_DOCKER" = 1 ] && [ -n "$CONTAINERS" ]; then
  left=""
  for n in $CONTAINERS; do
    docker inspect "$n" >/dev/null 2>&1 || continue
    docker rm -f "$n" >/dev/null 2>&1 || left="$left $n"
  done
  if [ -z "$left" ]; then ok "conteneurs retirés : $CONTAINERS" "containers removed: $CONTAINERS"
  else warn "conteneurs restants :$left" "containers left:$left"; fi
  [ -n "$PROJECT" ] && docker network rm "${PROJECT}_default" >/dev/null 2>&1 || true
fi

# ── 3. Images Docker ─────────────────────────────────────────
if [ "$HAVE_DOCKER" = 1 ] && [ -n "$IMAGES" ]; then
  step "Images Docker" "Docker images"
  freed=0; kept=""
  for id in $IMAGES; do
    tag="$(image_tag "$id")"; sz="$(image_size "$id")"
    if has_word "$SHARED_IMAGES" "$id"; then kept="$kept ${tag:-$id}"; continue; fi
    if docker rmi "$id" >/dev/null 2>&1 || docker rmi -f "$id" >/dev/null 2>&1; then
      freed=$(( freed + ${sz:-0} ))
    else
      kept="$kept ${tag:-$id}"
    fi
  done
  ok "$(hsize "$freed") libérés" "$(hsize "$freed") freed"
  [ -n "$kept" ] && warn "gardées, car utilisées par d'autres applications :$kept" "kept, used by other apps:$kept"
fi

# ── 4. Démarrage automatique et configuration ────────────────
if [ -n "$AUTOSTART_IDS" ]; then
  step "Démarrage automatique" "Autostart"
  for i in $AUTOSTART_IDS; do midclt call initshutdownscript.delete "$i" >/dev/null 2>&1; done
  ok "tâche « Desktroo autostart » supprimée" "“Desktroo autostart” task deleted"
fi
for e in $ETC_DIRS; do rm -rf "$e" && ok "$e supprimé" "$e deleted"; done

# ── 5. Fichiers de l'application ─────────────────────────────
if [ "$DIR_OK" = 1 ]; then
  step "Fichiers dans $DIR" "Files in $DIR"
  for f in $APP_FILES; do rm -rf "${DIR:?}/$f"; done
  ok "fichiers de l'application supprimés" "application files deleted"
  if [ "$PURGE_DB" = 1 ]; then
    for f in $DB_ITEMS; do rm -rf "${DIR:?}/$f"; done
    ok "bases MariaDB et réglages des sites supprimés" "MariaDB databases and website settings deleted"
  fi
  if [ "$PURGE_SETTINGS" = 1 ]; then
    for f in $SETTINGS_ITEMS; do rm -rf "${DIR:?}/$f"; done
    ok "réglages supprimés" "settings deleted"
  fi
  rest="$(ls -A "$DIR" 2>/dev/null)"
  if [ -z "$rest" ]; then
    if [ -n "$DATASET" ]; then
      say "  Le jeu de données $DATASET est vide : supprimez-le dans TrueNAS (Stockage) si vous n'en avez plus besoin." \
          "  Dataset $DATASET is empty: delete it in TrueNAS (Storage) if you no longer need it."
    else
      rmdir "$DIR" 2>/dev/null && ok "$DIR supprimé" "$DIR deleted"
    fi
  else
    say "  Restent dans $DIR : $(printf '%s' "$rest" | tr '\n' ' ')" "  Left in $DIR: $(printf '%s' "$rest" | tr '\n' ' ')"
  fi
elif [ -n "$DIR" ]; then
  warn "$DIR ne ressemble pas à une installation de Desktroo : aucun fichier supprimé" \
       "$DIR does not look like a Desktroo install: no file deleted"
fi

# ── 6. Corbeille ─────────────────────────────────────────────
if [ "$PURGE_TRASH" = 1 ] && [ -n "$TRASH_LIST" ]; then
  step "Corbeille Desktroo" "Desktroo bin"
  tb=0
  while IFS= read -r t; do
    [ -n "$t" ] || continue
    case "$t" in /mnt/*/.desktroo-corbeille) ;; *) continue ;; esac
    s="$(size_of "$t")"
    rm -rf "$t" && tb=$(( tb + s ))
  done <<< "$TRASH_LIST"
  ok "$(hsize "$tb") libérés" "$(hsize "$tb") freed"
fi

echo ""
say "Desktroo est désinstallé." "Desktroo is uninstalled."
[ -n "$VM_DIR" ] && [ -d "$VM_DIR" ] && say "Les disques des machines virtuelles restent dans $VM_DIR." "Virtual machine disks remain in $VM_DIR."
say "Si des snapshots contiennent les fichiers supprimés, l'espace ne revient qu'à leur expiration." \
    "If snapshots hold the deleted files, the space only comes back when they expire."
say "L'abonnement n'est pas arrêté par la désinstallation : https://desktroo.fr pour le gérer." \
    "Uninstalling does not cancel the subscription: manage it at https://desktroo.fr."
echo "DONE"
exit 0
