#!/usr/bin/env python3
# =============================================================
#  Desktroo — Wizard d'installation web
#
#  Usage depuis le shell TrueNAS :
#    python3 /mnt/<pool>/apps/desktop/setup-wizard.py
#
#  Puis ouvrir dans le navigateur :
#    http://IP_TRUENAS:8099/setup
# =============================================================

import http.server
import json
import os
import re
import subprocess
import threading
import socket
import sys
import secrets
import shutil
import queue
import time
import urllib.parse

PORT = 8090
GITHUB_RAW_DEFAULT = 'https://raw.githubusercontent.com/Nabief/desktroo-dist/main'
INSTALL_EVENTS = queue.Queue()
INSTALL_RUNNING = False
INSTALL_DONE = False

# ── Auto-détection IP ─────────────────────────────────────────
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

# ── Détection des pools ZFS montés sous /mnt ──────────────────
def list_pools():
    pools = []
    _SKIP = {'ix-apps', 'ix-applications', 'ix-virt', 'lost+found'}
    try:
        for name in sorted(os.listdir('/mnt')):
            if name.startswith('.') or name in _SKIP:
                continue
            if os.path.isdir(os.path.join('/mnt', name)):
                pools.append(name)
    except Exception:
        pass
    return pools

# ── Vérification prérequis ────────────────────────────────────
def check_prerequisites():
    results = {}
    results['root'] = os.geteuid() == 0
    results['docker'] = shutil.which('docker') is not None
    results['python'] = sys.version_info >= (3, 6)
    results['openssl'] = shutil.which('openssl') is not None
    return results

# ── Génération token ──────────────────────────────────────────
def generate_token():
    return secrets.token_urlsafe(24)

# ── Installation ──────────────────────────────────────────────
INSTALL_LOG = '/tmp/tnd-install.log'


def emit(msg, level='info', secret=False):
    # secret=True : le message s'affiche à l'écran mais n'est PAS écrit dans le
    # journal persistant (évite d'y laisser un mot de passe en clair).
    INSTALL_EVENTS.put({'msg': msg, 'level': level})
    try:
        import time as _t
        _logmsg = '(masqué — non journalisé)' if secret else msg
        with open(INSTALL_LOG, 'a', encoding='utf-8') as f:
            f.write('%s [%s] %s\n' % (_t.strftime('%H:%M:%S'), level, _logmsg))
    except Exception:
        pass

def run_cmd(cmd, shell=True):
    proc = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        emit(line.rstrip())
    proc.wait()
    return proc.returncode

def _midclt(args, timeout=60):
    """Appelle le middleware TrueNAS. Retourne (code, stdout, stderr)."""
    try:
        p = subprocess.run(['midclt'] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()
    except Exception as e:
        return 1, '', str(e)


def configure_truenas(ssh_user):
    """Automatise les prérequis TrueNAS via midclt : SSH + auth mot de passe,
    et sudo NOPASSWD pour l'utilisateur. Non bloquant (avertit si échec)."""
    if not shutil.which('midclt'):
        emit('⚠ midclt introuvable — configure SSH/sudo manuellement.', 'warn')
        return
    emit('▸ Configuration TrueNAS (SSH + sudo) via middleware...', 'step')

    # 1. SSH : autoriser l'authentification par mot de passe
    rc, out, err = _midclt(['call', 'ssh.update', '{"passwordauth": true}'])
    emit('✓ SSH : auth par mot de passe activée' if rc == 0
         else f'⚠ ssh.update a échoué : {err or out}', 'ok' if rc == 0 else 'warn')

    # 2. SSH : activer le service au boot + démarrer
    _midclt(['call', 'service.update', 'ssh', '{"enable": true}'])
    rc, out, err = _midclt(['call', 'service.start', 'ssh'])
    emit('✓ Service SSH démarré' if rc == 0
         else f'⚠ Démarrage SSH : {err or out}', 'ok' if rc == 0 else 'warn')

    # 3. sudo NOPASSWD pour l'utilisateur SSH
    rc, out, err = _midclt(['call', 'user.query', f'[["username","=","{ssh_user}"]]'])
    uid = None
    if rc == 0 and out:
        try:
            data = json.loads(out)
            if data:
                uid = data[0].get('id')
        except Exception:
            pass
    if uid is not None:
        payload = '{"sudo_commands": ["ALL"], "sudo_commands_nopasswd": ["ALL"]}'
        rc, out, err = _midclt(['call', 'user.update', str(uid), payload])
        emit(f'✓ sudo sans mot de passe activé pour {ssh_user}' if rc == 0
             else f'⚠ user.update a échoué : {err or out}', 'ok' if rc == 0 else 'warn')
    else:
        emit(f'⚠ Utilisateur {ssh_user} introuvable — active le sudo NOPASSWD manuellement.', 'warn')

    # 4. SSH 25.x : autoriser le login par mot de passe pour le groupe de l'utilisateur.
    #    Sur TrueNAS 25.x, passwordauth=true ne suffit pas : le login mot de passe
    #    est verrouille par 'password_login_groups' (vide => personne ; sshd met
    #    PasswordAuthentication no hors bloc Match Group). On ajoute le groupe
    #    principal de l'utilisateur SSH. Non bloquant (champ absent en < 25.x).
    try:
        grp_name = None
        rc, out, _ = _midclt(['call', 'user.query', f'[["username","=","{ssh_user}"]]'])
        u = (json.loads(out) or [{}])[0] if (rc == 0 and out) else {}
        g = u.get('group') or {}
        grp_name = g.get('bsdgrp_group') or g.get('group') or g.get('name')
        if not grp_name and g.get('id') is not None:
            rc, out, _ = _midclt(['call', 'group.query', json.dumps([["id", "=", g.get('id')]])])
            gg = (json.loads(out) or [{}])[0] if out else {}
            grp_name = gg.get('group') or gg.get('name')
        if grp_name:
            rc, out, _ = _midclt(['call', 'ssh.config'])
            cur = json.loads(out) if out else {}
            groups = list(cur.get('password_login_groups') or [])
            if grp_name not in groups:
                groups.append(grp_name)
                rc, out, err = _midclt(['call', 'ssh.update', json.dumps({"password_login_groups": groups})])
                if rc == 0:
                    _midclt(['call', 'service.restart', 'ssh'])
                    emit(f'✓ SSH : login mot de passe autorise pour le groupe {grp_name}', 'ok')
                else:
                    emit(f'⚠ password_login_groups non applique ({err or out}) — a regler en UI si besoin.', 'warn')
            else:
                emit(f'✓ SSH : groupe {grp_name} deja autorise (mot de passe)', 'ok')
    except Exception as _e:
        emit(f'⚠ Reglage password_login_groups ignore ({_e}).', 'warn')


def _ensure_dataset(mount_path):
    """Crée un vrai dataset ZFS (+ ses ancêtres) pour un chemin sous /mnt via
    midclt, afin qu'il apparaisse dans Storage. Retourne True si dataset(s) en
    place, False si on doit retomber sur un simple dossier."""
    if not shutil.which('midclt') or not mount_path.startswith('/mnt/'):
        return False
    ds = mount_path[len('/mnt/'):].strip('/')
    parts = ds.split('/')
    if len(parts) < 2:
        return False  # c'est le pool lui-même
    for i in range(2, len(parts) + 1):
        name = '/'.join(parts[:i])
        rc, out, _ = _midclt(['call', 'pool.dataset.query', json.dumps([["id", "=", name]])])
        exists = False
        try:
            exists = bool(json.loads(out))
        except Exception:
            exists = False
        if exists:
            continue
        # Ne pas écraser un dossier déjà rempli (installation existante)
        full = '/mnt/' + name
        if os.path.isdir(full) and os.listdir(full):
            emit(f'⚠ {full} existe déjà en dossier — conservé tel quel.', 'warn')
            return False
        rc, out, err = _midclt(['call', 'pool.dataset.create', json.dumps({"name": name})], timeout=120)
        if rc != 0:
            emit(f'⚠ Dataset {name} non créé ({err or out}) — dossier simple utilisé.', 'warn')
            return False
        emit(f'✓ Dataset créé : {name}', 'ok')
    return True


def run_install(config):
    global INSTALL_RUNNING, INSTALL_DONE, INSTALL_LOG
    INSTALL_RUNNING = True
    INSTALL_DONE = False
    try:
        open(INSTALL_LOG, 'w', encoding='utf-8').close()  # log neuf par install
    except Exception:
        pass

    try:
        install_dir  = config['install_dir']
        vm_dir       = config['vm_dir']
        iso_dir      = config['iso_dir']
        port         = config['port']
        truenas_ip   = config['truenas_ip']
        truenas_host = config['truenas_host']
        ssh_user     = config['ssh_user']
        ssh_pass     = config['ssh_pass']
        token        = config['token'] or generate_token()
        db_pass      = config.get('db_pass') or generate_token()

        # ── Sécurité : 2FA optionnelle ; desk_user / desk_pass sont le compte du portail (Authelia) ──
        desk_user   = config.get('desk_user') or 'admin'
        desk_pass   = config.get('desk_pass') or secrets.token_urlsafe(12)
        enable_2fa  = bool(config.get('enable_2fa'))
        domain_desktop = (config.get('domain_desktop') or '').strip().lower()
        domain_auth    = (config.get('domain_auth') or '').strip().lower()
        admin_email    = (config.get('admin_email') or 'admin@example.com').strip()
        npm_ip      = (config.get('npm_ip') or '').strip()
        smtp_host   = (config.get('smtp_host') or '').strip()
        smtp_port   = (config.get('smtp_port') or '465').strip()
        smtp_user   = (config.get('smtp_user') or '').strip()
        smtp_pass   = (config.get('smtp_pass') or '').strip()
        # ── 2FA : Authelia DERRIÈRE Nginx Proxy Manager (NPM) ──
        #   Authelia 4.39 exige des URLs HTTPS ; c'est NPM (Let's Encrypt) qui
        #   les fournit. L'assistant fait TOUT le côté NAS (secrets, hash,
        #   conteneur Authelia, nginx bureau sans barrière). Il reste à créer
        #   2 hôtes proxy dans NPM + les redirections DNS (affichés à la fin).
        #   Les 2 domaines doivent partager le même domaine parent.
        if enable_2fa and (not domain_desktop or not domain_auth or '.' not in domain_desktop):
            emit('⚠ 2FA demandée mais domaines manquants/invalides — 2FA désactivée.', 'warn')
            enable_2fa = False
        domain_parent = domain_desktop.split('.', 1)[1] if (enable_2fa and '.' in domain_desktop) else ''
        if enable_2fa and domain_parent and not domain_auth.endswith(domain_parent):
            emit('⚠ Les 2 domaines doivent partager le même domaine parent (%s) — 2FA désactivée.' % domain_parent, 'warn')
            enable_2fa = False

        if enable_2fa:
            # Bureau nginx SANS barrière locale : NPM + Authelia (en amont)
            # assurent l'authentification. Authelia est publié sur 9091 pour
            # que NPM (sur un autre hôte) puisse l'atteindre.
            _srv_auth = "    # Auth deleguee a NPM + Authelia (2FA) en amont."
            _s_exempt = ""
            _server_name = "_"
            _extra_server = ""
            _authelia_service = (
                "\n  authelia:\n"
                "    image: authelia/authelia:4.39\n"
                "    container_name: desktroo-authelia\n"
                "    restart: unless-stopped\n"
                "    ports:\n"
                "      - \"9091:9091\"\n"
                "    volumes:\n"
                "      - " + install_dir + "/authelia:/config\n"
                "    env_file:\n"
                "      - " + install_dir + "/authelia/secrets.env\n"
                "    environment:\n"
                "      TZ: \"Europe/Paris\"\n"
                "    healthcheck:\n"
                "      disable: true\n"
            )
        else:
            # Pas de barrière nginx : la connexion est vérifiée par fileops (voir SECURITE.md).
            _srv_auth = ""
            _s_exempt = ""
            _server_name = "_"
            _extra_server = ""
            _authelia_service = ""

        script_dir = os.path.dirname(os.path.abspath(__file__))

        # ── 1. Datasets / répertoires ─────────────────────────
        emit('▸ Création des datasets ZFS...', 'step')
        for d in (install_dir, vm_dir):
            if not _ensure_dataset(d):
                os.makedirs(d, exist_ok=True)  # repli : simple dossier
        try:
            os.chmod(vm_dir, 0o777)
        except Exception:
            pass
        # Sous-dossiers applicatifs (à l'intérieur du dataset d'install)
        for sub in ('websites/conf.d', 'websites/php/8.3/ini', 'websites/php/8.2/ini',
                    'websites/php/8.1/ini', 'websites/php/7.4/ini', 'mariadb'):
            os.makedirs(os.path.join(install_dir, sub), exist_ok=True)
        emit(f'✓ {install_dir}', 'ok')
        emit(f'✓ {vm_dir}', 'ok')

        # ── Journal persistant : /tmp → <install_dir>/install.log ──
        # (rapport d'install durable et découvrable ; on y recopie les 1res lignes)
        try:
            _persist = os.path.join(install_dir, 'install.log')
            _prev = ''
            try:
                with open(INSTALL_LOG, encoding='utf-8') as _lf:
                    _prev = _lf.read()
            except Exception:
                pass
            with open(_persist, 'w', encoding='utf-8') as _lf:
                _lf.write("===== Desktroo — journal d'installation =====\n")
                _lf.write(_prev)
            INSTALL_LOG = _persist
            emit('✓ Journal d\'installation : %s' % _persist, 'ok')
        except Exception:
            pass

        # ── 1b. Prérequis TrueNAS automatisés (SSH + sudo) ────
        configure_truenas(ssh_user)

        # ── 2. Sauvegarde config ──────────────────────────────
        emit('▸ Sauvegarde de la configuration...', 'step')
        os.makedirs('/etc/desktroo', exist_ok=True)
        config_content = f"""INSTALL_DIR={install_dir}
VM_DIR={vm_dir}
ISO_DIR={iso_dir}
PORT={port}
TRUENAS_IP={truenas_ip}
TRUENAS_HOST={truenas_host}
SSH_USER={ssh_user}
SSH_PASS={ssh_pass}
FILEOPS_TOKEN={token}
GITHUB_RAW={(config.get('github_raw') or GITHUB_RAW_DEFAULT).rstrip('/')}
"""
        with open('/etc/desktroo/config.env', 'w') as f:
            f.write(config_content)
        os.chmod('/etc/desktroo/config.env', 0o600)
        emit('✓ /etc/desktroo/config.env', 'ok')
        shutil.rmtree('/etc/truenas-desktop', ignore_errors=True)  # ancien emplacement (avant renommage)

        # ── 3. Récupération des fichiers (GitHub, sinon copie locale) ──
        emit('▸ Récupération des fichiers applicatifs...', 'step')
        # Un démarrage Docker précédent (avant que les fichiers existent) a pu
        # créer des DOSSIERS à la place des fichiers montés (bind-mount) → l'écriture
        # échouerait ensuite avec « Is a directory ». On retire ces dossiers parasites.
        for _bad in ('desktroo.html', 'vnc-viewer.html', 'fileops.py',
                     'nginx.conf', '.htpasswd', 'docker-compose.yml'):
            _bp = os.path.join(install_dir, _bad)
            if os.path.isdir(_bp):
                try:
                    shutil.rmtree(_bp)
                    emit('⚠ %s était un dossier (mount Docker d\'un essai précédent) — supprimé.' % _bad, 'warn')
                except Exception as _e:
                    emit('⚠ Impossible de supprimer le dossier parasite %s : %s' % (_bad, _e), 'warn')
        github_raw = (config.get('github_raw') or GITHUB_RAW_DEFAULT).rstrip('/')
        import urllib.request as _u
        for fname in ['fileops.py', 'desktroo.html', 'vnc-viewer.html']:
            dst = os.path.join(install_dir, fname)
            src = os.path.join(script_dir, fname)
            got = False
            if os.path.exists(src) and src != dst:
                try:
                    shutil.copy2(src, dst); got = True
                    emit(f'✓ {fname} (copié)', 'ok')
                except Exception:
                    pass
            if not got:
                try:
                    _u.urlretrieve(f'{github_raw}/{fname}', dst)
                    emit(f'✓ {fname} (téléchargé)', 'ok')
                except Exception as e:
                    emit(f'✗ Échec récupération {fname} : {e}', 'error')
                    raise RuntimeError(f'Impossible de récupérer {fname} depuis {github_raw}')

        # (4. Le jeton n'est plus inscrit dans la page : fileops le remet au navigateur
        #     après avoir vérifié les identifiants TrueNAS — voir SECURITE.md.)

        # ── 5. Génération docker-compose.yml ──────────────────
        emit('▸ Génération de docker-compose.yml...', 'step')
        # NB : '$$' dans ces commandes → docker compose écrit un '$' littéral dans le
        # YAML généré (sinon il substitue $v/$e/$last comme variables → watcher cassé).
        php_reload = (
            "apk add --no-cache curl >/dev/null 2>&1 || true; "
            "[ -x /usr/local/bin/install-php-extensions ] || { curl -sSLf https://github.com/mlocati/docker-php-extension-installer/releases/latest/download/install-php-extensions -o /usr/local/bin/install-php-extensions && chmod +x /usr/local/bin/install-php-extensions; }; "
            "[ -s /conf/extensions.txt ] && install-php-extensions $$(cat /conf/extensions.txt) || true; "
            "( last=''; lastext=''; while true; do e=$$(cat /conf/.extreload 2>/dev/null); if [ \"$$e\" != \"$$lastext\" ]; then lastext=\"$$e\"; { [ -s /conf/extensions.txt ] && install-php-extensions $$(cat /conf/extensions.txt) >/dev/null 2>&1; } || true; kill -USR2 1 2>/dev/null || true; fi; v=$$(cat /conf/.reload 2>/dev/null); if [ \"$$v\" != \"$$last\" ]; then last=\"$$v\"; kill -USR2 1 2>/dev/null || true; fi; sleep 3; done ) & exec php-fpm"
        )
        web_reload = "last=''; ( while true; do v=$$(cat /etc/nginx/conf.d/.reload 2>/dev/null); if [ \"$$v\" != \"$$last\" ]; then last=\"$$v\"; nginx -t && nginx -s reload; fi; sleep 3; done ) & exec nginx -g 'daemon off;'"

        def php_service(ver):
            return f"""  php{ver.replace('.','')}:
    image: php:{ver}-fpm-alpine
    container_name: desktroo-php{ver.replace('.','')}
    restart: unless-stopped
    environment:
      PHP_INI_SCAN_DIR: ":/conf/ini"
    volumes:
      - /mnt:/mnt
      - {install_dir}/websites/php/{ver}:/conf
    command: {json.dumps(["sh","-c",php_reload])}
    depends_on:
      - fileops
"""

        compose = f"""services:
  desktroo:
    image: nginx:alpine
    container_name: desktroo
    restart: unless-stopped
    user: root
    ports:
      - "{port}:80"
    volumes:
      - {install_dir}/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - {install_dir}/desktroo.html:/usr/share/nginx/html/index.html:ro
      - {install_dir}/vnc-viewer.html:/usr/share/nginx/html/vnc-viewer.html:ro

    depends_on:
      - fileops

  fileops:
    image: python:3.11-alpine
    container_name: desktroo-fileops
    restart: unless-stopped
    user: root
    stdin_open: true
    tty: true
    environment:
      FILEOPS_TOKEN: "{token}"
      FILEOPS_PORT: "8765"
      FILEOPS_WS_PORT: "8766"
      HOST_BOOTSTRAP: "1"
      APP_DIR: "{install_dir}"
      GITHUB_RAW: "{github_raw}"
      TRUENAS_SSH_HOST: "{truenas_ip}"
      TRUENAS_SSH_USER: "{ssh_user}"
      TRUENAS_SSH_PASS: "{ssh_pass}"
      TRUENAS_SSH_PORT: "22"
      VM_DIR: "{vm_dir}"
      ISO_DIR: "{iso_dir}"
      WEB_CONF_DIR: "{install_dir}/websites/conf.d"
      WEB_PHP_VERSIONS: '{{"8.3":"desktroo-php83:9000","8.2":"desktroo-php82:9000","8.1":"desktroo-php81:9000","7.4":"desktroo-php74:9000"}}'
      WEB_PHP_DEFAULT: "8.3"
      WEB_PHP_DIR: "{install_dir}/websites/php"
      WEB_PROXY_PORT: "8080"
      DB_HOST: "mariadb"
      DB_PORT: "3306"
      DB_ROOT_PASSWORD: "{db_pass}"
    volumes:
      - /mnt:/mnt
      - {install_dir}/fileops.py:/app/fileops.py:ro
    command: sh -c "apk add --no-cache qemu-img ca-certificates && {{ apk add --no-cache p7zip libarchive-tools 2>/dev/null || true; apk add --no-cache unrar 2>/dev/null || true; }} && pip install websockets paramiko pymysql --break-system-packages -q && python /app/fileops.py"
    expose:
      - "8765"
      - "8766"

  websites:
    image: nginx:alpine
    container_name: desktroo-websites
    restart: unless-stopped
    ports:
      - "8080:8080"
      - "8100-8130:8100-8130"
    volumes:
      - /mnt:/mnt
      - {install_dir}/websites/conf.d:/etc/nginx/conf.d
    command: {json.dumps(["sh","-c",web_reload])}
    depends_on:
      - php83
      - php82
      - php81
      - php74

{php_service('8.3')}
{php_service('8.2')}
{php_service('8.1')}
{php_service('7.4')}
  mariadb:
    image: mariadb:11
    container_name: desktroo-mariadb
    restart: unless-stopped
    environment:
      MARIADB_ROOT_PASSWORD: "{db_pass}"
      MARIADB_AUTO_UPGRADE: "1"
    expose:
      - "3306"
    volumes:
      - {install_dir}/mariadb:/var/lib/mysql
{_authelia_service}"""
        with open(os.path.join(install_dir, 'docker-compose.yml'), 'w') as f:
            f.write(compose)
        # Sauvegarde du mot de passe DB dans la config
        try:
            with open('/etc/desktroo/config.env', 'a') as f:
                f.write(f'DB_ROOT_PASSWORD={db_pass}\n')
        except Exception:
            pass
        emit('✓ docker-compose.yml (stack complète)', 'ok')

        # ── 6. Génération nginx.conf ──────────────────────────
        emit('▸ Génération de nginx.conf...', 'step')
        nginx = f"""server {{
    listen 80;
    server_name {_server_name};
    client_max_body_size 20g;
    client_body_timeout 3600s;
    root /usr/share/nginx/html;
    index index.html;
    # ── Barrière d'authentification devant tout le bureau ────────────
    # Login exigé avant d'accéder à la page (qui contient le token) et aux
    # endpoints fileops / terminal / VNC. /s/ (partages publics) est exempté.
    # 2FA : déléguer à un portail (Authelia / authentik) via auth_request.
{_srv_auth}

    location / {{
        try_files $uri /index.html;
    }}

    location = /api/current {{
        proxy_pass            https://{truenas_ip}/api/current;
        proxy_http_version    1.1;
        proxy_set_header      Upgrade           $http_upgrade;
        proxy_set_header      Connection        "upgrade";
        proxy_set_header      Host              {truenas_host};
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
    }}

    location /s/ {{
        {_s_exempt}
        proxy_pass            http://fileops:8765/s/;
        proxy_http_version    1.1;
        proxy_set_header      Host $host;
        proxy_buffering       off;
        proxy_max_temp_file_size 0;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
        proxy_connect_timeout 30s;
    }}

    location /api/ {{
        proxy_pass          https://{truenas_ip}/api/;
        proxy_http_version  1.1;
        proxy_ssl_verify    off;
        proxy_ssl_server_name off;
        proxy_set_header    Host              {truenas_host};
        proxy_set_header    Authorization     $http_authorization;
        proxy_pass_header   Authorization;
        proxy_set_header    Cookie            $http_cookie;
        proxy_pass_header   Set-Cookie;
        proxy_connect_timeout 10s;
        proxy_read_timeout    30s;
    }}

    location /_download/ {{
        proxy_pass            https://{truenas_ip}/_download/;
        proxy_http_version    1.1;
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_set_header      Host {truenas_host};
        proxy_read_timeout    120s;
    }}

    location /fileops/ {{
        proxy_pass         http://fileops:8765/;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 30s;
    }}

    location /truenas-shell {{
        proxy_pass         http://fileops:8766;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}

    location /vnc-proxy {{
        proxy_pass         http://fileops:8766;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}

    location = /vnc-viewer {{
        alias /usr/share/nginx/html/vnc-viewer.html;
        default_type text/html;
        add_header Cache-Control "no-cache";
    }}

    location /websocket {{
        proxy_pass            https://{truenas_ip}/websocket;
        proxy_http_version    1.1;
        proxy_set_header      Upgrade           $http_upgrade;
        proxy_set_header      Connection        "upgrade";
        proxy_set_header      Host              {truenas_host};
        proxy_set_header      Authorization     $http_authorization;
        proxy_pass_header     Authorization;
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
    }}
}}{_extra_server}"""
        with open(os.path.join(install_dir, 'nginx.conf'), 'w') as f:
            f.write(nginx)
        emit('✓ nginx.conf', 'ok')

        # La barrière « Basic auth » du bureau n'existe plus : fileops vérifie lui-même les
        # identifiants TrueNAS avant de remettre son jeton (la 2FA, optionnelle, passe par
        # Authelia). On efface le .htpasswd qu'une installation antérieure a pu laisser.
        try:
            os.remove(os.path.join(install_dir, '.htpasswd'))
        except OSError:
            pass

        # ── Authelia (2FA) : secrets + hash + fichiers de config ──
        if enable_2fa:
            emit('▸ Configuration Authelia (2FA)...', 'step')
            adir = os.path.join(install_dir, 'authelia')
            os.makedirs(adir, exist_ok=True)
            # Secrets : on PRÉSERVE ceux déjà présents. Régénérer la clé de
            # chiffrement casserait la base Authelia (db.sqlite3) à chaque
            # réinstallation → conteneur en boucle. On ne génère que le manquant.
            _sfile = os.path.join(adir, 'secrets.env')
            _sec = {}
            if os.path.exists(_sfile):
                for _l in open(_sfile):
                    if '=' in _l and not _l.lstrip().startswith('#'):
                        _k, _v = _l.split('=', 1)
                        _sec[_k.strip()] = _v.rstrip('\n')
            for _k in ('AUTHELIA_SESSION_SECRET',
                       'AUTHELIA_STORAGE_ENCRYPTION_KEY',
                       'AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET'):
                if not _sec.get(_k):
                    _sec[_k] = secrets.token_hex(32)
            if smtp_pass:
                _sec['AUTHELIA_NOTIFIER_SMTP_PASSWORD'] = smtp_pass
            with open(_sfile, 'w') as f:
                for _k, _v in _sec.items():
                    f.write('%s=%s\n' % (_k, _v))
            try:
                os.chmod(_sfile, 0o600)
            except Exception:
                pass
            pw_hash = ''
            try:
                _out = subprocess.check_output(
                    ['docker', 'run', '--rm', 'authelia/authelia:4.39',
                     'authelia', 'crypto', 'hash', 'generate', 'argon2',
                     '--password', desk_pass],
                    stderr=subprocess.STDOUT, timeout=240).decode()
                _m = re.search(r'\$argon2id\$[^\s]+', _out)
                pw_hash = _m.group(0) if _m else ''
            except Exception as e:
                emit('⚠ Hash Authelia non généré (%s) — à compléter dans users_database.yml' % e, 'warn')
            with open(os.path.join(adir, 'users_database.yml'), 'w') as f:
                f.write('users:\n')
                f.write('  %s:\n' % desk_user)
                f.write('    disabled: false\n')
                f.write("    displayname: '%s'\n" % desk_user)
                f.write("    password: '%s'\n" % (pw_hash or 'REMPLACE_PAR_LE_HASH_ARGON2ID'))
                f.write("    email: '%s'\n" % admin_email)
                f.write('    groups:\n      - admins\n')
            # Notifier : SMTP (email réel) si renseigné, sinon fichier local.
            if smtp_host and smtp_user and smtp_pass:
                _scheme = 'submissions' if str(smtp_port) == '465' else 'submission'
                _notifier_yaml = (
                    "notifier:\n  smtp:\n"
                    "    address: '%s://%s:%s'\n" % (_scheme, smtp_host, smtp_port) +
                    "    username: '%s'\n" % smtp_user +
                    "    sender: 'Desktroo <%s>'\n" % smtp_user +
                    "    subject: '[Authelia] {title}'\n"
                )
                emit('✓ Email (SMTP) configuré : %s via %s:%s' % (smtp_user, smtp_host, smtp_port), 'ok')
            else:
                _notifier_yaml = "notifier:\n  filesystem:\n    filename: '/config/notification.txt'\n"
            cfg = (
                "theme: 'dark'\n"
                "log:\n  level: 'info'\n"
                "server:\n  address: 'tcp://:9091'\n"
                "totp:\n  issuer: 'Desktroo'\n  period: 30\n"
                "authentication_backend:\n  file:\n    path: '/config/users_database.yml'\n"
                "access_control:\n  default_policy: 'deny'\n  rules:\n"
                "    - domain: '%s'\n      policy: 'two_factor'\n" % domain_desktop +
                "session:\n  cookies:\n"
                "    - name: 'authelia_session'\n"
                "      domain: '%s'\n" % domain_parent +
                "      authelia_url: 'https://%s'\n" % domain_auth +
                "      default_redirection_url: 'https://%s'\n" % domain_desktop +
                "      expiration: '1h'\n      inactivity: '15m'\n"
                "storage:\n  local:\n    path: '/config/db.sqlite3'\n" +
                _notifier_yaml
            )
            with open(os.path.join(adir, 'configuration.yml'), 'w') as f:
                f.write(cfg)
            # ── Snippets NPM (à monter dans le conteneur NPM sous /snippets) ──
            npmd = os.path.join(adir, 'npm')
            os.makedirs(npmd, exist_ok=True)
            with open(os.path.join(npmd, 'authelia-location.conf'), 'w') as f:
                f.write(
                    "## Authelia - endpoint interne d'autorisation (niveau server)\n"
                    "set $upstream_authelia http://%s:9091/api/authz/auth-request;\n" % truenas_ip +
                    "location /internal/authelia/authz {\n"
                    "    internal;\n"
                    "    proxy_pass $upstream_authelia;\n"
                    "    proxy_set_header X-Original-Method $request_method;\n"
                    "    proxy_set_header X-Original-URL $scheme://$http_host$request_uri;\n"
                    "    proxy_set_header X-Forwarded-For $remote_addr;\n"
                    "    proxy_set_header Content-Length \"\";\n"
                    "    proxy_set_header Connection \"\";\n"
                    "    proxy_pass_request_body off;\n"
                    "    proxy_next_upstream error timeout invalid_header http_500 http_502 http_503;\n"
                    "    proxy_redirect http:// $scheme://;\n"
                    "    proxy_http_version 1.1;\n"
                    "    proxy_cache_bypass $cookie_session;\n"
                    "    proxy_no_cache $cookie_session;\n"
                    "    proxy_buffers 4 32k;\n"
                    "    client_body_buffer_size 128k;\n"
                    "    send_timeout 5m;\n"
                    "    proxy_read_timeout 240;\n"
                    "    proxy_send_timeout 240;\n"
                    "    proxy_connect_timeout 240;\n"
                    "}\n"
                )
            with open(os.path.join(npmd, 'authelia-authrequest.conf'), 'w') as f:
                f.write(
                    "## Authelia - protege la location (a inclure DANS location /)\n"
                    "auth_request /internal/authelia/authz;\n"
                    "auth_request_set $user   $upstream_http_remote_user;\n"
                    "auth_request_set $groups $upstream_http_remote_groups;\n"
                    "auth_request_set $name   $upstream_http_remote_name;\n"
                    "auth_request_set $email  $upstream_http_remote_email;\n"
                    "proxy_set_header Remote-User   $user;\n"
                    "proxy_set_header Remote-Groups $groups;\n"
                    "proxy_set_header Remote-Name   $name;\n"
                    "proxy_set_header Remote-Email  $email;\n"
                    "auth_request_set $redirection_url $upstream_http_location;\n"
                    "error_page 401 =302 $redirection_url;\n"
                )
            with open(os.path.join(npmd, 'proxy.conf'), 'w') as f:
                f.write(
                    "## En-tetes proxy standard (a inclure DANS location /)\n"
                    "proxy_set_header Host              $host;\n"
                    "proxy_set_header X-Real-IP         $remote_addr;\n"
                    "proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;\n"
                    "proxy_set_header X-Forwarded-Proto $scheme;\n"
                    "proxy_set_header X-Forwarded-Host  $http_host;\n"
                    "proxy_set_header X-Forwarded-Uri   $request_uri;\n"
                    "proxy_set_header Upgrade           $http_upgrade;\n"
                    "proxy_set_header Connection        $connection_upgrade;\n"
                    "proxy_http_version 1.1;\n"
                    "proxy_read_timeout 3600;\n"
                    "proxy_send_timeout 3600;\n"
                    "send_timeout 3600;\n"
                    "client_max_body_size 20g;\n"
                )
            emit('✓ Authelia configuré (utilisateur: %s) — conteneur publié sur le port 9091' % desk_user, 'ok')
            emit('✓ Compte du portail 2FA — utilisateur: %s  mot de passe: %s' % (desk_user, desk_pass), 'ok', secret=True)
            emit('✓ Snippets NPM écrits dans %s' % npmd, 'ok')
            _npm = npm_ip or '<IP_de_NPM>'
            emit('===== A FAIRE DANS NPM (une seule fois) =====', 'step')
            emit("RECOMMANDE : NPMplus (fork de NPM) integre Authelia nativement -- plus simple que NPM standard.", 'step')
            emit("- Hote proxy PORTAIL : %s -> http %s port 9091 -- SSL Let's Encrypt + Force SSL + Websockets. Rien dans Advanced." % (domain_auth, truenas_ip), 'step')
            emit("- Hote proxy BUREAU : %s -> http %s port %s -- SSL + Force SSL + Websockets." % (domain_desktop, truenas_ip, port), 'step')
            emit("    NPMplus (recommande) : Auth Request = 'authelia (modern)', Auth Request Upstream = http://%s:9091 (sans chemin). Rien d'autre a monter." % truenas_ip, 'step')
            emit("    NPM standard : monte %s sous /snippets dans NPM, puis onglet Advanced :" % npmd, 'step')
            emit("      include /snippets/authelia-location.conf;", 'step')
            emit("      location / { include /snippets/proxy.conf; include /snippets/authelia-authrequest.conf; proxy_pass $forward_scheme://$server:$port; }", 'step')
            emit('━━━━━ DNS — pointer les 2 domaines vers l\'accès (comme tes autres services) ━━━━━', 'step')
            emit("   %s  →  %s      %s  →  %s" % (domain_desktop, _npm, domain_auth, _npm), 'step')
            if smtp_host and smtp_user and smtp_pass:
                emit("➤ Enrôlement TOTP : ouvre https://%s , connecte-toi (%s) ; le code de vérification est envoyé par email à %s." % (domain_desktop, desk_user, admin_email), 'step')
            else:
                emit("➤ Enrôlement TOTP : ouvre https://%s , connecte-toi (%s), scanne le QR — le code est dans %s/authelia/notification.txt" % (domain_desktop, desk_user, install_dir), 'step')

        # ── 7. Nettoyage des anciennes modifs /etc (réparation boot 25.x) ──
        emit('▸ Nettoyage des anciennes modifications systemd/libvirt...', 'step')
        _cleanup = (
            'systemctl disable truenas-desktop 2>/dev/null || true; '
            'rm -f /etc/systemd/system/truenas-desktop.service; '
            'rm -f /etc/systemd/system/libvirtd.service.d/notimeout.conf; '
            'rmdir /etc/systemd/system/libvirtd.service.d 2>/dev/null || true; '
            'rm -f /etc/tmpfiles.d/truenas-libvirt.conf; '
            'rm -f /etc/polkit-1/rules.d/80-truenas-libvirt.rules; '
            'systemctl daemon-reload 2>/dev/null || true'
        )
        run_cmd(_cleanup)

        # ── 7b. Migration depuis l'ancien nom « TrueNAS Desktop » ─────────
        # Réinstallation par-dessus une install antérieure au renommage : retire ce
        # qui porte encore l'ancien nom. Sans effet sur une installation neuve.
        # (Bloc supprimable une fois toutes les anciennes installs migrées.)
        _legacy = ['truenas-desktop', 'truenas-desktop-init', 'truenas-fileops', 'truenas-websites',
                   'truenas-php83', 'truenas-php82', 'truenas-php81', 'truenas-php74', 'truenas-mariadb']
        if enable_2fa:
            _legacy.append('truenas-authelia')  # recréé plus bas sous son nouveau nom
        run_cmd('for c in ' + ' '.join(_legacy) + '; do '
                'if docker container inspect "$c" >/dev/null 2>&1; then '
                'docker stop -t 30 "$c" >/dev/null 2>&1; docker rm "$c" >/dev/null 2>&1; '
                'echo "  ancien conteneur retiré : $c"; fi; done; true')
        # Les confs nginx des sites existants pointent encore sur les anciens conteneurs PHP :
        # sans cette réécriture, le nginx des sites refuse de démarrer (upstream introuvable).
        _confd = os.path.join(install_dir, 'websites', 'conf.d')
        try:
            for _fn in sorted(os.listdir(_confd)):
                if not _fn.endswith('.conf'):
                    continue
                _cp = os.path.join(_confd, _fn)
                with open(_cp, 'r', encoding='utf-8') as _cf:
                    _cc = _cf.read()
                if 'truenas-php' in _cc:
                    with open(_cp, 'w', encoding='utf-8') as _cf:
                        _cf.write(_cc.replace('truenas-php', 'desktroo-php'))
                    emit('  conf de site mise à jour : %s' % _fn)
        except OSError:
            pass

        # ── 8. Démarrage auto via Init/Shutdown Script TrueNAS (POSTINIT) ──
        # Remplace l'ancien service systemd (qui provoquait un « ordering cycle »
        # fatal sur TrueNAS 25.x). Un POSTINIT s'exécute APRÈS le middleware, hors
        # du chemin critique de boot : aucun impact sur ix-etc/middlewared.
        emit('▸ Configuration du démarrage automatique (POSTINIT)...', 'step')
        autostart = f"""#!/bin/bash
# Desktroo — demarrage POSTINIT (v1.4.0). Enregistre via midclt
# (initshutdownscript, when=POSTINIT). N'altere JAMAIS l'ordonnancement systemd.
set +e
INSTALL_DIR="{install_dir}"
LOG="$INSTALL_DIR/autostart.log"
echo "=== $(date '+%F %T') POSTINIT ===" >> "$LOG"
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
systemctl start libvirtd 2>/dev/null || systemctl start virtqemud 2>/dev/null || true
virsh -c qemu:///system net-start default >/dev/null 2>&1 || true
cd "$INSTALL_DIR" && /usr/bin/docker compose up -d >> "$LOG" 2>&1
echo "exit docker compose: $?" >> "$LOG"
"""
        autostart_path = os.path.join(install_dir, 'autostart.sh')
        # Étape non bloquante + tolérante aux ACL NFSv4 (aclmode restreint) : un
        # échec ici ne doit pas interrompre l'install (le bureau démarre quand même).
        try:
            try:
                if os.path.exists(autostart_path):
                    try:
                        os.remove(autostart_path)
                    except Exception:
                        pass
                with open(autostart_path, 'w') as f:
                    f.write(autostart)
            except OSError:
                # Dataset en ACL restreinte : on écrit via un fichier temporaire.
                import tempfile as _tf
                _fd, _tmp = _tf.mkstemp()
                with os.fdopen(_fd, 'w') as _tfh:
                    _tfh.write(autostart)
                shutil.move(_tmp, autostart_path)
            try:
                os.chmod(autostart_path, 0o755)  # inutile (lancé via 'bash'), ignoré si ACL refuse
            except OSError:
                pass
            if shutil.which('midclt'):
                _cmd = f'bash {autostart_path}'
                # L'ancien libellé est purgé aussi (install antérieure au renommage).
                for _cm in ("Desktroo autostart", "TrueNAS Desktop autostart"):
                    rc, out, err = _midclt(['call', 'initshutdownscript.query',
                                            json.dumps([["comment", "=", _cm]])])
                    try:
                        for _e in json.loads(out or '[]'):
                            _midclt(['call', 'initshutdownscript.delete', str(_e.get('id'))])
                    except Exception:
                        pass
                payload = json.dumps({"type": "COMMAND", "command": _cmd, "when": "POSTINIT",
                                      "enabled": True, "timeout": 300,
                                      "comment": "Desktroo autostart"})
                rc, out, err = _midclt(['call', 'initshutdownscript.create', payload])
                if rc == 0:
                    emit('✓ Démarrage auto configuré (Init/Shutdown Script POSTINIT)', 'ok')
                else:
                    emit(f'⚠ POSTINIT non créé ({err or out}) — à configurer en UI si besoin.', 'warn')
            else:
                emit('⚠ midclt introuvable — démarrage auto non configuré.', 'warn')
        except Exception as _pe:
            emit(f'⚠ Démarrage auto non configuré ({_pe}) — non bloquant, le bureau démarre quand même.', 'warn')

        # ── 9. Démarrage Docker ───────────────────────────────
        emit('▸ Démarrage de la stack Docker...', 'step')
        rc = run_cmd(f'cd {install_dir} && docker compose up -d --force-recreate')
        if rc == 0:
            emit('✓ Stack Docker démarrée', 'ok')
        else:
            emit('✗ Erreur démarrage Docker', 'error')
            emit('➤ Rapport d\'installation complet : %s' % INSTALL_LOG, 'error')
            INSTALL_RUNNING = False
            return

        emit('✓ Journal d\'installation : %s' % INSTALL_LOG, 'ok')
        emit(f'__DONE__{truenas_ip}:{port}', 'done')

    except Exception as e:
        import traceback as _tb
        emit('✗ Erreur : %s' % e, 'error')
        try:
            with open(INSTALL_LOG, 'a', encoding='utf-8') as _lf:
                _lf.write('\n----- TRACEBACK -----\n' + _tb.format_exc() + '\n')
        except Exception:
            pass
        emit('➤ Rapport d\'installation complet (à envoyer en cas de souci) : %s' % INSTALL_LOG, 'error')

    finally:
        INSTALL_RUNNING = False
        INSTALL_DONE = True


# ── HTML du wizard ────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Desktroo — Installation</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='t' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%231a2c4b'/%3E%3Cstop offset='1' stop-color='%230b1526'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='4' y='4' width='56' height='56' rx='15' fill='url(%23t)'/%3E%3Ccircle cx='26' cy='37' r='11' fill='none' stroke='%23e9eef7' stroke-width='6'/%3E%3Crect x='37' y='12' width='6.5' height='38' rx='3.25' fill='%23e9eef7'/%3E%3Ccircle cx='26' cy='37' r='4.6' fill='%233ecf8e'/%3E%3C/svg%3E">
<!--@FONTS@-->
<style>
  /* Charte Desktroo : mêmes couleurs, rayons et polices que le bureau (desktroo.html, « :root »).
     Toute évolution de la charte est à reporter ici. */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0a1220; --surface: #0f1a2e; --surface2: #16243f;
    --border: rgba(255,255,255,.09);
    --accent: #3ecf8e; --accent-rgb: 62,207,142; --accent2: #55dca0; --accent-ink: #04231a;
    --info: #5aa9ff;
    --success: #3ecf8e; --warn: #f59e0b; --error: #e05252;
    --text: #e9eef7; --dim: #9db0d0;
    --radius: 16px; --radius-sm: 11px;
    --font-body: "Figtree", system-ui, -apple-system, "Segoe UI", sans-serif;
    --font-display: "Schibsted Grotesk", system-ui, -apple-system, sans-serif;
    --font-mono: "JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
  }
  body { color: var(--text); font-family: var(--font-body);
         min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px;
         background:
           radial-gradient(60% 60% at 70% 0%, rgba(var(--accent-rgb),.16), transparent 70%),
           radial-gradient(50% 50% at 15% 10%, rgba(90,169,255,.14), transparent 70%),
           var(--bg);
         background-attachment: fixed; }
  button, input, select, textarea { font-family: inherit; }
  .card { background: rgba(15,26,46,.82); border: 1px solid var(--border); border-radius: 20px;
          width: 100%; max-width: 600px; overflow: hidden; box-shadow: 0 24px 60px rgba(0,0,0,.45);
          -webkit-backdrop-filter: blur(20px); backdrop-filter: blur(20px); }
  .header { padding: 36px 32px 26px; text-align: center; border-bottom: 1px solid var(--border); }
  .logo { width: 72px; height: 72px; margin: 0 auto 18px; }
  .logo svg { display: block; width: 100%; height: 100%; filter: drop-shadow(0 10px 22px rgba(0,0,0,.45)); }
  /* Le titre reprend le « oo » en accent du logo. */
  .header h1 { font-family: var(--font-display); font-size: 32px; font-weight: 800; letter-spacing: -0.03em; line-height: 1.1; }
  .dk-oo { color: var(--accent); }
  .header p  { color: var(--dim); font-size: 13px; margin-top: 6px; }
  /* Règles limitées à « .steps » : les lignes du journal portent aussi la classe « step ». */
  .steps { display: flex; padding: 24px 32px 20px; gap: 0; border-bottom: 1px solid var(--border); }
  .steps .step  { flex: 1; text-align: center; font-size: 11px; color: var(--dim); position: relative;
            display: flex; flex-direction: column; align-items: center; gap: 10px; }
  .steps .step::after { content: ''; position: absolute; bottom: 11px; left: calc(50% + 14px); right: calc(-50% + 14px);
                  height: 1px; background: var(--border); z-index: 0; }
  .steps .step:last-child::after { display: none; }
  .step-label { font-size: 11px; letter-spacing: 0.2px; }
  .step-dot { width: 24px; height: 24px; border-radius: 50%; background: var(--surface2);
               border: 2px solid var(--border); display: inline-flex; align-items: center;
               justify-content: center; font-size: 10px; font-weight: 700;
               position: relative; z-index: 1; flex-shrink: 0; }
  /* Étape en cours : aplat d'accent. Étapes franchies : contour, et trait de liaison teinté. */
  .step.active .step-dot  { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
  .step.done   .step-dot  { background: rgba(var(--accent-rgb),.14); border-color: rgba(var(--accent-rgb),.55); color: var(--accent2); }
  .steps .step.done::after { background: rgba(var(--accent-rgb),.45); }
  .steps .step.active { color: var(--text); }
  .body { padding: 32px; }

  /* Prérequis */
  .prereq { display: flex; align-items: center; gap: 12px; padding: 10px 0;
             border-bottom: 1px solid var(--border); }
  .prereq:last-child { border-bottom: none; }
  .prereq-icon { font-size: 18px; width: 24px; text-align: center; }
  .prereq-label { flex: 1; font-size: 14px; }
  .badge { font-size: 11px; padding: 3px 10px; border-radius: 20px; font-weight: 600; }
  .badge.ok   { background: rgba(var(--accent-rgb),.14); color: var(--accent2); }
  .badge.fail { background: rgba(224,82,82,.16);  color: #ff8585; }
  .badge.warn { background: rgba(245,158,11,.15); color: var(--warn); }

  /* Formulaire */
  .section-title { font-size: 11px; font-weight: 700; color: var(--dim); text-transform: uppercase;
                    letter-spacing: 1px; margin: 20px 0 12px; }
  .section-title:first-child { margin-top: 0; }
  .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .form-group { margin-bottom: 14px; }
  .form-group label { display: block; font-size: 12px; color: var(--dim); margin-bottom: 6px; font-weight: 500; }
  .form-group input { width: 100%; background: rgba(255,255,255,.07); border: 1px solid var(--border);
                       border-radius: var(--radius-sm); padding: 10px 14px; color: var(--text); font-size: 13px;
                       transition: border-color .2s, background .2s; outline: none; }
  .form-group input::placeholder { color: rgba(157,176,208,.55); }
  .form-group input:focus { border-color: var(--accent); background: rgba(var(--accent-rgb),.08); }
  .hint { font-size: 11px; color: var(--dim); margin-top: 4px; }

  /* Configuration écran par écran */
  .cfg-progress { display: flex; align-items: center; gap: 12px; margin-bottom: 22px; }
  .cfg-bar { flex: 1; display: flex; gap: 6px; }
  .cfg-bar span { flex: 1; height: 4px; border-radius: 2px; background: rgba(255,255,255,.1); transition: background .2s; }
  .cfg-bar span.done { background: rgba(var(--accent-rgb),.45); }
  .cfg-bar span.on   { background: var(--accent); }
  .cfg-count { font-size: 12px; color: var(--dim); font-variant-numeric: tabular-nums; }
  .slides { min-height: 248px; }
  .slide { animation: slide-in .22s ease both; }
  .slide.back { animation-name: slide-back; }
  @keyframes slide-in   { from { opacity: 0; transform: translateX(14px); }  to { opacity: 1; transform: none; } }
  @keyframes slide-back { from { opacity: 0; transform: translateX(-14px); } to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) { .slide { animation: none; } }
  .slide-title { font-family: var(--font-display); font-size: 19px; font-weight: 700; letter-spacing: -0.01em; margin-bottom: 18px; }
  .slide-title:has(+ .slide-lead) { margin-bottom: 6px; }
  .slide-lead { font-size: 13px; line-height: 1.5; color: var(--dim); margin-bottom: 20px; }
  .form-group input.invalid { border-color: var(--error); }
  .cfg-error { font-size: 13px; color: #ff8585; }
  .cfg-error:empty { display: none; }

  /* Log */
  .log { background: #070d18; border: 1px solid var(--border); border-radius: var(--radius-sm);
          padding: 16px; font-family: var(--font-mono);
          font-size: 12px; height: 320px; overflow-y: auto; line-height: 1.7; }
  .log::-webkit-scrollbar { display: none; }
  .log { scrollbar-width: none; }
  .log .step  { color: var(--info); }
  .log .ok    { color: var(--success); }
  .log .warn  { color: var(--warn); }
  .log .error { color: #ff8585; }
  .log .info  { color: #b6c4dc; }

  /* Succès */
  .success-box { text-align: center; padding: 20px 0; }
  .success-box .big-icon { font-size: 56px; margin-bottom: 16px; }
  .success-box h2 { font-family: var(--font-display); font-size: 22px; font-weight: 800;
                    letter-spacing: -0.02em; margin-bottom: 8px; }
  .success-box p  { color: var(--dim); font-size: 14px; }
  .open-btn { display: inline-block; margin-top: 20px; background: var(--accent);
               color: var(--accent-ink); padding: 12px 32px; border-radius: var(--radius-sm); text-decoration: none;
               font-family: var(--font-display); font-weight: 600; font-size: 15px; transition: opacity .2s; }
  .open-btn:hover { opacity: .9; }

  /* Boutons */
  .actions { display: flex; gap: 12px; justify-content: flex-end; margin-top: 24px; }
  .btn { padding: 11px 28px; border-radius: var(--radius-sm); border: none; cursor: pointer;
          font-family: var(--font-display); font-size: 14px; font-weight: 600;
          transition: opacity .2s, border-color .2s, color .2s; }
  .btn:hover { opacity: .9; }
  .btn-primary  { background: var(--accent); color: var(--accent-ink); }
  .btn-secondary{ background: rgba(255,255,255,.06); color: var(--dim); border: 1px solid var(--border); }
  .btn-secondary:hover { color: var(--text); border-color: rgba(255,255,255,.2); opacity: 1; }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn:focus-visible, .open-btn:focus-visible, .btn-browse:focus-visible, .dk-lang-btn:focus-visible,
  .modal-close:focus-visible { outline: 2px solid var(--accent2); outline-offset: 2px; }

  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(4,35,26,.25);
              border-top-color: var(--accent-ink); border-radius: 50%; animation: spin .7s linear infinite;
              margin-right: 8px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Pilule / interrupteur (toggle) */
  .form-group label.switch { display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; margin-bottom: 0; }
  .switch input { position: absolute; opacity: 0; width: 0; height: 0; }
  .switch .slider { display: inline-block; vertical-align: middle; width: 42px; height: 23px;
                    background: rgba(255,255,255,.07); border: 1px solid var(--border);
                    border-radius: 999px; position: relative; transition: .2s; flex-shrink: 0; }
  .switch .slider::before { content: ''; position: absolute; width: 17px; height: 17px; border-radius: 50%;
                    background: var(--dim); top: 2px; left: 2px; transition: .2s; }
  .switch input:checked + .slider { background: var(--accent); border-color: var(--accent); }
  .switch input:checked + .slider::before { transform: translateX(19px); background: #fff; }
  .switch input:focus-visible + .slider { box-shadow: 0 0 0 3px rgba(var(--accent-rgb),.35); }
  .switch-label { display: inline-block; vertical-align: middle; font-size: 13px; color: var(--text); font-weight: 500; }

  [hidden] { display: none !important; }

  /* Input avec bouton picker */
  .input-browse { display: flex; gap: 6px; }
  .input-browse input { flex: 1; }
  .btn-browse { background: rgba(255,255,255,.07); border: 1px solid var(--border); color: var(--dim);
                 border-radius: var(--radius-sm); padding: 0 12px; cursor: pointer; font-size: 16px;
                 transition: border-color .2s; flex-shrink: 0; }
  .btn-browse:hover { border-color: var(--accent); color: var(--text); }

  /* Modale navigateur */
  .modal-overlay { position: fixed; inset: 0; background: rgba(4,9,18,.72);
                    display: flex; align-items: center; justify-content: center; z-index: 100; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
            width: 480px; max-width: 95vw; max-height: 80vh; display: flex; flex-direction: column;
            box-shadow: 0 24px 60px rgba(0,0,0,.45); }
  .modal-header { padding: 16px 20px; border-bottom: 1px solid var(--border);
                   display: flex; align-items: center; gap: 10px; }
  .modal-header h3 { flex: 1; font-family: var(--font-display); font-size: 15px; font-weight: 700; }
  .modal-close { background: none; border: none; color: var(--dim); cursor: pointer;
                  font-size: 18px; padding: 2px 6px; border-radius: 4px; }
  .modal-close:hover { color: var(--text); }
  .modal-path { padding: 10px 20px; background: var(--surface2); font-size: 12px;
                 color: var(--dim); font-family: var(--font-mono); border-bottom: 1px solid var(--border); }
  .modal-list { flex: 1; overflow-y: auto; padding: 8px; scrollbar-width: none; }
  .modal-list::-webkit-scrollbar { display: none; }
  .modal-entry { display: flex; align-items: center; gap: 10px; padding: 9px 12px;
                  border-radius: 8px; cursor: pointer; font-size: 13px; }
  .modal-entry:hover { background: var(--surface2); }
  .modal-entry .icon { font-size: 16px; width: 20px; text-align: center; }
  .modal-footer { padding: 14px 20px; border-top: 1px solid var(--border);
                   display: flex; justify-content: flex-end; gap: 10px; }
  /* Sélecteur de langue */
  .dk-lang { display: flex; justify-content: center; gap: 6px; margin-top: 14px; }
  .dk-lang-btn { background: none; border: 1px solid transparent; border-radius: 999px; padding: 4px 12px;
                 font: inherit; font-size: 12px; color: var(--dim); cursor: pointer;
                 transition: color .15s, border-color .15s; }
  .dk-lang-btn:hover { color: var(--text); }
  .dk-lang-btn.active { color: var(--text); border-color: var(--border); cursor: default; }
</style>
<!--@I18N@-->
</head>
<body>
<div class="card">
  <div class="header">
    <div class="logo"><svg viewBox="4 4 56 56" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><defs><linearGradient id="dk-tile-g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1a2c4b"/><stop offset="1" stop-color="#0b1526"/></linearGradient></defs><rect x="4" y="4" width="56" height="56" rx="15" fill="url(#dk-tile-g)"/><rect x="4.25" y="4.25" width="55.5" height="55.5" rx="14.75" fill="none" stroke="#fff" stroke-opacity=".1" stroke-width=".5"/><circle cx="26" cy="37" r="11" fill="none" stroke="#e9eef7" stroke-width="6"/><rect x="37" y="12" width="6.5" height="38" rx="3.25" fill="#e9eef7"/><circle cx="26" cy="37" r="4.6" fill="#3ecf8e"/></svg></div>
    <h1 aria-label="Desktroo" translate="no">Desktr<span class="dk-oo">oo</span></h1>
    <p>Assistant d'installation</p>
    <div class="dk-lang" id="dk-lang-login" translate="no"></div>
  </div>

  <!-- Indicateur étapes -->
  <div class="steps">
    <div class="step active" id="s1"><span class="step-label">Prérequis</span><div class="step-dot" id="d1">1</div></div>
    <div class="step"        id="s2"><span class="step-label">Configuration</span><div class="step-dot" id="d2">2</div></div>
    <div class="step"        id="s3"><span class="step-label">Installation</span><div class="step-dot" id="d3">3</div></div>
    <div class="step"        id="s4"><span class="step-label">Terminé</span><div class="step-dot" id="d4">4</div></div>
  </div>

  <div class="body">

    <!-- Étape 1 : Prérequis -->
    <div id="page1">
      <div id="prereq-list">
        <div style="color:var(--dim);font-size:13px;">Vérification en cours...</div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" id="btn-next1" disabled onclick="goTo(2)">Continuer →</button>
      </div>
    </div>

    <!-- Étape 2 : Configuration -->
    <div id="page2" hidden>
      <div class="cfg-progress">
        <div class="cfg-bar" id="cfg-bar" role="progressbar" aria-valuemin="1"></div>
        <span class="cfg-count" id="cfg-count" translate="no"></span>
      </div>
      <div class="slides">
      <section class="slide" data-slide="paths">
        <h2 class="slide-title">📁 Chemins</h2>
        <p class="slide-lead">Les dossiers du NAS où Desktroo range ses fichiers, ses machines virtuelles et ses images ISO.</p>
      <div class="form-group">
        <label>Répertoire d'installation</label>
        <div class="input-browse">
          <input id="install_dir" value="/mnt/pool/apps/desktop" placeholder="/mnt/&lt;votre-pool&gt;/apps/desktop" />
          <button class="btn-browse" onclick="openBrowser('install_dir')" title="Parcourir">📁</button>
        </div>
        <div id="pool-hint" style="font-size:12px;color:var(--dim);margin-top:4px;"></div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Dossier VMs</label>
          <div class="input-browse">
            <input id="vm_dir" value="/mnt/pool/vms" />
            <button class="btn-browse" onclick="openBrowser('vm_dir')" title="Parcourir">📁</button>
          </div>
        </div>
        <div class="form-group">
          <label>Dossier ISOs (racine)</label>
          <div class="input-browse">
            <input id="iso_dir" value="/mnt/pool" />
            <button class="btn-browse" onclick="openBrowser('iso_dir')" title="Parcourir">📁</button>
          </div>
        </div>
      </div>
      </section>

      <section class="slide" data-slide="network" hidden>
        <h2 class="slide-title">🌐 Réseau</h2>
        <p class="slide-lead">L'adresse du NAS et le port sur lequel le bureau répondra.</p>
      <div class="form-row">
        <div class="form-group">
          <label>IP du TrueNAS</label>
          <input id="truenas_ip" placeholder="192.168.1.x" />
        </div>
        <div class="form-group">
          <label>Port du bureau</label>
          <input id="port" value="8099" />
        </div>
      </div>
      <div class="form-group">
        <label>Hostname / FQDN <span style="color:var(--dim)">(optionnel)</span></label>
        <input id="truenas_host" placeholder="même que l'IP si vide" />
        <div class="hint">Utilisé dans les en-têtes nginx. Laissez vide pour utiliser l'IP.</div>
      </div>
      </section>

      <section class="slide" data-slide="ssh" hidden>
        <h2 class="slide-title">🔐 Accès SSH</h2>
        <p class="slide-lead">Le compte TrueNAS avec lequel le bureau exécute ses commandes sur le NAS.</p>
      <div class="form-row">
        <div class="form-group">
          <label>Utilisateur SSH</label>
          <input id="ssh_user" value="truenas_admin" />
        </div>
        <div class="form-group">
          <label>Mot de passe SSH</label>
          <input id="ssh_pass" type="password" placeholder="••••••••" />
        </div>
      </div>
      </section>

      <section class="slide" data-slide="security" hidden>
        <h2 class="slide-title">🔑 Sécurité</h2>
      <div class="form-group">
        <label>Token sidecar</label>
        <input id="token" placeholder="Laissez vide pour générer automatiquement" />
        <div class="hint">Clé secrète du service fileops, remise au navigateur après la connexion.</div>
      </div>

      <div class="form-group">
        <label class="switch"><input type="checkbox" id="enable_2fa" onchange="cfgShow(cfgIndex)" /><span class="slider"></span><span class="switch-label">Activer la double authentification (2FA / Authelia)</span></label>
        <div class="hint">Ajoute un code TOTP. Nécessite 2 domaines locaux, demandés à l'étape suivante.</div>
      </div>
      </section>

      <section class="slide" data-slide="twofa" data-if="2fa" hidden>
        <h2 class="slide-title">🛡️ Double authentification</h2>
        <p class="slide-lead">Le compte avec lequel vous vous connecterez au portail de double authentification.</p>
        <div class="form-group">
          <label>Compte du portail 2FA — identifiant</label>
          <input id="desk_user" value="admin" />
        </div>
        <div class="form-group">
          <label>Compte du portail 2FA — mot de passe</label>
          <input id="desk_pass" type="password" placeholder="Laissez vide pour générer" />
          <div class="hint">Sans mot de passe saisi, l'assistant en génère un et l'affiche pendant l'installation.</div>
        </div>
        <div class="form-group">
          <label>E-mail administrateur</label>
          <input id="admin_email" placeholder="admin@exemple.fr" />
        </div>
      </section>

      <section class="slide" data-slide="domains" data-if="2fa" hidden>
        <h2 class="slide-title">🌍 Domaines</h2>
        <p class="slide-lead">Les deux adresses locales par lesquelles on atteint le bureau et le portail.</p>
        <div class="form-group">
          <label>Domaine du bureau</label>
          <input id="domain_desktop" placeholder="desktop.exemple.fr" />
        </div>
        <div class="form-group">
          <label>Domaine du portail 2FA</label>
          <input id="domain_auth" placeholder="auth.exemple.fr" />
        </div>
        <div class="form-group">
          <label>IP de Nginx Proxy Manager (NPM)</label>
          <input id="npm_ip" placeholder="192.168.1.2" />
          <div class="hint">Le HTTPS de la 2FA passe par NPM. Les 2 domaines pointeront vers cette IP.</div>
        </div>
        <div class="hint">L'assistant configure tout le côté NAS. Il reste ensuite à créer 2 hôtes proxy dans NPM + les redirections DNS vers l'IP de NPM — l'assistant affiche les valeurs exactes à la fin. L'enrôlement TOTP se fait après l'installation.</div>
      </section>

      <section class="slide" data-slide="mail" data-if="2fa" hidden>
        <h2 class="slide-title">✉️ Codes 2FA par e-mail</h2>
        <div class="form-group">
          <label class="switch"><input type="checkbox" id="enable_email" onchange="document.getElementById('emailfields').hidden=!this.checked;updateInstallBtn()" /><span class="slider"></span><span class="switch-label">Envoyer les codes 2FA par email (SMTP)</span></label>
          <div class="hint">Décoché : les codes 2FA sont écrits dans un fichier local (authelia/notification.txt) — le plus simple. Coché : renseigne et teste le SMTP ci-dessous.</div>
        </div>
        <div id="emailfields" hidden>
        <div class="form-row">
          <div class="form-group">
            <label>Serveur SMTP</label>
            <input id="smtp_host" placeholder="mail.exemple.fr" oninput="smtpChanged()" />
          </div>
          <div class="form-group">
            <label>Port</label>
            <input id="smtp_port" value="465" placeholder="465" oninput="smtpChanged()" />
          </div>
        </div>
        <div class="form-group">
          <label>Identifiant SMTP (adresse d'envoi)</label>
          <input id="smtp_user" placeholder="noreply@exemple.fr" oninput="smtpChanged()" />
        </div>
        <div class="form-group">
          <label>Mot de passe SMTP</label>
          <input id="smtp_pass" type="password" placeholder="Laisse vide pour envoyer les codes dans un fichier local" oninput="smtpChanged()" />
          <div class="hint">Si rempli : Authelia envoie les codes par email (port 465 = SSL, 587 = STARTTLS). Si vide : les codes sont écrits dans authelia/notification.txt.</div>
        </div>
        <div class="form-group">
          <button type="button" class="btn btn-secondary" id="btn-smtp-test" onclick="testSmtp()" style="width:100%;justify-content:center;">Tester le SMTP</button>
          <div id="smtp-status" class="hint" style="margin-top:6px;"></div>
          <div class="hint">Le test doit réussir avant de pouvoir installer (sinon l'enrôlement 2FA par email serait impossible).</div>
        </div>
        </div><!-- /emailfields -->
      </section>
      </div><!-- /slides -->

      <div class="cfg-error" id="cfg-error" role="alert"></div>
      <div class="actions">
        <button class="btn btn-secondary" onclick="cfgBack()">← Retour</button>
        <button class="btn btn-primary" id="cfg-next" onclick="cfgNext()">Continuer →</button>
        <button class="btn btn-primary" id="btn-install" onclick="startInstall()" hidden>Installer →</button>
      </div>
    </div>

    <!-- Étape 3 : Installation -->
    <div id="page3" hidden>
      <div class="log" id="log"></div>
      <div class="actions" style="margin-top:16px;">
        <button class="btn btn-secondary" id="btn-cancel" onclick="window.close()">Fermer</button>
        <button class="btn btn-primary" id="btn-finish" hidden onclick="goTo(4)">Continuer →</button>
      </div>
    </div>

    <!-- Étape 4 : Succès -->
    <div id="page4" hidden>
      <div class="success-box">
        <div class="big-icon">🎉</div>
        <h2>Installation réussie !</h2>
        <p>Desktroo est prêt.</p>
        <a id="open-link" href="#" class="open-btn" target="_blank">Ouvrir le bureau →</a>
      </div>
    </div>

  </div>
</div>

<!-- Modale navigateur de dossiers -->
<div class="modal-overlay" id="browser-modal" hidden>
  <div class="modal">
    <div class="modal-header">
      <h3>📁 Choisir un dossier</h3>
      <button class="modal-close" onclick="closeBrowser()">✕</button>
    </div>
    <div class="modal-path" id="browser-path">/mnt</div>
    <div class="modal-list" id="browser-list"></div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeBrowser()">Annuler</button>
      <button class="btn btn-primary"   onclick="selectCurrent()">Choisir ce dossier</button>
    </div>
  </div>
</div>

<script>
let currentPage = 1;

function goTo(n) {
  document.getElementById('page' + currentPage).hidden = true;
  document.getElementById('s'    + currentPage).classList.remove('active');
  if (n > currentPage) document.getElementById('s' + currentPage).classList.add('done');
  currentPage = n;
  document.getElementById('page' + n).hidden = false;
  document.getElementById('s'    + n).classList.add('active');
  if (n === 2) cfgShow(cfgIndex);
}

// ── Étape 2 : la configuration se déroule écran par écran ─────
// Chaque <section class="slide"> est un écran ; ceux marqués data-if="2fa" ne font
// partie du parcours que si la double authentification est activée.
var cfgIndex = 0;
function cfgSlides() {
  var twofa = document.getElementById('enable_2fa').checked;
  return Array.prototype.filter.call(document.querySelectorAll('#page2 .slide'), function (sl) {
    return sl.getAttribute('data-if') !== '2fa' || twofa;
  });
}
function cfgShow(i) {
  var list = cfgSlides(), all = document.querySelectorAll('#page2 .slide'), k, h = '';
  i = Math.max(0, Math.min(i, list.length - 1));
  list[i].classList.toggle('back', i < cfgIndex);
  cfgIndex = i;
  for (k = 0; k < all.length; k++) all[k].hidden = all[k] !== list[i];
  for (k = 0; k < list.length; k++) h += '<span class="' + (k < i ? 'done' : k === i ? 'on' : '') + '"></span>';
  var bar = document.getElementById('cfg-bar');
  bar.innerHTML = h;
  bar.setAttribute('aria-valuenow', i + 1); bar.setAttribute('aria-valuemax', list.length);
  document.getElementById('cfg-count').textContent = (i + 1) + ' / ' + list.length;
  var last = i === list.length - 1;
  document.getElementById('cfg-next').hidden = last;
  document.getElementById('btn-install').hidden = !last;
  cfgError('');
  updateInstallBtn();
}
// Ce qui manque sur un écran : [identifiant du champ, message], ou null si tout y est.
function cfgCheck(slide) {
  var name = slide.getAttribute('data-slide');
  if (name === 'paths' && !_v('install_dir')) return ['install_dir', T("Répertoire d'installation obligatoire")];
  if (name === 'network' && !_v('truenas_ip')) return ['truenas_ip', T('IP TrueNAS obligatoire')];
  if (name === 'ssh' && !_v('ssh_pass')) return ['ssh_pass', T('Mot de passe SSH obligatoire')];
  if (name === 'domains') {
    var miss = ['domain_desktop', 'domain_auth'].filter(function (id) { return _v(id).indexOf('.') === -1; })[0];
    if (miss) return [miss, T('Les deux domaines sont obligatoires pour la double authentification.')];
  }
  return null;
}
function cfgError(msg, id) {
  var marked = document.querySelectorAll('#page2 input.invalid'), k;
  for (k = 0; k < marked.length; k++) marked[k].classList.remove('invalid');
  document.getElementById('cfg-error').textContent = msg || '';
  var el = id && document.getElementById(id);
  if (el) { el.classList.add('invalid'); el.focus(); }
}
function cfgFocus() {
  var el = cfgSlides()[cfgIndex].querySelector('input:not([type=checkbox])');
  if (el) el.focus();
}
function cfgNext() {
  var bad = cfgCheck(cfgSlides()[cfgIndex]);
  if (bad) { cfgError(bad[1], bad[0]); return; }
  cfgShow(cfgIndex + 1); cfgFocus();
}
function cfgBack() {
  if (cfgIndex === 0) { goTo(1); return; }
  cfgShow(cfgIndex - 1); cfgFocus();
}
document.addEventListener('DOMContentLoaded', function () {
  var page = document.getElementById('page2');
  // Entrée dans un champ : écran suivant (jamais le lancement de l'installation).
  page.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || e.target.tagName !== 'INPUT' || e.target.type === 'checkbox') return;
    e.preventDefault();
    if (!document.getElementById('cfg-next').hidden) cfgNext();
  });
  page.addEventListener('input', function (e) {
    if (e.target.classList && e.target.classList.contains('invalid')) cfgError('');
  });
});

// ── Vérification SMTP avant install (2FA par email) ───────────
var smtpState = 'untested';
function _v(id){ var e = document.getElementById(id); return e ? e.value.trim() : ''; }
function _emailOn(){ var e = document.getElementById('enable_email'); return !!(e && e.checked); }
function smtpUsesEmail(){
  return _emailOn() && !!(_v('smtp_host') && _v('smtp_user') && document.getElementById('smtp_pass').value);
}
function smtpChanged(){
  smtpState = 'untested';
  var s = document.getElementById('smtp-status');
  if (s){ s.textContent = ''; s.style.color = ''; }
  updateInstallBtn();
}
function styleSmtpBtn(){
  var b = document.getElementById('btn-smtp-test');
  if (!b) return;
  var has = !!(_v('smtp_host') || _v('smtp_user') || document.getElementById('smtp_pass').value);
  if (smtpState === 'ok'){
    b.style.background = 'rgba(62,207,142,.14)'; b.style.borderColor = 'rgba(62,207,142,.55)'; b.style.color = 'var(--accent2)';
  } else if (has){
    b.style.background = 'rgba(245,158,11,.15)'; b.style.borderColor = 'var(--warn)'; b.style.color = 'var(--warn)';
  } else {
    b.style.background = ''; b.style.borderColor = ''; b.style.color = '';
  }
}
function updateInstallBtn(){
  styleSmtpBtn();
  var btn = document.getElementById('btn-install');
  if (!btn) return;
  var twofa = document.getElementById('enable_2fa').checked;
  var block = twofa && smtpUsesEmail() && smtpState !== 'ok';
  btn.disabled = block;
  btn.style.opacity = block ? '0.5' : '';
  btn.style.cursor  = block ? 'not-allowed' : '';
  btn.title = block ? T('Teste le SMTP (il doit réussir), ou laisse le mot de passe SMTP vide.') : '';
}
async function testSmtp(){
  var btn = document.getElementById('btn-smtp-test');
  var s   = document.getElementById('smtp-status');
  var host = _v('smtp_host'), port = _v('smtp_port') || '465', user = _v('smtp_user');
  var pass = document.getElementById('smtp_pass').value;
  if (!host || !user || !pass){
    s.textContent = T('Renseigne serveur, identifiant et mot de passe SMTP.');
    s.style.color = 'var(--warn)';
    return;
  }
  btn.disabled = true; var old = btn.textContent; btn.textContent = T('Test en cours…');
  s.textContent = ''; s.style.color = '';
  try {
    var r = await fetch('/smtp-test', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: host, port: port, user: user, pass: pass }) });
    var j = await r.json();
    if (j.ok){ smtpState = 'ok';
      s.textContent = T('✓ Connexion SMTP réussie (authentification OK).'); s.style.color = 'var(--success)'; }
    else { smtpState = 'fail';
      s.textContent = T('✗ Échec SMTP : ') + (j.error || T('inconnu')); s.style.color = '#ff8585'; }
  } catch(e){ smtpState = 'fail';
    s.textContent = T('✗ Échec SMTP : ') + e; s.style.color = '#ff8585'; }
  btn.disabled = false; btn.textContent = old;
  updateInstallBtn();
}

// ── Navigateur de dossiers ────────────────────────────────────
let _browserTarget = null;
let _browserPath   = '/mnt';

function openBrowser(inputId) {
  _browserTarget = inputId;
  const cur = document.getElementById(inputId).value.trim();
  browseTo(cur || '/mnt');
  document.getElementById('browser-modal').hidden = false;
}

function closeBrowser() {
  document.getElementById('browser-modal').hidden = true;
}

function selectCurrent() {
  if (_browserTarget) document.getElementById(_browserTarget).value = _browserPath;
  closeBrowser();
}

function browseTo(path) {
  _browserPath = path;
  document.getElementById('browser-path').textContent = path;
  const list = document.getElementById('browser-list');
  list.innerHTML = T('<div style="padding:20px;color:var(--dim);text-align:center">Chargement...</div>');
  fetch('/browse?path=' + encodeURIComponent(path))
    .then(r => r.json())
    .then(data => {
      _browserPath = data.path;
      document.getElementById('browser-path').textContent = data.path;
      if (!data.entries.length) {
        list.innerHTML = T('<div style="padding:20px;color:var(--dim);text-align:center">Dossier vide</div>');
        return;
      }
      list.innerHTML = '';
      data.entries.forEach(e => {
        const div = document.createElement('div');
        div.className = 'modal-entry';
        div.innerHTML = `<span class="icon">${e.type === 'parent' ? '↩' : '📁'}</span><span>${e.name}</span>`;
        div.onclick = () => browseTo(e.path);
        list.appendChild(div);
      });
    })
    .catch(function(err){
      list.innerHTML = T('<div style="padding:20px;color:var(--error);text-align:center">Erreur : ') + err + '</div>';
    });
}

// ── Étape 1 : prérequis ───────────────────────────────────────
fetch('/check').then(r => r.json()).then(data => {
  const icons = { root: '👤', docker: '🐳', python: '🐍', openssl: '🔑' };
  const labels = { root: T('Exécuté en root'), docker: T('Docker disponible'),
                   python: 'Python 3.6+', openssl: T('OpenSSL (génération token)') };
  let allOk = true;
  let html = '';
  for (const [k, ok] of Object.entries(data)) {
    const critical = k !== 'openssl';
    if (!ok && critical) allOk = false;
    const badge = ok ? '<span class="badge ok">✓ OK</span>'
                     : critical ? T('<span class="badge fail">✗ Manquant</span>')
                                : T('<span class="badge warn">⚠ Optionnel</span>');
    html += `<div class="prereq">
      <span class="prereq-icon">${icons[k]}</span>
      <span class="prereq-label">${labels[k]}</span>
      ${badge}
    </div>`;
  }
  document.getElementById('prereq-list').innerHTML = html;
  if (allOk) document.getElementById('btn-next1').disabled = false;

  // Auto-remplir l'IP
  fetch('/ip').then(r => r.text()).then(ip => {
    document.getElementById('truenas_ip').value = ip.trim();
  });

  // Auto-détecter le pool et pré-remplir les chemins
  fetch('/pools').then(r => r.json()).then(pools => {
    if (pools && pools.length) {
      const p = '/mnt/' + pools[0];
      document.getElementById('install_dir').value = p + '/apps/desktop';
      document.getElementById('vm_dir').value = p + '/vms';
      document.getElementById('iso_dir').value = p;
      if (pools.length > 1) {
        const hint = document.getElementById('pool-hint');
        if (hint) hint.textContent = T('Pools détectés : ') + pools.join(', ') + T(' — utilise 📁 pour en choisir un autre.');
      }
    }
  }).catch(() => {});
});

// ── Étape 3 : installation ─────────────────────────────────────
function startInstall() {
  const ip   = document.getElementById('truenas_ip').value.trim();
  const pass = document.getElementById('ssh_pass').value.trim();
  // Dernier contrôle de tous les écrans : on revient sur le premier qui est incomplet.
  var list = cfgSlides(), bad, k;
  for (k = 0; k < list.length; k++) {
    bad = cfgCheck(list[k]);
    if (bad) { cfgShow(k); cfgError(bad[1], bad[0]); return; }
  }
  if (document.getElementById('enable_2fa').checked && smtpUsesEmail() && smtpState !== 'ok') {
    alert(T('Teste le SMTP (il doit réussir) avant de lancer, ou laisse le mot de passe SMTP vide pour utiliser le fichier local.'));
    return;
  }

  const config = {
    install_dir:  document.getElementById('install_dir').value.trim(),
    vm_dir:       document.getElementById('vm_dir').value.trim(),
    iso_dir:      document.getElementById('iso_dir').value.trim(),
    port:         document.getElementById('port').value.trim(),
    truenas_ip:   ip,
    truenas_host: document.getElementById('truenas_host').value.trim() || ip,
    ssh_user:     document.getElementById('ssh_user').value.trim(),
    ssh_pass:     pass,
    token:        document.getElementById('token').value.trim(),
    desk_user:      document.getElementById('desk_user').value.trim(),
    desk_pass:      document.getElementById('desk_pass').value.trim(),
    enable_2fa:     document.getElementById('enable_2fa').checked,
    domain_desktop: document.getElementById('domain_desktop').value.trim(),
    domain_auth:    document.getElementById('domain_auth').value.trim(),
    admin_email:    document.getElementById('admin_email').value.trim(),
    npm_ip:         document.getElementById('npm_ip').value.trim(),
    smtp_host:      _emailOn() ? document.getElementById('smtp_host').value.trim() : '',
    smtp_port:      document.getElementById('smtp_port').value.trim(),
    smtp_user:      _emailOn() ? document.getElementById('smtp_user').value.trim() : '',
    smtp_pass:      _emailOn() ? document.getElementById('smtp_pass').value : '',
  };

  goTo(3);

  fetch('/install', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config)
  });

  // SSE pour la progression
  const log = document.getElementById('log');
  const es  = new EventSource('/events');
  es.onmessage = function(e) {
    const data = JSON.parse(e.data);
    if (data.msg.startsWith('__DONE__')) {
      es.close();
      const addr = data.msg.replace('__DONE__', '');
      document.getElementById('open-link').href = 'http://' + addr;
      const done = document.createElement('div');
      done.className = 'ok';
      done.textContent = T('✅ Installation terminée — vérifie le journal ci-dessus, puis clique « Continuer ».');
      log.appendChild(done);
      log.scrollTop = log.scrollHeight;
      document.getElementById('btn-finish').hidden = false;
      return;
    }
    const cls = data.level === 'step' ? 'step' : data.level;
    const line = document.createElement('div');
    line.className = cls;
    line.textContent = Tdyn(data.msg);   // message envoyé par emit(), en français
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  };
  es.onerror = function() { es.close(); };
}
</script>
</body>
</html>"""


# ── Traductions de la page (français → anglais) ────────────────
# La page et les messages restent écrits en français ; le moteur ci-dessous (le même que
# celui du bureau, voir tools/i18n/README.md) les traduit dans le navigateur.
# - I18N_EN : clé = texte français, valeur = traduction. Un message d'emit() a une entrée
#   par message, ses parties variables notées {0}, {1}… dans l'ordre où elles apparaissent.
# - I18N_JS : copie de tools/i18n (« node tools/i18n/i18n.js embed setup-wizard.py »).
I18N_EN = r"""{
"Test en cours…": "Testing…",
"inconnu": "unknown",
"Chargement...": "Loading...",
"Dossier vide": "Empty folder",
"Erreur :": "Error:",
"Terminé": "Done",
"Parcourir": "Browse",
"🌐 Réseau": "🌐 Network",
"Token sidecar": "Sidecar token",
"← Retour": "← Back",
"Fermer": "Close",
"Annuler": "Cancel",
"Teste le SMTP (il doit réussir), ou laisse le mot de passe SMTP vide.": "Test the SMTP settings (the test must succeed), or leave the SMTP password empty.",
"Renseigne serveur, identifiant et mot de passe SMTP.": "Enter the SMTP server, username and password.",
"✓ Connexion SMTP réussie (authentification OK).": "✓ SMTP connection successful (authentication OK).",
"✗ Échec SMTP :": "✗ SMTP failed:",
"Exécuté en root": "Running as root",
"Docker disponible": "Docker available",
"OpenSSL (génération token)": "OpenSSL (token generation)",
"✗ Manquant": "✗ Missing",
"⚠ Optionnel": "⚠ Optional",
"Pools détectés :": "Pools detected:",
"— utilise 📁 pour en choisir un autre.": "— use 📁 to choose another one.",
"IP TrueNAS obligatoire": "TrueNAS IP is required",
"Mot de passe SSH obligatoire": "SSH password is required",
"Teste le SMTP (il doit réussir) avant de lancer, ou laisse le mot de passe SMTP vide pour utiliser le fichier local.": "Test the SMTP settings (the test must succeed) before starting, or leave the SMTP password empty to use the local file.",
"✅ Installation terminée — vérifie le journal ci-dessus, puis clique « Continuer ».": "✅ Installation complete — check the log above, then click “Continue”.",
"Assistant d'installation": "Setup wizard",
"Prérequis": "Prerequisites",
"Vérification en cours...": "Checking...",
"Continuer →": "Continue →",
"📁 Chemins": "📁 Paths",
"Répertoire d'installation": "Installation directory",
"Dossier VMs": "VM folder",
"Dossier ISOs (racine)": "ISO folder (root)",
"IP du TrueNAS": "TrueNAS IP",
"Port du bureau": "Desktop port",
"même que l'IP si vide": "same as the IP if empty",
"Utilisé dans les en-têtes nginx. Laissez vide pour utiliser l'IP.": "Used in the nginx headers. Leave empty to use the IP.",
"🔐 Accès SSH": "🔐 SSH access",
"Utilisateur SSH": "SSH user",
"Mot de passe SSH": "SSH password",
"🔑 Sécurité": "🔑 Security",
"Laissez vide pour générer automatiquement": "Leave empty to generate automatically",
"Clé secrète du service fileops, remise au navigateur après la connexion.": "Secret key of the fileops service, handed to the browser after sign-in.",
"Compte du portail 2FA — identifiant": "2FA portal account — username",
"Sans mot de passe saisi, l'assistant en génère un et l'affiche pendant l'installation.": "If you enter no password, the wizard generates one and shows it during the installation.",
"Compte du portail 2FA — mot de passe": "2FA portal account — password",
"Laissez vide pour générer": "Leave empty to generate",
"Activer la double authentification (2FA / Authelia)": "Enable two-factor authentication (2FA / Authelia)",
"Ajoute un code TOTP. Nécessite 2 domaines locaux, demandés à l'étape suivante.": "Adds a TOTP code. Requires 2 local domains, asked for in the next step.",
"Les dossiers du NAS où Desktroo range ses fichiers, ses machines virtuelles et ses images ISO.": "The NAS folders where Desktroo keeps its files, its virtual machines and its ISO images.",
"L'adresse du NAS et le port sur lequel le bureau répondra.": "The address of the NAS and the port the desktop will answer on.",
"Le compte TrueNAS avec lequel le bureau exécute ses commandes sur le NAS.": "The TrueNAS account the desktop uses to run its commands on the NAS.",
"🛡️ Double authentification": "🛡️ Two-factor authentication",
"Le compte avec lequel vous vous connecterez au portail de double authentification.": "The account you will sign in with on the two-factor authentication portal.",
"🌍 Domaines": "🌍 Domains",
"Les deux adresses locales par lesquelles on atteint le bureau et le portail.": "The two local addresses used to reach the desktop and the portal.",
"✉️ Codes 2FA par e-mail": "✉️ 2FA codes by email",
"Répertoire d'installation obligatoire": "Installation directory is required",
"Les deux domaines sont obligatoires pour la double authentification.": "Both domains are required for two-factor authentication.",
"Domaine du bureau": "Desktop domain",
"Domaine du portail 2FA": "2FA portal domain",
"E-mail administrateur": "Administrator email",
"IP de Nginx Proxy Manager (NPM)": "Nginx Proxy Manager (NPM) IP",
"Le HTTPS de la 2FA passe par NPM. Les 2 domaines pointeront vers cette IP.": "HTTPS for 2FA goes through NPM. Both domains will point to this IP.",
"Envoyer les codes 2FA par email (SMTP)": "Send 2FA codes by email (SMTP)",
"Décoché : les codes 2FA sont écrits dans un fichier local (authelia/notification.txt) — le plus simple. Coché : renseigne et teste le SMTP ci-dessous.": "Unchecked: 2FA codes are written to a local file (authelia/notification.txt) — the simplest option. Checked: fill in and test the SMTP settings below.",
"Serveur SMTP": "SMTP server",
"Identifiant SMTP (adresse d'envoi)": "SMTP username (sender address)",
"Mot de passe SMTP": "SMTP password",
"Laisse vide pour envoyer les codes dans un fichier local": "Leave empty to write the codes to a local file",
"Si rempli : Authelia envoie les codes par email (port 465 = SSL, 587 = STARTTLS). Si vide : les codes sont écrits dans authelia/notification.txt.": "If filled in: Authelia sends the codes by email (port 465 = SSL, 587 = STARTTLS). If empty: the codes are written to authelia/notification.txt.",
"Tester le SMTP": "Test SMTP",
"Le test doit réussir avant de pouvoir installer (sinon l'enrôlement 2FA par email serait impossible).": "The test must succeed before you can install (otherwise 2FA enrollment by email would be impossible).",
"L'assistant configure tout le côté NAS. Il reste ensuite à créer 2 hôtes proxy dans NPM + les redirections DNS vers l'IP de NPM — l'assistant affiche les valeurs exactes à la fin. L'enrôlement TOTP se fait après l'installation.": "The wizard configures everything on the NAS side. You then need to create 2 proxy hosts in NPM and the DNS records pointing to the NPM IP — the wizard shows the exact values at the end. TOTP enrollment happens after installation.",
"Installer →": "Install →",
"Installation réussie !": "Installation successful!",
"Desktroo est prêt.": "Desktroo is ready.",
"Ouvrir le bureau →": "Open the desktop →",
"📁 Choisir un dossier": "📁 Choose a folder",
"Choisir ce dossier": "Choose this folder",
"▸ Configuration TrueNAS (SSH + sudo) via middleware...": "▸ Configuring TrueNAS (SSH + sudo) through the middleware...",
"✓ SSH : auth par mot de passe activée": "✓ SSH: password authentication enabled",
"⚠ ssh.update a échoué : {0}": "⚠ ssh.update failed: {0}",
"✓ Service SSH démarré": "✓ SSH service started",
"⚠ Démarrage SSH : {0}": "⚠ Starting SSH: {0}",
"⚠ midclt introuvable — configure SSH/sudo manuellement.": "⚠ midclt not found — configure SSH/sudo manually.",
"✓ sudo sans mot de passe activé pour {0}": "✓ passwordless sudo enabled for {0}",
"⚠ user.update a échoué : {0}": "⚠ user.update failed: {0}",
"⚠ Utilisateur {0} introuvable — active le sudo NOPASSWD manuellement.": "⚠ User {0} not found — enable sudo NOPASSWD manually.",
"✓ Dataset créé : {0}": "✓ Dataset created: {0}",
"▸ Création des datasets ZFS...": "▸ Creating ZFS datasets...",
"▸ Sauvegarde de la configuration...": "▸ Saving the configuration...",
"▸ Récupération des fichiers applicatifs...": "▸ Fetching the application files...",
"▸ Génération de docker-compose.yml...": "▸ Generating docker-compose.yml...",
"✓ docker-compose.yml (stack complète)": "✓ docker-compose.yml (full stack)",
"▸ Génération de nginx.conf...": "▸ Generating nginx.conf...",
"▸ Nettoyage des anciennes modifications systemd/libvirt...": "▸ Cleaning up old systemd/libvirt changes...",
"▸ Configuration du démarrage automatique (POSTINIT)...": "▸ Configuring automatic startup (POSTINIT)...",
"▸ Démarrage de la stack Docker...": "▸ Starting the Docker stack...",
"⚠ Reglage password_login_groups ignore ({0}).": "⚠ password_login_groups setting skipped ({0}).",
"⚠ {0} existe déjà en dossier — conservé tel quel.": "⚠ {0} already exists as a folder — kept as is.",
"⚠ Dataset {0} non créé ({1}) — dossier simple utilisé.": "⚠ Dataset {0} not created ({1}) — plain folder used.",
"⚠ 2FA demandée mais domaines manquants/invalides — 2FA désactivée.": "⚠ 2FA requested but domains are missing or invalid — 2FA disabled.",
"⚠ Les 2 domaines doivent partager le même domaine parent ({0}) — 2FA désactivée.": "⚠ Both domains must share the same parent domain ({0}) — 2FA disabled.",
"✓ Compte du portail 2FA — utilisateur: {0} mot de passe: {1}": "✓ 2FA portal account — user: {0} password: {1}",
"▸ Configuration Authelia (2FA)...": "▸ Configuring Authelia (2FA)...",
"✓ Authelia configuré (utilisateur: {0}) — conteneur publié sur le port 9091": "✓ Authelia configured (user: {0}) — container published on port 9091",
"✓ Snippets NPM écrits dans {0}": "✓ NPM snippets written to {0}",
"===== A FAIRE DANS NPM (une seule fois) =====": "===== TO DO IN NPM (once) =====",
"RECOMMANDE : NPMplus (fork de NPM) integre Authelia nativement -- plus simple que NPM standard.": "RECOMMENDED: NPMplus (a fork of NPM) supports Authelia natively -- simpler than standard NPM.",
"- Hote proxy PORTAIL : {0} -> http {1} port 9091 -- SSL Let's Encrypt + Force SSL + Websockets. Rien dans Advanced.": "- PORTAL proxy host: {0} -> http {1} port 9091 -- Let's Encrypt SSL + Force SSL + Websockets. Nothing in Advanced.",
"- Hote proxy BUREAU : {0} -> http {1} port {2} -- SSL + Force SSL + Websockets.": "- DESKTOP proxy host: {0} -> http {1} port {2} -- SSL + Force SSL + Websockets.",
"NPMplus (recommande) : Auth Request = 'authelia (modern)', Auth Request Upstream = http://{0}:9091 (sans chemin). Rien d'autre a monter.": "NPMplus (recommended): Auth Request = 'authelia (modern)', Auth Request Upstream = http://{0}:9091 (no path). Nothing else to mount.",
"NPM standard : monte {0} sous /snippets dans NPM, puis onglet Advanced :": "Standard NPM: mount {0} as /snippets in NPM, then in the Advanced tab:",
"━━━━━ DNS — pointer les 2 domaines vers l'accès (comme tes autres services) ━━━━━": "━━━━━ DNS — point both domains to the access point (like your other services) ━━━━━",
"✓ Stack Docker démarrée": "✓ Docker stack started",
"✗ Erreur démarrage Docker": "✗ Docker startup error",
"➤ Rapport d'installation complet : {0}": "➤ Full installation report: {0}",
"➤ Rapport d'installation complet (à envoyer en cas de souci) : {0}": "➤ Full installation report (send it if something goes wrong): {0}",
"✓ SSH : groupe {0} deja autorise (mot de passe)": "✓ SSH: group {0} already allowed (password)",
"✓ Email (SMTP) configuré : {0} via {1}:{2}": "✓ Email (SMTP) configured: {0} via {1}:{2}",
"➤ Enrôlement TOTP : ouvre https://{0} , connecte-toi ({1}) ; le code de vérification est envoyé par email à {2}.": "➤ TOTP enrollment: open https://{0} , sign in ({1}); the verification code is sent by email to {2}.",
"➤ Enrôlement TOTP : ouvre https://{0} , connecte-toi ({1}), scanne le QR — le code est dans {2}/authelia/notification.txt": "➤ TOTP enrollment: open https://{0} , sign in ({1}), scan the QR code — the code is in {2}/authelia/notification.txt",
"⚠ midclt introuvable — démarrage auto non configuré.": "⚠ midclt not found — automatic startup not configured.",
"⚠ Démarrage auto non configuré ({0}) — non bloquant, le bureau démarre quand même.": "⚠ Automatic startup not configured ({0}) — not blocking, the desktop starts anyway.",
"✓ SSH : login mot de passe autorise pour le groupe {0}": "✓ SSH: password login allowed for group {0}",
"⚠ password_login_groups non applique ({0}) — a regler en UI si besoin.": "⚠ password_login_groups not applied ({0}) — set it in the UI if needed.",
"⚠ {0} était un dossier (mount Docker d'un essai précédent) — supprimé.": "⚠ {0} was a folder (Docker mount from a previous attempt) — removed.",
"✓ {0} (copié)": "✓ {0} (copied)",
"✓ {0} (téléchargé)": "✓ {0} (downloaded)",
"⚠ Hash Authelia non généré ({0}) — à compléter dans users_database.yml": "⚠ Authelia hash not generated ({0}) — complete it in users_database.yml",
"conf de site mise à jour : {0}": "site configuration updated: {0}",
"✓ Démarrage auto configuré (Init/Shutdown Script POSTINIT)": "✓ Automatic startup configured (Init/Shutdown Script POSTINIT)",
"⚠ POSTINIT non créé ({0}) — à configurer en UI si besoin.": "⚠ POSTINIT not created ({0}) — configure it in the UI if needed.",
"⚠ Impossible de supprimer le dossier parasite {0} : {1}": "⚠ Unable to remove the stray folder {0}: {1}",
"✗ Échec récupération {0} : {1}": "✗ Failed to fetch {0}: {1}",
"ancien conteneur retiré : {0}": "old container removed: {0}",
"(optionnel)": "(optional)",
"/mnt/\u003cvotre-pool>/apps/desktop": "/mnt/\u003cyour-pool>/apps/desktop"
}"""

I18N_JS = r"""
/* ===== Langues : français (langue du code) et anglais =====
   Le code reste écrit en français. Chaque texte affiché passe par T('…'), qui le rend
   tel quel en français et le traduit sinon à l'aide du dictionnaire embarqué
   (<script id="dk-i18n-en">, clé = segment français, valeur = traduction).
   - T('…')   : littéral du code (résultat mis en cache) ;
   - Tt`…`    : gabarit ;
   - Tf('… {0} …', a) : phrase dont l'ordre des mots change d'une langue à l'autre ;
   - Tdyn(s)  : message composé à l'exécution (toasts, erreurs du serveur) ;
   - le HTML statique est traduit une fois, au chargement, par dkTranslateDom().
   Ajouter un texte : l'écrire en français, l'entourer de T(), puis lancer
   « node tools/i18n/i18n.js check desktroo.html » pour lister ce qui reste à traduire. */

/* Découpage d'un fragment (HTML ou texte brut) en segments traduisibles.
   Ce fichier est partagé : l'outil tools/i18n l'utilise tel quel, et le même code est
   embarqué dans desktroo.html (bloc « dk-i18n »). Il doit rester en JavaScript simple (ES5).

   Un segment est un passage de texte continu. Les balises de mise en forme nues
   (<b>, <strong>, <em>, <code>…) en font partie, pour traduire la phrase entière ;
   toute autre balise sépare deux segments. Les attributs title, placeholder,
   aria-label et alt sont des segments à part. */

var DK_ATTRS = 'title|placeholder|aria-label|alt';
var DK_ATTR_RE = new RegExp('(\\b(?:' + DK_ATTRS + ')\\s*=\\s*)(["\'])([\\s\\S]*?)(\\2|$)', 'gi');
var DK_INLINE_RE = /^<\/?(?:b|strong|i|em|u|code|kbd|small|sub|sup|mark)>$/i;
var DK_LETTER_RE = /[A-Za-z\u00C0-\u024F]/;
var DK_ENT = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00A0', mdash: '\u2014', ndash: '\u2013',
               hellip: '\u2026', laquo: '\u00AB', raquo: '\u00BB', rarr: '\u2192', larr: '\u2190', times: '\u00D7' };

function dkDecode(s) {
  if (s.indexOf('&') === -1) return s;
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, function (m, e) {
    if (e.charAt(0) === '#') {
      var n = (e.charAt(1) === 'x' || e.charAt(1) === 'X') ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return isNaN(n) ? m : String.fromCodePoint(n);
    }
    var v = DK_ENT[e.toLowerCase()];
    return v === undefined ? m : v;
  });
}

/* Clé de dictionnaire d'un segment : entités décodées, espaces repliés, bords nettoyés.
   La même phrase donne ainsi la même clé qu'elle vienne du code ou du DOM. */
function dkKey(s) { return dkDecode(s).replace(/\s+/g, ' ').replace(/^ | $/g, ''); }

/* Découpe en jetons {tag, inline, s}. Tolère les fragments qui commencent ou finissent
   au milieu d'une balise (concaténations du code). */
function dkTokens(lit) {
  var out = [], i = 0, n = lit.length, start = 0, k;
  // Fragment qui commence dans une balise : tout ce qui précède le premier « > » orphelin.
  var gt = lit.indexOf('>'), lt = lit.indexOf('<');
  if (gt !== -1 && (lt === -1 || gt < lt)) {
    var head = lit.slice(0, gt + 1);
    if (/["=]/.test(head)) { out.push({ tag: true, s: head }); i = start = gt + 1; }
  }
  while (i < n) {
    var c = lit.charAt(i);
    if (c === '<' && /[A-Za-z\/!]/.test(lit.charAt(i + 1) || '')) {
      if (i > start) out.push({ tag: false, s: lit.slice(start, i) });
      // Comme le navigateur : un guillemet n'ouvre une valeur que juste après « = ».
      var j = i + 1, q = '', last = '';
      while (j < n) {
        var d = lit.charAt(j);
        if (q) { if (d === q) q = ''; }
        else if ((d === '"' || d === "'") && last === '=') q = d;
        else if (d === '>') break;
        if (!/\s/.test(d)) last = d;
        j++;
      }
      out.push({ tag: true, s: lit.slice(i, Math.min(j + 1, n)) });
      i = start = Math.min(j + 1, n);
    } else i++;
  }
  if (start < n) out.push({ tag: false, s: lit.slice(start) });
  for (k = 0; k < out.length; k++) {
    // Un « texte » qui contient attr="… est en réalité un bout de balise coupé par une concaténation.
    if (!out[k].tag && /[\w-]=["']/.test(out[k].s)) out[k].tag = true;
    out[k].inline = out[k].tag && DK_INLINE_RE.test(out[k].s);
  }
  return out;
}

/* Traduit un passage (texte + balises de mise en forme). Les balises et les blancs de
   bord sont mis de côté : la clé commence et finit par du texte. */
function dkMapRun(run, fn) {
  var a = -1, b = -1, x, raw = '';
  for (x = 0; x < run.length; x++) {
    raw += run[x].s;
    if (!run[x].tag && /\S/.test(run[x].s)) { if (a === -1) a = x; b = x; }
  }
  if (a === -1) return raw;
  var head = '', tail = '', core = '', letters = false;
  var sa = /^\s*/.exec(run[a].s)[0], sb = /\s*$/.exec(run[b].s)[0];
  for (x = 0; x < a; x++) head += run[x].s;
  for (x = b + 1; x < run.length; x++) tail += run[x].s;
  for (x = a; x <= b; x++) {
    var s = run[x].s;
    if (x === b) s = s.slice(0, s.length - sb.length);
    if (x === a) s = s.slice(sa.length);
    if (!run[x].tag && DK_LETTER_RE.test(s)) letters = true;
    core += s;
  }
  if (!letters) return raw;
  var tr = fn(dkKey(core), 'text', core);
  return tr === undefined ? raw : head + sa + tr + sb + tail;
}

/* Applique fn(cle, 'text'|'attr') → traduction|undefined à chaque segment et rend le
   fragment recomposé. fn n'est appelée que pour les segments contenant une lettre. */
function dkMapSegments(lit, fn) {
  var toks = dkTokens(lit), out = '', run = [], k;
  for (k = 0; k < toks.length; k++) {
    var t = toks[k];
    if (!t.tag || t.inline) { run.push(t); continue; }
    if (run.length) { out += dkMapRun(run, fn); run = []; }
    out += t.s.replace(DK_ATTR_RE, function (m, pre, q, v, end) {
      if (!DK_LETTER_RE.test(v)) return m;
      var tr = fn(dkKey(v), 'attr', v);
      if (tr === undefined) return m;
      tr = q === '"' ? tr.replace(/"/g, '&quot;') : tr.replace(/'/g, '&#39;');
      return pre + q + /^\s*/.exec(v)[0] + tr + /\s*$/.exec(v)[0] + end;
    });
  }
  if (run.length) out += dkMapRun(run, fn);
  return out;
}

var DK_LANGS = { fr: 'Français', en: 'English' };
function dkDetectLang() {
  try { var v = localStorage.getItem('dk-lang'); if (DK_LANGS[v]) return v; } catch (_) {}
  var n = (navigator.languages && navigator.languages[0]) || navigator.language || 'fr';
  return /^fr\b/i.test(n) ? 'fr' : 'en';
}
var DK_LANG = dkDetectLang();
/* Locale des dates et des nombres : celle du navigateur s'il est anglophone, sinon en-GB
   (jour avant le mois, 24 h), plus proche des habitudes européennes que en-US. */
var DK_LOCALE = DK_LANG === 'fr' ? 'fr-FR' : (/^en\b/i.test(navigator.language || '') ? navigator.language : 'en-GB');
var DK_DICT = null, _dkCache = Object.create(null);
document.documentElement.lang = DK_LANG;

function dkDict() {
  if (DK_DICT) return DK_DICT;
  DK_DICT = {};
  try {
    var el = document.getElementById('dk-i18n-' + DK_LANG);
    if (el) DK_DICT = JSON.parse(el.textContent);
  } catch (e) { console.warn('[i18n] dictionnaire illisible', e); }
  return DK_DICT;
}
function dkLookup(key) {
  var d = dkDict();
  return Object.prototype.hasOwnProperty.call(d, key) ? d[key] : undefined;
}
function T(s) {
  if (DK_LANG === 'fr' || typeof s !== 'string' || !s) return s;
  var c = _dkCache[s];
  if (c === undefined) c = _dkCache[s] = dkMapSegments(s, dkLookup);
  return c;
}
function Tt(strings) {
  var out = T(strings[0]), i;
  for (i = 1; i < strings.length; i++) out += String(arguments[i]) + T(strings[i]);
  return out;
}
function Tf(fmt) {
  var args = arguments;
  return T(fmt).replace(/\{(\d)\}/g, function (m, i) { return args[+i + 1] === undefined ? m : String(args[+i + 1]); });
}
/* Entrées du dictionnaire dont la clé contient des trous ({0}, {1}…) : compilées une fois
   en expressions régulières, de la plus longue à la plus courte. */
var _dkPatterns = null;
function dkPatterns() {
  if (_dkPatterns) return _dkPatterns;
  _dkPatterns = [];
  var d = dkDict(), k;
  for (k in d) {
    if (!/\{\d\}/.test(k)) continue;
    _dkPatterns.push({
      len: k.length, to: d[k],
      order: (k.match(/\{\d\}/g) || []).map(function (x) { return +x.charAt(1); }),
      re: new RegExp('^' + k.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{\d\}/g, '([\\s\\S]+?)') + '$')
    });
  }
  _dkPatterns.sort(function (a, b) { return b.len - a.len; });
  return _dkPatterns;
}

/* Message composé à l'exécution : on essaie la phrase entière, puis sans ses symboles de
   tête ni sa ponctuation de fin, puis un modèle à trous, puis son seul préfixe
   (« Libellé : », « Libellé — ») ; le détail qui suit est traduit s'il est connu,
   sinon laissé tel quel. */
function Tdyn(s) {
  if (DK_LANG === 'fr' || typeof s !== 'string' || !s) return s;
  function look(key) {
    var v = dkLookup(key), seps = [' : ', ': ', ' \u2014 '], pats, k, i, m, head, rest, r;
    if (v !== undefined) return v;
    // Modèles à trous : « Le port {0} est déjà utilisé{1}. »
    pats = dkPatterns();
    for (k = 0; k < pats.length; k++) {
      m = pats[k].re.exec(key);
      if (!m) continue;
      return pats[k].to.replace(/\{(\d)\}/g, function (all, n) {
        var j = pats[k].order.indexOf(+n), val = j === -1 ? all : m[j + 1], tr = j === -1 ? undefined : look(val);
        return tr === undefined ? val : tr;
      });
    }
    for (k = 0; k < seps.length; k++) {
      i = key.indexOf(seps[k]);
      if (i <= 0) continue;
      head = key.slice(0, i) + seps[k].replace(/\s+$/, '');
      v = dkLookup(head);
      if (v === undefined) continue;
      rest = key.slice(i + seps[k].length); r = look(rest);
      return v + ' ' + (r === undefined ? rest : r);
    }
    return undefined;
  }
  return dkMapSegments(s, function (key) {
    var v = look(key);
    if (v !== undefined) return v;
    var m = /^([^A-Za-zÀ-ɏ0-9]*)([\s\S]*?)([\s.:!?…]*)$/.exec(key);
    if (!m || !m[2] || m[2] === key) return undefined;
    v = look(m[2] + m[3]);
    if (v !== undefined) return m[1] + v;
    v = look(m[2]);
    return v === undefined ? undefined : m[1] + v + m[3];
  });
}

/* Les messages d'erreur du serveur (fileops) arrivent en français : on les traduit à la
   lecture de la réponse, quel que soit l'endroit du code qui a lancé la requête. */
if (DK_LANG !== 'fr' && window.Response && Response.prototype.json) {
  (function () {
    var readJson = Response.prototype.json;
    Response.prototype.json = function () {
      var url = this.url || '';
      return readJson.call(this).then(function (d) {
        if (d && typeof d === 'object' && url.indexOf('/fileops/') !== -1) {
          ['error', 'message'].forEach(function (k) { if (typeof d[k] === 'string') d[k] = Tdyn(d[k]); });
        }
        return d;
      });
    };
  })();
}

/* ── HTML statique : même découpage que pour le code (texte + balises de mise en forme
      nues), mais directement sur les nœuds, pour ne toucher à aucun autre élément. ── */
var DK_INLINE_TAGS = { B: 1, STRONG: 1, I: 1, EM: 1, U: 1, CODE: 1, KBD: 1, SMALL: 1, SUB: 1, SUP: 1, MARK: 1 };
function dkEsc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function dkBareInline(el) {
  if (!DK_INLINE_TAGS[el.tagName] || el.attributes.length) return false;
  for (var c = el.firstChild; c; c = c.nextSibling) {
    if (c.nodeType === 1 ? !dkBareInline(c) : c.nodeType !== 3) return false;
  }
  return true;
}
function dkInlineHtml(n) {
  if (n.nodeType === 3) return dkEsc(n.nodeValue);
  var t = n.tagName.toLowerCase(), h = '<' + t + '>';
  for (var c = n.firstChild; c; c = c.nextSibling) h += dkInlineHtml(c);
  return h + '</' + t + '>';
}
function dkTranslateRun(nodes) {
  var html = '', i;
  for (i = 0; i < nodes.length; i++) html += dkInlineHtml(nodes[i]);
  if (!DK_LETTER_RE.test(html)) return;
  var out = dkMapSegments(html, dkLookup);
  if (out === html) return;
  if (nodes.length === 1 && nodes[0].nodeType === 3 && !/[<&]/.test(out)) { nodes[0].nodeValue = out; return; }
  var tpl = document.createElement('template');
  tpl.innerHTML = out;
  var parent = nodes[0].parentNode;
  parent.insertBefore(tpl.content, nodes[0]);
  for (i = 0; i < nodes.length; i++) parent.removeChild(nodes[i]);
}
function dkTranslateDom(root) {
  if (DK_LANG === 'fr' || !root) return;
  var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, TEMPLATE: 1 }, ATTRS = DK_ATTRS.split('|');
  (function walk(el) {
    var i, v, tr, k, run = [];
    for (i = 0; i < ATTRS.length; i++) {
      v = el.getAttribute(ATTRS[i]);
      if (v && DK_LETTER_RE.test(v)) { tr = dkLookup(dkKey(v)); if (tr !== undefined) el.setAttribute(ATTRS[i], dkDecode(tr)); }
    }
    var kids = Array.prototype.slice.call(el.childNodes);
    for (i = 0; i < kids.length; i++) {
      k = kids[i];
      if (k.nodeType === 8) continue;
      if (k.nodeType === 3 || (k.nodeType === 1 && dkBareInline(k))) { run.push(k); continue; }
      if (run.length) { dkTranslateRun(run); run = []; }
      if (k.nodeType === 1 && !SKIP[k.tagName] && k.getAttribute('translate') !== 'no') walk(k);
    }
    if (run.length) dkTranslateRun(run);
  })(root);
}

/* ── Sélecteur de langue ── */
function dkSetLang(lang) {
  if (!DK_LANGS[lang] || lang === DK_LANG) return;
  try { localStorage.setItem('dk-lang', lang); } catch (_) {}
  location.reload();   // les textes sont résolus au chargement : on repart d'une page neuve
}
function dkToggleLang() { dkSetLang(DK_LANG === 'fr' ? 'en' : 'fr'); }
function dkInstallLangSwitch() {
  var box = document.getElementById('dk-lang-login'), lab = document.getElementById('dk-lang-other'), h = '', l;
  if (box) {
    for (l in DK_LANGS) {
      h += '<button type="button" class="dk-lang-btn' + (l === DK_LANG ? ' active' : '') + '" lang="' + l + '"'
         + (l === DK_LANG ? ' aria-current="true"' : '') + ' onclick="dkSetLang(\'' + l + '\')">' + DK_LANGS[l] + '</button>';
    }
    box.innerHTML = h;
  }
  if (lab) { l = DK_LANG === 'fr' ? 'en' : 'fr'; lab.textContent = DK_LANGS[l]; lab.lang = l; }
}
document.addEventListener('DOMContentLoaded', function () {
  dkTranslateDom(document.body);
  dkInstallLangSwitch();
});
"""

HTML = HTML.replace('<!--@I18N@-->',
                    '<script type="application/json" id="dk-i18n-en">' + I18N_EN + '</script>\n'
                    '<script id="dk-i18n">' + I18N_JS + '</script>')

# ── Polices de la charte ──────────────────────────────────────
# Figtree (texte) et Schibsted Grotesk (titres, boutons), embarquées pour que l'assistant
# n'appelle aucun serveur tiers. Ce sont les deux déclarations de desktroo.html
# (bloc « dk-fonts »), reprises telles quelles ; licence SIL Open Font License 1.1.
# Le journal d'installation garde la police à chasse fixe du système.
FONTS_CSS = r"""
@font-face{font-family:"Figtree";font-style:normal;font-display:swap;font-weight:300 900;src:url(data:font/woff2;base64,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) format("woff2");unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:"Schibsted Grotesk";font-style:normal;font-display:swap;font-weight:400 900;src:url(data:font/woff2;base64,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) format("woff2");unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
"""

HTML = HTML.replace('<!--@FONTS@-->', '<style id="dk-fonts">' + FONTS_CSS + '</style>')


# ── Serveur HTTP ──────────────────────────────────────────────
def _smtp_test(host, port, user, password):
    """Teste la connexion + authentification SMTP telle qu'Authelia l'utilisera
    (465 = SSL implicite, sinon STARTTLS). Retourne {'ok': bool, 'error': str}."""
    import smtplib, ssl
    host = (host or '').strip()
    user = (user or '').strip()
    password = password or ''
    try:
        port = int(str(port or '465').strip())
    except Exception:
        port = 465
    if not host or not user or not password:
        return {'ok': False, 'error': 'Renseigne serveur, identifiant et mot de passe SMTP.'}
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as s:
                s.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                try:
                    s.starttls(context=ctx)
                    s.ehlo()
                except smtplib.SMTPException:
                    pass  # certains serveurs 587 acceptent sans STARTTLS
                s.login(user, password)
        return {'ok': True, 'error': ''}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


class WizardHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Silencieux

    def do_GET(self):
        if self.path in ('/', '/setup'):
            self._html()
        elif self.path == '/check':
            self._json(check_prerequisites())
        elif self.path == '/ip':
            self._text(get_local_ip())
        elif self.path == '/pools':
            self._json(list_pools())
        elif self.path == '/events':
            self._sse()
        elif self.path.startswith('/browse'):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path   = params.get('path', ['/mnt'])[0]
            self._browse(path)
        else:
            self.send_error(404)

    def _browse(self, path):
        try:
            path = os.path.realpath(path)
            entries = []
            if path != '/':
                entries.append({'name': '..', 'path': str(os.path.dirname(path)), 'type': 'parent'})
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if os.path.isdir(full) and not name.startswith('.'):
                    entries.append({'name': name, 'path': full, 'type': 'dir'})
            self._json({'path': path, 'entries': entries})
        except Exception as e:
            self._json({'path': path, 'entries': [], 'error': str(e)})

    def do_POST(self):
        if self.path == '/install':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            config = json.loads(body)
            if not INSTALL_RUNNING:
                t = threading.Thread(target=run_install, args=(config,), daemon=True)
                t.start()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
        elif self.path == '/smtp-test':
            length = int(self.headers.get('Content-Length', 0))
            try:
                cfg = json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                cfg = {}
            self._json(_smtp_test(cfg.get('host'), cfg.get('port'),
                                  cfg.get('user'), cfg.get('pass')))
        else:
            self.send_error(404)

    def _html(self):
        data = HTML.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, txt):
        data = txt.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        try:
            while True:
                try:
                    item = INSTALL_EVENTS.get(timeout=30)
                    msg  = json.dumps(item)
                    self.wfile.write(f'data: {msg}\n\n'.encode())
                    self.wfile.flush()
                    if item.get('level') == 'done':
                        break
                except queue.Empty:
                    # Keepalive
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────
if __name__ == '__main__':
    ip = get_local_ip()
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), WizardHandler)
    print()
    print('╔══════════════════════════════════════════╗')
    print('║           Desktroo  —  Wizard            ║')
    print('╚══════════════════════════════════════════╝')
    print()
    print('  Ouvrez dans votre navigateur / Open in your browser :')
    print(f'  ➜  http://{ip}:{PORT}/setup')
    print()
    print('  Ctrl+C pour arrêter / Ctrl+C to stop.')
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nWizard arrêté.')
