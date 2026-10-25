#!/usr/bin/env python3
# Desktroo — © 2026 Mdm-services.fr. Tous droits réservés.
# Logiciel propriétaire, soumis à la licence d'utilisation de Desktroo (LICENSE.md, https://desktroo.fr).
# Copie, modification et redistribution interdites sans accord écrit. Cette mention ne doit pas être retirée.
"""
Desktroo – File-ops & PTY terminal sidecar
HTTP  → port FILEOPS_PORT   (default 8765)
WS    → port FILEOPS_WS_PORT (default 8766)

Fonctionnalités :
  - Opérations fichiers (list/delete/rename/copy/move)
  - Terminal PTY via WebSocket
  - Gestion VMs QEMU/KVM via libvirt+SSH (/libvirt2/…)
  - Proxy VNC WebSocket via SSH (/vnc-proxy)
"""
import asyncio, fcntl, json, logging, os, pty, re, struct, subprocess, sys, termios
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from threading import Thread
from urllib.parse import parse_qsl, urlparse

import websockets

try:
    import paramiko
    _HAS_PARAMIKO = True
except ImportError:
    _HAS_PARAMIKO = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

TOKEN      = os.environ.get('FILEOPS_TOKEN',    'changeme-secret-token')
PORT       = int(os.environ.get('FILEOPS_PORT',    '8765'))
WS_PORT    = int(os.environ.get('FILEOPS_WS_PORT', '8766'))

SSH_HOST_CONF = (os.environ.get('TRUENAS_SSH_HOST', '192.168.1.10') or '').strip()
SSH_USER   = os.environ.get('TRUENAS_SSH_USER', 'truenas_admin')
SSH_PASS   = os.environ.get('TRUENAS_SSH_PASS', '')
SSH_PORT_N = int(os.environ.get('TRUENAS_SSH_PORT', '22'))


# ── MDM-NAS-GATEWAY-V1 : joindre le NAS même quand son adresse sur le réseau local change ──
# Le sidecar tourne SUR le NAS : la passerelle de son réseau Docker, c'est le NAS lui-même. Si l'adresse
# saisie à l'installation ne répond plus (NAS en DHCP, VM qui change d'IP), on passe par cette passerelle.
# TRUENAS_SSH_HOST: "auto" force ce mode dès le départ.
def _container_gateway():
    """Passerelle par défaut du conteneur (lue dans /proc/net/route), ou ''."""
    import socket as _so
    try:
        with open('/proc/net/route') as fh:
            for line in fh.read().splitlines()[1:]:
                f = line.split()
                if len(f) > 3 and f[1] == '00000000' and int(f[3], 16) & 2:
                    return _so.inet_ntoa(struct.pack('<L', int(f[2], 16)))
    except Exception:
        pass
    return ''


def _tcp_ok(host, port, timeout=3):
    import socket as _so
    if not host:
        return False
    try:
        _so.create_connection((host, int(port)), timeout=timeout).close()
        return True
    except Exception:
        return False


def _resolve_ssh_host():
    conf = SSH_HOST_CONF
    if conf and conf.lower() != 'auto' and _tcp_ok(conf, SSH_PORT_N):
        return conf, False
    gw = _container_gateway()
    if gw and _tcp_ok(gw, SSH_PORT_N):
        if conf and conf.lower() != 'auto':
            log.warning('NAS injoignable à %s : passage par la passerelle Docker %s (le NAS lui-même)', conf, gw)
        return gw, True
    return (conf if conf.lower() != 'auto' else (gw or conf)), False


SSH_HOST, SSH_VIA_GATEWAY = _resolve_ssh_host()
VM_DIR     = os.environ.get('VM_DIR',  '/mnt/Truenas_Stockage/vms')
ISO_DIR    = os.environ.get('ISO_DIR', '/mnt/Truenas_Stockage')

# ── Version & mise à jour ─────────────────────────────────────────────────────
APP_VERSION = '3.1.2'
APP_DIR     = os.environ.get('APP_DIR', '')  # dossier d'install (contient fileops.py, HTML…)
GITHUB_RAW  = os.environ.get('GITHUB_RAW', 'https://raw.githubusercontent.com/Nabief/desktroo-dist/main').rstrip('/')

# MDM-ACCESS-POLICY-V1-BEGIN
import tempfile as _tempfile
import threading as _access_threading
from datetime import datetime as _access_datetime, timezone as _access_timezone

# Données de l'application (droits d'accès, partages, sites, licence, identifiant
# d'installation…) : elles vivent dans le dossier d'installation, sous « data ».
# Jusqu'à la 2.0.0, faute de réglage, ce dossier retombait sur un chemin écrit en dur
# (_LEGACY_DATA_DIR) quel que soit le NAS : une installation faite ailleurs y a rangé
# ses données. On les rapatrie une fois, au démarrage. Sans APP_DIR (aucun installeur
# ne le laisse vide), l'ancien chemin reste le repli.
_LEGACY_DATA_DIR = '/mnt/Truenas_Stockage/apps/desktop/data'
ACCESS_DATA_DIR = (os.environ.get('ACCESS_DATA_DIR')
                   or (os.path.join(APP_DIR, 'data') if APP_DIR else _LEGACY_DATA_DIR))


def _access_migrate_legacy_data(new=None, old=None):
    """Copie les données de l'ancien emplacement vers le nouveau, puis retire l'ancien.
    Ne fait rien si le nouvel emplacement contient déjà quelque chose."""
    new = new or ACCESS_DATA_DIR
    old = old or _LEGACY_DATA_DIR
    try:
        if os.path.realpath(new) == os.path.realpath(old) or not os.path.isdir(old):
            return False
        if os.path.isdir(new) and os.listdir(new):
            return False
        import shutil
        names = os.listdir(old)
        os.makedirs(new, exist_ok=True)
        for name in names:
            src, dst = os.path.join(old, name), os.path.join(new, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        # Tout est copié : l'ancien dossier ne sert plus. On remonte en retirant les
        # dossiers restés vides (un vrai pool monté là n'est jamais vide : il reste).
        shutil.rmtree(old)
        parent = os.path.dirname(old)
        while parent not in ('/mnt', '/', ''):
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
        log.info('Données rapatriées de %s vers %s (%d élément(s))', old, new, len(names))
        return True
    except Exception as exc:
        log.warning('Rapatriement des données de %s vers %s impossible : %s', old, new, exc)
        return False


_access_migrate_legacy_data()


def _remove_legacy_page():
    """Retire l'ancienne page « truenas-desktop.html » (avant le renommage) restée dans le
    dossier d'installation. On ne la retire que si la nouvelle page est bien là."""
    if not APP_DIR:
        return False
    old, new = os.path.join(APP_DIR, 'truenas-desktop.html'), os.path.join(APP_DIR, 'desktroo.html')
    try:
        if os.path.isfile(old) and os.path.isfile(new):
            os.remove(old)
            log.info('Ancienne page retirée : %s', old)
            return True
    except OSError as exc:
        log.warning('Ancienne page non retirée (%s) : %s', old, exc)
    return False


_remove_legacy_page()
ACCESS_POLICY_FILE = os.path.join(ACCESS_DATA_DIR, 'access-policy.json')
ACCESS_HISTORY_FILE = os.path.join(ACCESS_DATA_DIR, 'access-history.json')
_access_lock = _access_threading.RLock()


def _access_default_policy():
    return {'version': 1, 'subjects': {}}


def _access_read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            value = json.load(handle)
        return value
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.warning('Unable to read access data %s: %s', path, exc)
        return default


def _access_write_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(
        prefix='.access-', suffix='.tmp', dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def access_policy_get():
    with _access_lock:
        policy = _access_read_json(ACCESS_POLICY_FILE, _access_default_policy())
        if not isinstance(policy, dict):
            policy = _access_default_policy()
        policy.setdefault('version', 1)
        if not isinstance(policy.get('subjects'), dict):
            policy['subjects'] = {}
        return policy


def access_history_get(limit=150):
    with _access_lock:
        history = _access_read_json(ACCESS_HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        limit = max(1, min(int(limit or 150), 1000))
        return history[-limit:][::-1]


def access_history_add(event):
    if not isinstance(event, dict):
        raise ValueError('Historique invalide')
    safe = {
        'time': _access_datetime.now(_access_timezone.utc).isoformat(),
        'actor': str(event.get('actor') or 'unknown')[:128],
        'action': str(event.get('action') or 'Modification')[:256],
        'subject': str(event.get('subject') or '')[:256],
        'resource': str(event.get('resource') or '')[:512],
        'before': event.get('before'),
        'after': event.get('after'),
    }
    with _access_lock:
        history = _access_read_json(ACCESS_HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        history.append(safe)
        history = history[-1000:]
        _access_write_json(ACCESS_HISTORY_FILE, history)
    return safe


def access_policy_set(subject, applications, actor='unknown'):
    subject = str(subject or '').strip().lower()
    if not re.fullmatch(r'(user|group):\d+', subject):
        raise ValueError('Sujet invalide')
    if not isinstance(applications, dict):
        raise ValueError('La politique applications doit être un objet')

    cleaned = {}
    for app_name, mode in applications.items():
        name = str(app_name or '').strip()
        value = str(mode or '').strip().upper()
        if not name or len(name) > 255:
            continue
        if value in ('ALLOW', 'DENY'):
            cleaned[name] = value
        elif value not in ('', 'INHERIT'):
            raise ValueError('Mode application invalide pour ' + name)

    with _access_lock:
        policy = access_policy_get()
        subjects = policy.setdefault('subjects', {})
        before = dict((subjects.get(subject) or {}).get('applications') or {})
        if cleaned:
            entry = subjects.setdefault(subject, {})
            entry['applications'] = cleaned
        else:
            subjects.pop(subject, None)
        policy['updated_at'] = _access_datetime.now(_access_timezone.utc).isoformat()
        _access_write_json(ACCESS_POLICY_FILE, policy)

    access_history_add({
        'actor': actor,
        'action': 'Politique applications modifiée',
        'subject': subject,
        'resource': 'applications',
        'before': before,
        'after': cleaned,
    })
    return policy
# MDM-ACCESS-POLICY-V1-END


# ── Threading HTTP server ─────────────────────────────────────────────────────
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── MDM-SERVER-LOGIN-V1 : connexion vérifiée par le serveur ──────────────────
# Le jeton (TOKEN) n'est plus écrit dans la page du bureau. Le navigateur l'obtient
# en présentant des identifiants TrueNAS à POST /auth/login ; fileops les vérifie
# lui-même auprès de TrueNAS (même appel JSON-RPC que le bureau) et ne remet le jeton
# qu'à un compte ayant les pleins droits d'administration.
import threading as _login_threading
import time as _login_time
import hmac as _login_hmac

TRUENAS_API_HOST = os.environ.get('TRUENAS_API_HOST') or SSH_HOST
LOGIN_MAX_FAILURES = 10      # échecs tolérés…
LOGIN_WINDOW = 300           # …sur cette durée (secondes), tous clients confondus
_login_lock = _login_threading.Lock()
_login_failures = []


def _login_blocked():
    now = _login_time.time()
    with _login_lock:
        _login_failures[:] = [t for t in _login_failures if now - t < LOGIN_WINDOW]
        return len(_login_failures) >= LOGIN_MAX_FAILURES


def _login_note_failure():
    with _login_lock:
        _login_failures.append(_login_time.time())


async def _truenas_verify_async(username, password, apikey, session_token=''):
    """Ouvre une session JSON-RPC sur TrueNAS avec ces identifiants.
    Rend (identifiants_valides, résultat de auth.me ou None).
    « session_token » : jeton à usage unique que TrueNAS a délivré à la session déjà ouverte du bureau. C'est la voie
    des comptes à double authentification, dont le mot de passe seul ne suffit plus : le code a été donné au bureau,
    et TrueNAS atteste ici que ce jeton vient bien de cette session."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # certificat auto-signé du NAS, comme côté nginx
    ctx.verify_mode = ssl.CERT_NONE
    uri = 'wss://%s/api/current' % TRUENAS_API_HOST
    async with websockets.connect(uri, ssl=ctx, open_timeout=10, max_size=4 * 1024 * 1024) as ws:
        async def call(ident, method, params):
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 12))
                if isinstance(msg, dict) and msg.get('id') == ident:
                    return msg
        if session_token:
            r = await call(1, 'auth.login_with_token', [session_token])
        elif apikey:
            r = await call(1, 'auth.login_with_api_key', [apikey])
        else:
            r = await call(1, 'auth.login', [username, password, None])
        if r.get('error') or r.get('result') is not True:
            return False, None
        me = await call(2, 'auth.me', [])
        return True, (None if me.get('error') else me.get('result'))


def _truenas_verify(username, password, apikey, session_token=''):
    return asyncio.run(asyncio.wait_for(_truenas_verify_async(username, password, apikey, session_token), 30))


def _truenas_is_full_admin(me):
    """Le compte a-t-il les pleins droits ? fileops donne un accès root (fichiers,
    terminal) : un compte TrueNAS aux droits restreints ne doit pas l'obtenir.
    Si TrueNAS ne renvoie aucune information de privilège, on ne bloque pas."""
    if not isinstance(me, dict) or not isinstance(me.get('privilege'), dict):
        return True
    priv = me['privilege']
    roles = priv.get('roles')
    if isinstance(roles, dict):
        roles = roles.get('$set')
    if isinstance(roles, (list, tuple)) and 'FULL_ADMIN' in roles:
        return True
    for rule in priv.get('allowlist') or []:
        if isinstance(rule, dict) and rule.get('method') == '*' and rule.get('resource') == '*':
            return True
    return False


def auth_login(body):
    """Traite POST /auth/login. Rend (code HTTP, réponse JSON)."""
    if _login_blocked():
        return 429, {'error': 'Trop de tentatives de connexion. Réessaie dans quelques minutes.'}
    body = body if isinstance(body, dict) else {}
    apikey = str(body.get('apikey') or '').strip()
    username = str(body.get('username') or '').strip()
    password = str(body.get('password') or '')
    session_token = str(body.get('session_token') or '').strip()
    if session_token and not re.match(r'^[A-Za-z0-9_-]{16,256}$', session_token):
        return 400, {'error': 'Jeton de session invalide.'}
    if not apikey and not session_token and not (username and password):
        return 400, {'error': 'Identifiants manquants.'}
    try:
        ok, me = _truenas_verify(username, password, apikey, session_token)
    except Exception as exc:
        log.warning('Connexion : TrueNAS injoignable (%s) : %s', TRUENAS_API_HOST, exc)
        return 502, {'error': 'TrueNAS injoignable : vérification des identifiants impossible.'}
    if not ok:
        _login_note_failure()
        log.warning('Connexion refusée : %s', 'jeton de session TrueNAS refusé' if session_token else 'identifiants TrueNAS incorrects')
        return 401, {'error': 'Session TrueNAS refusée : reconnecte-toi.' if session_token else "Identifiant ou mot de passe incorrect, ou compte sans droits d'administration du NAS."}
    if not _truenas_is_full_admin(me):
        log.warning('Connexion refusée : compte sans les pleins droits (%s)',
                    (me or {}).get('pw_name') or username or 'clé API')
        return 403, {'error': "Ce compte TrueNAS n'a pas les pleins droits d'administration."}
    return 200, {'ok': True, 'token': TOKEN, 'user': str((me or {}).get('pw_name') or username or '')[:64]}
# ── MDM-SERVER-LOGIN-V1 (fin) ────────────────────────────────────────────────


# ── SSH connection pool ───────────────────────────────────────────────────────
import threading as _threading
_ssh_pool   = []
_ssh_lock   = _threading.Lock()
_SSH_MAX    = 4   # max idle connections kept

def _ssh_get():
    """Get a live SSH client from pool or create a new one."""
    with _ssh_lock:
        while _ssh_pool:
            client = _ssh_pool.pop()
            try:
                transport = client.get_transport()
                if transport and transport.is_active():
                    return client
                client.close()
            except Exception:
                pass
    if not _HAS_PARAMIKO:
        raise RuntimeError('paramiko non disponible — relancez le container')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SSH_HOST, port=SSH_PORT_N, username=SSH_USER, password=SSH_PASS,
                   timeout=10, auth_timeout=15, banner_timeout=15)
    return client

def _ssh_put(client):
    """Return a client to the pool (if healthy)."""
    try:
        transport = client.get_transport()
        if transport and transport.is_active():
            with _ssh_lock:
                if len(_ssh_pool) < _SSH_MAX:
                    _ssh_pool.append(client)
                    return
        client.close()
    except Exception:
        pass

# ── SSH helpers ───────────────────────────────────────────────────────────────
def ssh_exec(cmd, timeout=30):
    client = _ssh_get()
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out  = stdout.read().decode('utf-8', errors='replace')
        err  = stderr.read().decode('utf-8', errors='replace')
        code = stdout.channel.recv_exit_status()
        _ssh_put(client)
        return out.strip(), err.strip(), code
    except Exception:
        try: client.close()
        except: pass
        raise


def ssh_ok(cmd, timeout=30):
    out, err, code = ssh_exec(cmd, timeout)
    if code != 0:
        raw = (err or out or 'exit ' + str(code)).strip()
        # Show up to 5 lines so virsh errors are readable
        lines = raw.splitlines()
        msg = ' | '.join(lines[:5])
        raise RuntimeError(msg)
    return out


def shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _privileged_remove(path):
    """Supprime un fichier/dossier. Essaie localement (conteneur root) puis, en cas
    d'échec (ACL ZFS/NFSv4 refusant la suppression), bascule sur 'sudo rm' côté host
    via SSH — le vrai root du système, qui a l'accès complet aux datasets."""
    path = str(path or '')
    if not path or not os.path.exists(path):
        return
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            _sh_shutil.rmtree(path)
        else:
            os.remove(path)
        return
    except Exception as local_err:
        try:
            out, err, code = ssh_exec("sudo -n rm -rf " + shq(path), timeout=90)
        except Exception as ssh_err:
            raise RuntimeError('Suppression impossible : ' + str(local_err) + ' / SSH: ' + str(ssh_err))
        if code != 0 or os.path.exists(path):
            raise RuntimeError('Suppression refusée (' + (err or out or str(local_err)).strip()[:200] + ')')


def _unique_path(dst):
    """Retourne un chemin libre à partir de dst : name (1).ext, name (2).ext…"""
    if not os.path.exists(dst):
        return dst
    d = os.path.dirname(dst)
    base = os.path.basename(dst.rstrip('/'))
    # Gère .tar.gz / .tar.bz2 comme extension composée.
    low = base.lower()
    for comp in ('.tar.gz', '.tar.bz2', '.tar.xz'):
        if low.endswith(comp):
            stem, ext = base[:-len(comp)], base[-len(comp):]
            break
    else:
        stem, ext = os.path.splitext(base)
    i = 1
    while True:
        cand = os.path.join(d, stem + ' (' + str(i) + ')' + ext)
        if not os.path.exists(cand):
            return cand
        i += 1


# ── libvirt / virsh helpers ───────────────────────────────────────────────────
# MDM-SUDO-VIRSH-26 : commandes libvirt exécutées via sudo non interactif.
VIRSH_URI = 'qemu:///system'

# MDM-LIBVIRT-AUTORECOVER-V1-BEGIN
_libvirt_ensure_lock = _threading.Lock()
_libvirt_last_ok = 0.0
_libvirt_cfg_ensured = False


def _ensure_libvirt_apparmor_off():
    """Après une mise à jour de TrueNAS, l'intégration AppArmor de libvirt peut
    être cassée ('cannot load AppArmor profile') et empêcher le démarrage des VMs.
    On force security_driver = "none" dans qemu.conf (idempotent) — la VM reste
    isolée par KVM et la gestion DAC des permissions de disque continue.
    Renvoie True si un changement a été appliqué."""
    inner = (
        'if ! grep -q \'^security_driver = "none"\' /etc/libvirt/qemu.conf 2>/dev/null; then '
        'cp -f /etc/libvirt/qemu.conf /etc/libvirt/qemu.conf.bak 2>/dev/null || true; '
        'sed -i \'s/^[#[:space:]]*security_driver[[:space:]]*=.*/security_driver = "none"/\' /etc/libvirt/qemu.conf 2>/dev/null || true; '
        'grep -q \'^security_driver = "none"\' /etc/libvirt/qemu.conf || echo \'security_driver = "none"\' >> /etc/libvirt/qemu.conf; '
        'systemctl restart libvirtd 2>/dev/null || systemctl restart virtqemud 2>/dev/null || true; '
        'echo CHANGED; '
        'fi'
    )
    out, _err, _code = ssh_exec("sudo -n sh -c " + shq(inner), timeout=60)
    return 'CHANGED' in (out or '')


# MDM-HOST-BOOTSTRAP-V2 : préparation du host pour la gestion des VMs, réalisée
# automatiquement via SSH+sudo. NE MODIFIE PLUS /etc (ni drop-in systemd libvirtd,
# ni règle polkit, ni 'systemctl enable') : ces écritures provoquaient un
# « ordering cycle » systemd fatal sur TrueNAS 25.x (ix-etc/middlewared en échec).
# libvirt est démarré À LA DEMANDE ; les anciennes modifs sont nettoyées.
# Idempotent. Installation 100% interface web (aucun shell requis).
_host_bootstrap_done = False


def _host_bootstrap():
    global _host_bootstrap_done
    if _host_bootstrap_done:
        return
    if not (SSH_HOST and SSH_USER and SSH_PASS):
        log.info('Host bootstrap ignoré (SSH non configuré).')
        return
    script = (
        'set +e\n'
        # -- Réparation : supprime les anciennes modifs /etc qui cassaient le boot 25.x --
        'systemctl disable truenas-desktop 2>/dev/null || true\n'
        'rm -f /etc/systemd/system/truenas-desktop.service\n'
        'rm -f /etc/systemd/system/libvirtd.service.d/notimeout.conf\n'
        'rmdir /etc/systemd/system/libvirtd.service.d 2>/dev/null || true\n'
        'rm -f /etc/tmpfiles.d/truenas-libvirt.conf\n'
        'rm -f /etc/polkit-1/rules.d/80-truenas-libvirt.rules\n'
        'systemctl daemon-reload 2>/dev/null || true\n'
        # -- libvirt À LA DEMANDE : start seulement, jamais enable ni drop-in --
        'systemctl unmask libvirtd.socket libvirtd-ro.socket libvirtd-admin.socket 2>/dev/null || true\n'
        'systemctl start libvirtd 2>/dev/null || systemctl start virtqemud 2>/dev/null || true\n'
        'for i in $(seq 1 15); do [ -S /run/libvirt/libvirt-sock ] && break; sleep 1; done\n'
        'virsh -c qemu:///system net-start default 2>/dev/null || true\n'
        'echo HOST_BOOTSTRAP_OK\n'
    )
    try:
        out, err, _code = ssh_exec("sudo -n sh -c " + shq(script), timeout=120)
        if 'HOST_BOOTSTRAP_OK' in (out or ''):
            _host_bootstrap_done = True
            log.info('Host bootstrap OK (libvirt à la demande + réseau default, sans modif /etc).')
        else:
            log.warning('Host bootstrap incomplet : %s', ((err or out) or '')[:200])
    except Exception as e:
        log.warning('Host bootstrap échec : %s', e)


# ── MDM-SELF-UPDATE-V1 : version & mise à jour depuis GitHub ──────────────────
def _fetch_text(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'Desktroo'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace').strip()


# MDM-NAS-GATEWAY-V1 : IP du NAS sur le réseau local, demandée au NAS lui-même (mise en cache 5 min).
_NAS_LAN = {'ip': '', 't': 0.0}


def _nas_lan_ip():
    import time as _t
    now = _t.time()
    if _NAS_LAN['ip'] and now - _NAS_LAN['t'] < 300:
        return _NAS_LAN['ip']
    try:
        out, _err, _code = ssh_exec("ip -4 route get 1.1.1.1 2>/dev/null || ip -4 route get 192.168.0.1 2>/dev/null", timeout=8)
        m = re.search(r'\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})', out or '')
        if m:
            _NAS_LAN['ip'], _NAS_LAN['t'] = m.group(1), now
    except Exception:
        pass
    return _NAS_LAN['ip']


def _heal_nginx_conf():
    """Si nginx.conf vise une IP du NAS qui ne répond plus, la remplace par la passerelle Docker et recharge nginx.
    Réécriture SUR PLACE (même inode) : le fichier est monté seul dans le conteneur du bureau."""
    import time as _t
    if not SSH_VIA_GATEWAY or not APP_DIR:
        return
    conf = os.path.join(APP_DIR, 'nginx.conf')
    try:
        txt = open(conf, encoding='utf-8').read()
    except Exception:
        return
    ips = set(re.findall(r'proxy_pass\s+https://(\d{1,3}(?:\.\d{1,3}){3})[/:;]', txt))
    gw = SSH_HOST
    stale = [ip for ip in ips if ip != gw and not _tcp_ok(ip, 443)]
    if not stale or not _tcp_ok(gw, 443):
        return
    new = txt
    for ip in stale:
        new = re.sub(r'(https?://)' + re.escape(ip) + r'(?=[/:;])', r'\g<1>' + gw, new)
    if new == txt:
        return
    try:
        with open(conf, 'r+', encoding='utf-8') as fh:
            fh.seek(0); fh.write(new); fh.truncate()
        log.warning('nginx.conf : %s remplacé par la passerelle %s', ', '.join(sorted(stale)), gw)
        _t.sleep(1)
        ssh_exec("sudo -n docker exec desktroo nginx -s reload || docker exec desktroo nginx -s reload", timeout=30)
    except Exception as e:
        log.warning('nginx.conf : réparation impossible : %s', e)


_TRUENAS_LAN_TRY = {'t': 0.0, 'ip': ''}


def _truenas_ip():
    """Renvoie l'adresse IP LAN réelle du NAS, indépendante d'un nom de domaine
    configuré comme hôte SSH. On refuse toute IP publique : mieux vaut retomber
    sur le nom d'hôte que d'ouvrir une IP WAN. Priorité :
    IP littérale SSH > proxy_pass de nginx.conf (IP choisie à l'install) >
    IP privée réellement connectée en SSH > DNS si privée > hôte brut."""
    import socket as _socket, re as _re, os as _os

    # MDM-NAS-GATEWAY-V1 : par la passerelle Docker, l'adresse d'accès ne dit rien de l'IP du NAS sur le réseau local.
    if SSH_VIA_GATEWAY:
        return _nas_lan_ip()

    def _is_ip(x):
        return bool(_re.match(r'^\d{1,3}(\.\d{1,3}){3}$', str(x or '')))

    def _is_private(x):
        m = _re.match(r'^(\d+)\.(\d+)\.(\d+)\.\d+$', str(x or ''))
        if not m:
            return False
        a, b = int(m.group(1)), int(m.group(2))
        return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or a == 127

    host = (SSH_HOST or '').strip()
    if _is_ip(host):
        return host

    # 1) IP littérale du proxy_pass de nginx.conf (celle choisie à l'installation).
    try:
        base = os.environ.get('APP_DIR', '') or (APP_DIR or '')
        conf = _os.path.join(base, 'nginx.conf') if base else ''
        if conf and _os.path.isfile(conf):
            txt = open(conf, 'r', encoding='utf-8', errors='replace').read()
            m = _re.search(r'proxy_pass\s+https?://(\d{1,3}(?:\.\d{1,3}){3})', txt)
            if m:
                return m.group(1)
    except Exception:
        pass

    # 2) IP privée réellement jointe par le sidecar en SSH.
    try:
        with _ssh_lock:
            for _c in list(_ssh_pool):
                try:
                    _tr = _c.get_transport()
                    _sock = getattr(_tr, 'sock', None)
                    if _tr and _tr.is_active() and _sock is not None:
                        _ip = _sock.getpeername()[0]
                        if _is_private(_ip):
                            return str(_ip)
                except Exception:
                    pass
    except Exception:
        pass

    # 3) résolution DNS, seulement si le résultat est une IP privée (LAN).
    try:
        _socket.setdefaulttimeout(3)
        _r = _socket.gethostbyname(host)
        if _is_private(_r):
            return _r
    except Exception:
        pass

    # 3 bis) le NAS donne lui-même son adresse sur le réseau local (nom d'hôte qui vise l'adresse publique :
    #        sans cela, « Ouvrir » enverrait vers le domaine, où les ports des apps ne sont pas ouverts).
    try:
        _now = _login_time.time()
        if _now - _TRUENAS_LAN_TRY['t'] > 60 or _TRUENAS_LAN_TRY['ip']:
            _TRUENAS_LAN_TRY['t'] = _now
            _TRUENAS_LAN_TRY['ip'] = _nas_lan_ip() if _is_private(_nas_lan_ip()) else ''
        if _TRUENAS_LAN_TRY['ip']:
            return _TRUENAS_LAN_TRY['ip']
    except Exception:
        pass

    # 4) repli : le nom d'hôte tel quel (jamais d'IP publique).
    return host


def _dist_channel():
    """'beta' sur un NAS de test (fichier data/store/beta, le même que pour les apps en test), sinon 'stable'."""
    try:
        # Les versions bêta, comme les autres mises à jour, demandent une clé gratuite (3.1.0).
        return 'beta' if (os.path.exists(STORE_BETA_FILE) and _lic_registered()) else 'stable'
    except OSError:
        return 'stable'


# Programme bêta : s'inscrire crée data/store/beta (mises à jour depuis la branche « beta » du dépôt, apps en test
# de Desktroo Apps visibles), se désinscrire le retire. L'invitation n'est faite qu'une fois : la réponse est notée.
def _beta_offer_file():
    return os.path.join(STORE_DIR, 'beta-offer')


def dist_channel_set(body):
    ch = str((body or {}).get('channel') or '')
    if ch not in ('beta', 'stable'):
        return 400, {'ok': False, 'error': 'Canal inconnu.'}
    if ch == 'beta' and not _lic_registered():
        return 403, {'ok': False, 'need_key': True,
                     'error': 'Le programme bêta est réservé aux installations qui ont une clé gratuite : activez-la dans « Licence », puis réessayez.'}
    os.makedirs(os.path.dirname(STORE_BETA_FILE), exist_ok=True)
    if ch == 'beta':
        with open(STORE_BETA_FILE, 'w') as fh:
            fh.write('Programme bêta de Desktroo : inscrit depuis le bureau le %s\n' % _login_time.strftime('%Y-%m-%d %H:%M'))
    elif os.path.exists(STORE_BETA_FILE):
        os.unlink(STORE_BETA_FILE)
    beta_offer_answer({'answer': ch})
    return 200, dict(_version_status(), ok=True)


def beta_offer_answer(body):
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(_beta_offer_file(), 'w') as fh:
        fh.write('%s %s\n' % (str((body or {}).get('answer') or 'non')[:20], _login_time.strftime('%Y-%m-%d %H:%M')))
    return 200, {'ok': True}


# ── Profil : photo ou avatar de chaque compte (data/profiles/<empreinte du nom>.json et .img) ──
# Gardé sur le NAS, donc le même sur tous les navigateurs ; il fait partie de la sauvegarde de Desktroo.
PROFILE_MAX_BYTES = 600 * 1024
_PROFILE_TYPES = ((b'\x89PNG\r\n\x1a\n', 'image/png'), (b'\xff\xd8\xff', 'image/jpeg'), (b'RIFF', 'image/webp'))


def _profile_paths(handler):
    from urllib.parse import unquote
    import hashlib
    u = unquote(handler.headers.get('X-Desktroo-User', '') or '').strip()
    if not re.match(r'^[A-Za-z0-9._@-]{1,64}$', u):
        raise ValueError('Compte inconnu.')
    base = os.path.join(ACCESS_DATA_DIR, 'profiles', hashlib.sha256(u.lower().encode('utf-8')).hexdigest()[:24])
    return base + '.json', base + '.img'


def profile_get(handler):
    pj, _pi = _profile_paths(handler)
    try:
        with open(pj, encoding='utf-8') as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def profile_avatar_set(handler, body):
    import base64
    pj, pi = _profile_paths(handler)
    kind = str((body or {}).get('kind') or '')
    rec = None
    if kind == 'preset':
        pid = str(body.get('id') or '')
        if not re.match(r'^[a-z0-9-]{1,24}$', pid):
            raise ValueError('Avatar inconnu.')
        rec = {'kind': 'preset', 'id': pid}
    elif kind == 'photo':
        raw = str(body.get('data') or '')
        raw = raw.split(',', 1)[1] if raw.startswith('data:') else raw
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception:
            raise ValueError('Image illisible.')
        if not data or len(data) > PROFILE_MAX_BYTES:
            raise ValueError('Image trop lourde (600 Ko au plus).')
        ctype = next((t for magic, t in _PROFILE_TYPES if data.startswith(magic)), '')
        if not ctype or (ctype == 'image/webp' and data[8:12] != b'WEBP'):
            raise ValueError('Format d\'image non pris en charge (PNG, JPEG ou WebP).')
        os.makedirs(os.path.dirname(pi), exist_ok=True)
        with open(pi + '.tmp', 'wb') as fh:
            fh.write(data)
        os.replace(pi + '.tmp', pi)
        rec = {'kind': 'photo', 'type': ctype, 'v': int(_login_time.time())}
    elif kind != 'none':
        raise ValueError('Demande invalide.')
    if kind != 'photo' and os.path.exists(pi):
        os.unlink(pi)
    os.makedirs(os.path.dirname(pj), exist_ok=True)
    d = profile_get(handler)
    d['avatar'] = rec
    with open(pj + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(d, fh)
    os.replace(pj + '.tmp', pj)
    return {'ok': True, 'avatar': rec}


PROFILE_FAV_MAX = 60


def profile_favorites_set(handler, body):
    """Favoris de Fichiers du compte : [{path, name}], dans l'ordre choisi. Chemins absolus, sans « .. »."""
    raw = (body or {}).get('favorites')
    if not isinstance(raw, list):
        raise ValueError('Demande invalide.')
    out, seen = [], set()
    for f in raw[:PROFILE_FAV_MAX]:
        p = str((f or {}).get('path') if isinstance(f, dict) else f or '').strip()
        if len(p) > 1 and p.endswith('/'):
            p = p.rstrip('/')
        if not p.startswith('/') or len(p) > 1024 or '\x00' in p or '/../' in p + '/' or p in seen:
            continue
        seen.add(p)
        name = str((f or {}).get('name') or '') if isinstance(f, dict) else ''
        out.append({'path': p, 'name': re.sub(r'[\x00-\x1f]', '', name)[:80]})
    pj, _pi = _profile_paths(handler)
    os.makedirs(os.path.dirname(pj), exist_ok=True)
    d = profile_get(handler)
    d['fs_favorites'] = out
    with open(pj + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(d, fh)
    os.replace(pj + '.tmp', pj)
    return {'ok': True, 'favorites': out}


def profile_photo(handler):
    _pj, pi = _profile_paths(handler)
    rec = (profile_get(handler).get('avatar') or {})
    if rec.get('kind') != 'photo' or not os.path.isfile(pi):
        return handler._json(404, {'error': 'Pas de photo.'})
    with open(pi, 'rb') as fh:
        body = fh.read()
    handler.send_response(200)
    handler.send_header('Content-Type', rec.get('type') or 'image/jpeg')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-cache')
    handler.end_headers()
    handler.wfile.write(body)


def _dist_raw(channel=None):
    """Adresse du dépôt de distribution : branche « main » (stable) ou « beta ». Un GITHUB_RAW personnalisé reste intact."""
    base = GITHUB_RAW
    if (channel or _dist_channel()) == 'beta' and base.endswith('/main'):
        return base[:-5] + '/beta'
    return base


# Identifiant de démarrage du service. Après une mise à jour, l'ancien service répond encore quelques secondes
# (Docker lui laisse dix secondes pour s'arrêter) : la page attend de voir cet identifiant changer avant de se
# recharger, au lieu de croire que le premier service qui répond est le nouveau.
_BOOT_ID = "%x" % int(_login_time.time() * 1000)


def _version_status():
    latest = ''
    try:
        latest = _fetch_text(_dist_raw() + '/version.txt')[:40]
    except Exception:
        latest = ''
    if not latest and _dist_channel() == 'beta':   # branche beta absente : on suit le stable
        try:
            latest = _fetch_text(_dist_raw('stable') + '/version.txt')[:40]
        except Exception:
            latest = ''

    def _norm(v):   # « 2.58.1b2 » (bêta n° 2) précède « 2.58.1 » (stable) et suit « 2.58.1b1 »
        v = str(v or '').strip()
        m = re.match(r'^(.*\d)[bB](\d*)$', v)
        nums = [int(x) for x in re.findall(r'\d+', m.group(1) if m else v)[:4]]
        return nums + [0] * (4 - len(nums)) + ([0, int(m.group(2) or 0)] if m else [1, 0])

    if latest:
        _RELEASES_LATEST['v'] = latest
    upd = False
    if latest:
        try:
            upd = _norm(latest) > _norm(APP_VERSION)
        except Exception:
            upd = (latest != APP_VERSION)
    # Mises à jour : avec une clé gratuite, ou sans clé pour un correctif de sécurité (security.txt du dist, stable).
    reg = _lic_registered()
    allowed, sec = True, ''
    if upd and not reg:
        try:
            sec = re.sub(r'[^0-9A-Za-z.]', '', _fetch_text(_dist_raw('stable') + '/security.txt')[:40])
        except Exception:
            sec = ''
        try:
            allowed = bool(sec) and _norm(sec) > _norm(APP_VERSION)
        except Exception:
            allowed = False
        if not allowed:
            sec = ''
    return {'version': APP_VERSION, 'latest': latest, 'update_available': bool(upd), 'channel': _dist_channel(), 'boot': _BOOT_ID,
            'registered': reg, 'update_allowed': bool(allowed), 'security': sec,
            'beta_offer': _dist_channel() != 'beta' and not os.path.exists(_beta_offer_file()),
            'truenas_host': ('' if SSH_VIA_GATEWAY else SSH_HOST), 'truenas_ip': _truenas_ip()}


# Notes de version. Lues dans le dépôt de distribution, car elles doivent décrire aussi les versions
# PLUS RÉCENTES que celle installée — c'est là tout l'intérêt avant de mettre à jour. Repli sur la copie
# locale déposée par la dernière mise à jour, pour un NAS sans accès à Internet.
_RELEASES = {}          # langue -> {'t', 'v', 'src'}
_RELEASES_TTL = 900.0
_RELEASES_FILES = {'fr': 'RELEASES.md', 'en': 'RELEASES.en.md'}
_RELEASES_LATEST = {'v': ''}   # dernière version annoncée par version.txt (voir _version_status)


def _releases_has(text, ver):
    """Les notes contiennent-elles la version « ver » (titre « ## 2.58.0 — … ») ?"""
    return not ver or bool(re.search(r'^##\s+' + re.escape(ver) + r'(\s|$)', text or '', re.M))


def _releases_text(force=False, lang='fr'):
    lang = 'en' if str(lang or '').lower().startswith('en') else 'fr'
    now = _login_time.time()
    hit = _RELEASES.get(lang)
    want = _RELEASES_LATEST['v']
    # En cache, sauf si une version plus récente a été annoncée depuis : ses notes doivent apparaître.
    if not force and hit and hit['v'] and now - hit['t'] < _RELEASES_TTL and _releases_has(hit['v'], want):
        return hit['v'], hit['src']
    # Les notes anglaises peuvent manquer (NAS resté sur une distribution plus ancienne) :
    # mieux vaut les notes françaises que rien du tout, et la source le dit.
    noms = [_RELEASES_FILES[lang]] + ([_RELEASES_FILES['fr']] if lang != 'fr' else [])
    text, src = '', ''
    for nom in noms:
        suffixe = '' if nom == _RELEASES_FILES[lang] else ':fr'
        try:
            text = _fetch_text(_dist_raw() + '/' + nom, timeout=20)[:200000]
        except Exception:
            text = ''
        # Copie encore en cache chez GitHub (quelques minutes après une publication) : on la contourne.
        if text and not _releases_has(text, want):
            try:
                fresh = _fetch_text(_dist_raw() + '/' + nom + '?v=' + re.sub(r'[^0-9A-Za-z.]', '', want) + '.' + str(int(now)), timeout=20)[:200000]
                if _releases_has(fresh, want):
                    text = fresh
            except Exception:
                pass
        if text:
            src = 'remote' + suffixe
            break
        if APP_DIR:
            try:
                with open(os.path.join(APP_DIR, nom), 'r', encoding='utf-8', errors='replace') as fh:
                    text = fh.read()[:200000].strip()
            except Exception:
                text = ''
            if text:
                src = 'local' + suffixe
                break
    if text:
        _RELEASES[lang] = {'t': now, 'v': text, 'src': src}
    return text, src


def _do_update():
    """Télécharge la dernière version des fichiers dans APP_DIR.
    Le HTML est servi à chaud ; fileops.py nécessite un redémarrage."""
    if not APP_DIR or not os.path.isdir(APP_DIR):
        raise RuntimeError("APP_DIR introuvable — impossible de localiser l'installation.")
    import urllib.request
    updated = []
    for f in ('fileops.py', 'desktroo.html', 'vnc-viewer.html'):
        dst = os.path.join(APP_DIR, f)
        req = urllib.request.Request(_dist_raw() + '/' + f, headers={'User-Agent': 'Desktroo'})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        # Écriture SUR PLACE (même inode) : indispensable pour que les bind-mounts
        # de fichier unique (nginx/fileops) voient le nouveau contenu.
        with open(dst, 'wb') as fh:
            fh.write(data)
        updated.append(f)
    # La licence d'utilisation et les notes de version accompagnent les fichiers ; leur absence ne fait pas
    # échouer la mise à jour. La copie locale des notes sert aux NAS sans accès à Internet.
    for extra in ('LICENSE.md', 'RELEASES.md', 'RELEASES.en.md', 'uninstall.sh'):
        try:
            req = urllib.request.Request(_dist_raw() + '/' + extra, headers={'User-Agent': 'Desktroo'})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            with open(os.path.join(APP_DIR, extra), 'wb') as fh:
                fh.write(data)
        except Exception:
            pass
    _RELEASES.clear()   # la prochaine lecture ira rechercher les notes
    return updated


def _schedule_self_restart():
    def _r():
        _sh_time.sleep(3)
        try:
            # Redémarre le bureau (nginx) puis le sidecar. Avec l'écriture sur place
            # ce n'est plus strictement nécessaire pour le HTML, mais garantit la
            # prise en compte du nouveau fileops.py.
            ssh_exec("sudo -n docker restart desktroo desktroo-fileops", timeout=45)
        except Exception:
            pass
    _threading.Thread(target=_r, daemon=True).start()


# ── Désinstallation ───────────────────────────────────────────────────────────
# Le travail est fait par uninstall.sh, le script publié dans le dépôt pour la ligne de commande : une seule
# logique, que l'interface s'ouvre encore ou non. Desktroo ne peut pas arrêter son propre conteneur pendant qu'il
# répond : le script est copié dans /tmp du NAS (il survit ainsi à la suppression du dossier d'installation) et
# lancé détaché, quelques secondes après la réponse. La licence est libérée ici, avant, pour que la page puisse
# dire si cela a marché.
_UNINSTALL_HOST_SCRIPT = '/tmp/desktroo-uninstall.sh'
_UNINSTALL_HOST_LOG = '/tmp/desktroo-uninstall.log'
_UNINSTALL_DELAY = 6
_uninstall_lock = _threading.Lock()
_uninstall_started = False


def _uninstall_script_src():
    """Chemin de uninstall.sh dans le dossier d'installation, le même pour le NAS et pour ce conteneur.
    Une installation qui ne l'a pas encore reçu le télécharge depuis le dépôt de distribution."""
    if not APP_DIR or not os.path.isdir(APP_DIR):
        raise RuntimeError("Dossier d'installation introuvable.")
    p = os.path.join(APP_DIR, 'uninstall.sh')
    try:
        with open(p, 'rb') as fh:
            head = fh.read(4096)
        if head.startswith(b'#!/bin/bash') and b'Desktroo' in head:
            return p
    except OSError:
        pass
    import urllib.request
    req = urllib.request.Request(GITHUB_RAW + '/uninstall.sh', headers={'User-Agent': 'Desktroo'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
    except Exception as e:
        raise RuntimeError('Script de désinstallation introuvable, et téléchargement impossible : ' + str(e))
    if not data.startswith(b'#!/bin/bash') or b'Desktroo' not in data[:4096]:
        raise RuntimeError('Script de désinstallation téléchargé illisible.')
    with open(p, 'wb') as fh:
        fh.write(data)
    return p


def _uninstall_parse_plan(out):
    """Lit la sortie de « uninstall.sh --plan » : une valeur par ligne, CLE=valeur."""
    plan = {'containers': [], 'images': [], 'autostart': 0, 'etc': [], 'trash': []}
    ints = {'APPFILES_BYTES': 'appfiles_bytes', 'DB_BYTES': 'db_bytes', 'SETTINGS_BYTES': 'settings_bytes'}
    texts = {'VERSION': 'version', 'MODE': 'mode', 'APP': 'app', 'PROJECT': 'project', 'DIR': 'dir',
             'DATASET': 'dataset', 'VM_DIR': 'vm_dir', 'HOSTNAME': 'hostname'}

    def _int(x):
        try:
            return max(0, int(str(x).strip() or 0))
        except ValueError:
            return 0
    for ln in str(out or '').splitlines():
        if '=' not in ln:
            continue
        k, v = ln.split('=', 1)
        k = k.strip()
        if k in texts:
            plan[texts[k]] = v.strip()
        elif k in ints:
            plan[ints[k]] = _int(v)
        elif k in ('DIR_OK', 'ACTIVATION', 'END'):
            plan[k.lower()] = v.strip() == '1'
        elif k == 'CONTAINER':
            name, _, state = v.partition('|')
            plan['containers'].append({'name': name, 'state': state})
        elif k == 'IMAGE':
            parts = v.split('|', 3)
            if len(parts) == 4:
                plan['images'].append({'id': parts[0], 'bytes': _int(parts[1]), 'shared': parts[2] == '1',
                                       'tag': parts[3]})
        elif k == 'STOREAPP':
            plan.setdefault('store_apps', []).append(v.strip())
        elif k == 'AUTOSTART':
            plan['autostart'] += 1
        elif k == 'ETC':
            plan['etc'].append(v.strip())
        elif k == 'TRASH':
            size, _, path = v.partition('|')
            plan['trash'].append({'path': path, 'bytes': _int(size)})
    plan['images_bytes'] = sum(i['bytes'] for i in plan['images'] if not i['shared'])
    plan['trash_bytes'] = sum(t['bytes'] for t in plan['trash'])
    return plan


def _uninstall_plan():
    src = _uninstall_script_src()
    out, err, code = ssh_exec('sudo -n bash ' + shq(src) + ' --plan', timeout=180)
    plan = _uninstall_parse_plan(out)
    if code != 0 or not plan.get('end'):
        raise RuntimeError('État des lieux impossible : ' + ((err or out or 'exit %s' % code).strip()[:300]))
    plan.pop('end', None)
    # Ce que la désinstallation laisse en place, pour que la fenêtre le dise.
    kept = []
    for p in (plan.get('vm_dir') or VM_DIR, _web_dataset_path(), DOWNLOAD_DIR):
        if p and os.path.isdir(p) and p not in kept:
            kept.append(p)
    plan['kept'] = kept
    plan['started'] = _uninstall_started
    return plan


def _uninstall_release_licence():
    """Libère le poste sur le serveur. Renvoie 'released', 'none' (rien à libérer) ou 'failed'."""
    key = _lic_read_activation_key()
    if not key:
        return 'none'
    resp = _lic_server_post('deactivate', {'activation_key': key, 'install_id': _lic_install_id()})
    if isinstance(resp, dict) and resp.get('ok'):
        for f in (LICENSE_FILE, LIC_REFRESH_FILE, ACTIVATION_KEY_FILE):
            try:
                os.remove(f)
            except OSError:
                pass
        return 'released'
    return 'failed'


def _uninstall_run(body, by=''):
    global _uninstall_started
    body = body if isinstance(body, dict) else {}
    with _uninstall_lock:
        if _uninstall_started:
            raise ValueError('La désinstallation est déjà lancée.')
        plan = _uninstall_plan()
        typed = str(body.get('confirm') or '').strip()
        host = str(plan.get('hostname') or '').strip()
        if not host or not typed or typed not in (host, host.split('.')[0]):
            raise ValueError('Le nom saisi ne correspond pas au nom du NAS.')
        purge = {'db': bool(body.get('purge_db')), 'settings': bool(body.get('purge_settings')),
                 'trash': bool(body.get('purge_trash'))}
        lang = 'en' if str(body.get('lang') or '').lower().startswith('en') else 'fr'
        args = ['--yes', '--no-release', '--delay', str(_UNINSTALL_DELAY), '--lang', lang]
        if plan.get('dir'):
            args += ['--dir', plan['dir']]
        for k in ('db', 'settings', 'trash'):
            if purge[k]:
                args.append('--purge-' + k)
        # Le script est mis en place AVANT de libérer la licence : si la copie échoue, rien n'a bougé.
        src = _uninstall_script_src()
        out, err, code = ssh_exec('sudo -n install -m 700 ' + shq(src) + ' ' + _UNINSTALL_HOST_SCRIPT, timeout=30)
        if code != 0:
            raise RuntimeError('Lancement impossible : ' + ((err or out or 'exit %s' % code).strip()[:300]))
        release = _uninstall_release_licence() if body.get('release', True) else 'skipped'
        cmd = ('{ sudo -n setsid nohup bash ' + _UNINSTALL_HOST_SCRIPT + ' ' + ' '.join(shq(a) for a in args)
               + ' > ' + _UNINSTALL_HOST_LOG + ' 2>&1 < /dev/null & } && echo STARTED')
        out, err, code = ssh_exec(cmd, timeout=30)
        if 'STARTED' not in (out or ''):
            raise RuntimeError('Lancement impossible : ' + ((err or out or 'exit %s' % code).strip()[:300]))
        _uninstall_started = True
    try:
        _px_log('desktroo.uninstall', by, purge=[k for k in purge if purge[k]] or None, licence=release)
    except Exception:
        pass
    return {'ok': True, 'release': release, 'purge': purge, 'plan': plan, 'delay': _UNINSTALL_DELAY,
            'log': _UNINSTALL_HOST_LOG}


def _libvirt_path_requires_daemon(path):
    """Return True only for routes that actually call virsh/libvirt."""
    path = str(path or '')
    return (
        path == '/libvirt/test'
        or path == '/libvirt/networks'
        or path.startswith('/libvirt/vms')
        or path == '/libvirt2/networks'
        or path.startswith('/libvirt2/vms')
    )


def ensure_libvirtd():
    """
    Ensure the QEMU/libvirt daemon is available on the TrueNAS host.

    Supports both monolithic libvirtd and modular virtqemud/virtproxyd.
    The health check uses virsh instead of assuming one socket filename.
    """
    global _libvirt_last_ok, _libvirt_cfg_ensured
    import time as _time

    with _libvirt_ensure_lock:
        # Une seule fois par process : garantir que l'intégration AppArmor cassée
        # (fréquente après une MAJ TrueNAS) ne bloque pas le démarrage des VMs.
        if not _libvirt_cfg_ensured:
            _libvirt_cfg_ensured = True
            try:
                _ensure_libvirt_apparmor_off()
            except Exception as _cfg_e:
                log.warning('libvirt apparmor cfg: %s', _cfg_e)

        now = _time.monotonic()
        if now - _libvirt_last_ok < 4:
            return

        probe = (
            "sudo -n virsh -c " + shq(VIRSH_URI)
            + " list --all --name >/dev/null 2>&1"
        )
        _, _, probe_code = ssh_exec(probe, timeout=15)
        if probe_code == 0:
            _libvirt_last_ok = _time.monotonic()
            return

        start_cmd = r"""
set +e

unit_loaded() {
  [ "$(sudo -n systemctl show -p LoadState --value "$1" 2>/dev/null)" = "loaded" ]
}

start_unit() {
  if unit_loaded "$1"; then
    sudo -n systemctl start "$1" >/dev/null 2>&1
  fi
}

if unit_loaded virtqemud.socket || unit_loaded virtqemud.service; then
  for unit in     virtlogd.socket     virtlockd.socket     virtqemud.socket     virtproxyd.socket     virtnetworkd.socket     virtstoraged.socket     virtinterfaced.socket     virtnodedevd.socket     virtnwfilterd.socket     virtsecretd.socket
  do
    start_unit "$unit"
  done

  if ! unit_loaded virtqemud.socket; then
    start_unit virtqemud.service
  fi
else
  # Après une mise à jour de TrueNAS, les sockets libvirt peuvent être MASQUÉS :
  # libvirtd tourne alors en --timeout sans socket -> /run/libvirt/libvirt-sock absent
  # et virsh ne peut pas se connecter. On démasque puis on repart en activation par socket.
  sudo -n systemctl unmask     virtlogd.socket     virtlockd.socket     libvirtd.socket     libvirtd-ro.socket     libvirtd-admin.socket     >/dev/null 2>&1
  sudo -n systemctl daemon-reload >/dev/null 2>&1
  # Socket principal absent alors que libvirtd tourne : arrêter le service pour
  # laisser l'activation par socket le relancer proprement (attaché au socket).
  if [ ! -S /run/libvirt/libvirt-sock ] && [ ! -S /var/run/libvirt/libvirt-sock ]; then
    sudo -n systemctl stop libvirtd.service >/dev/null 2>&1
  fi
  for unit in     virtlogd.socket     virtlockd.socket     libvirtd.socket     libvirtd-ro.socket     libvirtd-admin.socket
  do
    start_unit "$unit"
  done
  start_unit libvirtd.service
fi

for i in $(seq 1 30); do
  if sudo -n virsh -c 'qemu:///system' list --all --name >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done

echo "QEMU/libvirt indisponible après tentative d'auto-récupération." >&2
echo "--- unités libvirt ---" >&2
sudo -n systemctl --no-pager --plain --full status   libvirtd.service libvirtd.socket   virtqemud.service virtqemud.socket virtproxyd.socket   2>&1 | tail -n 60 >&2
echo "--- sockets /run/libvirt ---" >&2
ls -la /run/libvirt /var/run/libvirt 2>&1 | tail -n 60 >&2
exit 1
"""

        out, err, code = ssh_exec(start_cmd, timeout=50)
        if code != 0:
            details = (err or out or "Impossible de démarrer QEMU/libvirt").strip()
            raise RuntimeError(details)

        out, err, code = ssh_exec(probe, timeout=15)
        if code != 0:
            details = (
                err or out or
                "Le daemon libvirt a démarré mais virsh ne répond pas"
            ).strip()
            raise RuntimeError(details)

        _libvirt_last_ok = _time.monotonic()


def _libvirt_watchdog():
    """Continuously recover libvirt after boot or a daemon failure."""
    import time as _time

    delay = 3
    first_success = True

    while True:
        try:
            ensure_libvirtd()
            if first_success:
                log.info(
                    "QEMU/libvirt disponible — surveillance automatique active"
                )
                first_success = False
            delay = 60
        except Exception as error:
            log.warning("Auto-récupération QEMU/libvirt : %s", error)
            delay = min(max(delay * 2, 10), 300)

        _time.sleep(delay)
# MDM-LIBVIRT-AUTORECOVER-V1-END


def virsh(subcmd, timeout=30):
    ensure_libvirtd()
    return ssh_ok("sudo -n virsh -c '" + VIRSH_URI + "' " + subcmd, timeout)


def virsh_list_all():
    out = virsh('list --all')
    vms = []
    for line in out.splitlines():
        m = re.match(r'^\s*(\S+)\s+(\S+)\s+(.+)$', line)
        if m and m.group(1) not in ('Id', '---', '---------'):
            vid = m.group(1)
            vms.append({
                'id':    None if vid == '-' else _try_int(vid),
                'name':  m.group(2),
                'state': m.group(3).strip(),
            })
    return vms


def virsh_dominfo(name):
    out = virsh('dominfo ' + shq(name))
    info = {}
    for line in out.splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            key = re.sub(r'[^a-z0-9_]', '_', k.strip().lower())
            info[key] = v.strip()
    return info


def virsh_domdisplay(name):
    try:
        out, _, code = ssh_exec(
            "sudo -n virsh -c '" + VIRSH_URI + "' domdisplay " + shq(name))
        if code != 0 or not out:
            return None
        # virsh returns vnc://host:DISPLAY (display offset, not real port)
        # real port = 5900 + display_number
        m = re.match(r'(vnc|spice)://([^:]+):(\d+)', out.strip())
        if m:
            port = int(m.group(3))
            if port < 100:          # display number, convert to real port
                port += 5900
            return {'type': m.group(1), 'host': m.group(2), 'port': port}
    except Exception:
        pass
    return None


def virsh_domblklist(name):
    out, _, _ = ssh_exec(
        "sudo -n virsh -c '" + VIRSH_URI + "' domblklist " + shq(name) + ' --details')
    disks = []
    for line in out.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            disks.append({
                'type': parts[0], 'device': parts[1],
                'target': parts[2], 'source': parts[3],
            })
    return disks


def virsh_net_list():
    out, _, _ = ssh_exec("sudo -n virsh -c '" + VIRSH_URI + "' net-list --all")
    nets = []
    for line in out.splitlines()[2:]:
        parts = line.split()
        if parts:
            nets.append({'name': parts[0], 'state': parts[1] if len(parts) > 1 else 'unknown'})
    return nets


def _try_int(s):
    try:
        return int(s)
    except Exception:
        return s


# ── libvirt XML builder ───────────────────────────────────────────────────────
def virsh_parse_devices(name):
    """Parse dumpxml to extract NICs, USB hostdevs, sound, boot order."""
    import xml.etree.ElementTree as ET
    xml_str = ssh_ok("sudo -n virsh -c '" + VIRSH_URI + "' dumpxml --inactive " + shq(name) + " 2>/dev/null || sudo -n virsh -c '" + VIRSH_URI + "' dumpxml " + shq(name))
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return {}

    devices_el = root.find('devices')
    if devices_el is None:
        return {}

    # NICs
    nics = []
    for iface in devices_el.findall('interface'):
        src_el    = iface.find('source')
        mac_el    = iface.find('mac')
        model_el  = iface.find('model')
        itype     = iface.get('type', 'network')
        source    = ''
        if src_el is not None:
            source = src_el.get('network') or src_el.get('bridge') or src_el.get('dev') or ''
        nics.append({
            'type':    'interface',
            'itype':   itype,
            'mac':     mac_el.get('address', '') if mac_el is not None else '',
            'model':   model_el.get('type', '') if model_el is not None else '',
            'source':  source,
        })

    # USB hostdevs
    usb_devs = []
    for hdev in devices_el.findall('hostdev'):
        if hdev.get('type') != 'usb': continue
        src_el  = hdev.find('source')
        vendor  = ''
        product = ''
        if src_el is not None:
            v_el = src_el.find('vendor')
            p_el = src_el.find('product')
            if v_el is not None: vendor  = v_el.get('id','').replace('0x','')
            if p_el is not None: product = p_el.get('id','').replace('0x','')
        usb_devs.append({'type': 'hostdev', 'vendor': vendor, 'product': product, 'name': vendor+':'+product})

    # Sound
    sounds = []
    for snd in devices_el.findall('sound'):
        sounds.append({'type': 'sound', 'model': snd.get('model','')})

    # Video
    video_model = 'vga'
    vid_el = devices_el.find('video')
    if vid_el is not None:
        m_el = vid_el.find('model')
        if m_el is not None:
            video_model = m_el.get('type', 'vga')

    # Boot order
    # Prefer libvirt per-device boot order:
    #   <disk device="cdrom"><boot order="1"/></disk>
    #   <disk device="disk"><boot order="2"/></disk>
    # Fallback to legacy:
    #   <os><boot dev="cdrom"/><boot dev="hd"/></os>
    boot_order = []
    boot_items = []

    for d in devices_el.findall('disk'):
        b = d.find('boot')
        if b is None or not b.get('order'):
            continue

        dev_type = d.get('device', '')
        if dev_type == 'disk':
            boot_dev = 'hd'
        elif dev_type == 'cdrom':
            boot_dev = 'cdrom'
        elif dev_type == 'floppy':
            boot_dev = 'fd'
        else:
            continue

        try:
            boot_rank = int(b.get('order'))
        except Exception:
            boot_rank = 999

        boot_items.append((boot_rank, boot_dev))

    for iface in devices_el.findall('interface'):
        b = iface.find('boot')
        if b is None or not b.get('order'):
            continue
        try:
            boot_rank = int(b.get('order'))
        except Exception:
            boot_rank = 999
        boot_items.append((boot_rank, 'network'))

    if boot_items:
        seen_boot = set()
        for _, boot_dev in sorted(boot_items, key=lambda x: x[0]):
            if boot_dev not in seen_boot:
                boot_order.append(boot_dev)
                seen_boot.add(boot_dev)
    else:
        os_el = root.find('os')
        if os_el is not None:
            for b in os_el.findall('boot'):
                dev = b.get('dev','')
                if dev and dev not in boot_order:
                    boot_order.append(dev)

    # Memory / vCPUs
    mem_el  = root.find('memory')
    vcpu_el = root.find('vcpu')
    memory_mb = int(mem_el.text or 0) // 1024 if mem_el is not None else 0
    vcpus     = int(vcpu_el.text or 1)          if vcpu_el is not None else 1

    return {
        'devices':    nics + usb_devs + sounds,
        'video_model': video_model,
        'boot_order': boot_order,
        'memory_mb':  memory_mb,
        'vcpus':      vcpus,
    }


# ── Batch-parse helpers (work on already-fetched text) ───────────────────────
def _parse_dominfo(text):
    info = {}
    for line in text.splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            key = re.sub(r'[^a-z0-9_]', '_', k.strip().lower())
            info[key] = v.strip()
    return info

def _parse_domdisplay(text):
    text = text.strip()
    if not text:
        return None
    m = re.match(r'(vnc|spice)://([^:]+):(-?\d+)', text)
    if m:
        port = int(m.group(3))
        if port < 100:
            port += 5900
        return {'type': m.group(1), 'host': m.group(2), 'port': port}
    return None

def _parse_domblklist(text):
    disks = []
    for line in text.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            disks.append({
                'type': parts[0], 'device': parts[1],
                'target': parts[2], 'source': parts[3],
            })
    return disks

def _parse_dumpxml(xml_str):
    """Same as virsh_parse_devices but takes XML string directly."""
    import xml.etree.ElementTree as ET
    if not xml_str or not xml_str.strip():
        return {}
    try:
        root = ET.fromstring(xml_str.strip())
    except Exception:
        return {}
    devices_el = root.find('devices')
    if devices_el is None:
        return {}
    nics = []
    for iface in devices_el.findall('interface'):
        src_el   = iface.find('source')
        mac_el   = iface.find('mac')
        model_el = iface.find('model')
        itype    = iface.get('type', 'network')
        source   = ''
        if src_el is not None:
            source = src_el.get('network') or src_el.get('bridge') or src_el.get('dev') or ''
        nics.append({'type': 'interface', 'itype': itype,
                     'mac':   mac_el.get('address', '')  if mac_el   is not None else '',
                     'model': model_el.get('type', '')   if model_el is not None else '',
                     'source': source})
    usb_devs = []
    for hdev in devices_el.findall('hostdev'):
        if hdev.get('type') != 'usb': continue
        src_el = hdev.find('source')
        vendor = product = ''
        if src_el is not None:
            v_el = src_el.find('vendor');  p_el = src_el.find('product')
            if v_el is not None: vendor  = v_el.get('id','').replace('0x','')
            if p_el is not None: product = p_el.get('id','').replace('0x','')
        usb_devs.append({'type': 'hostdev', 'vendor': vendor, 'product': product,
                         'name': vendor + ':' + product})
    sounds = []
    for snd in devices_el.findall('sound'):
        sounds.append({'type': 'sound', 'model': snd.get('model','')})
    video_model = 'vga'
    vid_el = devices_el.find('video')
    if vid_el is not None:
        m_el = vid_el.find('model')
        if m_el is not None:
            video_model = m_el.get('type', 'vga')
    boot_order = []
    os_el = root.find('os')
    if os_el is not None:
        for b in os_el.findall('boot'):
            boot_order.append(b.get('dev',''))
    mem_el   = root.find('memory')
    vcpu_el  = root.find('vcpu')
    mem_mb   = int(mem_el.text or 0) // 1024 if mem_el  is not None else 0
    vcpus    = int(vcpu_el.text or 1)         if vcpu_el is not None else 1
    return {'devices': nics + usb_devs + sounds,
            'video_model': video_model,
            'boot_order': boot_order, 'memory_mb': mem_mb, 'vcpus': vcpus}


def build_vm_xml(name, memory_mb, vcpus, disk_path, iso_path, uefi, secure_boot, tpm, network, disk_bus='sata', net_type='nat', bridge_iface='br0', net_model='e1000', boot_disk_first=False):
    nvram_path = disk_path.replace('.qcow2', '_VARS.fd')

    if secure_boot:
        code_fd       = '/usr/share/OVMF/OVMF_CODE_4M.ms.fd'
        vars_tpl      = '/usr/share/OVMF/OVMF_VARS_4M.ms.fd'
        smm_tag       = '<smm state="on"/>'
        loader_secure = ' secure="yes"'
    elif uefi:
        code_fd       = '/usr/share/OVMF/OVMF_CODE_4M.fd'
        vars_tpl      = '/usr/share/OVMF/OVMF_VARS_4M.fd'
        smm_tag       = ''
        loader_secure = ''
    else:
        code_fd = vars_tpl = nvram_path = None
        smm_tag = loader_secure = ''

    os_extra = ''
    if uefi or secure_boot:
        os_extra = (
            '<loader readonly="yes"' + loader_secure + ' type="pflash">' + code_fd + '</loader>'
            '<nvram template="' + vars_tpl + '">' + nvram_path + '</nvram>'
        )

    # Always include a CDROM slot so ISO can be swapped later with virsh change-media
    if iso_path:
        cdrom_tag = (
            '<disk type="file" device="cdrom">'
            '<driver name="qemu"/>'
            '<source file="' + iso_path + '"/>'
            '<target dev="sdb" bus="sata"/>'
            '<readonly/>'
            '</disk>'
        )
    else:
        cdrom_tag = (
            '<disk type="file" device="cdrom">'
            '<driver name="qemu"/>'
            '<target dev="sdb" bus="sata"/>'
            '<readonly/>'
            '</disk>'
        )

    tpm_tag = ''
    if tpm:
        tpm_tag = (
            '<tpm model="tpm-crb">'
            '<backend type="emulator" version="2.0"/>'
            '</tpm>'
        )

    boot_tags = ('<boot dev="hd"/><boot dev="cdrom"/>'
                 if boot_disk_first else
                 '<boot dev="cdrom"/><boot dev="hd"/>')

    # Interface disque : cible + machine + contrôleur selon le bus choisi.
    # IDE n'existe pas sur q35 -> on bascule la machine sur i440fx (pc) dans ce cas.
    machine = 'pc' if disk_bus == 'ide' else 'q35'
    _dev_map = {'virtio': 'vda', 'sata': 'sda', 'scsi': 'sda', 'ide': 'hda', 'usb': 'sda'}
    disk_target = _dev_map.get(disk_bus, 'sda')
    scsi_ctrl = '<controller type="scsi" model="virtio-scsi" index="0"/>' if disk_bus == 'scsi' else ''

    return (
        '<domain type="kvm">'
        '<name>' + name + '</name>'
        '<memory unit="MiB">' + str(memory_mb) + '</memory>'
        '<currentMemory unit="MiB">' + str(memory_mb) + '</currentMemory>'
        '<vcpu placement="static">' + str(vcpus) + '</vcpu>'
        '<os>'
        '<type arch="x86_64" machine="' + machine + '">hvm</type>'
        + os_extra +
        boot_tags +
        '</os>'
        '<features>'
        '<acpi/><apic/>' + smm_tag +
        '</features>'
        '<cpu mode="host-passthrough" check="none" migratable="on"/>'
        '<clock offset="localtime">'
        '<timer name="rtc" tickpolicy="catchup"/>'
        '<timer name="pit" tickpolicy="delay"/>'
        '<timer name="hpet" present="no"/>'
        '<timer name="hypervclock" present="yes"/>'
        '</clock>'
        '<devices>'
        '<disk type="file" device="disk">'
        '<driver name="qemu" type="qcow2" discard="unmap"/>'
        '<source file="' + disk_path + '"/>'
        '<target dev="' + disk_target + '" bus="' + disk_bus + '"/>'
        '</disk>'
        + cdrom_tag +
        '<controller type="sata" index="0"/>'
        + scsi_ctrl +
        ('<interface type="bridge">'
           + '<source bridge="' + (bridge_iface or 'br0') + '"/>'
           + '<model type="' + net_model + '"/>'
           + '</interface>'
           if net_type == 'bridge' else
           '<interface type="network">'
           + '<source network="' + (network or 'default') + '"/>'
           + '<model type="' + net_model + '"/>'
           + '</interface>')
        +
        '<graphics type="vnc" port="-1" autoport="yes">'
        '<listen type="address" address="0.0.0.0"/>'
        '</graphics>'
        '<video><model type="qxl" ram="65536" vram="65536" vgamem="16384" heads="1" primary="yes"/></video>'
        '<memballoon model="virtio"/>'
        + tpm_tag +
        '</devices>'
        '</domain>'
    )


# ── websockets path compat ────────────────────────────────────────────────────
def _get_ws_path(ws):
    # websockets < 10
    for attr in ('path', 'request_uri', '_path'):
        v = getattr(ws, attr, None)
        if v and isinstance(v, str):
            return v
    # websockets >= 10: ws.request
    try:
        req = ws.request
        # ws.request.path  (includes query string in most versions)
        p = getattr(req, 'path', None)
        if p and isinstance(p, str):
            return p
        # ws.request.url.path + query
        url = getattr(req, 'url', None)
        if url:
            path = str(getattr(url, 'path', '') or '/')
            query = getattr(url, 'query', '') or getattr(url, 'query_string', '') or ''
            if isinstance(query, bytes):
                query = query.decode('utf-8', errors='replace')
            return path + ('?' + str(query) if query else '')
        # websockets 14+: ws.request.headers 'path' key
        hdrs = getattr(req, 'headers', None)
        if hdrs:
            raw_line = hdrs.get(':path', '') or hdrs.get('path', '')
            if raw_line:
                return raw_line
    except Exception:
        pass
    return '/'


# ── PTY helpers ───────────────────────────────────────────────────────────────
def set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    except Exception:
        pass


_active_session = None


# ── Terminal : session sur le NAS par SSH ─────────────────────────────────────
# Le Terminal de Desktroo est celui du NAS, pas celui du conteneur du service : zpool, zfs et docker
# n'existent que sur l'hôte. Un bash sans fichiers de profil donne une invite simple, que le
# mini-terminal du navigateur sait afficher (il ne gère ni couleurs ni déplacements de curseur).
_TERM_SSH_ENV = 'env TERM=dumb PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"'
_TERM_SSH_CMD = ('if command -v bash >/dev/null 2>&1; then exec ' + _TERM_SSH_ENV
                 + " PS1='\\u@\\h:\\w\\$ ' bash --noprofile --norc -i; else exec " + _TERM_SSH_ENV + ' sh -i; fi')


def _term_marker(mode, detail=''):
    """Annonce au navigateur où la session s'ouvre (séquence OSC, jamais affichée)."""
    clean = ''.join(ch for ch in str(detail) if ch.isprintable() and ch not in ';')[:160]
    return ('\x1b]777;desktroo=%s;%s\x07' % (mode, clean)).encode('utf-8')


def _term_ssh_open():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SSH_HOST, port=SSH_PORT_N, username=SSH_USER, password=SSH_PASS,
                   timeout=10, auth_timeout=15, banner_timeout=15)
    try:
        chan = client.get_transport().open_session(timeout=15)
        chan.get_pty(term='dumb', width=200, height=50)
        chan.exec_command(_TERM_SSH_CMD)
    except Exception:
        client.close()
        raise
    return client, chan


async def _terminal_ssh(ws, client, chan):
    loop = asyncio.get_event_loop()

    async def ssh_to_ws():
        queue = asyncio.Queue()
        fd = chan.fileno()

        def _on_readable():
            try:
                if chan.recv_ready():
                    queue.put_nowait(chan.recv(16384) or None)
                elif chan.closed or chan.eof_received or chan.exit_status_ready():
                    queue.put_nowait(None)
            except Exception:
                queue.put_nowait(None)
        loop.add_reader(fd, _on_readable)
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                try:
                    await ws.send(data)
                except Exception:
                    break
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    async def ws_to_ssh():
        async for msg in ws:
            raw = msg.encode('utf-8', errors='replace') if isinstance(msg, str) else bytes(msg)
            if raw[:1] == b'{':
                try:
                    if 'cols' in json.loads(raw):
                        continue   # largeur fixe : le mini-terminal ne replie pas les lignes lui-même
                except Exception:
                    pass
            try:
                await loop.run_in_executor(None, chan.sendall, raw)
            except Exception:
                break

    t1 = asyncio.ensure_future(ssh_to_ws())
    t2 = asyncio.ensure_future(ws_to_ssh())
    try:
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        t1.cancel()
        t2.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
        raise
    finally:
        for closer in (chan.close, client.close):
            try:
                closer()
            except Exception:
                pass


# ── WebSocket: terminal PTY ───────────────────────────────────────────────────
async def terminal_session(ws):
    global _active_session

    path   = _get_ws_path(ws)
    params = dict(parse_qsl(urlparse(path).query))
    if params.get('token', '') != TOKEN:
        log.warning('Terminal: rejected (bad token)')
        try:
            await ws.close(1008, 'Unauthorized')
        except Exception:
            pass
        return

    prev = _active_session
    if prev and not prev.done():
        prev.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(prev), timeout=3.0)
        except Exception:
            pass

    _active_session = asyncio.current_task()

    # D'abord le NAS lui-même ; le shell du conteneur ne sert que si SSH n'est pas disponible.
    why = ''
    if not _HAS_PARAMIKO:
        why = 'paramiko absent'
    elif not (SSH_HOST and SSH_USER and SSH_PASS):
        why = 'connexion SSH au NAS non configurée'
    else:
        try:
            client, chan = await asyncio.get_event_loop().run_in_executor(None, _term_ssh_open)
        except Exception as e:
            why = str(e) or e.__class__.__name__
            log.warning('Terminal: SSH vers le NAS impossible (%s), repli sur le conteneur', why)
        else:
            log.info('Terminal: session SSH sur %s', SSH_HOST)
            try:
                await ws.send(_term_marker('ssh', '%s@%s' % (SSH_USER, SSH_HOST)))
            except Exception:
                pass
            await _terminal_ssh(ws, client, chan)
            return
    try:
        await ws.send(_term_marker('local', why))
    except Exception:
        pass

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd, 24, 80)
    env         = os.environ.copy()
    env['TERM'] = 'xterm-256color'
    env['PS1']  = r'\u@\h:\w\$ '

    proc = subprocess.Popen(
        ['/bin/sh', '-i'],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True, env=env,
    )
    os.close(slave_fd)

    loop = asyncio.get_event_loop()

    async def pty_to_ws():
        queue = asyncio.Queue()
        def _on_readable():
            try:
                data = os.read(master_fd, 4096)
                if data:
                    queue.put_nowait(data)
            except OSError:
                try:
                    loop.remove_reader(master_fd)
                except Exception:
                    pass
                queue.put_nowait(None)
        loop.add_reader(master_fd, _on_readable)
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                try:
                    await ws.send(data)
                except Exception:
                    break
        finally:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

    async def ws_to_pty():
        async for msg in ws:
            raw = msg.encode('utf-8', errors='replace') if isinstance(msg, str) else bytes(msg)
            try:
                j = json.loads(raw)
                if 'cols' in j:
                    set_winsize(master_fd, int(j.get('rows', 24)), int(j['cols']))
                    continue
            except Exception:
                pass
            try:
                os.write(master_fd, raw)
            except OSError:
                break

    t1 = asyncio.ensure_future(pty_to_ws())
    t2 = asyncio.ensure_future(ws_to_pty())
    try:
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        t1.cancel()
        t2.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
        raise
    finally:
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ── WebSocket: VNC proxy via SSH tunnel ───────────────────────────────────────
async def vnc_proxy_session(ws):
    path   = _get_ws_path(ws)
    params = dict(parse_qsl(urlparse(path).query))
    if params.get('token', '') != TOKEN:
        try:
            await ws.close(1008, 'Unauthorized')
        except Exception:
            pass
        return

    vnc_port = int(params.get('port', '5900'))
    loop = asyncio.get_event_loop()

    def _connect_ssh():
        transport = paramiko.Transport((SSH_HOST, SSH_PORT_N))
        transport.connect(username=SSH_USER, password=SSH_PASS)
        channel = transport.open_channel(
            'direct-tcpip', ('127.0.0.1', vnc_port), ('127.0.0.1', 0)
        )
        return transport, channel

    try:
        transport, channel = await loop.run_in_executor(None, _connect_ssh)
    except Exception as e:
        log.warning('VNC proxy connect failed: %s', e)
        try:
            await ws.close(1011, str(e))
        except Exception:
            pass
        return

    log.info('VNC proxy: tunnel open (port=%d)', vnc_port)

    async def vnc_to_ws():
        q = asyncio.Queue()
        def _readable():
            try:
                data = channel.recv(16384)
                q.put_nowait(data if data else None)
            except Exception:
                q.put_nowait(None)
        try:
            loop.add_reader(channel.fileno(), _readable)
        except Exception:
            pass
        try:
            while True:
                data = await q.get()
                if not data:
                    break
                try:
                    await ws.send(data)
                except Exception:
                    break
        finally:
            try:
                loop.remove_reader(channel.fileno())
            except Exception:
                pass

    async def ws_to_vnc():
        try:
            async for msg in ws:
                data = msg if isinstance(msg, bytes) else msg.encode()
                await loop.run_in_executor(None, channel.sendall, data)
        except Exception:
            pass

    t1 = asyncio.ensure_future(vnc_to_ws())
    t2 = asyncio.ensure_future(ws_to_vnc())
    try:
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        try:
            channel.close()
        except Exception:
            pass
        try:
            transport.close()
        except Exception:
            pass


# ── WebSocket router ──────────────────────────────────────────────────────────
async def ws_handler(ws):
    raw  = _get_ws_path(ws)
    path = urlparse(raw).path
    log.info('WS connect: raw=%r parsed_path=%r', raw, path)
    if path == '/vnc-proxy' and _HAS_PARAMIKO:
        await vnc_proxy_session(ws)
    else:
        await terminal_session(ws)


# ── HTTP handler ──────────────────────────────────────────────────────────────
# MDM-LIBVIRT2-20260710
# Modèle VM/QEMU/libvirt V2.
# Fournit un modèle JSON cohérent aux opérations V2.
def _lv2_split_sections(text):
    sections = {}
    cur = None
    for ln in (text or '').splitlines():
        if ln.startswith('===') and ln.endswith('==='):
            cur = ln.strip('=')
            sections[cur] = []
        elif cur:
            sections[cur].append(ln)
    return sections


def _lv2_parse_key_values(text):
    data = {}
    for ln in (text or '').splitlines():
        if ':' not in ln:
            continue
        k, v = ln.split(':', 1)
        key = k.strip().lower().replace(' ', '_')
        data[key] = v.strip()
    return data


def _lv2_parse_domblklist(text):
    rows = []
    for ln in (text or '').splitlines():
        line = ln.strip()
        if not line or line.startswith('-') or line.lower().startswith('type '):
            continue
        parts = line.split(None, 3)
        if len(parts) >= 4:
            rows.append({
                'type': parts[0],
                'device': parts[1],
                'target': parts[2],
                'source': parts[3],
            })
    return rows


def _lv2_parse_domiflist(text):
    rows = []
    for ln in (text or '').splitlines():
        line = ln.strip()
        if not line or line.startswith('-') or line.lower().startswith('interface '):
            continue
        parts = line.split()
        if len(parts) >= 5:
            rows.append({
                'interface': parts[0],
                'type': parts[1],
                'source': parts[2],
                'model': parts[3],
                'mac': parts[4],
            })
    return rows


def _lv2_xml_attr(el, path, attr='', default=''):
    node = el.find(path) if el is not None else None
    if node is None:
        return default
    if attr:
        return node.get(attr, default)
    return (node.text or default).strip()


def _lv2_parse_xml(xml_str, blklist=None, iflist=None):
    import xml.etree.ElementTree as ET

    blklist = blklist or []
    iflist = iflist or []

    model = {
        'resources': {'memory_mb': 0, 'current_memory_mb': 0, 'vcpus': 1},
        'firmware': {'type': 'bios', 'loader': '', 'secure_boot': False, 'nvram': ''},
        'machine': {'arch': '', 'machine': '', 'type': ''},
        'boot_order': [],
        'boot_mode': 'unknown',
        'boot_conflict': False,
        'disks': [],
        'cdroms': [],
        'nics': [],
        'display': {},
        'video': {},
        'sound': [],
        'usb': [],
        'controllers': [],
        'raw': {'has_xml': False},
    }

    if not xml_str or not xml_str.strip():
        return model

    try:
        root = ET.fromstring(xml_str.strip())
    except Exception as e:
        model['raw']['xml_error'] = str(e)
        return model

    model['raw']['has_xml'] = True

    mem = root.find('memory')
    curmem = root.find('currentMemory')
    vcpu = root.find('vcpu')

    def mem_to_mb(node):
        if node is None or not (node.text or '').strip():
            return 0
        try:
            val = int((node.text or '0').strip())
        except Exception:
            return 0
        unit = (node.get('unit') or 'KiB').lower()
        if unit in ('kib', 'kb'):
            return val // 1024
        if unit in ('mib', 'mb'):
            return val
        if unit in ('gib', 'gb'):
            return val * 1024
        return val // 1024

    model['resources']['memory_mb'] = mem_to_mb(mem)
    model['resources']['current_memory_mb'] = mem_to_mb(curmem) or model['resources']['memory_mb']
    try:
        model['resources']['vcpus'] = int((vcpu.text or '1').strip()) if vcpu is not None else 1
    except Exception:
        model['resources']['vcpus'] = 1

    os_el = root.find('os')
    if os_el is not None:
        type_el = os_el.find('type')
        if type_el is not None:
            model['machine']['arch'] = type_el.get('arch', '')
            model['machine']['machine'] = type_el.get('machine', '')
            model['machine']['type'] = (type_el.text or '').strip()

        loader = os_el.find('loader')
        if loader is not None:
            model['firmware']['type'] = 'uefi'
            model['firmware']['loader'] = (loader.text or '').strip()
            model['firmware']['secure_boot'] = (
                loader.get('secure') in ('yes', 'on', 'true', '1')
                or 'secboot' in (loader.text or '').lower()
            )

        nvram = os_el.find('nvram')
        if nvram is not None:
            model['firmware']['nvram'] = (nvram.text or '').strip()

    devices = root.find('devices')
    if devices is None:
        return model

    blk_by_target = {d.get('target', ''): d for d in blklist}
    boot_items = []
    legacy_boot = []

    if os_el is not None:
        for b in os_el.findall('boot'):
            dev = b.get('dev', '')
            if dev:
                legacy_boot.append(dev)

    for disk in devices.findall('disk'):
        dev_type = disk.get('device', '')
        disk_type = disk.get('type', '')

        target_el = disk.find('target')
        driver_el = disk.find('driver')
        source_el = disk.find('source')
        boot_el = disk.find('boot')

        target = target_el.get('dev', '') if target_el is not None else ''
        bus = target_el.get('bus', '') if target_el is not None else ''

        source = ''
        if source_el is not None:
            source = (
                source_el.get('file')
                or source_el.get('dev')
                or source_el.get('name')
                or source_el.get('protocol')
                or ''
            )

        if not source and target in blk_by_target:
            source = blk_by_target[target].get('source', '')

        item = {
            'device': dev_type,
            'type': disk_type,
            'target': target,
            'bus': bus,
            'source': source,
            'format': driver_el.get('type', '') if driver_el is not None else '',
            'driver': driver_el.get('name', '') if driver_el is not None else '',
            'readonly': disk.find('readonly') is not None,
            'boot_order': None,
            'empty': (not source or source == '-'),
        }

        if boot_el is not None and boot_el.get('order'):
            try:
                item['boot_order'] = int(boot_el.get('order'))
            except Exception:
                item['boot_order'] = 999

            if dev_type == 'disk':
                boot_items.append((item['boot_order'], 'hd'))
            elif dev_type == 'cdrom':
                boot_items.append((item['boot_order'], 'cdrom'))
            elif dev_type == 'floppy':
                boot_items.append((item['boot_order'], 'fd'))

        if dev_type == 'cdrom':
            model['cdroms'].append(item)
        else:
            model['disks'].append(item)

    for iface in devices.findall('interface'):
        mac_el = iface.find('mac')
        src_el = iface.find('source')
        model_el = iface.find('model')
        target_el = iface.find('target')
        boot_el = iface.find('boot')

        source = ''
        if src_el is not None:
            source = src_el.get('network') or src_el.get('bridge') or src_el.get('dev') or ''

        item = {
            'type': iface.get('type', ''),
            'mac': mac_el.get('address', '') if mac_el is not None else '',
            'source': source,
            'model': model_el.get('type', '') if model_el is not None else '',
            'target': target_el.get('dev', '') if target_el is not None else '',
            'boot_order': None,
        }

        if boot_el is not None and boot_el.get('order'):
            try:
                item['boot_order'] = int(boot_el.get('order'))
            except Exception:
                item['boot_order'] = 999
            boot_items.append((item['boot_order'], 'network'))

        model['nics'].append(item)

    graphics = devices.find('graphics')
    if graphics is not None:
        model['display'] = {
            'type': graphics.get('type', ''),
            'port': graphics.get('port', ''),
            'websocket': graphics.get('websocket', ''),
            'autoport': graphics.get('autoport', ''),
            'listen': graphics.get('listen', ''),
        }

    video = devices.find('video')
    if video is not None:
        vmodel = video.find('model')
        model['video'] = {
            'model': vmodel.get('type', '') if vmodel is not None else '',
            'vram': vmodel.get('vram', '') if vmodel is not None else '',
            'heads': vmodel.get('heads', '') if vmodel is not None else '',
            'primary': vmodel.get('primary', '') if vmodel is not None else '',
        }

    for snd in devices.findall('sound'):
        model['sound'].append({'model': snd.get('model', '')})

    for hdev in devices.findall('hostdev'):
        if hdev.get('type') == 'usb':
            src = hdev.find('source')
            vendor = src.find('vendor').get('id', '') if src is not None and src.find('vendor') is not None else ''
            product = src.find('product').get('id', '') if src is not None and src.find('product') is not None else ''
            model['usb'].append({'vendor': vendor, 'product': product})

    for ctrl in devices.findall('controller'):
        model['controllers'].append({
            'type': ctrl.get('type', ''),
            'index': ctrl.get('index', ''),
            'model': ctrl.get('model', ''),
        })

    if boot_items:
        model['boot_mode'] = 'per-device'
        seen = set()
        for _, dev in sorted(boot_items, key=lambda x: x[0]):
            if dev not in seen:
                model['boot_order'].append(dev)
                seen.add(dev)
    elif legacy_boot:
        model['boot_mode'] = 'legacy-os'
        seen = set()
        for dev in legacy_boot:
            if dev not in seen:
                model['boot_order'].append(dev)
                seen.add(dev)
    else:
        model['boot_mode'] = 'none'

    # Sécurité affichage/API : l'ordre de boot ne doit jamais contenir de doublons,
    # même si un ancien XML libvirt contient plusieurs entrées héritées.
    deduped = []
    seen = set()
    for dev in model.get('boot_order', []):
        if dev not in seen:
            deduped.append(dev)
            seen.add(dev)
    model['boot_order'] = deduped

    model['boot_conflict'] = bool(boot_items and legacy_boot)

    return model


def _lv2_vm_names():
    # Auto-réimport : après une mise à jour de TrueNAS qui a pu vider
    # /etc/libvirt/qemu, on redéfinit dans libvirt toute VM dont le XML existe
    # encore dans VM_DIR (créé par cet outil) mais n'est plus connue de libvirt.
    u = "'" + VIRSH_URI + "'"
    vmdir = shq(VM_DIR.rstrip('/'))
    # Noms des VMs qui doivent redémarrer au boot (espaces exclus par la validation).
    auto_names = ' '.join(str(n) for n, v in _vm_autostart_read().items() if v)
    script = (
        'AUTO=' + shq(auto_names) + '; '
        'for f in ' + vmdir + '/*.xml; do '
        '[ -e "$f" ] || continue; '
        'n=$(basename "$f" .xml); '
        'if ! sudo -n virsh -c ' + u + ' dominfo "$n" >/dev/null 2>&1; then '
        'sudo -n virsh -c ' + u + ' define "$f" >/dev/null 2>&1 || true; '
        'for a in $AUTO; do [ "$a" = "$n" ] && sudo -n virsh -c ' + u + ' autostart "$n" >/dev/null 2>&1; done; '
        'fi; '
        'done; '
        'sudo -n virsh -c ' + u + ' list --all --name'
    )
    out, _err, _code = ssh_exec(script, timeout=60)
    return [x.strip() for x in out.splitlines() if x.strip()]


def _lv2_vm_snapshot_names(name):
    out, err, code = ssh_exec(
        "sudo -n virsh -c '" + VIRSH_URI + "' snapshot-list " + shq(name) + " --name 2>/dev/null || true",
        timeout=20
    )
    return [x.strip() for x in out.splitlines() if x.strip()]


def _lv2_live_resources(res, state, info, xml):
    """Une VM en marche garde les processeurs et la mémoire reçus à son démarrage : les réglages modifiés depuis
    ne valent qu'après un arrêt complet. « dominfo » donne ce avec quoi elle tourne, le XML ce qui est prévu."""
    st = str(state or '').lower()
    if 'running' not in st and 'paused' not in st:
        return
    m = re.match(r'\s*(\d+)\s*([KMG])?', str(info.get('max_memory') or ''), re.I)
    live_mem = 0
    if m:
        n, unit = int(m.group(1)), (m.group(2) or 'K').upper()
        live_mem = n // 1024 if unit == 'K' else (n if unit == 'M' else n * 1024)
    try:
        live_cpu = int(str(info.get('cpu(s)') or '0').strip() or 0)
    except ValueError:
        live_cpu = 0
    if live_mem:
        res['live_memory_mb'] = live_mem
    if live_cpu:
        res['live_vcpus'] = live_cpu
    partial = bool(re.search(r'<vcpu[^>]*\bcurrent=', xml or ''))   # processeurs ajoutables à chaud : le compte courant diffère du maximum
    res['pending_restart'] = bool(
        (live_mem and res.get('memory_mb') and abs(live_mem - int(res['memory_mb'])) > 1)
        or (live_cpu and res.get('vcpus') and not partial and live_cpu != int(res['vcpus'])))


def _lv2_get_vm(name, include_xml=False):
    cmd = (
        "echo '===DOMSTATE===' && (sudo -n virsh -c '" + VIRSH_URI + "' domstate " + shq(name) + " 2>&1 || true)"
        + " && echo '===DOMINFO===' && (sudo -n virsh -c '" + VIRSH_URI + "' dominfo " + shq(name) + " 2>&1 || true)"
        + " && echo '===DISPLAY===' && (sudo -n virsh -c '" + VIRSH_URI + "' domdisplay " + shq(name) + " 2>/dev/null || true)"
        + " && echo '===BLKLIST===' && (sudo -n virsh -c '" + VIRSH_URI + "' domblklist --details " + shq(name) + " 2>&1 || true)"
        + " && echo '===IFLIST===' && (sudo -n virsh -c '" + VIRSH_URI + "' domiflist " + shq(name) + " 2>&1 || true)"
        + " && echo '===XML===' && (sudo -n virsh -c '" + VIRSH_URI + "' dumpxml --inactive " + shq(name)
        + " 2>/dev/null || sudo -n virsh -c '" + VIRSH_URI + "' dumpxml " + shq(name) + " 2>&1 || true)"
    )

    batch = ssh_ok(cmd, timeout=45)
    sec = _lv2_split_sections(batch)

    state = '\n'.join(sec.get('DOMSTATE', [])).strip()
    info = _lv2_parse_key_values('\n'.join(sec.get('DOMINFO', [])))
    display_raw = '\n'.join(sec.get('DISPLAY', [])).strip()
    blklist = _lv2_parse_domblklist('\n'.join(sec.get('BLKLIST', [])))
    iflist = _lv2_parse_domiflist('\n'.join(sec.get('IFLIST', [])))
    xml = '\n'.join(sec.get('XML', []))

    parsed = _lv2_parse_xml(xml, blklist=blklist, iflist=iflist)
    _lv2_live_resources(parsed['resources'], state, info, xml)

    vm = {
        'api': 'libvirt2',
        'version': 2,
        'readonly': False,
        'name': name,
        'state': state,
        'uuid': info.get('uuid', ''),
        'id': info.get('id', ''),
        'autostart': info.get('autostart', ''),
        'persistent': info.get('persistent', ''),
        'managed_save': info.get('managed_save', ''),
        'resources': parsed['resources'],
        'firmware': parsed['firmware'],
        'machine': parsed['machine'],
        'boot_order': parsed['boot_order'],
        'boot_mode': parsed['boot_mode'],
        'boot_conflict': parsed['boot_conflict'],
        'disks': parsed['disks'],
        'cdroms': parsed['cdroms'],
        'nics': parsed['nics'],
        'display': parsed['display'],
        'display_uri': display_raw,
        'video': parsed['video'],
        'sound': parsed['sound'],
        'usb': parsed['usb'],
        'controllers': parsed['controllers'],
        'snapshots': _lv2_vm_snapshot_names(name),
        'raw_sources': {
            'domblklist': blklist,
            'domiflist': iflist,
        }
    }

    if include_xml:
        vm['xml'] = xml

    return vm


# MDM-LIBVIRT2-INSTALLATION-20260710
# Actions V2 propres pour installation VM : ISO + boot.
# Fonctions dédiées aux handlers /libvirt2/vms/{name}/iso.
def _lv2_list_isos():
    cmd = (
        "find " + shq(ISO_DIR)
        + " -maxdepth 4 -type f \\( -iname '*.iso' -o -iname '*.img' \\) 2>/dev/null | sort"
    )
    out, err, code = ssh_exec(cmd, timeout=30)
    if code != 0:
        return []
    items = []
    for path in out.splitlines():
        path = path.strip()
        if not path:
            continue
        items.append({
            'path': path,
            'name': os.path.basename(path),
            'dir': os.path.dirname(path),
        })
    return items


def _lv2_state(name):
    out, err, code = ssh_exec(
        "sudo -n virsh -c '" + VIRSH_URI + "' domstate " + shq(name),
        timeout=15
    )
    return (out or err or '').strip().lower()


def _lv2_remote_file_exists(path):
    out, err, code = ssh_exec("[ -f " + shq(path) + " ]", timeout=10)
    return code == 0


def _lv2_dumpxml(name):
    return ssh_ok(
        "sudo -n virsh -c '" + VIRSH_URI + "' dumpxml --inactive " + shq(name)
        + " 2>/dev/null || sudo -n virsh -c '" + VIRSH_URI + "' dumpxml " + shq(name),
        timeout=35
    )


def _lv2_write_remote_text(path, text):
    import base64
    payload = base64.b64encode(text.encode('utf-8')).decode('ascii')
    ssh_ok(
        "printf %s " + shq(payload) + " | base64 -d > " + shq(path),
        timeout=25
    )


def _lv2_define_xml(name, xml_text):
    import re
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', name)
    tmp = "/tmp/libvirt2-" + safe + ".xml"
    _lv2_write_remote_text(tmp, xml_text)
    ssh_ok(
        "sudo -n virsh -c '" + VIRSH_URI + "' define " + shq(tmp),
        timeout=35
    )
    return tmp


def _lv2_xml_patch(name, patch_fn):
    xml = _lv2_dumpxml(name)
    new_xml = patch_fn(xml)
    tmp = _lv2_define_xml(name, new_xml)
    return tmp


def _lv2_xml_to_text(root):
    import xml.etree.ElementTree as ET
    return ET.tostring(root, encoding='unicode')


def _lv2_insert_before_alias_or_address(node, child):
    children = list(node)
    for idx, existing in enumerate(children):
        if existing.tag in ('alias', 'address'):
            node.insert(idx, child)
            return
    node.append(child)


def _lv2_set_iso(name, iso_path):
    import xml.etree.ElementTree as ET

    iso_path = str(iso_path or '').strip()

    if iso_path and not _lv2_remote_file_exists(iso_path):
        raise RuntimeError("ISO introuvable : " + iso_path)

    result = {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'mount_iso' if iso_path else 'eject_iso',
        'iso_path': iso_path,
        'config_updated': False,
        'live_updated': False,
        'warning': '',
    }

    target_ref = {'target': 'sdb'}

    def patch(xml):
        root = ET.fromstring(xml)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        cdrom = None
        for disk in devices.findall('disk'):
            if disk.get('device') == 'cdrom':
                cdrom = disk
                break

        if cdrom is None:
            cdrom = ET.Element('disk', {'type': 'file', 'device': 'cdrom'})
            cdrom.append(ET.Element('driver', {'name': 'qemu', 'type': 'raw'}))
            cdrom.append(ET.Element('target', {'dev': 'sdb', 'bus': 'sata'}))
            cdrom.append(ET.Element('readonly'))
            devices.append(cdrom)

        if not cdrom.get('type'):
            cdrom.set('type', 'file')
        cdrom.set('device', 'cdrom')

        driver = cdrom.find('driver')
        if driver is None:
            cdrom.insert(0, ET.Element('driver', {'name': 'qemu', 'type': 'raw'}))
        else:
            if not driver.get('name'):
                driver.set('name', 'qemu')
            if not driver.get('type'):
                driver.set('type', 'raw')

        target = cdrom.find('target')
        if target is None:
            target = ET.Element('target', {'dev': 'sdb', 'bus': 'sata'})
            cdrom.append(target)
        if not target.get('dev'):
            target.set('dev', 'sdb')
        if not target.get('bus'):
            target.set('bus', 'sata')

        target_ref['target'] = target.get('dev') or 'sdb'

        for source in list(cdrom.findall('source')):
            cdrom.remove(source)

        if iso_path:
            source = ET.Element('source', {'file': iso_path})
            children = list(cdrom)
            inserted = False
            for idx, child in enumerate(children):
                if child.tag == 'target':
                    cdrom.insert(idx, source)
                    inserted = True
                    break
            if not inserted:
                cdrom.append(source)

        if cdrom.find('readonly') is None:
            cdrom.append(ET.Element('readonly'))

        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)
    result['config_updated'] = True

    state = _lv2_state(name)
    target = target_ref.get('target') or 'sdb'

    if 'running' in state:
        v = "sudo -n virsh -c '" + VIRSH_URI + "'"
        if iso_path:
            cmds = [
                v + " change-media " + shq(name) + " " + shq(target) + " " + shq(iso_path) + " --insert --live",
                v + " change-media " + shq(name) + " " + shq(target) + " " + shq(iso_path) + " --update --live",
            ]
        else:
            cmds = [
                v + " change-media " + shq(name) + " " + shq(target) + " --eject --live",
                v + " change-media " + shq(name) + " " + shq(target) + " --eject",
            ]

        last_error = ''
        for cmd in cmds:
            out, err, code = ssh_exec(cmd, timeout=30)
            if code == 0:
                result['live_updated'] = True
                break
            last_error = (err or out or ('exit ' + str(code))).strip()

        if not result['live_updated']:
            result['warning'] = (
                "Configuration persistante modifiée, mais la VM est active et "
                "l'application live du CD-ROM a échoué. Redémarrer la VM si l'ISO "
                "n'est pas visible dans l'OS invité. Détail : " + last_error
            )

    return result


def _lv2_set_boot(name, order):
    import xml.etree.ElementTree as ET

    allowed = ('cdrom', 'hd', 'network', 'fd')
    clean = []
    for item in order or []:
        item = str(item).strip().lower()
        if item in allowed and item not in clean:
            clean.append(item)

    if not clean:
        raise RuntimeError('Ordre de boot vide')

    applied_ref = {'applied': []}

    def patch(xml):
        root = ET.fromstring(xml)

        os_el = root.find('os')
        if os_el is not None:
            for boot in list(os_el.findall('boot')):
                os_el.remove(boot)

        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        for node in devices.iter():
            for boot in list(node.findall('boot')):
                node.remove(boot)

        targets = {'cdrom': None, 'hd': None, 'network': None, 'fd': None}

        for disk in devices.findall('disk'):
            dev = disk.get('device', '')
            if dev == 'cdrom' and targets['cdrom'] is None:
                targets['cdrom'] = disk
            elif dev == 'disk' and targets['hd'] is None:
                targets['hd'] = disk
            elif dev == 'floppy' and targets['fd'] is None:
                targets['fd'] = disk

        for iface in devices.findall('interface'):
            if targets['network'] is None:
                targets['network'] = iface

        applied = []
        for dev in clean:
            node = targets.get(dev)
            if node is None:
                continue
            boot = ET.Element('boot', {'order': str(len(applied) + 1)})
            _lv2_insert_before_alias_or_address(node, boot)
            applied.append(dev)

        if not applied:
            raise RuntimeError('Aucun périphérique de boot correspondant trouvé')

        applied_ref['applied'] = applied
        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'set_boot',
        'order': applied_ref['applied'],
    }


# MDM-LIBVIRT2-STATE-20260710
# Actions état VM V2 isolées sur /libvirt2.
def _lv2_vm_state_action(name, action):
    action = str(action or '').strip().lower()

    commands = {
        'start': 'start',
        'shutdown': 'shutdown',
        'destroy': 'destroy',
        'reboot': 'reboot',
        'reset': 'reset',
        'suspend': 'suspend',
        'resume': 'resume',
        'wakeup': 'dompmwakeup',
    }

    if action not in commands:
        raise RuntimeError('Action état inconnue : ' + action)

    cmd = (
        "sudo -n virsh -c '" + VIRSH_URI + "' "
        + commands[action] + " " + shq(name)
    )

    out, err, code = ssh_exec(cmd, timeout=45)
    if code != 0:
        raise RuntimeError((err or out or ('exit ' + str(code))).strip())

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': action,
        'message': (out or '').strip(),
    }


# ── Persistance de l'autostart (survit à une réimportation après MAJ TrueNAS) ──
def _vm_autostart_store():
    return os.path.join(ACCESS_DATA_DIR, 'vm_autostart.json')


def _vm_autostart_read():
    d = _access_read_json(_vm_autostart_store(), {})
    return d if isinstance(d, dict) else {}


def _vm_autostart_set(name, enabled):
    try:
        d = _vm_autostart_read()
        d[str(name)] = bool(enabled)
        _access_write_json(_vm_autostart_store(), d)
    except Exception as e:
        log.warning('vm autostart persist: %s', e)


def _lv2_repair_uefi(name):
    """Régénère le NVRAM UEFI d'une VM (sauvegarde l'ancien) puis la redémarre.
    Utile si une mise à jour d'OVMF casse l'amorçage (Shell UEFI au lieu de l'OS)."""
    import xml.etree.ElementTree as ET
    xml_str = ssh_ok("sudo -n virsh -c '" + VIRSH_URI + "' dumpxml --inactive " + shq(name)
                     + " 2>/dev/null || sudo -n virsh -c '" + VIRSH_URI + "' dumpxml " + shq(name))
    nvram_path = None
    tpl_from_xml = None
    secure = False
    try:
        rootx = ET.fromstring(xml_str)
        nv = rootx.find('./os/nvram')
        if nv is not None:
            if (nv.text or '').strip():
                nvram_path = nv.text.strip()
            tpl_from_xml = nv.get('template')
        loader = rootx.find('./os/loader')
        if loader is not None and loader.get('secure') == 'yes':
            secure = True
        fw = rootx.find('./os/firmware')
        if fw is not None:
            for feat in fw.findall('feature'):
                if feat.get('name') == 'secure-boot' and feat.get('enabled') == 'yes':
                    secure = True
    except Exception:
        pass
    if tpl_from_xml and '.ms.' in tpl_from_xml:
        secure = True
    if not nvram_path:
        nvram_path = os.path.join(VM_DIR, name + '_VARS.fd')
    # On repart d'un NVRAM en « mode Setup » (variables OVMF neutres, sans clé
    # enrôlée) : Secure Boot n'est PAS imposé, donc OVMF amorce n'importe quel OS
    # (Windows, BSD, Linux) sans « Access Denied ». C'est le repli le plus fiable
    # pour rétablir le démarrage après une MAJ d'OVMF. (Le Secure Boot pourra être
    # réactivé ensuite depuis le firmware si besoin.)
    _ = (secure, tpl_from_xml)  # détection conservée pour usage futur
    src_tpl = '/usr/share/OVMF/OVMF_VARS_4M.fd'
    ts = _sh_time.strftime('%Y%m%d-%H%M%S')
    u = "'" + VIRSH_URI + "'"
    script = (
        'sudo -n virsh -c ' + u + ' destroy ' + shq(name) + ' >/dev/null 2>&1 || true; '
        'if [ -f ' + shq(nvram_path) + ' ]; then '
        'sudo -n cp -f ' + shq(nvram_path) + ' ' + shq(nvram_path + '.bak-' + ts) + ' 2>/dev/null || true; fi; '
        # Pré-amorce le NVRAM avec le bon template (clés MS si Secure Boot) ;
        # sinon on le supprime pour que libvirt le recrée depuis le template du XML.
        'if [ -f ' + shq(src_tpl) + ' ]; then sudo -n cp -f ' + shq(src_tpl) + ' ' + shq(nvram_path) + ' 2>/dev/null || true; '
        'else sudo -n rm -f ' + shq(nvram_path) + ' >/dev/null 2>&1 || true; fi; '
        'sudo -n virsh -c ' + u + ' start ' + shq(name) + ' 2>&1'
    )
    out, err, code = ssh_exec(script, timeout=90)
    combined = ((out or '') + '\n' + (err or '')).strip()
    started = ('started' in combined.lower()) or ('démarr' in combined.lower())
    # réapplique l'autostart si la VM en avait
    if started and _vm_autostart_read().get(str(name)):
        ssh_exec("sudo -n virsh -c " + u + " autostart " + shq(name), timeout=20)
    return {
        'api': 'libvirt2', 'ok': bool(started), 'name': name, 'action': 'repair-uefi',
        'nvram': nvram_path,
        'message': ('NVRAM UEFI régénéré et VM redémarrée. Rouvrez la console pour vérifier le boot.'
                    if started else ('Échec : ' + (combined[-400:] or 'raison inconnue'))),
    }


def _lv2_set_disk_bus(name, bus):
    """Change l'interface du disque principal d'une VM existante (virtio/sata/scsi).
    La VM doit être arrêtée. Ajoute un contrôleur virtio-scsi si besoin et persiste
    le XML dans VM_DIR pour survivre à une réimportation."""
    import xml.etree.ElementTree as ET
    bus = str(bus or '').strip().lower()
    if bus not in ('virtio', 'sata', 'scsi'):
        raise ValueError('Bus invalide : virtio, sata ou scsi.')
    st = _lv2_state(name)
    if 'run' in st or 'paus' in st:
        raise ValueError('Arrêtez la VM avant de changer le bus disque.')
    dev_map = {'virtio': 'vda', 'sata': 'sda', 'scsi': 'sda'}

    def patch(xml_text):
        root = ET.fromstring(xml_text)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('XML invalide (aucun <devices>).')
        disk = None
        for d in devices.findall('disk'):
            if d.get('device', 'disk') == 'disk':
                disk = d
                break
        if disk is None:
            raise RuntimeError('Disque principal introuvable.')
        tgt = disk.find('target')
        if tgt is None:
            tgt = ET.SubElement(disk, 'target')
        tgt.set('bus', bus)
        tgt.set('dev', dev_map[bus])
        addr = disk.find('address')
        if addr is not None:
            disk.remove(addr)  # bus-spécifique : laisser libvirt réattribuer
        if bus == 'scsi' and not any(c.get('type') == 'scsi' for c in devices.findall('controller')):
            c = ET.SubElement(devices, 'controller')
            c.set('type', 'scsi')
            c.set('model', 'virtio-scsi')
            c.set('index', '0')
        return ET.tostring(root, encoding='unicode')

    _lv2_xml_patch(name, patch)
    try:
        new_xml = _lv2_dumpxml(name)
        with open(os.path.join(VM_DIR, name + '.xml'), 'w', encoding='utf-8') as f:
            f.write(new_xml)
    except Exception as e:
        log.warning('persist disk bus xml: %s', e)
    return {
        'api': 'libvirt2', 'ok': True, 'name': name, 'action': 'disk-bus', 'bus': bus,
        'message': 'Bus disque changé en ' + bus + '. Prend effet au prochain démarrage.',
    }


def _lv2_set_autostart(name, enabled):
    enabled = bool(enabled)

    if enabled:
        cmd = "sudo -n virsh -c '" + VIRSH_URI + "' autostart " + shq(name)
    else:
        cmd = "sudo -n virsh -c '" + VIRSH_URI + "' autostart --disable " + shq(name)

    out, err, code = ssh_exec(cmd, timeout=30)
    if code != 0:
        raise RuntimeError((err or out or ('exit ' + str(code))).strip())

    _vm_autostart_set(name, enabled)
    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'autostart',
        'enabled': enabled,
        'message': (out or '').strip(),
    }


# MDM-LIBVIRT2-RESOURCES-20260710
# Modification persistante CPU/RAM via XML libvirt.
# Ne tente pas de hotplug live : changement appliqué au prochain démarrage si VM active.
def _lv2_set_resources(name, vcpus, memory_mb):
    import xml.etree.ElementTree as ET

    try:
        vcpus = int(vcpus)
        memory_mb = int(memory_mb)
    except Exception:
        raise RuntimeError('CPU/RAM invalides')

    if vcpus < 1 or vcpus > 256:
        raise RuntimeError('Nombre de vCPU invalide : attendu entre 1 et 256')

    if memory_mb < 256 or memory_mb > 1048576:
        raise RuntimeError('Mémoire invalide : attendu entre 256 MB et 1048576 MB')

    def _insert_after(root, after_tag, node):
        children = list(root)
        for idx, child in enumerate(children):
            if child.tag == after_tag:
                root.insert(idx + 1, node)
                return
        root.insert(0, node)

    def patch(xml):
        root = ET.fromstring(xml)

        mem = root.find('memory')
        if mem is None:
            mem = ET.Element('memory', {'unit': 'MiB'})
            _insert_after(root, 'uuid', mem)

        mem.set('unit', 'MiB')
        mem.text = str(memory_mb)

        cur = root.find('currentMemory')
        if cur is None:
            cur = ET.Element('currentMemory', {'unit': 'MiB'})
            _insert_after(root, 'memory', cur)

        cur.set('unit', 'MiB')
        cur.text = str(memory_mb)

        vcpu = root.find('vcpu')
        if vcpu is None:
            vcpu = ET.Element('vcpu', {'placement': 'static'})
            _insert_after(root, 'currentMemory', vcpu)

        if not vcpu.get('placement'):
            vcpu.set('placement', 'static')
        vcpu.text = str(vcpus)

        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'set_resources',
        'vcpus': vcpus,
        'memory_mb': memory_mb,
        'persistent': True,
        'live_applied': False,
        'warning': (
            'VM active : CPU/RAM modifiés dans la configuration persistante. '
            'Redémarrer la VM pour appliquer.'
            if running else ''
        )
    }


# MDM-LIBVIRT2-DISKS-20260710
# Disques V2 — ajout disque qcow2 persistant.
def _lv2_safe_name(value, fallback='disk'):
    import re
    value = str(value or '').strip()
    value = re.sub(r'[^A-Za-z0-9_.-]+', '-', value)
    value = value.strip('-._')
    return value or fallback


def _lv2_remote_path_available(path):
    out, err, code = ssh_exec("[ ! -e " + shq(path) + " ]", timeout=10)
    return code == 0


def _lv2_pick_disk_path(vm_name, label):
    safe_vm = _lv2_safe_name(vm_name, 'vm')
    safe_label = _lv2_safe_name(label, 'data')

    base = os.path.join(VM_DIR, safe_vm + '-' + safe_label + '.qcow2')
    if _lv2_remote_path_available(base):
        return base

    for i in range(2, 100):
        candidate = os.path.join(VM_DIR, safe_vm + '-' + safe_label + '-' + str(i) + '.qcow2')
        if _lv2_remote_path_available(candidate):
            return candidate

    raise RuntimeError('Impossible de trouver un nom de disque disponible')


def _lv2_pick_target(used, bus):
    bus = str(bus or 'virtio').lower()

    if bus == 'virtio':
        prefix = 'vd'
    else:
        prefix = 'sd'

    letters = 'abcdefghijklmnopqrstuvwxyz'
    for a in letters:
        target = prefix + a
        if target not in used:
            return target

    for a in letters:
        for b in letters:
            target = prefix + a + b
            if target not in used:
                return target

    raise RuntimeError('Aucun target disque disponible')


def _lv2_add_disk(name, size_gb, bus='virtio', label='data'):
    import xml.etree.ElementTree as ET

    try:
        size_gb = int(size_gb)
    except Exception:
        raise RuntimeError('Taille disque invalide')

    if size_gb < 1 or size_gb > 16384:
        raise RuntimeError('Taille disque invalide : attendu entre 1 GB et 16384 GB')

    bus = str(bus or 'virtio').strip().lower()
    if bus not in ('virtio', 'sata', 'scsi'):
        raise RuntimeError('Bus disque invalide : virtio, sata ou scsi attendu')

    label = _lv2_safe_name(label, 'data')
    disk_path = _lv2_pick_disk_path(name, label)

    created = False
    target_ref = {'target': ''}

    try:
        ssh_ok(
            "mkdir -p " + shq(VM_DIR) + " && qemu-img create -f qcow2 "
            + shq(disk_path) + " " + shq(str(size_gb) + "G"),
            timeout=300
        )
        created = True

        def patch(xml):
            root = ET.fromstring(xml)
            devices = root.find('devices')
            if devices is None:
                raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

            used = set()
            last_disk_index = -1

            children = list(devices)
            for idx, node in enumerate(children):
                if node.tag == 'disk':
                    last_disk_index = idx
                    target_el = node.find('target')
                    if target_el is not None and target_el.get('dev'):
                        used.add(target_el.get('dev'))

            target = _lv2_pick_target(used, bus)
            target_ref['target'] = target

            disk = ET.Element('disk', {'type': 'file', 'device': 'disk'})
            disk.append(ET.Element('driver', {
                'name': 'qemu',
                'type': 'qcow2',
                'cache': 'none',
                'discard': 'unmap'
            }))
            disk.append(ET.Element('source', {'file': disk_path}))
            disk.append(ET.Element('target', {'dev': target, 'bus': bus}))

            if last_disk_index >= 0:
                devices.insert(last_disk_index + 1, disk)
            else:
                devices.append(disk)

            return _lv2_xml_to_text(root)

        _lv2_xml_patch(name, patch)

    except Exception:
        if created:
            ssh_exec("rm -f " + shq(disk_path), timeout=20)
        raise

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'add_disk',
        'disk': {
            'path': disk_path,
            'size_gb': size_gb,
            'bus': bus,
            'target': target_ref.get('target', ''),
            'format': 'qcow2'
        },
        'persistent': True,
        'live_applied': False,
        'warning': (
            'VM active : disque ajouté à la configuration persistante. '
            'Redémarrer la VM pour voir le nouveau disque dans le système invité.'
            if running else ''
        )
    }


# MDM-LIBVIRT2-DISK-RESIZE-20260710
# Redimensionnement protégé : qcow2 uniquement, agrandissement uniquement.
def _lv2_qcow2_virtual_size_bytes(path):
    import json
    import re

    out, err, code = ssh_exec(
        "qemu-img info --output=json " + shq(path),
        timeout=30
    )

    if code == 0 and out.strip():
        try:
            data = json.loads(out)
            return int(data.get('virtual-size') or data.get('virtual_size') or 0)
        except Exception:
            pass

    out = ssh_ok("qemu-img info " + shq(path), timeout=30)
    m = re.search(r'virtual size:.*\((\d+) bytes\)', out)
    if m:
        return int(m.group(1))

    raise RuntimeError('Impossible de lire la taille virtuelle qcow2')


def _lv2_disk_in_inactive_xml(name, disk_path):
    import xml.etree.ElementTree as ET

    xml = _lv2_dumpxml(name)
    root = ET.fromstring(xml)
    devices = root.find('devices')

    if devices is None:
        return None

    for disk in devices.findall('disk'):
        if disk.get('device') != 'disk':
            continue

        src = disk.find('source')
        tgt = disk.find('target')
        drv = disk.find('driver')

        if src is not None and src.get('file') == disk_path:
            return {
                'target': tgt.get('dev', '') if tgt is not None else '',
                'bus': tgt.get('bus', '') if tgt is not None else '',
                'format': drv.get('type', '') if drv is not None else '',
                'driver': drv.get('name', '') if drv is not None else '',
            }

    return None


def _lv2_resize_disk(name, disk_path, new_size_gb):
    disk_path = str(disk_path or '').strip()

    if not disk_path:
        raise RuntimeError('Chemin disque manquant')

    if not disk_path.startswith(VM_DIR.rstrip('/') + '/'):
        raise RuntimeError('Par sécurité, seuls les disques dans VM_DIR peuvent être redimensionnés')

    if not disk_path.lower().endswith('.qcow2'):
        raise RuntimeError('Seuls les disques qcow2 sont pris en charge')

    try:
        new_size_gb = int(new_size_gb)
    except Exception:
        raise RuntimeError('Nouvelle taille invalide')

    if new_size_gb < 1 or new_size_gb > 16384:
        raise RuntimeError('Nouvelle taille invalide : attendu entre 1 GB et 16384 GB')

    info = _lv2_disk_in_inactive_xml(name, disk_path)
    if not info:
        raise RuntimeError('Ce disque n’est pas attaché à cette VM dans la configuration libvirt')

    if (info.get('format') or '').lower() not in ('qcow2', ''):
        raise RuntimeError('Format disque non pris en charge : ' + str(info.get('format')))

    current_bytes = _lv2_qcow2_virtual_size_bytes(disk_path)
    new_bytes = new_size_gb * 1024 * 1024 * 1024

    if new_bytes <= current_bytes:
        current_gb = round(current_bytes / 1024 / 1024 / 1024, 2)
        raise RuntimeError(
            'Réduction interdite. Taille actuelle : '
            + str(current_gb)
            + ' GB. Choisir une taille supérieure.'
        )

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    live_attached = False
    if running:
        for d in virsh_domblklist(name):
            if d.get('source') == disk_path:
                live_attached = True
                break

    if live_attached:
        raise RuntimeError(
            'Disque attaché live à une VM active. '
            'Arrêter la VM avant redimensionnement pour éviter tout risque.'
        )

    out, err, code = ssh_exec(
        "qemu-img resize " + shq(disk_path) + " " + shq(str(new_size_gb) + "G"),
        timeout=300
    )

    if code != 0:
        raise RuntimeError((err or out or ('exit ' + str(code))).strip())

    after_bytes = _lv2_qcow2_virtual_size_bytes(disk_path)

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'resize_disk',
        'disk': {
            'path': disk_path,
            'target': info.get('target', ''),
            'bus': info.get('bus', ''),
            'old_size_gb': round(current_bytes / 1024 / 1024 / 1024, 2),
            'new_size_gb': round(after_bytes / 1024 / 1024 / 1024, 2),
        },
        'warning': (
            'VM active : disque redimensionné côté image qcow2, mais il ne sera visible '
            'dans le système invité qu’après redémarrage si le disque n’est pas attaché live.'
            if running else ''
        )
    }


# MDM-LIBVIRT2-DISK-REMOVE-20260710
# Détachement / suppression disque protégée.
def _lv2_any_vm_references_path(path):
    refs = []
    for vm_name in _lv2_vm_names():
        try:
            xml = _lv2_dumpxml(vm_name)
            if path in xml:
                refs.append(vm_name)
        except Exception:
            pass
    return refs


def _lv2_remove_disk(name, disk_path, delete_file=False):
    import xml.etree.ElementTree as ET

    disk_path = str(disk_path or '').strip()
    delete_file = bool(delete_file)

    if not disk_path:
        raise RuntimeError('Chemin disque manquant')

    if not disk_path.startswith(VM_DIR.rstrip('/') + '/'):
        raise RuntimeError('Par sécurité, seuls les disques dans VM_DIR peuvent être détachés')

    if not disk_path.lower().endswith('.qcow2'):
        raise RuntimeError('Seuls les disques qcow2 sont pris en charge')

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    live_attached = False
    if running:
        for d in virsh_domblklist(name):
            if d.get('source') == disk_path:
                live_attached = True
                break

    if live_attached:
        raise RuntimeError(
            'Disque attaché live à une VM active. '
            'Arrêter la VM avant détachement/suppression.'
        )

    removed_ref = {'target': '', 'bus': '', 'source': disk_path, 'format': ''}

    def patch(xml):
        root = ET.fromstring(xml)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        disks = [d for d in devices.findall('disk') if d.get('device') == 'disk']
        if len(disks) <= 1:
            raise RuntimeError('Refus : impossible de supprimer le seul disque de la VM')

        found = None

        for disk in disks:
            src = disk.find('source')
            tgt = disk.find('target')
            drv = disk.find('driver')
            boot = disk.find('boot')

            if src is not None and src.get('file') == disk_path:
                target = tgt.get('dev', '') if tgt is not None else ''
                bus = tgt.get('bus', '') if tgt is not None else ''
                fmt = drv.get('type', '') if drv is not None else ''

                protected_targets = ('vda', 'sda', 'hda', 'xvda')
                if target in protected_targets:
                    raise RuntimeError('Refus : le disque ' + target + ' semble être un disque principal')

                if boot is not None:
                    raise RuntimeError('Refus : ce disque possède un ordre de boot')

                found = disk
                removed_ref['target'] = target
                removed_ref['bus'] = bus
                removed_ref['format'] = fmt
                break

        if found is None:
            raise RuntimeError('Disque introuvable dans la configuration libvirt de cette VM')

        devices.remove(found)
        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    deleted = False
    delete_warning = ''

    if delete_file:
        refs = _lv2_any_vm_references_path(disk_path)
        if refs:
            raise RuntimeError(
                'Disque détaché de la VM demandée, mais fichier non supprimé car encore référencé par : '
                + ', '.join(refs)
            )

        out, err, code = ssh_exec("rm -f " + shq(disk_path), timeout=30)
        if code != 0:
            raise RuntimeError((err or out or ('exit ' + str(code))).strip())
        deleted = True
    else:
        delete_warning = 'Fichier qcow2 conservé sur le stockage.'

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'remove_disk',
        'disk': removed_ref,
        'detached': True,
        'file_deleted': deleted,
        'warning': delete_warning,
    }


# MDM-LIBVIRT2-NETWORK-20260710
# Réseau V2 : ajout / retrait protégé de cartes réseau.
def _lv2_mac_norm(mac):
    import re
    mac = str(mac or '').strip().lower()
    if not re.match(r'^[0-9a-f]{2}(:[0-9a-f]{2}){5}$', mac):
        raise RuntimeError('Adresse MAC invalide')
    return mac


def _lv2_all_mac_addresses():
    import xml.etree.ElementTree as ET

    macs = set()

    for vm_name in _lv2_vm_names():
        try:
            xml = _lv2_dumpxml(vm_name)
            root = ET.fromstring(xml)
            devices = root.find('devices')
            if devices is None:
                continue

            for iface in devices.findall('interface'):
                mac = iface.find('mac')
                if mac is not None and mac.get('address'):
                    macs.add(mac.get('address').lower())
        except Exception:
            pass

    return macs


def _lv2_mac_usable(mac, own=None):
    """Adresse MAC choisie à la main : bien formée, adresse de carte (pas de groupe), et libre.
    « own » est l'adresse actuelle de la carte qu'on modifie : elle a le droit de rester la même."""
    mac = _lv2_mac_norm(mac)
    if int(mac[:2], 16) & 1:
        raise RuntimeError('Adresse MAC refusée : le premier octet doit être pair (adresse d’une seule carte).')
    if mac == '00:00:00:00:00:00':
        raise RuntimeError('Adresse MAC invalide')
    if mac != own and mac in _lv2_all_mac_addresses():
        raise RuntimeError('Adresse MAC déjà utilisée par une carte d’une machine virtuelle.')
    return mac


def _lv2_generate_mac():
    import random

    used = _lv2_all_mac_addresses()

    for _ in range(200):
        mac = '52:54:00:%02x:%02x:%02x' % (
            random.randint(0x00, 0xff),
            random.randint(0x00, 0xff),
            random.randint(0x00, 0xff)
        )
        if mac not in used:
            return mac

    raise RuntimeError('Impossible de générer une MAC disponible')


def _lv2_list_network_sources():
    import xml.etree.ElementTree as ET

    sources = {
        'br0': {'kind': 'bridge', 'name': 'br0', 'label': 'br0'}
    }

    # Réseaux virtuels libvirt fournissant le NAT, notamment "default".
    net_out, net_err, net_code = ssh_exec(
        "sudo -n virsh -c '" + VIRSH_URI + "' net-list --all --name",
        timeout=20
    )
    if net_code == 0:
        for line in net_out.splitlines():
            network = line.strip()
            if network:
                sources['network:' + network] = {
                    'kind': 'nat',
                    'name': network,
                    'label': network,
                }

    # Bridges détectés côté hôte si possible.
    cmd = 'for i in /sys/class/net/*; do [ -d "$i/bridge" ] && basename "$i"; done'
    out, err, code = ssh_exec("sh -lc " + shq(cmd), timeout=20)
    if code == 0:
        for line in out.splitlines():
            br = line.strip()
            if br:
                sources[br] = {'kind': 'bridge', 'name': br, 'label': br}

    # Sources déjà utilisées dans les XML libvirt.
    for vm_name in _lv2_vm_names():
        try:
            xml = _lv2_dumpxml(vm_name)
            root = ET.fromstring(xml)
            devices = root.find('devices')
            if devices is None:
                continue

            for iface in devices.findall('interface'):
                src = iface.find('source')
                if src is None:
                    continue

                bridge = src.get('bridge') or src.get('dev') or src.get('network') or ''
                if bridge and not any(item.get('name') == bridge and item.get('kind') == (iface.get('type', 'bridge') or 'bridge') for item in sources.values()):
                    sources[bridge] = {
                        'kind': iface.get('type', 'bridge') or 'bridge',
                        'name': bridge,
                        'label': bridge,
                    }
        except Exception:
            pass

    return sorted(sources.values(), key=lambda x: x.get('name', ''))


def _lv2_insert_interface_node(devices, iface):
    children = list(devices)
    for idx, node in enumerate(children):
        if node.tag in ('input', 'graphics', 'video', 'sound', 'memballoon'):
            devices.insert(idx, iface)
            return
    devices.append(iface)


def _lv2_add_nic(name, source='default', model='virtio', net_type='nat', mac=''):
    import re
    import xml.etree.ElementTree as ET

    source = str(source or '').strip()
    model = str(model or 'virtio').strip().lower()
    net_type = str(net_type or 'nat').strip().lower()

    if net_type not in ('nat', 'bridge'):
        raise RuntimeError('Type réseau invalide : utiliser nat ou bridge')

    if not re.match(r'^[A-Za-z0-9_.:-]+$', source):
        raise RuntimeError('Source réseau invalide')

    allowed_models = ('virtio', 'e1000', 'rtl8139', 'vmxnet3')
    if model not in allowed_models:
        raise RuntimeError('Modèle carte réseau invalide')

    # adresse choisie par l'utilisateur, sinon tirée au hasard parmi les adresses libres
    mac = _lv2_mac_usable(mac) if str(mac or '').strip() else _lv2_generate_mac()

    def patch(xml):
        root = ET.fromstring(xml)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        iface_type = 'network' if net_type == 'nat' else 'bridge'
        source_attr = 'network' if net_type == 'nat' else 'bridge'
        iface = ET.Element('interface', {'type': iface_type})
        iface.append(ET.Element('mac', {'address': mac}))
        iface.append(ET.Element('source', {source_attr: source}))
        iface.append(ET.Element('model', {'type': model}))

        _lv2_insert_interface_node(devices, iface)

        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'add_nic',
        'nic': {
            'type': 'network' if net_type == 'nat' else 'bridge',
            'source': source,
            'model': model,
            'mac': mac,
        },
        'persistent': True,
        'live_applied': False,
        'warning': (
            'VM active : carte réseau ajoutée à la configuration persistante. '
            'Redémarrer la VM pour qu’elle apparaisse dans le système invité.'
            if running else ''
        )
    }


def _lv2_set_nic_mac(name, mac, new_mac):
    """Remplace l'adresse MAC d'une carte dans la configuration persistante de la machine."""
    import xml.etree.ElementTree as ET

    mac = _lv2_mac_norm(mac)
    new_mac = _lv2_mac_usable(new_mac, own=mac)

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    if new_mac != mac:
        def patch(xml):
            root = ET.fromstring(xml)
            devices = root.find('devices')
            if devices is None:
                raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')
            for iface in devices.findall('interface'):
                mac_el = iface.find('mac')
                if mac_el is not None and (mac_el.get('address') or '').lower() == mac:
                    mac_el.set('address', new_mac)
                    return _lv2_xml_to_text(root)
            raise RuntimeError('Carte réseau introuvable dans la configuration libvirt')

        _lv2_xml_patch(name, patch)

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'set_nic_mac',
        'nic': {'mac': new_mac, 'previous': mac},
        'changed': new_mac != mac,
        'persistent': True,
        'warning': (
            'VM active : la nouvelle adresse MAC est enregistrée. '
            'Arrêter puis démarrer la VM pour qu’elle soit prise en compte.'
            if running and new_mac != mac else ''
        )
    }


def _lv2_remove_nic(name, mac):
    import xml.etree.ElementTree as ET

    mac = _lv2_mac_norm(mac)

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    if running:
        out, err, code = ssh_exec(
            "sudo -n virsh -c '" + VIRSH_URI + "' domiflist " + shq(name),
            timeout=20
        )
        if mac in (out or '').lower():
            raise RuntimeError(
                'Carte réseau attachée live à une VM active. '
                'Arrêter la VM avant suppression pour éviter toute coupure réseau.'
            )

    removed_ref = {'mac': mac}

    def patch(xml):
        root = ET.fromstring(xml)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        ifaces = devices.findall('interface')
        if len(ifaces) <= 1:
            raise RuntimeError('Refus : impossible de supprimer la dernière carte réseau')

        found = None

        for iface in ifaces:
            mac_el = iface.find('mac')
            if mac_el is None:
                continue

            if (mac_el.get('address') or '').lower() == mac:
                boot = iface.find('boot')
                if boot is not None:
                    raise RuntimeError('Refus : cette carte réseau possède un ordre de boot')

                src = iface.find('source')
                mdl = iface.find('model')

                removed_ref['type'] = iface.get('type', '')
                removed_ref['source'] = (
                    src.get('bridge') or src.get('dev') or src.get('network') or ''
                    if src is not None else ''
                )
                removed_ref['model'] = mdl.get('type', '') if mdl is not None else ''

                found = iface
                break

        if found is None:
            raise RuntimeError('Carte réseau introuvable dans la configuration libvirt')

        devices.remove(found)
        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'remove_nic',
        'nic': removed_ref,
        'persistent': True,
        'warning': 'Carte réseau retirée de la configuration persistante.'
    }


# MDM-LIBVIRT2-SNAPSHOTS-20260710
# Snapshots V2 prudents : création/restauration/suppression uniquement VM arrêtée.
def _lv2_snapshot_vm_must_be_stopped(name):
    state = _lv2_state(name)
    if 'running' in state or 'paused' in state:
        raise RuntimeError(
            'Action snapshot refusée : arrêter la VM avant création/restauration/suppression.'
        )
    return state


def _lv2_snapshot_name_norm(name):
    import re

    name = str(name or '').strip()
    if not name:
        raise RuntimeError('Nom snapshot manquant')

    if not re.match(r'^[A-Za-z0-9_.:-]+$', name):
        raise RuntimeError('Nom snapshot invalide')

    return name


# ── MDM-SHARE-LINKS-V1-BEGIN ────────────────────────────────────────────────
# Liens de partage publics : partager un fichier (ou un dossier en .zip) via une
# URL /s/<token> accessible sans compte TrueNAS. Options : expiration, mot de
# passe (pbkdf2), limite de téléchargements. Stockés dans data/shares.json.
import secrets as _sh_secrets
import hashlib as _sh_hashlib
import hmac as _sh_hmac
import time as _sh_time
import zipfile as _sh_zipfile
import mimetypes as _sh_mimetypes
import shutil as _sh_shutil
import html as _sh_html
from datetime import datetime as _sh_dt, timezone as _sh_tz

SHARE_FILE = os.path.join(ACCESS_DATA_DIR, 'shares.json')
SHARE_ROOT = os.environ.get('SHARE_ROOT', '/mnt')
_share_lock = _access_threading.RLock()


def _share_read():
    with _share_lock:
        data = _access_read_json(SHARE_FILE, {})
        return data if isinstance(data, dict) else {}


def _share_write(data):
    with _share_lock:
        _access_write_json(SHARE_FILE, data)


def _share_now():
    return int(_sh_time.time())


def _share_purge():
    """Supprime les liens définitivement inutilisables (révoqués, expirés,
    limite de téléchargements atteinte). Renvoie le dictionnaire nettoyé."""
    now = _share_now()
    with _share_lock:
        data = _share_read()
        changed = False
        for tok in list(data.keys()):
            rec = data[tok]
            if not isinstance(rec, dict):
                del data[tok]
                changed = True
                continue
            exp = rec.get('expires_at')
            maxd = rec.get('max_downloads')
            expired = exp and now > int(exp)
            exhausted = maxd and int(rec.get('downloads', 0)) >= int(maxd)
            if rec.get('revoked') or expired or exhausted:
                del data[tok]
                changed = True
        if changed:
            _share_write(data)
        return data


def _share_purge_loop(interval=600):
    while True:
        try:
            _share_purge()
        except Exception as exc:
            log.warning('share purge error: %s', exc)
        _sh_time.sleep(interval)


def _share_fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '—'
    for unit in ('o', 'Ko', 'Mo', 'Go'):
        if n < 1024:
            return ('%.0f %s' if unit == 'o' else '%.1f %s') % (n, unit)
        n /= 1024
    return '%.1f To' % n


def _share_dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _share_size(rec):
    # Pour un dossier, on NE parcourt PAS l'arborescence (trop coûteux sur un NAS,
    # peut bloquer /share/list). La taille n'est affichée que pour les fichiers.
    if rec.get('is_dir'):
        return None
    try:
        return os.path.getsize(rec.get('path', ''))
    except OSError:
        return None


def _share_hash_pw(password, salt=None):
    salt = salt or _sh_secrets.token_hex(16)
    dk = _sh_hashlib.pbkdf2_hmac(
        'sha256', (password or '').encode('utf-8'), salt.encode('utf-8'), 120000
    )
    return salt + '$' + dk.hex()


def _share_verify_pw(password, stored):
    if not stored or '$' not in str(stored):
        return False
    salt = str(stored).split('$', 1)[0]
    return _sh_hmac.compare_digest(_share_hash_pw(password or '', salt), stored)


def _share_valid(rec):
    if not rec:
        return (False, 'Lien introuvable')
    if rec.get('revoked'):
        return (False, 'Lien révoqué')
    exp = rec.get('expires_at')
    if exp and _share_now() > int(exp):
        return (False, 'Lien expiré')
    maxd = rec.get('max_downloads')
    if maxd and int(rec.get('downloads', 0)) >= int(maxd):
        return (False, 'Nombre maximum de téléchargements atteint')
    if not os.path.exists(rec.get('path', '')):
        return (False, 'Fichier introuvable sur le serveur')
    return (True, '')


def _share_create(path, expires_in=None, max_downloads=None, password=None):
    real = os.path.realpath(str(path or ''))
    root = SHARE_ROOT.rstrip('/')
    if not (real == root or real.startswith(root + '/')):
        raise PermissionError('Le partage est limité à ' + SHARE_ROOT + '/')
    if not os.path.exists(real):
        raise FileNotFoundError('Chemin introuvable : ' + str(path))
    token = _sh_secrets.token_urlsafe(18)
    rec = {
        'token': token,
        'path': real,
        'name': os.path.basename(real.rstrip('/')) or 'download',
        'is_dir': os.path.isdir(real),
        'created_at': _share_now(),
        'expires_at': (_share_now() + int(expires_in)) if expires_in else None,
        'max_downloads': int(max_downloads) if max_downloads else None,
        'downloads': 0,
        'password': _share_hash_pw(password) if password else None,
        'revoked': False,
    }
    data = _share_read()
    data[token] = rec
    _share_write(data)
    return rec


def _share_public(rec):
    valid, reason = _share_valid(rec)
    return {
        'token': rec.get('token'),
        'name': rec.get('name'),
        'is_dir': bool(rec.get('is_dir')),
        'has_password': bool(rec.get('password')),
        'created_at': rec.get('created_at'),
        'expires_at': rec.get('expires_at'),
        'max_downloads': rec.get('max_downloads'),
        'downloads': int(rec.get('downloads', 0)),
        'size': _share_size(rec),
        'valid': valid,
        'reason': reason,
    }


def _share_iso(ts):
    if not ts:
        return ''
    try:
        return _sh_dt.fromtimestamp(int(ts), _sh_tz.utc).astimezone().strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ''


def _share_content_disposition(filename):
    from urllib.parse import quote
    ascii_name = (filename or 'download').encode('ascii', 'ignore').decode('ascii') or 'download'
    ascii_name = ascii_name.replace('"', '').replace('\\', '')
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quote(filename or 'download'))


_SHARE_CSS = (
    "<style>:root{color-scheme:dark}*{box-sizing:border-box}"
    "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
    "background:radial-gradient(1200px 600px at 50% -10%,#1b2942,#0b1424 60%);"
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#e7edf6;padding:24px}"
    ".card{width:100%;max-width:420px;background:rgba(18,28,48,.72);border:1px solid rgba(255,255,255,.10);"
    "border-radius:18px;padding:34px 30px;text-align:center;backdrop-filter:blur(18px);"
    "box-shadow:0 24px 60px rgba(0,0,0,.45)}"
    ".icon{font-size:52px;line-height:1;margin-bottom:14px}"
    "h1{font-size:20px;margin:0 0 10px;word-break:break-word}"
    ".muted{color:#93a2ba;font-size:13px;margin:0 0 22px;line-height:1.6}"
    ".inp{width:100%;padding:11px 14px;border-radius:11px;border:1px solid rgba(255,255,255,.16);"
    "background:rgba(255,255,255,.06);color:#fff;font-size:14px;margin-bottom:12px;outline:none}"
    ".inp:focus{border-color:#3ea6ff}"
    ".btn{display:inline-block;width:100%;padding:12px 16px;border-radius:11px;border:0;cursor:pointer;"
    "background:linear-gradient(180deg,#3ea6ff,#2b7fe0);color:#fff;font-size:15px;font-weight:600;"
    "text-decoration:none;transition:filter .15s}.btn:hover{filter:brightness(1.08)}"
    ".err{color:#ff8a8a;font-size:13px;margin:12px 0 0;min-height:16px}"
    ".foot{margin-top:20px;font-size:11px;color:#5f7characters}</style>"
).replace('#5f7characters', '#5f7597')


def _share_page(inner, title='Partage'):
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + _sh_html.escape(title) + '</title>' + _SHARE_CSS +
        '</head><body><div class="card">' + inner +
        '<div class="foot">Desktroo</div></div></body></html>'
    )


def _share_error_page(msg):
    return _share_page(
        '<div class="icon">&#128274;</div><h1>Lien indisponible</h1>'
        '<p class="muted">' + _sh_html.escape(msg) + '</p>',
        'Lien indisponible'
    )


def _share_landing_page(rec):
    valid, reason = _share_valid(rec)
    if not valid:
        return _share_error_page(reason)
    pub = _share_public(rec)
    name = _sh_html.escape(pub.get('name') or 'Fichier')
    size = _share_fmt_bytes(pub['size']) if pub['size'] is not None else '—'
    icon = '&#128193;' if pub['is_dir'] else '&#128196;'
    meta = []
    if pub['is_dir']:
        meta.append('Dossier — téléchargé en .zip')
    meta.append('Taille : ' + size)
    if pub['expires_at']:
        meta.append('Expire le ' + _share_iso(pub['expires_at']))
    if pub['max_downloads']:
        meta.append('Téléchargements : ' + str(pub['downloads']) + ' / ' + str(pub['max_downloads']))
    metahtml = ' &middot; '.join(_sh_html.escape(m) for m in meta)
    token = _sh_html.escape(rec.get('token', ''))
    dl = '/s/' + token + '/download'
    if pub['has_password']:
        body = (
            '<div class="icon">' + icon + '</div><h1>' + name + '</h1>'
            '<p class="muted">' + metahtml + '</p>'
            '<input id="pw" type="password" placeholder="Mot de passe" class="inp" autofocus>'
            '<button class="btn" onclick="go()">Télécharger</button>'
            '<p id="err" class="err"></p>'
            '<script>'
            'function go(){var p=document.getElementById("pw").value;'
            'var e=document.getElementById("err");e.textContent="Vérification…";'
            'fetch("' + dl + '?check=1&pw="+encodeURIComponent(p)).then(function(r){'
            'if(r.status===200){e.textContent="";window.location="' + dl + '?pw="+encodeURIComponent(p);}'
            'else{e.textContent="Mot de passe incorrect.";}})'
            '.catch(function(){e.textContent="Erreur réseau.";});}'
            'document.getElementById("pw").addEventListener("keydown",function(ev){'
            'if(ev.key==="Enter")go();});'
            '</script>'
        )
    else:
        body = (
            '<div class="icon">' + icon + '</div><h1>' + name + '</h1>'
            '<p class="muted">' + metahtml + '</p>'
            '<a class="btn" href="' + dl + '">Télécharger</a>'
        )
    return _share_page(body, rec.get('name') or 'Partage')
# ── MDM-SHARE-LINKS-V1-END ──────────────────────────────────────────────────


# ── MDM-WEBSITES-V1-BEGIN ───────────────────────────────────────────────────
# Sites web servis par le conteneur nginx dédié 'desktroo-websites'.
# Types : static | php | proxy. Accès par port (8100-8130) et/ou server_name.
WEB_FILE = os.path.join(ACCESS_DATA_DIR, 'sites.json')
WEB_CONF_DIR = os.environ.get('WEB_CONF_DIR', '/mnt/Truenas_Stockage/apps/desktop/websites/conf.d')
WEB_LOG_DIR = os.environ.get('WEB_LOG_DIR') or os.path.join(os.path.dirname(WEB_CONF_DIR.rstrip('/')), 'logs')
DB_CONTAINER = os.environ.get('DB_CONTAINER', 'desktroo-mariadb')
WEB_PHP_UPSTREAM = os.environ.get('WEB_PHP_UPSTREAM', 'desktroo-php82:9000')
# Versions PHP disponibles : {version: upstream fastcgi}. Un conteneur php-fpm par version.
_WEB_PHP_DEFAULTS = {
    '8.3': 'desktroo-php83:9000',
    '8.2': 'desktroo-php82:9000',
    '8.1': 'desktroo-php81:9000',
    '7.4': 'desktroo-php74:9000',
}
try:
    WEB_PHP_VERSIONS = json.loads(os.environ.get('WEB_PHP_VERSIONS', '') or '{}')
    if not isinstance(WEB_PHP_VERSIONS, dict) or not WEB_PHP_VERSIONS:
        WEB_PHP_VERSIONS = dict(_WEB_PHP_DEFAULTS)
except Exception:
    WEB_PHP_VERSIONS = dict(_WEB_PHP_DEFAULTS)
WEB_PHP_DEFAULT = os.environ.get('WEB_PHP_DEFAULT') or (
    '8.3' if '8.3' in WEB_PHP_VERSIONS else sorted(WEB_PHP_VERSIONS.keys(), reverse=True)[0]
)
WEB_PHP_DIR = os.environ.get('WEB_PHP_DIR', '/mnt/Truenas_Stockage/apps/desktop/websites/php')
WEB_PHP_PROFILE_FILE = os.path.join(ACCESS_DATA_DIR, 'php_profiles.json')
_PHP_INI_KEY_RE = re.compile(r'^[A-Za-z0-9_.]{1,64}$')
_PHP_EXT_RE = re.compile(r'^[A-Za-z0-9_]{1,40}$')
WEB_PORT_MIN = int(os.environ.get('WEB_PORT_MIN', '8100'))
WEB_PORT_MAX = int(os.environ.get('WEB_PORT_MAX', '8130'))
WEB_ROOT_ALLOWED = os.environ.get('WEB_ROOT_ALLOWED', '/mnt')
WEB_DATASET_POOL = os.environ.get('WEB_DATASET_POOL') or (
    VM_DIR.split('/')[2] if VM_DIR.startswith('/mnt/') and len(VM_DIR.split('/')) > 2 else 'Truenas_Stockage'
)
WEB_DATASET_NAME = os.environ.get('WEB_DATASET_NAME', 'Web')
WEB_WWW_GID = os.environ.get('WEB_WWW_GID', '82')  # gid www-data des conteneurs php-fpm alpine
WEB_PROXY_PORT = int(os.environ.get('WEB_PROXY_PORT', '8080'))  # port partagé du reverse-proxy intégré
WEB_PROXY_FILE = os.path.join(ACCESS_DATA_DIR, 'web_proxy.json')
_WEB_DS_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_web_lock = _access_threading.RLock()


def _web_proxy_get():
    d = _access_read_json(WEB_PROXY_FILE, {})
    if not isinstance(d, dict):
        d = {}
    return {'enabled': bool(d.get('enabled', True)), 'port': WEB_PROXY_PORT}


def _web_proxy_set(enabled):
    _access_write_json(WEB_PROXY_FILE, {'enabled': bool(enabled)})
    _web_regenerate()
    return _web_proxy_get()


# ── Couleur du thème de l'installation ────────────────────────────────────────
# Chaque navigateur peut choisir sa couleur ; celle-ci est proposée à ceux qui n'ont rien choisi,
# écran de connexion compris. Ce n'est qu'une couleur (un nom de la palette, ou une teinte libre
# « #rrggbb ») : la lecture est ouverte sans jeton.
BRANDING_FILE = os.path.join(ACCESS_DATA_DIR, 'branding.json')
_BRAND_ACCENTS = ('green', 'blue', 'red', 'amber', 'yellow', 'gray')
_BRAND_HEX = re.compile(r'#[0-9a-f]{6}')


def _branding_ok(accent):
    return isinstance(accent, str) and (accent in _BRAND_ACCENTS or _BRAND_HEX.fullmatch(accent) is not None)


def _branding_get():
    data = _access_read_json(BRANDING_FILE, {})
    accent = data.get('accent') if isinstance(data, dict) else None
    return {'accent': accent if _branding_ok(accent) else 'green'}


def _branding_set(accent):
    accent = str(accent or '').strip().lower()
    if not _branding_ok(accent):
        raise ValueError('couleur inconnue')
    _access_write_json(BRANDING_FILE, {'accent': accent})
    return _branding_get()


# ── Installation comme application (PWA) ─────────────────────────────────────
# Le navigateur propose d'installer Desktroo quand la page annonce un manifeste et des icônes de 192 et
# 512 px. Ils sont servis ici, sans jeton (le navigateur les lit avant toute connexion) : rien de secret.
import base64 as _pwa_b64

_PWA_ICONS = {
    '/app-icon-192.png': ('image/png', _pwa_b64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAAAwFBMVEVOUmAgLkc4R1wfJzc2sX/MzMxoaICVlZUAAEAhXliGj6ASIDro7fYAAAAoNEw9zo0h'
    'LkZteIobKULK0NwqNk6Ol6ewuMUsOFAoNU0qOE4sOE9NWW7T2eRYY3h6hJbFzNjd4uw3RFukrLonNUwXN0Sbo7K8w88tN1AbSU0neGQ4uYNCTmSCjJ1jboH/'
    '//80QVcvl3M9SWAjJkY3RFsrhmpSVGlga34dU1IiLEczqHp1dYeosL4bKEEiYlm/v78AAC3rVfM0AAAAQHRSTlMQShoN/wUQAwT/////AP3//////87//5BS'
    'ba///////////y7///8U////////AXH//xNL/xD//yr/Dv8p/wQFKXaZrQAAB8NJREFUeNrtnXljojoQwGn3eu8Zo0QuORTvs7X3dttu9/t/qwVbu6iQC0KS'
    'fc6/tTI/5sgkwozRpJXLkdf1LcuCQiW5gN/1RpfUahlUuj95rmDFj0Bc7+myGoBfO+URatQkCL1DjP4pDfDl1q9V9wMK//ZLKQBjk3xJuyFN2snlN1+5Ae7d'
    '9OY3pEpqBveeC+B1I1/9HcLmlR3gNonchiKSxPMtI8CNr8Tdz1jBv2EB8JRS/z2cPWqAT6463pP1I5cS4NFSUf9tJMxoAEbKuU8mEkZkAE9Z/bcEHgmgq6b7'
    '/HGjLh5Acf1zCIxD/2k0VCfwigHO1Nc/JTgrAnjUQf+U4DEf4JMe+qe56FMugK9u/jwk8PMAujJ3LvypyMgswI2GPgSjIwBLI/0TAusQoKtLAOzCoLsPcAO1'
    'MkDqRLM9AFcvA6QmcLMA5xIyEAzM3kNQwgTnGQC3bgdCvQ54E2fFS+D+ATDqNoAZgj/yPODcJZ9/ANRtgAjsyRSWMEEKcF+zAVbgQBzOKLh/B6h5DYDhIQCw'
    '+RLR5h3Ar9eDJuBYuMJgW9MZaQjXa4Ac/cGQzwTnW4BuvQYw8wBivnvR3QJY8j2I14esFGBWcxXh5AJEfD40SwC8mgHCXACTD8BLAOpexTrVAaRrmVH7TqZS'
    'AKtpvNZdB1UJ0IbfjFHdO4EqARAcGZ7eAJ7RrXsrUCVAspQZvt4Avv4Alt4Alv4AUG8A+BcAtHUGaJ8ATgAngBPACeAEcAI4AZwA/vcAg6hn25NnZzixbTOC'
    'WgGszOGxRv2JOdABAPamoFgcs600QNt0AEmcC2UB4DoGNBKaSEUAaANqCXtIOQAzBiwSRmoBrPqAVRyoEMAa8IipCgCcAj4ZIiUAghjwSmegAEAPlJFAOoAN'
    'ysmFZIAhKCtrqQAOKC+2RIAhqEJ60gBsUI2YkgB6oCqJpAAEoDoJJQDAGIgWsQBU5VvYd/qxmgCkAI6HF9e7ehOuTEc1gBX+xtvB4T+gaKIUAM6B4h4qvWcT'
    'DWDiVtbiMyA4VAQAk4FC/KOGUawEQLEvEJ/Zhh0FABC2MCMIcuQDFNYQD9VugYQBhNjNSXVFoCiAiN9/2LYRogCeCw566C+B+jIB2gUnDMer13xxd/Xj6m4x'
    '5qwEBQEULGKH+X+8WLZ28nlx+CUXEgHyV9ODtxXGd619uRuzF7OCAPKtv7+AjZetQ1mOmbdDYgAGFBlo3sqRAwJHFoBJNsC41SITXMsCyA2ByV6SXOYDtH7s'
    'fdFUEkCHuAZftYpkwXYmLwaA6EHfW8UyZgpjIQC5b3z1s59YYgCuiLdCOEBAykE4A+yb4FkKQEQqo++wAAuWIBACYGJ+piB60H4iMtUBuCauAR+S+aoHKQA9'
    'QhKa0wMEUgByd1OINob3ovhaHYDM338SAOakqko0wLqkC41lu1CvuiCO1EyjLcXT6AWplruiXshsdUqJHnUUZ2K4MZECMCBuiHH6L5m2xTWW09kPLDAAP0l1'
    'bQ0AISmKMSb43GCJYUEADnFP/52qmKbY1YsByM0d4d5HflJEcNEBn3iAgKKDQn4Y/Mt6yC4GIP/XjU6DdDC0nJO3pvWczE1pLjY+XM/uOH4jEASQb/v48HB6'
    '/iO7mR9Tne/VA1Bg/JxeNN8XV8vW56vFnPMHAlE/cPQxz45RyjOQCVCUP+h7uVA+KCsKoDCBXFNeIQJyAQrX0JCuIRD1k1LCAAofVYkDiu+/ANIBMGUM+ZoM'
    'j6yIA8A4wYTw5SxPPgl83AajRh+XjMxQkeeFsJXMdFWUfRhfkxD5xBa+lhzmpaMV82sGQh/6I9zMvr338h68mHQAUAqAohqLO87QXttDJwR8UgKAojWJCYSL'
    '4N4qwwpVDSsGoGrPg/qV6e/I6S8Ew4r0nyJJDZIqIpiiyhsk0fbYqoQg0b/yHlvUrRZhpxL9Cw4KIk4Al6FRXntaNn4xGW3FpX/aKI+hVSEq9yLTBFeZIE6A'
    'kXHD8iqfXcFSRT60Z1mIvzG26+R+mTK8xtbnAW8SYm6YCvmWNKeNLa34Ome/N0xlbXfJ8UZWbOJLq5CzVdxby1rmpsGQNZYnkBBKgwZvCBhp22b2dpERy6LW'
    'CQjlbYe7VV/a+puvcTYyaUNhmr9CXX9YsegtHCqAt8bZ91w9XwOKEju2i50DPvw3dOxeUKLfbDttf1+ieTx6wAZDPIkaYmXXPL5E+34U2WHBPjkQ3soXbQd7'
    'GaVnuAwicz3ZvYUY953J2gxq6aD5NlOquhEWaLCqeQ7AxwiLZCnQZo5U1gAziWNcKjPAO8AMQk0N8LeMMtJxmNTN/jgvTy8TtD+GO+o6UM3XfaTdy/FQQU8f'
    'gux00MxYR1efsY5u7lzKX5YeBAhav/JHm77oMtr0pWi47Isew2UfNR/ve6b5gOWzv3vEtdpeRDNkXOUx6ejIf3IBmiNLTSNAaD02aQCahq8iAYT+rEkHsA1l'
    'pJz7dHNVzQfYuhFSSn1r1GQBaF4mRoDqeA/sXjbZAJKNvgvVOG1pQ+jOCtUsBkj8SAWEVP0RRkkcQLN5voFSYyHxfbjBq4j/a7P55dZPGSRAoFR7//aVoCAJ'
    'IJGvmyQl1QuxVR5amyeydhQASUp6unW3EPWJ5XpPlzS6UQG8UYy8rm9ZgkGSC/hdb3RJrdZvuGe3xWy5Bi4AAAAASUVORK5CYII=')),
    '/app-icon-512.png': ('image/png', _pwa_b64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAMAAADDpiTIAAAAwFBMVEUtT1kLETIgJkihqbhQUV0hLkcVJDxga37///82sX8hLUY4RVczQFcgL0aHh4cSIDro'
    '7fYAAAAqNk49zo0dK0NOWW8oNU0hLkYoNUzU2uQpNUyPmKjc4uxsd4qxuMXEytZZZHg3Q1onNkwmNEwIGDV5g5WcpLOjq7q7ws6Ci50bKkNBTWMMIjEvl3MX'
    'N0RjboEbR0w3t4IdKkMmNUsjZ1szp3ond2PL0t0rh2sVIjscKkMMI0EhLkYdK0QgLUcgW1ZLpZgwAAAAQHRSTlP/EhP/C2ZT/wH/ySJNjwj+/wD+////0P+R'
    '/6//////////bk8O//////8q/xH//////24t//////8uURK2ijX/ZkxlRwAAGDhJREFUeNrt3YlC4joXB/DqOHfmfnfubVpo6Q5lR3DfdZb3f6sPQREVbZqt'
    'afM/D6CQ8+vJSdIWy6kqvh/8+NHv94Mginzfd42L5ZeOoiBYDsGPHwffK0uDVcH/PLgY9CMDU/4ph6g/uDhoPIDhoB9EyPbHEQX9wbChAPYHAa56umoQDPYb'
    'BuDg0+R3Op22gbH82p8iOGgIgOHO5C9TbiM2seSwE8Gw7gAe7iNknsdBdP9QYwBvs99B7mkUdN4aqCeA3wGSLwpB8LtuAA5fzftIPjcCPzisD4DF+Xb2kXwe'
    'BNsGzhe1ALD4imtfVh3oL7QHsAhw8cssA8FCawB3AbIv20Bwpy2A3xHSr4JA9FtLAIOt9CNfMmKLwIV2AC5e0t9BqmRFRzwBMQCGAWq/6pkgGOoDoI/0V0Gg'
    'rwmAQx/pr4aAf6gBgGGE9FdHIBpWDGDxL9JfLYGviyoB3PtY+FW9KPTvKwMw/InLX4ci8HNYDYA7H+nXg4B/VwWAc1R/feaBc+UANuUfl78WRYB5GmAEcIfL'
    'X7cicKcSwC9c/voVgV/KACx+4vLXsQj8XKgBgPLfpGnAYu7+Uf41nAbO5QM4x+WvcxE4lw3gF2760DQ6bK1gOQD7Acq/7tPAvxIBLCLkX38B0UIWgKGP6b8O'
    'jYA/lANggfzXRcBCBgDkv5ECqAEcYvlXp+XgoWgAh1j+1Ws5eCgWwAPyXzMB/oNIAE/9P5Z/NVoOUq4FqAA8IP91FPAgDECE/NdRQCQKQID5v54CAjEAkP/a'
    'doKBCAB9rP/rux/Q5wcwQP7rLGDAC+Ae+a+3gK98AC6Q/7oLuOABsOcj/3UX4O9xAAiQ//oLCNgBDLAB1ITtgAErgEPkvxkC9hkBoAFoShvABgANgAltgIUG'
    'wOw2wPr8FhDkv/axPhVYlAaABsCMNsBCA2B2G/ABgN+YAJrWBvwuBcDHLQCGTALWJ/cAYOQaJaBPD2CBCaCJk8CCGgA6QHP6QOvDDhCj1jgBh5QAfEwAzZwE'
    'fDoA6ABN6gPfAxiiA2xuHzikAIAO0Kg+0Np9FwjGy5Q+0EIHaEp0dvaB1s67ADBa5SLvpaN4kk3iUdrL9S4BgwIAKAClw4tDsh3d2bxGS0ELBYDv2h8n5H2E'
    'sVuXEmDteBMA0ko9ohPyUehIoL3jrQEWlgDs6Y/JZzHr6FkCDj8BEKEDoI9eQj6P0KtBCbDe/xQEUks1ljEpjpGWJeDhQwABCgD1SHYJTWQdDUtA8BGAIQqA'
    'sPK/mQYsDUvA8AMAKAC0MSfUEbp6lwALBYDhMgrpAZBpW+sSsAXgKwoA5VXUJWVirNWH77z5ZSHrzS4w0lscE1IuZtqVAH8XgAs8CkDZAJKykWtXAi52AAhQ'
    'AOjipjSATLsSEOwAgBaQLlJSPjzdALjvAQzQAtJ1gCEDgK52K8HBOwA4B6SLEWGJnm4lIHoLAJsAlIOXMAG40W4O2HsDADOArCWAfguBV3OAhRmgVIwZAYx0'
    'KwHRawBDbALQRcIIYKrdHLB4BaCPGYAqPMIaOpXX9tZrxC1sA5eJGTOAVLcS4G8DWGAGoIuMGUCs09fovBwJWlgDlImQGcBE070gC+cAZYI5/3ptBm6dB1ib'
    'FgAzAMWVww4g1A6A/wLgH8wAdJGzAyB6Sd68Rd7atABIr8xVoF7rwHUJGGwAoAUwEkCwAYCDICMB+M8ADtECGAeg/fyIkPW8D4zsGgVg0wRYOAk0F0DwBAAt'
    'gKEA/DWAO7QABgJoP70qwEILYDCAwQoAdgFMBPDcBFjoAQ0GEK0A4CTIWAD+I4AD9IBGAlg1AQdLABcAYC6AiyUAHAUaDGCwBIBVoJkA7PUvSFhYBZoMIFgC'
    'wCrQYADREgDuCDcYgP8EAIsAAwGs14EW7gYxGsChhVWgqQBWc8C9hVWg0QC+AoDZAPoAYDoA7AMZDSAAANMBYCPQaAARAAAAABgNAG8HMhqAb+GOQMMB4DDQ'
    'WACrV0UBAADgMBAAkFoTAbQBAAAAAAAAAAAAAAAQAIAAAAQAIAAAAQAIAEAAAAIAEACAAAAEACAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACr5JPvfy3O20AcAUAO78ZDbJujdh8nqck/CmO83i1HMBoJkA2lZvNJ4mFEPencxOxDoAgKov+29xt+zAh5M0B4AG'
    'AMjTccg6+Ek2mgNAjQHM00lIeCObeW0AqCEAL06IqJj02gBQKwB5HBKhkYw9AKgLAHfUJRIijOcAoD+Adjol0uJmlgOA1gBcgRP/Bz3hSRsAdAWQj4mKiF0A'
    '0BGAlxFVMWkDgG4ATrpEYSSjNgBoBKA9ConiCEsQAADJkSakggjTNgBoMfd3SUURpgBQ/cJvQiqM0AOAaj/MjFQcExcAKmz9Q1J5JCkAVBTWlGgR3TkAVDH5'
    'j4k2EbcBQHX0EqJRhN8AQG3ERLPIcgBQWP67RL9IAcDM8r+JMQCo+QBjoml0XQCQH3mXaBuJBwDSD36I1jEDAMO6/3ergQ4ASIyMaB+hCwDS/vWU1CDCHAAM'
    'Wv3TtIIAIKb9D0ldogcA4mOekPrECACE7/6RWkUMAEYt/3fcKQQAImNGahfTNgCYs/2z82SgDQCCYkRqGRkAiIkTUtOYAICI8EhtIwYAo/P/vB8AADz7f0md'
    'AZATAODc/w9JvcMDAJ7o3JC6xxwAmn7+W3A2mAMAc2SkARG6AGDSBuCOTWEAMHAB+GpDCACYFgAJQZgMIEP2jQYwQvKNBuAh90YDaIfIvdEAvshenE+zyXgc'
    'x7NRHMfjSZaFAKATAIm3AE7jUc/a9dFzL42zBAB0AJDLSn7x7//k6ZcEAKoGIOMRoDCm/vGn+agLAFUCED8BlP7Jn3x2AwBVAWiLrsGTHsvHmMchAFQCQOwZ'
    'UBKzj2EaAoB6AEI7wGTW4fowJzcAoBqAwJtAwhH/Z+x1AUApAIFPAcRiPmGaAIA6AOL2gLvCfv27ytcSGwdAVAeYpCI/lXcDAGoAiOoAx6IHbgQASgCIuQsk'
    '6UmgeQMA8gH0xMz+UkatPQEA6QCEXGUTWTtUKQBIBiCkAKTyvvs8BACpAER0AJ7ML6/+VYVGAbAEtH9zud++0wUAeQD4u6wwl/71pwAgC4DLn38Fw9XOAEAS'
    'AO5NwBs1ozUBACkAOnou/ysWYBCAUQ3qv9D9SgB4/XeT2uRfZSdoDgDObbYkVzkI6laD5gDg3AX21I6CspdXGQNgru3+7+7IAUAsAL414ET9OJwAgFAAXCW1'
    '29xfsDMFgFeXBcBWTAFAk8uJvQE8Pj09Zm8EEwAQFjxjOWZI/OXt0dXfrac4uzq6vTzVsw0wBADPDBCW/Ryn11etHXF2a5X92F8AQFCMlU0Al0etT+Lq+liz'
    'ScAQAImiCeCyVRi3ba0mATMA9NRMAJdnLZq41mkSMAMAxwxwQv1PrL9btHFJPwkAQLUzQJe687tqlYizP7psBxkBgGNfnfYRoOtWyTii/MOdBAC4g/0keEr5'
    'H25bpeOKcj0wAgDuYL/FinIJeNRiiDM6AZLfaWoEgFBuATi+arHFqQYlwAQArtwO4PisxRp/5H58AODcTQmp8t/iCKrN4TEAVLQLMKL561c8AFo0fcAcAPiC'
    '+W5AmjfAHXHlv3VG8yWnAFBJC0BzCnDd4owrin/yDQAqOQigWAP+aXHHLcVXAACeYN1MTSj2f1sCguJgYAIAHDGVNwNciQBA0Qj2AKCCbSBPxQRAdyzQBgCO'
    'v8c6NMV/+kwMAIodwQwAlB8FFj8Lci0o/xQrgRQAlC8CCh8Ga7eEReGGoAsAzMF6llL4NPCtOAB/yzvPAoBYUgtw3BIYhUvBCQCwBmP/lEneA36zIyyrjgEA'
    'a/GcFf3dltAo6gI8AGANSbsAf8QCKNoQbgOA4lWgq3IGoJgDugDAFp6kHrAlOIo2gzIAYAvGLZSuimOgMo8LxQCgFEDRIuBaNIAzOd8DABjXT7GiYwDqM8Ee'
    'ALDFTM5GsPD8Fy0EcwBQuhHYU7gNSLUZ2AYApQA8xT1gcRcIAGzBeE94QUG+FA+gaCsoBACmYDxFyRUvAopvCgAApWdBrrKjYNp1YBcAmGIqZVyOxANoAYAU'
    'AIzjVvC/r9QDyABAJQDl+0CFO0EAoFMFAICm9wAd7aaAKQBotApAE2j4PsAt9gHM3gmUsBFU9IBYAgAanQVUsBWMswAcBgEAQzDeEHKi23GwCwBswXgrVdFj'
    'AeIBFNwV6gGAUgBFzwYLXwcWnQWdAABbMA5c0TtC/6juAWcAwBaMN1MWviBI8S2B0p4ObTyAuaSBOVK7DyjtXYGNB8DaPc9tpTsBhe8JSgCAMRhHrui+8GOl'
    'i0B5rwhpPgDG2ln4kjixB4JFr4rrAYDiw4DCN4VbKtcA8n46qPkAZrJGRmQJKPyqXQBQvBFQ/GMRp+rOASS+KbL5AFjXgXHhXz5StgaU94YYAwCw9s/dwr98'
    'rKwASPwBUQPeFZxIG5ojNccANsdvXgAA+x7aSFUJKH5ffE4AQPk6kOZXY6+VbALKOwkyAwDzy1VyW8UkQDEB2CEAVLAMKH5XpJDNgGOJ3wAAVsE6NjQ/G3jM'
    '+4wQzS8HxgDAFcwPVc0p/vip7BXgckQSAOAK5hct0/xuHN+5MNWPyKcEALjCkzs6HE8JXVF9/hAAOP8i8+jEttQaQHX9y/zJMEMAcDxZTTc8p7I2gOQeBJoD'
    'YCa5BLCtBU5lT2AAIGAMacen9I7Q2bGtQwEwAwDHaXpM+z+uZbR/trwHQowCwPN6lZz2f5yWKAJ//6EejRAAqjwOKH5EiIEAffqlHgOZBKDDMUKpLZZAmfRL'
    '/MFIswDwzAFJqSE6LdgWOros9bm/AICY+EYU9IHPBq4/WhQe/Sn5p3oEACpfB9D8jvy7fYHLd8fEZ7enpf+MmwCAqOB5uDZhGqTjU+vy+vbo6Pb68s/pMdOH'
    'nhIAEBVcxXRazUjMCADoMQdQ3RskPDwCAAJjTNS2AfzjEAKArcscwNgGVLVyBYBdwddRd9uKh2FCAEDsmHO2VJnaUYgJAAgGwLurOlE5CCkBANEAuC+qWN0Y'
    '9AgAiAfA/YDdSNUQzAkASADAf7KSKsp/AgBSAPDvrCipAR4hACAFgIDb6xT0AT0CALIACBhb6WuBlACANAAinrHJ5O4IzQgASAQg4vKadiR++TEBAJkAhDxn'
    'G85lfXU3IwAgFYCgCitpMeAlBAAkA+iIGeNMxrDFhACAbACimuxQ+A0CbpcAgAIAwh61i8X2gmlCAEAJAGH7bInAjWGvosvfRAACV1rTXFD1HxMCAMoAiLzZ'
    'Pm7XufobCoD9nVG75oEZ7wCmXUIAQCkAwS9dijmGsDMKCQEA1QBEn7dOLMbJKE4IAQD1AMTfcd0dlR7HTpoRHcJIADKeui9lQJfsmwpA0qH7zTilWBi6vbhL'
    '9AkzAch77CbJZt/mHwxqO++NJiHRKwwF4MrNQ9KdzEZpetLreZ7X+5amo9F4qlvqjQag+M5LjUNnADJvvVF/7xUA0HbIKwC+bABKXr8BAFwA5H42N0H2tQTw'
    'mHlfAQC0AVoDiOQDqOgeLAkRNg5ApASA3W1G/pMUALTcDVAVntdAAIEKAHbehEbwxG4egEARgCY0gjO7kQD6agBI/z0G6TGxmwigrwyA0BvEKojMBgCTF4Pr'
    't9c1EcBA9mnQS3ypb/5D124cgNVZ0FfrQh2Adm1PBZLcbiiAC8uRfhy4dfpwU1MAc7uBAFZnQf9Zjq+sCajthtDmAVWe18q1dWwBfMdyIoUA7LyOAk5exozj'
    'r+i5EbgEEKgEYLs39b3+uX4MIdRzH2gJoK8UgN2pWSeYvHpPDfuOdqYlgP4SgMJ14PoqyuqU/zAXdKw51nIRMFgCuFAMoIo3dDHHzZvayI431hLAP0sAjnIA'
    '9dkT7L6dG9nvcO1puQr8/gjAV9sEPEZaj/y/f1cl+zpQ01XgI4BIPQDV7+lliy87PjjrMlbPHjBaAQgqAFDNq/pKn/+Lm71SLQEEKwB99U3A45aQ5vcJfvB+'
    'OtbNYD1PAgYrAHeVALDbWreC048yxla5praWAA5XAFbLgCqE9vSdBj5etLHd2TTXcgZwnTWAqCIAtqvprmDy2ZqNZeqa2FoCiJ4A9KsCoOk9Il1X9AIm13MG'
    '6D8BOKymCVg3VfodD44LhiITOKFU3gKsAFTWBKxKkWZHA2Hhll3ph127rp4zwGMLsAYQVQhAs9uFaV5JWnIpmGiY/00LsAbQrxSAnWtTBLp0k3W5nWzP1hRA'
    'fwPgosImYN1YadEJ0L+WvMwWxkjH/K9vCN0AqLQJWH+gUfV7AmV+nSSrcwO43QI8AYiqBlDxq9ypmr9XMa51/l9agCcA/arngMeYV3g6EI7Kfnu6PqCnZ/43'
    'uwDPAO7c6kvA46CGdUm/TXWeGea2vgXAHW4BqOKmkJ0yqyDAlH6aSSvu2BoD8J1tAFrMAav4pngiSEbsX3v+2VlGpuvl/zQDnL8CMNRjDljX1kz/q79w/dr1'
    'bH1jawZ4BqDDOmBrZ2iiaN/nhP9iOtlxnpVMeratOYDIeQ1Anzlg/REV3C2SCbpI273xdj8Yxp6td2ytAV4A6DQHrAnM5PaDY6FTdCf3euko7Xl5x9Y+tmeA'
    'DQC95oCnZiCWZWDSa9vmxvYM8AJgoNcc8GxgLH6LODsxOfsvt4O+AbDn6lcCJBjI0o5teLycA7wCUM3jAZRd1kRU9l0b8fRAwDsAFwrfFVPegDf6wlcIsi+9'
    'DpJvPz0TeLEDgC7bwZ9sD6RjptdLhF9GHhK/XQB8ZxeAr1q2gW9LwTyNM+rFQZjFqYey/64FPN8JYOjqXgJevkXem026halHzS/YBHgNYN0G1mmJlHu9XpqO'
    'ZnE8nmTZZBzHj78h963nWW1k+pMCEDi7AdSoBCB4CsDiAwD1KwEI3gLwGsADSoBpBeA1AJQA4wrAGwCHKAHNLwCHnwDQ8UwQIRZA5HwG4AIloOkF4PenANYl'
    'AF1AczuANwXgHYABSkCzC8BFAQCUALMKwHsAWAgYtATYBWC9F4BjlMZF5/0ewG4AOBFocAEYUgBAH9jc/A8cGgDoA5vaAfoOHYDfKAGGdIAfAMCZUDMLQODQ'
    'AthHCTCjA/wIwPpRUQhoVv77Dj2A9S3imASa3gF+DACTgBEd4McA1n0gBDQn/4FTDsB6EoCApuTfd8oCWLhoA5rTALgPpQFgR7hJBWDglAeANqD5DcDnAPbQ'
    'BjSlAdhjAvB0bwjagNo3AHcOGwC0AU1vAIoAoA1oeANQCABtQLMbgEIAT20ABNQ3/x9tAVMCcO4hoN75v3f4ADwJwF3CDc1/MQDnHALqGKu7wLffBsUM4Gkp'
    'AAE1zP+/jggA67uEsSFUvw2gyBEDYB8C6pn/fUEAnAcfAuqXf//BEQXAGUJA/fI/dMQBQA2oXf/nPjgiATxvCWItUJf8HzpiATiHPvYE67P/49Pmnx7A8ywA'
    'AXXI/8IRD8BZQEBd8j90ZABwFhEE1CH/0cKRA+B5VxgCtM5/UCql5QA8C8ByUNvl/wfPgIoC8PTcMAQ0Jf+lATzdKIppQM/y//kNoEIAbASgCOh3+b99EbAU'
    'AJstIewK6rb7R7/9wwXAGUaYBnQs/9HQUQPg+TYxTANalf9zplSyAXDufBQBvS5//85RCQDTQBPKPw+Al2kAvWD13R9j+ecD8DINoBOoePYvePpLFgBn+NMF'
    'gerT7/4cOtUA2Dw4hlagusnfdf/iSiEfAGf4y0UrUOXk7/4aOlUCcJy9/2EeqLD67/HmjxuA4/zlgkA16ees/qIAbM0DIKAy/bzVXxiA7XkABBTN/QKqv0AA'
    '2/MA2kEV6f/fX4ISJwqAszj3XSwK1dR+1/9r4egG4LEKvEwEmAkkpl/Y1S8agOPc/QQB2en/eSc0ZWIBLNvBlxUBDIjPvvtrT3DCRAN4tSiEAbHZF7Pwkw1g'
    'SeDchQHx2XfPhxKSJQPA442jgb/90TtAUD75ne0R9INDOZmSBGAZv4NXfIGAOfmuG3yVliZ5AJYN4X3kAgFv8t3ofk9ikmQCWLUDbw08KgCD3al/l/vlok/K'
    'xK8OwOMW4f3rfgAOqDK/mvfvF9LTIx/AY/wz2I1gg2GpwbTodD4bET8YHChJjRoAj7FfgACxlfx9ZWlRB2CNoB9BwWe5j/oKk68ewCq+/7ecEeDgbeaDH//8'
    '9119NioA8Oxg/2LQ7/eDIIp830AOyy8dRUGwHILBxcH3ytLwf7YzoLTWYEFiAAAAAElFTkSuQmCC')),
    '/app-icon-maskable.png': ('image/png', _pwa_b64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAMAAADDpiTIAAAAwFBMVEUSIDro7fY9zo27ws+RmakpN09LV2xtd4obKULT2eM3Q1rc4uywuMV5g5TEy9ZZZHij'
    'q7mbpLOBi5zL0t1BTWMzqHtjbYEvl3IWNkQbRkw3uYMhLkcndmM9SWAjZ1sriGsdVFIgW1aosL6Gj6A2sX9ga34AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABVbVQ6AAAAQHRSTlP/////////////////////////////////'
    '/////////////////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAsnI8uwAADu9JREFUeNrtnQli3CYUQIfRvs/u8Xh32vtfsWnapk3qWCBWwXsH0MjwzP98'
    'EGw2AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADhUrSntqAZUuSh6sdG/EUz9gMa'
    'pER96cXPZBUOpDLu78XH9CcaJ4Huz8Wv2bU0UOSD/0F8zp5AEDOnRswy0UzRUgkZchoqUnIhR/9AW8WY/WVCloZEIML0bxTydBgQHb1QYaxpsbg4CDV6miwq'
    'JqHKgUaLiFaoQz0gIsYFApAIxsNFLIGCUDQcFwkgGAIioVrW/2JP00VB3SwUQLA4nPQAQBYQCbvFAjQ0Xgx0iwUgBsRAubz/xUDzrZ9cQ4CM5ls/jYYAlALW'
    'T6HT/6KkAddOqyXAhQZMOQckC4yAi5YAlIJWz6AlAMsBSc8C2RmWvAAUAhAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQ'
    'ABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAABEAABEAAB'
    'EAABEAABEAABEAABEAABEMADdVG0p7KcLlU1DFV1maaybAsEiJ7iNA37rPlVIzfZez5cygIB4uN0OeyzTra1j30+lA8IEEvnD323pM2b3WByNEAAH5QLO/87'
    'Y17WCLDazhdGyA4lAqyNad8Jg3T7EgGS7f2/U4K8RIBke/8fB54QIPC4b6/3/54hHgoECLbAVx2FA3YnBAiyzHdohCOyCQGC6/5cuORY1QgQUujfCdc0wwMC'
    'hJL3Z8ILeYEAIfz3j8IbKgoggJ3Y/y580lUI4HXilwvfHEsE8EbViADoWwRILvj/lAo8IID74L8T4SCTCiCA0eB/EGExlgjgkFMjgmNXI4ArBhEixycEcBP9'
    'exEoFQK4SP47ESx9gQC2yUXINCUCWKUdReDkCGCz9CfCZywQwNbkfyfWQHdBADvZ/1GshBwBbIT/RqyGPQIkNfv7YD5YI4BZJrEusgIBUkv/fyoMFwhgjoNY'
    'H02LAKbYizXSlQhgZvrfi5UyIYABHjKxWioESKj88xFfEED3/38Uq+aAAHrxf+X9/58ogABL6MXqmRBgOTsRASUCLCWPof9F1yLAMgYRB02BAEu4CBGTAQig'
    'uv4r4mGsEUCVk4iJrEYAxQJgF5UAYocAamQiMioESG8C+OMYgABpJoDfywEIkGwCYCKLTEqAkQ5PWoCc/k5agJLuTloAEoDEBbBfAejG3aEqT21RFPVf14g+'
    'lWWV9w0CRJ8AdP2ndwG2Uz4iQKwJwNfOl7nrowhSgmQEsLYHeKdyy0cxjAgQ0x6QrFK+CfgpbxDA/QzAytC/9KIv61eQIcDPWNgEuuB2n3+pqwYBVp0BLrrf'
    '6wcuIwKsNgPcPZl4qylDgFVmgJIXOcgo0CDA6jLA5mLw1fyfTp+CAEYzwLw2bGePAJZpgxz9Q4kDCQhg8P63wcoL1nsEWMUA0DzZescKAVaQAbzXFi0dESD0'
    'KUBl9TW9hYHoBTDUsN3J9osOCBDwANC09l91QgALmNkHNBYu3tXLsdUZA4DE7L9287Y+Dq7PGADmi3/ufG0QILwBYHD5wiMChDYA7N0q2yCAwcm1ifKP68Jl'
    'hwAhVVh75y99QgBj6G8EGmv3b10iQDAteSx8vPcFAUJJAdu1vjgC/Il2OlX5evMRAUKIADt/9YsOAfTRXQc81v7efUIA/xFAKwG43t9fV5EGxCvA5CUBeH18'
    'uTvftn9zO9+9PN4HnQZkRABjCcD1+W77EbeXBQ64SgOiFUCzDNypfvZ9/3jefsLdm4cyZtICTE4DwPNv21lUx4EjAuigtxn4qDTYPN62UpxfwysJZ0SAjygV'
    'fupNsvu/RQKVqUGPAL4igMIa4PW8VeJZIQ9EAF/zaPk1oLetKueroz8ibQG0ptHym4BetguQzgQeEMBPCiA7AFxv20W8BDQERCqAVgYtuwvsdbuUczhZQKQC'
    '5A6mAG/brXUD9giwjMx+kzxvdbjJpYItArhPASbr///fDJBbbO4RwHUKMMqV/re6nK3/JQkLoHP2VuWm/2UNGBFgAToDp9TQfDMggNxssEIAtynATuYH7rZG'
    'kFkeLBDAbQogkwI+m+l/ualAjwDKVHYjwHVrijv/MSBKATTKQHt3AUByWaBAAJc5oEQEeDXX/9ub9xgQpQCj1QhwMyjA9tF3DIhSAKvN8bw1yvwPPiGAIhpR'
    '8+B4AJAaAjoEcDYLnF8IfDPb/zJZQI8AzmaB8ynA2bAA2/kPBgYEUOOLxda4mu5/iSWBEwKosfyTgPkTAR+NCyCRBiKAq1ng5D4CyMSADAEczQJP7iOATD14'
    'jwCO1gJnc8BnCwLMx4ABAdyUARqXywAKCwITArgRYP6LMBv9P78xpEUAFVp7k4BXKwLM14IQQIXl0+bZc8EfrQgwnwQ0COCkEjw7C3yxI8Ds1rAeARSY7M0C'
    '7+wIMJsF7hFAgeVH7c4eDHe2I8DsmQE5AjhZC5oVwE7/zy8JI4AKy8smhScBZueBBwRwIsDc0XBXSwLc2fuTUhRg+Xg5Vwm+tyTA2V5UQwAV5p78ZkmALQKs'
    'Q4BXS/1/QwCTHKzlAN5CADlAGLMAb0kgs4C46wAv9qIalUCjpeCbp0IQpWA3awGzXwX4KgXvEMDJauCsAL4Wg1gNdCPA7PFAvpaDRwRQYPnHlLNfBj77qQOx'
    'I0iJ5VvCdnOPvvqZBRYIoMLy5po/IvDmJQcsEUAFjUPWZ59tZVPg7FFRFwRQYnlrzG4IuPexEmCzEMi3gWrzwI2PMpDNMgBfB6vNA21UAuaPizwigKP14Nlp'
    'gI2JoMWsNlEBlq8GzX8ceHU/CbR7lThnBKmtB5qPAfcWR7REBdAYMueTANPzAIlD40cEcDYPlDgp1vAQMLsQpHsJaooCLM+aJe4Mvnc9AJQIoIrG6mnheAiY'
    'HwAsXx7IaeE/Msw//ep0CmD5oFDuC1BeDzK6IHD1HQG4MUR9ImhwZ9jzxncEiFMAndKZxHHRxoKATACwHAEivTVM40iVRub5Zr4Ru9kezdIVQGcb9ZPMD7y4'
    'qQFu7N8eHKcAOt/S5VK/YCANeJP5nVogwAK0DtYr3BjwLPUzAwK4TgKk0sCv/5q/Wb8pRPcPSVkAnT00jdy93vXZQf9fBAI4TwLkro/WiwJvkj8xIoCHJOAo'
    '+yt3VvN/B3PAeAXQK59Msr+ybIPYb1fZ5/cIsJR3nUYZpX/mfsGXIi/ST3cwAEQrgN70qZL/IdWS0O1V/tlHBPCTBIiukP+l+7OF7F8/k01dAM38OVf5KflM'
    '4HxVeGzRIYC3GCC1KvwfBaRSgbt7pYfmAgE0KNy2y9vclPD2cnUZxBBAt4byu/IPvr78chw4P16VH5chgNcYoJIH/psQPn6QEd491wseVQkE8BoDJK4Q+5jr'
    '/fPjy/nrYHC+e3l8u78ue4qjABCzANpj6ODx3esjAvhdEPqT0t+77wQCeI8BsuvCFrgIBDCA9lLKuy91OwQIIgZIbg4yzcNRIICRhtRvnsrDa9eZQAAzGCim'
    'ekgEe4EAoaSBPgzYCQQIqTG7dnWjFgJ852SghZrC5RtXAgFCqgZ+M6CNuP9jF8DICWvdKdbxP34BDG2rc5QJ7gQCBDqmXlzM/3uBAOYxVFW1vzT4kAkEsICp'
    'z2t3lleGnhqBAIHWg//iaHUyUAmBAKFn1vYWBh56gQAh14P/WR62FAZOjUCA8LOAP2tCT4G/IAJ8hMn/r9z4IPCUCQRYQTnw+yBgNhMociEQwDZmU6zRYGW4'
    '6gQCrCoP/Mbe0AJhdRQCAVa5yJLrK1BXjRAI4IbafFvv9epC9RBE96cigJWN9u8auUDZCYEALrEy18qqRZHglDdCIIBbbH1s2VeKhYH2cBQhkYoAFjfbvP9e'
    'y/f+KAIjGQFqm8Null/mcsK6rHaNCI9kBDCyQ/jTnYN9Xk3tB4NBUV4O+1EESjoCOFpzafp+t8/zfDjk+X733mcibBISYNMLSFqAoqG/kxbAydm7CBAyBzo8'
    'bQFIA1IXoOji68IRAZJOA445AoRREvZEiwBJpwHlBgEUFwWymPr/skEAZQPGePp/2CBAyhXBfIMAKRuw3yDAMtooygH9BgFSLgdkNQKkbMBYbxBAg8vK+//f'
    '4wsRYBlVJP2vudNpt8GAdfe/5t+RpyuA2c/G3cb/wtifMSQsQEAfaCnO/37Ye6y33blKWYBNu8qK0E9hW+/j9ylpATbFcX39/7+orfW00yZxA1a3Nvj/oK3z'
    'J3R14gJ4OqHX6JCtMw/cb2C3pv7/6NzylhRAj/XsEus+PqdwJAJoloVXMh3MfnEexUAE0J0OrmKT0Jdfvj4RIIEw0JTm376n41dTFew/O45o6fcuJ/p9LRWB'
    'mZL9sixgR68bm09bHv7nTilfdgBOS5//FAYCXRqQuK5myZpwTo//LwwEWRaUWrBTf3O3V6KuhSm4QWAn10/Kn7t0BICPGzKsQyRG6TsrVWcCJX39q5YMZ3Gg'
    'U9muobbReaCjP2nKQAqDivcSqMximAHOJNUBlIUy5Ruq5ItZFV08Fwf2vqf+S64rbuU2OHXEfwn83ua18HYyqRsoR+Z/kiOqr6pAp3EjzWV2HntgD4D8KOBj'
    'QtAMWj1UHz7NBHL+/dVyAdfrxMfK5jv3VH/Um/PgcEaQmdmh0Q4fJTBjzvLvskHV1QVvvcHsvKjef3x2xdivwe8OpgR748Nz0ZZTNVRT2dL5+o05WC0P9hdy'
    '8+CxduFXxvC8mnmh+Sv/xoHeX1d5yKAD3Y7//VXGAiM3wPUDs7I1J4WXXCMrzPKSpG/9PEy58vSw6+cvmYRVDQVlddhnEiGhy/KqJOTHSt1+FSHfv/dj82Pp'
    'uBn7/ZdqouuTsqFoy9NTWxQPRHoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMMof'
    'c31e08tMlPIAAAAASUVORK5CYII=')),
    '/app-icon.svg': ('image/svg+xml', '<svg viewBox="4 4 56 56" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#1a2c4b"/><stop offset="1" stop-color="#0b1526"/></linearGradient></defs><rect x="4" y="4" width="56" height="56" rx="15" fill="url(#g)"/><rect x="4.25" y="4.25" width="55.5" height="55.5" rx="14.75" fill="none" stroke="#fff" stroke-opacity=".1" stroke-width=".5"/><circle cx="26" cy="37" r="11" fill="none" stroke="#e9eef7" stroke-width="6"/><rect x="37" y="12" width="6.5" height="38" rx="3.25" fill="#e9eef7"/><circle cx="26" cy="37" r="4.6" fill="#3ecf8e"/></svg>'.encode('utf-8')),
}


def _pwa_manifest():
    icon = lambda src, sizes, kind, purpose: {'src': '/fileops' + src, 'sizes': sizes, 'type': kind, 'purpose': purpose}
    return {
        'id': '/', 'name': 'Desktroo', 'short_name': 'Desktroo', 'description': 'Le bureau web pour votre TrueNAS',
        'start_url': '/', 'scope': '/', 'display': 'standalone', 'background_color': '#0a1220', 'theme_color': '#0a1220',
        'icons': [icon('/app-icon-192.png', '192x192', 'image/png', 'any'), icon('/app-icon-512.png', '512x512', 'image/png', 'any'),
                  icon('/app-icon-maskable.png', '512x512', 'image/png', 'maskable'), icon('/app-icon.svg', 'any', 'image/svg+xml', 'any')],
    }


# ── Noms d'affichage des applications ─────────────────────────────────────────
# TrueNAS ne permet pas de renommer une application, et toutes les applications personnalisées s'y
# appellent « Custom App ». Desktroo garde ici le nom choisi pour chacune : nom technique → nom affiché.
APP_LABELS_FILE = os.path.join(ACCESS_DATA_DIR, 'app_labels.json')
_APP_LABEL_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,62}')


def _app_labels_get():
    data = _access_read_json(APP_LABELS_FILE, {})
    out = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str) and _APP_LABEL_NAME.fullmatch(k) and v.strip():
                out[k] = v.strip()[:60]
    return out


def _app_labels_set(name, label):
    name = str(name or '').strip()
    if not _APP_LABEL_NAME.fullmatch(name):
        raise ValueError("nom d'application invalide")
    label = ' '.join(''.join(c for c in str(label or '') if c.isprintable()).split())[:60]
    labels = _app_labels_get()
    if label:
        if name not in labels and len(labels) >= 500:
            raise ValueError('trop de noms enregistrés')
        labels[name] = label
    else:
        labels.pop(name, None)   # nom vide : on reprend celui de TrueNAS
    _access_write_json(APP_LABELS_FILE, labels)
    return labels


# ── Météo du widget ───────────────────────────────────────────────────────────
# Prévisions : MET Norway (api.met.no, données libres sous licence CC BY 4.0). Recherche de ville :
# Nominatim (OpenStreetMap). Le NAS interroge ces services à la place du navigateur : il s'annonce
# comme leurs conditions le demandent, garde chaque prévision jusqu'à l'heure d'expiration donnée
# par le service, et n'envoie que des coordonnées arrondies au centième de degré (≈ 1 km).
import email.utils as _wx_email
import time as _wx_time
import threading as _wx_threading

_WX_UA = 'Desktroo/%s (https://desktroo.fr)' % APP_VERSION
_WX_CACHE = {}
_WX_LOCK = _wx_threading.Lock()
_WX_GEO_LOCK = _wx_threading.Lock()
_WX_GEO_LAST = [0.0]


def _wx_get(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': _WX_UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(4 * 1024 * 1024).decode('utf-8')), r.headers


def _wx_coord(value, limit):
    try:
        f = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError('coordonnées invalides')
    if f != f or not (-limit <= f <= limit):
        raise ValueError('coordonnées invalides')
    return f


def _weather(lat, lon):
    """Prévisions heure par heure pour un point : température, vent, humidité, symbole du temps."""
    lat, lon = _wx_coord(lat, 90), _wx_coord(lon, 180)
    key, now = (lat, lon), _wx_time.time()
    with _WX_LOCK:
        hit = _WX_CACHE.get(key)
    if hit and hit['until'] > now:
        return hit['data']
    try:
        raw, headers = _wx_get('https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=%.2f&lon=%.2f' % (lat, lon))
    except Exception as e:
        if hit:
            return hit['data']   # la prévision précédente plutôt que rien
        raise RuntimeError('service météo injoignable (%s)' % e)
    until = now + 1800
    try:
        until = max(now + 600, min(_wx_email.parsedate_to_datetime(headers.get('Expires')).timestamp(), now + 7200))
    except Exception:
        pass
    props = raw.get('properties') if isinstance(raw, dict) else None
    props = props if isinstance(props, dict) else {}
    hours = []
    for item in (props.get('timeseries') or [])[:120]:
        d = item.get('data') or {}
        inst = (d.get('instant') or {}).get('details') or {}
        nxt = d.get('next_1_hours') or d.get('next_6_hours') or d.get('next_12_hours') or {}
        hours.append({'t': item.get('time'), 'temp': inst.get('air_temperature'), 'wind': inst.get('wind_speed'),
                      'hum': inst.get('relative_humidity'), 'sym': (nxt.get('summary') or {}).get('symbol_code') or '',
                      'rain': (nxt.get('details') or {}).get('precipitation_amount')})
    data = {'ok': True, 'lat': lat, 'lon': lon, 'updated': (props.get('meta') or {}).get('updated_at'),
            'hours': hours, 'source': 'MET Norway'}
    with _WX_LOCK:
        if len(_WX_CACHE) > 64:
            _WX_CACHE.clear()
        _WX_CACHE[key] = {'until': until, 'data': data}
    return data


def _weather_search(query, lang='fr'):
    """Villes correspondant à un nom. Une demande par seconde au plus : c'est la règle d'usage de Nominatim."""
    from urllib.parse import urlencode
    query = ' '.join(str(query or '').split())[:80]
    if len(query) < 2:
        raise ValueError('nom de ville trop court')
    lang = lang if re.fullmatch(r'[a-z]{2}', str(lang or '')) else 'fr'
    with _WX_GEO_LOCK:
        wait = _WX_GEO_LAST[0] + 1.1 - _wx_time.time()
        if wait > 0:
            _wx_time.sleep(min(wait, 1.1))
        _WX_GEO_LAST[0] = _wx_time.time()
        try:
            raw, _ = _wx_get('https://nominatim.openstreetmap.org/search?' + urlencode(
                {'q': query, 'format': 'jsonv2', 'limit': 8, 'addressdetails': 1, 'accept-language': lang}))
        except Exception as e:
            raise RuntimeError('recherche de ville injoignable (%s)' % e)
    out, seen = [], set()
    for r in raw if isinstance(raw, list) else []:
        try:
            lat, lon = _wx_coord(r.get('lat'), 90), _wx_coord(r.get('lon'), 180)
        except ValueError:
            continue
        addr = r.get('address') if isinstance(r.get('address'), dict) else {}
        name = str(r.get('name') or str(r.get('display_name') or '').split(',')[0]).strip()[:80]
        region = ', '.join(str(x) for x in (addr.get('state') or addr.get('county'), addr.get('country')) if x)[:120]
        if not name or (name, region) in seen:
            continue
        seen.add((name, region))
        out.append({'name': name, 'region': region, 'lat': lat, 'lon': lon})
    return out[:6]


# ── Conteneurs Docker de l'hôte (fenêtre Conteneurs) ──────────────────────────
# Lecture par SSH, comme pour les sites web. Les variables d'environnement des conteneurs
# (souvent des mots de passe) ne sortent jamais d'ici.
# Les commandes sont lancées par « sh -c » : l'interpréteur de l'utilisateur TrueNAS est zsh, qui ne découpe pas
# « $D ps » en « sudo -n docker ps » (il cherchait une commande nommée « sudo -n docker » et la fenêtre
# retombait toujours en vue réduite). Si Docker ne répond ni avec sudo ni sans, sa propre erreur est renvoyée.
_CT_DOCKER = ('if sudo -n docker version >/dev/null 2>&1; then D="sudo -n docker"; '
              'elif docker version >/dev/null 2>&1; then D=docker; '
              'else E=$(sudo -n docker version 2>&1 >/dev/null | tail -n 1); '
              '[ -n "$E" ] || E=$(docker version 2>&1 >/dev/null | tail -n 1); '
              'echo "${E:-Docker ne répond pas sur le NAS}" >&2; exit 3; fi; ')


def _ct_sh(script):
    return 'sh -c ' + shq(_CT_DOCKER + script)



def _ct_id(value):
    """Identifiant de conteneur validé (hexadécimal), seul élément repris dans une commande."""
    cid = str(value or '').strip().lower()
    if not (12 <= len(cid) <= 64) or any(ch not in '0123456789abcdef' for ch in cid):
        raise ValueError('identifiant de conteneur invalide')
    return cid


def _ct_trim(c):
    st = c.get('State') or {}
    cfg = c.get('Config') or {}
    hc = c.get('HostConfig') or {}
    ns = c.get('NetworkSettings') or {}
    labels = cfg.get('Labels') or {}
    ports, seen = [], set()
    for key, binds in sorted((ns.get('Ports') or {}).items()):
        cport, _sep, proto = str(key).partition('/')
        for b in (binds or []):
            hp = str((b or {}).get('HostPort') or '')
            k = (hp, cport, proto)
            if not hp or k in seen:
                continue   # même port publié en IPv4 et en IPv6 : une seule ligne
            seen.add(k)
            ports.append({'container_port': cport, 'protocol': proto or 'tcp',
                          'host_ip': (b or {}).get('HostIp') or '', 'host_port': hp})
    mounts = [{'type': m.get('Type') or '', 'source': m.get('Source') or m.get('Name') or '',
               'destination': m.get('Destination') or '', 'rw': bool(m.get('RW'))}
              for m in (c.get('Mounts') or []) if isinstance(m, dict)]
    nets = [{'name': n, 'ip': (v or {}).get('IPAddress') or ''}
            for n, v in sorted((ns.get('Networks') or {}).items())]
    return {
        'id': c.get('Id') or '', 'name': str(c.get('Name') or '').lstrip('/'),
        'image': cfg.get('Image') or '', 'image_id': str(c.get('Image') or ''), 'state': st.get('Status') or '',
        'health': ((st.get('Health') or {}).get('Status')) or '',
        'started_at': st.get('StartedAt') or '', 'finished_at': st.get('FinishedAt') or '',
        'exit_code': st.get('ExitCode'), 'restart_count': c.get('RestartCount') or 0,
        'restart_policy': ((hc.get('RestartPolicy') or {}).get('Name')) or '',
        'created': c.get('Created') or '', 'network_mode': hc.get('NetworkMode') or '',
        'project': labels.get('com.docker.compose.project') or '',
        'service': labels.get('com.docker.compose.service') or '',
        'ports': ports, 'mounts': mounts, 'networks': nets,
    }


def _ct_list():
    cmd = _ct_sh('ids=$($D ps -aq) || exit 3; [ -z "$ids" ] && echo "[]" || $D inspect $ids')
    raw = json.loads(ssh_ok(cmd, timeout=45) or '[]')
    items = [_ct_trim(c) for c in raw if isinstance(c, dict)]
    items.sort(key=lambda x: (x['project'] or '~', x['name']))
    return items


def _ct_size(text):
    """« 123.4MiB » → octets."""
    t = str(text or '').strip()
    num = ''
    while t and (t[0].isdigit() or t[0] == '.'):
        num += t[0]
        t = t[1:]
    mult = {'b': 1, 'kb': 1000, 'mb': 1000 ** 2, 'gb': 1000 ** 3, 'tb': 1000 ** 4,
            'kib': 1024, 'mib': 1024 ** 2, 'gib': 1024 ** 3, 'tib': 1024 ** 4}.get(t.strip().lower(), 1)
    try:
        return int(float(num) * mult)
    except ValueError:
        return 0


def _ct_stats():
    cmd = _ct_sh("$D stats --no-stream --format '{{.ID}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}'")
    out = ssh_ok(cmd, timeout=45)
    res = {}
    for line in out.splitlines():
        parts = line.strip().split('|')
        if len(parts) != 4 or not parts[0]:
            continue
        used, _sep, limit = parts[2].partition('/')
        def pct(v):
            try:
                return round(float(str(v).strip().rstrip('%')), 2)
            except ValueError:
                return 0.0
        res[parts[0]] = {'cpu': pct(parts[1]), 'mem': _ct_size(used), 'mem_limit': _ct_size(limit), 'mem_pct': pct(parts[3])}
    return res


def _ct_logs(cid, lines=300):
    cid = _ct_id(cid)
    n = max(20, min(2000, int(lines)))
    out, err, code = ssh_exec(_ct_sh('$D logs --tail %d --timestamps %s 2>&1' % (n, cid)), timeout=45)
    if code != 0 and not out:
        raise RuntimeError(err or 'journal illisible')
    return out[-400000:]


def _ct_restart(cid):
    cid = _ct_id(cid)
    ssh_ok(_ct_sh('$D restart -t 20 %s' % cid), timeout=90)
    return True


# ── MDM-CT-UPDATE-V1 : mettre à jour l'image d'un conteneur hors applications TrueNAS ────────
# Un conteneur créé par « docker compose » porte, en étiquettes, son projet, son service et ses fichiers. On s'en sert
# pour relancer exactement ce que son auteur a écrit : « pull » puis « up -d » de ses services, rien d'autre.
# Les conteneurs des applications TrueNAS (projets « ix-… ») ne passent pas par ici : c'est TrueNAS qui les met à jour.
# La commande est détachée sur l'hôte : elle doit survivre au redémarrage de fileops si c'est lui qu'on met à jour.
_CT_UPD_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')


def _ct_update_log_path(project):
    return '/tmp/desktroo-maj-%s.log' % project


def _ct_update_plan(cids):
    """Conteneurs → un plan par projet compose. Rend (plans, conteneurs laissés de côté avec la raison)."""
    ids = []
    for c in (cids or [])[:60]:
        cid = _ct_id(c)
        if cid not in ids:
            ids.append(cid)
    if not ids:
        raise ValueError('aucun conteneur indiqué')
    raw = json.loads(ssh_ok(_ct_sh('$D inspect ' + ' '.join(ids)), timeout=45) or '[]')
    plans, skipped = {}, []
    for c in raw:
        if not isinstance(c, dict):
            continue
        labels = ((c.get('Config') or {}).get('Labels') or {})
        name = str(c.get('Name') or '').lstrip('/')
        project = str(labels.get('com.docker.compose.project') or '')
        service = str(labels.get('com.docker.compose.service') or '')
        files = [f for f in str(labels.get('com.docker.compose.project.config_files') or '').split(',') if f]
        wd = str(labels.get('com.docker.compose.project.working_dir') or '')
        if not project or not service or not files:
            skipped.append({'name': name, 'why': 'nocompose'})
            continue
        if project.startswith('ix-'):
            skipped.append({'name': name, 'why': 'truenas'})
            continue
        if (not _CT_UPD_NAME.match(project) or not _CT_UPD_NAME.match(service) or len(files) > 8
                or any((not f.startswith('/')) or '\n' in f or '\x00' in f for f in files)
                or (wd and ((not wd.startswith('/')) or '\n' in wd or '\x00' in wd))):
            skipped.append({'name': name, 'why': 'invalid'})
            continue
        key = (project, wd, tuple(files))
        p = plans.setdefault(key, {'project': project, 'services': [], 'containers': []})
        if service not in p['services']:
            p['services'].append(service)
        p['containers'].append(name)
    return plans, skipped


def _ct_update(cids):
    plans, skipped = _ct_update_plan(cids)
    started = []
    for (project, wd, files), p in plans.items():
        base = ('$D compose -p ' + shq(project) + ((' --project-directory ' + shq(wd)) if wd else '')
                + ''.join(' -f ' + shq(f) for f in files))
        svc = ' '.join(shq(s) for s in p['services'])
        log_path = _ct_update_log_path(project)
        inner = (_CT_DOCKER + '{ ' + base + ' pull ' + svc + ' && ' + base + ' up -d ' + svc + '; rc=$?; echo "DK_EXIT=$rc"; } > '
                 + shq(log_path) + ' 2>&1')
        # un fichier compose déplacé ou effacé : on laisse ces conteneurs de côté plutôt que d'échouer au milieu
        _o, _e, missing = ssh_exec('sh -c ' + shq(' && '.join('[ -f ' + shq(f) + ' ]' for f in files)), timeout=20)
        if missing != 0:
            skipped.extend({'name': n, 'why': 'missing'} for n in p['containers'])
            continue
        outer = ': > ' + shq(log_path) + '; nohup setsid sh -c ' + shq(inner) + ' </dev/null >/dev/null 2>&1 &'
        ssh_ok('sh -c ' + shq(outer), timeout=30)
        started.append({'project': project, 'services': p['services'], 'containers': p['containers']})
    return {'ok': True, 'started': started, 'skipped': skipped}


def _ct_update_status(project):
    """Où en est la mise à jour d'un projet : en cours, terminée (code de sortie), et la fin de son journal."""
    project = str(project or '')
    if not _CT_UPD_NAME.match(project) or project.startswith('ix-'):
        raise ValueError('projet invalide')
    lp = shq(_ct_update_log_path(project))
    out, _err, _code = ssh_exec('sh -c ' + shq('[ -f ' + lp + ' ] && tail -c 6000 ' + lp + ' || echo DK_AUCUN'), timeout=20)
    text = out or ''
    if text.strip() == 'DK_AUCUN':
        return {'ok': True, 'project': project, 'running': False, 'exit_code': None, 'log': ''}
    m = re.search(r'DK_EXIT=(\d+)\s*$', text)
    return {'ok': True, 'project': project, 'running': m is None, 'exit_code': int(m.group(1)) if m else None,
            'log': re.sub(r'DK_EXIT=\d+\s*$', '', text)[-3000:]}
# ── MDM-CT-UPDATE-V1 (fin) ────────────────────────────────────────────────────


# ── MDM-CT-FRESH-V1 : images que Desktroo vient de mettre à jour ───────────
# TrueNAS ne compare les images au registre qu'une fois par jour. Après une mise à jour faite hors de ses applications
# (docker compose), il annonce donc encore « mise à jour disponible » jusqu'au lendemain. On retient ici, pour tous les
# navigateurs, quelle image porte chaque étiquette depuis la mise à jour : tant que c'est la même, l'annonce est périmée.
import time as _fresh_time
CT_FRESH_FILE = os.path.join(ACCESS_DATA_DIR, 'images-fresh.json')
_CT_FRESH_TTL = 26 * 3600
_CT_FRESH_TAG = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,299}$')
_CT_FRESH_ID = re.compile(r'^(sha256:)?[0-9a-f]{12,64}$')


def _ct_fresh_read():
    data = _access_read_json(CT_FRESH_FILE, {})
    now, out = _fresh_time.time(), {}
    if isinstance(data, dict):
        for tag, v in data.items():
            if (isinstance(tag, str) and isinstance(v, dict) and _CT_FRESH_TAG.match(tag)
                    and isinstance(v.get('id'), str) and _CT_FRESH_ID.match(v['id'])
                    and isinstance(v.get('at'), (int, float)) and 0 <= now - v['at'] < _CT_FRESH_TTL):
                out[tag] = {'id': v['id'], 'at': v['at']}
    return out


def _ct_fresh_get():
    return {'ok': True, 'fresh': {tag: v['id'] for tag, v in _ct_fresh_read().items()}}


def _ct_fresh_add(items):
    if not isinstance(items, list) or not items or len(items) > 100:
        raise ValueError('liste invalide')
    cur, now = _ct_fresh_read(), _fresh_time.time()
    for it in items:
        tag = str((it or {}).get('tag') or '') if isinstance(it, dict) else ''
        iid = str((it or {}).get('id') or '') if isinstance(it, dict) else ''
        if not _CT_FRESH_TAG.match(tag) or not _CT_FRESH_ID.match(iid):
            raise ValueError('image invalide')
        cur[tag] = {'id': iid, 'at': now}
    if len(cur) > 300:      # garde-fou : les plus récentes seulement
        cur = dict(sorted(cur.items(), key=lambda kv: kv[1]['at'], reverse=True)[:300])
    _access_write_json(CT_FRESH_FILE, cur)
    return _ct_fresh_get()
# ── MDM-CT-FRESH-V1 (fin) ──────────────────────────────────────────


# ── MDM-TN-UPLOAD-V1 : envoi d'un fichier à TrueNAS ────────────────────────
# Certaines opérations de TrueNAS reçoivent un fichier (restaurer la configuration, mettre à jour par fichier). Elles
# passent par son adresse /_upload, que le serveur web du bureau ne relaie pas. Le service fait le relais, sans rien
# garder : le fichier traverse, de la requête du navigateur à TrueNAS. C'est TrueNAS qui décide si l'opération est
# permise, d'après le jeton à usage unique que la session du bureau a obtenu de lui ; le service ne relaie que les
# opérations de la liste ci-dessous.
_TN_UPLOAD_MAX = {'config.upload': 12 * 1024 * 1024, 'update.file': 8 * 1024 ** 3}
_TN_UPLOAD_TOKEN = re.compile(r'^[A-Za-z0-9_-]{16,256}$')


def _truenas_upload(method, params, token, length, rfile, filename):
    """Relaie « length » octets lus sur « rfile » vers TrueNAS. Rend (statut HTTP, réponse de TrueNAS)."""
    import http.client
    import ssl
    import uuid
    if method not in _TN_UPLOAD_MAX:
        raise ValueError('opération non relayée')
    if not _TN_UPLOAD_TOKEN.match(token or ''):
        raise ValueError('jeton invalide')
    if not isinstance(params, list):
        raise ValueError('paramètres invalides')
    if not isinstance(length, int) or length <= 0 or length > _TN_UPLOAD_MAX[method]:
        raise ValueError('taille de fichier refusée')
    boundary = 'dk' + uuid.uuid4().hex
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', filename or '')[:120] or 'fichier'
    pre = ('--%s\r\nContent-Disposition: form-data; name="data"\r\nContent-Type: application/json\r\n\r\n%s\r\n'
           '--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
           'Content-Type: application/octet-stream\r\n\r\n'
           % (boundary, json.dumps({'method': method, 'params': params}), boundary, safe)).encode('utf-8')
    post = ('\r\n--%s--\r\n' % boundary).encode('ascii')

    def body():
        yield pre
        left = length
        while left > 0:
            chunk = rfile.read(min(1024 * 1024, left))
            if not chunk:
                raise IOError('envoi interrompu')
            left -= len(chunk)
            yield chunk
        yield post

    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # certificat auto-signé du NAS, comme pour la connexion
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(TRUENAS_API_HOST, timeout=900, context=ctx)
    try:
        conn.request('POST', '/_upload', body=body(), headers={
            'Authorization': 'Token ' + token,
            'Content-Type': 'multipart/form-data; boundary=' + boundary,
            'Content-Length': str(len(pre) + length + len(post)),
        })
        resp = conn.getresponse()
        raw = resp.read(65536).decode('utf-8', 'replace')
        status = resp.status
    finally:
        conn.close()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            data = {'result': data}
    except Exception:
        data = {'error': raw.strip()[:500]}
    return status, data
# ── MDM-TN-UPLOAD-V1 (fin) ─────────────────────────────────────────


# ── MDM-FILES-PLUS-V1 : corbeille, versions précédentes, lecteur, analyse de l'espace, journal, recherche ──
# Six services rendus à la fenêtre Fichiers et aux pages qui en dépendent. Tout ce bloc ne touche que des fichiers
# du NAS déjà accessibles au service ; il n'ouvre aucun accès nouveau vers l'extérieur.
import base64 as _px_b64
import hashlib as _px_hash
import hmac as _px_hmac
import secrets as _px_secrets
import subprocess as _px_sp
import threading as _px_th
import time as _px_time
from urllib.parse import quote as _px_quote, unquote as _px_unquote

_PX_BASE = '/mnt'                        # la corbeille et les versions ne concernent que les pools
_PX_TRASH = '.desktroo-corbeille'        # un dossier caché à la racine de chaque dataset : y déplacer est instantané
_PX_ID_RE = re.compile(r'^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$')
_PX_SNAP_RE = re.compile(r'^[A-Za-z0-9_.:\- ]{1,200}$')
_px_lock = _px_th.Lock()


class _PxNoTrash(ValueError):
    """L'élément ne peut pas aller à la corbeille : la raison est dans le message."""
    code = 'no_trash'


class _PxDatasetRoot(_PxNoTrash):
    """Un dataset ne se supprime pas comme un dossier : la page ne propose pas de le supprimer pour de bon."""
    code = 'dataset_root'


def _px_dev(p):
    return os.stat(p).st_dev


def _px_fs_root(path):
    """Racine du système de fichiers (le dataset) qui contient « path » : là où le périphérique change en remontant."""
    d = os.path.dirname(os.path.abspath(str(path)).rstrip('/')) or '/'
    dev = _px_dev(d)
    while d != '/':
        up = os.path.dirname(d) or '/'
        try:
            if _px_dev(up) != dev:
                break
        except OSError:
            break
        d = up
    return d


def _px_under_base(p):
    p = os.path.abspath(str(p))
    return p == _PX_BASE or p.startswith(_PX_BASE.rstrip('/') + '/')


def _px_by(handler):
    try:
        return _px_unquote(handler.headers.get('X-Desktroo-User', '') or '')[:64]
    except Exception:
        return ''


def _px_host(script, timeout=60):
    """Une commande sur le NAS, par SSH, en administrateur si le compte le permet."""
    out, err, code = ssh_exec('sudo -n sh -c ' + shq(script), timeout=timeout)
    if code != 0 and re.search(r'sudo: |password is required|not allowed|not in the sudoers', err or ''):
        out, err, code = ssh_exec('sh -c ' + shq(script), timeout=timeout)
    return out, err, code


def _px_tree_size(path, budget=20000):
    """Taille d'un dossier, sans y passer plus de « budget » entrées : (octets, complet)."""
    total, seen = 0, 0
    try:
        if not os.path.isdir(path) or os.path.islink(path):
            return os.lstat(path).st_size, True
        for root, dirs, files in os.walk(path):
            for nm in files:
                seen += 1
                if seen > budget:
                    return total, False
                try:
                    total += os.lstat(os.path.join(root, nm)).st_size
                except OSError:
                    pass
            seen += len(dirs)
    except OSError:
        return total, False
    return total, True


# ── Journal d'activité : ce que Desktroo a fait sur les fichiers ──
_PX_ACT_FILE = os.path.join(ACCESS_DATA_DIR, 'activity.jsonl')
_PX_ACT_MAX = 4 * 1024 * 1024


def _px_log(kind, by='', **info):
    """Une ligne par action. Jamais bloquant : un journal qui ne s'écrit pas ne doit pas faire échouer l'action."""
    try:
        rec = {'t': int(_px_time.time()), 'kind': str(kind), 'by': str(by or '')}
        for k, v in info.items():
            if v is not None and v != '':
                rec[k] = v
        line = json.dumps(rec, ensure_ascii=False) + '\n'
        with _px_lock:
            os.makedirs(os.path.dirname(_PX_ACT_FILE), exist_ok=True)
            try:
                if os.path.getsize(_PX_ACT_FILE) > _PX_ACT_MAX:      # on garde la moitié la plus récente
                    with open(_PX_ACT_FILE, 'rb') as f:
                        f.seek(-_PX_ACT_MAX // 2, os.SEEK_END)
                        tail = f.read()
                    tail = tail[tail.find(b'\n') + 1:]
                    with open(_PX_ACT_FILE + '.tmp', 'wb') as f:
                        f.write(tail)
                    os.replace(_PX_ACT_FILE + '.tmp', _PX_ACT_FILE)
            except OSError:
                pass
            with open(_PX_ACT_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
    except Exception as e:
        log.warning('journal d\'activité : %s', e)


def _px_activity(limit=500, since=0):
    limit = max(1, min(5000, int(limit or 500)))
    out = []
    try:
        with open(_PX_ACT_FILE, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 2 * 1024 * 1024))
            raw = f.read().decode('utf-8', errors='replace')
        lines = raw.splitlines()
        if size > 2 * 1024 * 1024:
            lines = lines[1:]
        for ln in reversed(lines):
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if since and rec.get('t', 0) < since:
                break
            out.append(rec)
            if len(out) >= limit:
                break
    except FileNotFoundError:
        pass
    return out


# ── Corbeille ──
_PX_TRASH_CONF = os.path.join(ACCESS_DATA_DIR, 'trash.json')


def _px_trash_conf():
    conf = {'enabled': True, 'days': 30}
    try:
        with open(_PX_TRASH_CONF, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict):
            conf['enabled'] = bool(d.get('enabled', True))
            conf['days'] = max(1, min(365, int(d.get('days', 30))))
    except Exception:
        pass
    return conf


def _px_trash_conf_set(body):
    conf = _px_trash_conf()
    if 'enabled' in body:
        conf['enabled'] = bool(body.get('enabled'))
    if 'days' in body:
        try:
            days = int(body.get('days'))
        except (TypeError, ValueError):
            raise ValueError('Durée invalide')
        if days < 1 or days > 365:
            raise ValueError('La durée va de 1 à 365 jours')
        conf['days'] = days
    os.makedirs(os.path.dirname(_PX_TRASH_CONF), exist_ok=True)
    tmp = _PX_TRASH_CONF + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(conf, f)
    os.replace(tmp, _PX_TRASH_CONF)
    return conf


def _px_in_trash(path):
    return ('/' + _PX_TRASH + '/') in (os.path.abspath(str(path)) + '/')


def _px_trash_why_not(path):
    """Pourquoi cet élément ne peut pas aller à la corbeille (None s'il le peut)."""
    p = os.path.abspath(str(path)).rstrip('/')
    if not _px_under_base(p) or p == _PX_BASE or os.path.dirname(p) == _PX_BASE:
        return 'Cet emplacement n\'a pas de corbeille : seuls les fichiers des pools en ont une.'
    if _px_in_trash(p):
        return 'Cet élément est déjà dans la corbeille.'
    if _px_is_dataset_root(p):
        return 'C\'est un dataset : il se supprime depuis Stockage, pas depuis Fichiers.'
    return None


def _px_is_dataset_root(p):
    try:
        return os.path.isdir(p) and not os.path.islink(p) and _px_dev(p) != _px_dev(os.path.dirname(p))
    except OSError:
        return False


# MDM-FS-JOBS-V1 : les déplacements / copies lancés sur le NAS sont inscrits ici, côté serveur : tous les appareils
# du même utilisateur les voient, et n'importe lequel peut les annuler (le navigateur qui les a lancés n'est plus requis).
_FS_JOBS = {}
_FS_JOBS_LOCK = _threading.Lock()


def _fs_job_ssh(by, kind, src, dst, script, timeout=7200, bid=''):
    """Exécute `script` sur le NAS (root) en tant que tâche suivie. Rend (out, err, code) ; lève 'Annulé' si on l'a annulée."""
    jid = _sh_secrets.token_hex(4)
    job = {'id': jid, 'kind': kind, 'src': src, 'dst': dst, 'by': by or '', 'bid': bid or '', 't0': int(_login_time.time()), 'cancel': False}
    with _FS_JOBS_LOCK:
        _FS_JOBS[jid] = job
    try:
        out, err, code = ssh_exec('sudo -n sh -c ' + shq(': dtjob-' + jid + '; ' + script), timeout=timeout)
    finally:
        with _FS_JOBS_LOCK:
            _FS_JOBS.pop(jid, None)
    if job['cancel']:
        raise RuntimeError('Opération annulée.')
    return out, err, code


def _fs_jobs_list(by):
    with _FS_JOBS_LOCK:
        jobs = [dict(j) for j in _FS_JOBS.values() if not by or not j.get('by') or j.get('by') == by]
    now = int(_login_time.time())
    return [{'id': j['id'], 'kind': j['kind'], 'src': j['src'], 'dst': j['dst'], 'by': j['by'], 'bid': j.get('bid', ''), 'elapsed': now - j['t0']} for j in sorted(jobs, key=lambda x: x['t0'])]


_FS_BATCH_CANCELLED = {}   # lot (une sélection déplacée fichier par fichier par un navigateur) -> heure d'annulation


def _fs_batch_check(bid):
    """Un lot annulé n'envoie plus rien : les fichiers suivants sont refusés tout de suite."""
    now = _login_time.time()
    for k in [k for k, t in _FS_BATCH_CANCELLED.items() if now - t > 3600]:
        _FS_BATCH_CANCELLED.pop(k, None)
    if bid and bid in _FS_BATCH_CANCELLED:
        raise ValueError('Opération annulée.')


def _fs_job_cancel(jid, by, bid=''):
    with _FS_JOBS_LOCK:
        job = _FS_JOBS.get(jid)
        if not job and bid:   # annulation d'un lot : toutes ses tâches en cours
            jobs = [j for j in _FS_JOBS.values() if j.get('bid') == bid]
            job = jobs[0] if jobs else None
    if not bid and job and job.get('bid'):
        bid = job['bid']
    if bid:
        _FS_BATCH_CANCELLED[bid] = _login_time.time()
    if not job:
        if bid:
            return
        raise ValueError('Tâche introuvable (déjà terminée ?).')
    if by and job.get('by') and job['by'] != by:
        raise ValueError('Tâche introuvable (déjà terminée ?).')
    jid = job['id']
    job['cancel'] = True
    # Le motif « dtjo[b]- » ne se trouve pas lui-même dans cette commande. On coupe le shell d'abord, puis ses enfants (cp, mv).
    script = ("pids=$(pgrep -f 'dtjo[b]-%s'); kids=''; for p in $pids; do kids=\"$kids $(pgrep -P $p)\"; done; "
              "[ -n \"$pids\" ] && kill -TERM $pids; [ -n \"$kids\" ] && kill -TERM $kids; true") % jid
    ssh_exec('sudo -n sh -c ' + shq(script), timeout=30)


def _px_move(src, dst, timeout=7200):
    try:
        os.rename(src, dst)
    except OSError as local_err:
        out, err, code = ssh_exec('sudo -n mv -f -- ' + shq(src) + ' ' + shq(dst), timeout=timeout)
        if code != 0 or os.path.lexists(src):
            raise RuntimeError((err or out or str(local_err)).strip()[:200])


def _px_trash(path, by=''):
    """Déplace un élément dans la corbeille de son dataset. Rend sa fiche."""
    src = os.path.abspath(str(path)).rstrip('/')
    if not os.path.lexists(src):
        raise ValueError('Introuvable')
    if not _px_trash_conf()['enabled']:
        raise _PxNoTrash('La corbeille est désactivée.')
    why = _px_trash_why_not(src)
    if why:
        raise (_PxDatasetRoot if why.startswith('C\'est un dataset') else _PxNoTrash)(why)
    root = _px_fs_root(src)
    if not _px_under_base(root) or root == _PX_BASE:
        raise _PxNoTrash('Cet emplacement n\'a pas de corbeille : seuls les fichiers des pools en ont une.')
    is_dir = os.path.isdir(src) and not os.path.islink(src)
    size, complete = _px_tree_size(src) if is_dir else (os.lstat(src).st_size, True)
    tid = _px_time.strftime('%Y%m%d-%H%M%S') + '-' + _px_secrets.token_hex(3)
    tdir = os.path.join(root, _PX_TRASH)
    slot = os.path.join(tdir, tid)
    name = os.path.basename(src)
    rec = {'id': tid, 'name': name, 'path': src, 'deleted_at': int(_px_time.time()), 'by': str(by or ''),
           'is_dir': is_dir, 'size': size, 'size_complete': complete}
    payload = json.dumps(rec, ensure_ascii=False)
    try:
        os.makedirs(slot, mode=0o700, exist_ok=True)
        try:
            os.chmod(tdir, 0o700)
        except OSError:
            pass
        with open(slot + '.json', 'w', encoding='utf-8') as f:
            f.write(payload)
    except OSError:
        # Droits ZFS qui refusent l'écriture au service : le vrai administrateur du NAS s'en charge.
        b64 = _px_b64.b64encode(payload.encode('utf-8')).decode('ascii')
        script = ('mkdir -p -m 700 ' + shq(slot) + ' && chmod 700 ' + shq(tdir) + ' && printf %s ' + shq(b64)
                  + ' | base64 -d > ' + shq(slot + '.json'))
        out, err, code = ssh_exec('sudo -n sh -c ' + shq(script), timeout=60)
        if code != 0:
            raise RuntimeError('Corbeille inaccessible : ' + (err or out or 'échec').strip()[:200])
    try:
        _px_move(src, os.path.join(slot, name))
    except Exception:
        for leftover in (slot + '.json',):
            try:
                os.remove(leftover)
            except OSError:
                pass
        try:
            os.rmdir(slot)
        except OSError:
            pass
        raise
    rec['root'] = root
    return rec


def _px_trash_roots():
    roots = []
    try:
        with open('/proc/mounts', encoding='utf-8', errors='replace') as f:
            for ln in f:
                parts = ln.split()
                if len(parts) >= 3:
                    mp = parts[1].replace('\\040', ' ')
                    if _px_under_base(mp) and mp != _PX_BASE and mp not in roots:
                        roots.append(mp)
    except OSError:
        pass
    return roots


# Taille réelle de la corbeille. À la suppression, un gros dossier n'est mesuré qu'en partie (« ≥ … ») pour ne pas
# faire attendre : sa taille exacte est calculée ensuite, en arrière-plan. La place occupée sur le disque (du -sk de
# chaque dossier de corbeille, ce que « Vider » libère vraiment, comme l'annonce la désinstallation) l'est aussi.
_PX_TRASH_BG = {'sizes': {}, 'busy': set(), 'disk': None, 'disk_key': None, 'disk_busy': False}
_PX_TRASH_BG_LOCK = _access_threading.Lock()


def _px_trash_size_bg(slot, tid):
    try:
        size, _c = _px_tree_size(slot, budget=10 ** 9)
        with _PX_TRASH_BG_LOCK:
            _PX_TRASH_BG['sizes'][tid] = size
        try:   # la fiche garde la taille exacte (si le service a le droit d'y écrire)
            with open(slot + '.json', encoding='utf-8') as f:
                rec = json.load(f)
            rec['size'], rec['size_complete'] = size, True
            with open(slot + '.json', 'w', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False))
        except Exception:
            pass
    finally:
        with _PX_TRASH_BG_LOCK:
            _PX_TRASH_BG['busy'].discard(tid)


def _px_trash_disk_bg(tdirs, key):
    total = 0
    try:
        for t in tdirs:
            try:
                out = subprocess.run(['du', '-sk', t], capture_output=True, text=True, timeout=900).stdout
                total += int((out.split() or ['0'])[0]) * 1024
            except Exception:
                pass
        with _PX_TRASH_BG_LOCK:
            _PX_TRASH_BG['disk'], _PX_TRASH_BG['disk_key'] = total, key
    finally:
        with _PX_TRASH_BG_LOCK:
            _PX_TRASH_BG['disk_busy'] = False


def _px_trash_disk(roots=None):
    """Place occupée par toutes les corbeilles, ou None tant qu'elle se calcule. Recalculée quand leur contenu change."""
    tdirs, key = [], []
    for root in (roots if roots is not None else _px_trash_roots()):
        t = os.path.join(root, _PX_TRASH)
        try:
            key.append((t, tuple(sorted(os.listdir(t)))))
            tdirs.append(t)
        except OSError:
            continue
    key = tuple(key)
    with _PX_TRASH_BG_LOCK:
        if _PX_TRASH_BG['disk_key'] == key:
            return _PX_TRASH_BG['disk']
        if not _PX_TRASH_BG['disk_busy']:
            _PX_TRASH_BG['disk_busy'] = True
            _access_threading.Thread(target=_px_trash_disk_bg, args=(tdirs, key), daemon=True).start()
    return None


def _px_trash_list(roots=None):
    conf = _px_trash_conf()
    items = []
    for root in (roots if roots is not None else _px_trash_roots()):
        tdir = os.path.join(root, _PX_TRASH)
        try:
            names = os.listdir(tdir)
        except OSError:
            continue
        for nm in names:
            if not nm.endswith('.json') or not _PX_ID_RE.match(nm[:-5]):
                continue
            try:
                with open(os.path.join(tdir, nm), encoding='utf-8') as f:
                    rec = json.load(f)
            except Exception:
                continue
            if not isinstance(rec, dict) or rec.get('id') != nm[:-5]:
                continue
            rec['root'] = root
            rec['expires_at'] = int(rec.get('deleted_at', 0)) + conf['days'] * 86400
            rec['present'] = os.path.lexists(os.path.join(tdir, rec['id'], str(rec.get('name', ''))))
            if rec.get('size_complete') is False and rec['present']:
                tid = rec['id']
                with _PX_TRASH_BG_LOCK:
                    known = _PX_TRASH_BG['sizes'].get(tid)
                    if known is None and tid not in _PX_TRASH_BG['busy']:
                        _PX_TRASH_BG['busy'].add(tid)
                        _access_threading.Thread(target=_px_trash_size_bg, args=(os.path.join(tdir, tid), tid), daemon=True).start()
                if known is not None:
                    rec['size'], rec['size_complete'] = known, True
            items.append(rec)
    items.sort(key=lambda r: -int(r.get('deleted_at', 0)))
    return items


def _px_trash_slot(root, tid):
    root = os.path.abspath(str(root or '')).rstrip('/')
    if not _PX_ID_RE.match(str(tid or '')) or not _px_under_base(root) or root == _PX_BASE:
        raise ValueError('Élément de corbeille invalide')
    slot = os.path.join(root, _PX_TRASH, tid)
    try:
        with open(slot + '.json', encoding='utf-8') as f:
            rec = json.load(f)
    except Exception:
        raise ValueError('Cet élément n\'est plus dans la corbeille')
    return slot, rec


def _px_trash_restore(root, tid):
    slot, rec = _px_trash_slot(root, tid)
    name = str(rec.get('name', ''))
    src = os.path.join(slot, name)
    if not name or '/' in name or not os.path.lexists(src):
        raise ValueError('Le contenu de cet élément a disparu de la corbeille')
    dst = str(rec.get('path') or '')
    # La fiche dit d'où vient l'élément. On ne la croit que dans son propre dataset : une fiche fabriquée à la main
    # ne peut pas faire poser un fichier ailleurs.
    root_abs = os.path.abspath(str(root)).rstrip('/')
    if not dst.startswith(root_abs + '/') or _px_in_trash(dst) or os.path.abspath(dst) != dst.rstrip('/'):
        raise ValueError('Emplacement d\'origine invalide')
    anc = os.path.dirname(dst)
    while anc != '/' and not os.path.isdir(anc):
        anc = os.path.dirname(anc)
    if _px_fs_root(os.path.join(anc, 'x')) != root_abs:
        raise ValueError('Emplacement d\'origine invalide')
    parent = os.path.dirname(dst)
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            out, err, code = ssh_exec('sudo -n mkdir -p -- ' + shq(parent), timeout=60)
            if code != 0:
                raise RuntimeError('Dossier d\'origine impossible à recréer : ' + (err or out).strip()[:200])
    if os.path.lexists(dst):
        dst = _unique_path(dst)
    _px_move(src, dst)
    _px_trash_drop(slot)
    return dst


def _px_trash_drop(slot):
    """Retire un emplacement de corbeille (contenu, fiche), puis le dossier de corbeille s'il est vide."""
    if os.path.lexists(slot):
        _privileged_remove(slot)
    try:
        os.remove(slot + '.json')
    except FileNotFoundError:
        pass
    except OSError:
        ssh_exec('sudo -n rm -f -- ' + shq(slot + '.json'), timeout=60)
    try:
        os.rmdir(os.path.dirname(slot))
    except OSError:
        pass


def _px_trash_purge(root, tid):
    slot, rec = _px_trash_slot(root, tid)
    _px_trash_drop(slot)
    return rec


def _px_trash_expire():
    conf = _px_trash_conf()
    now, n = _px_time.time(), 0
    for rec in _px_trash_list():
        if now - int(rec.get('deleted_at', 0)) > conf['days'] * 86400:
            try:
                _px_trash_drop(os.path.join(rec['root'], _PX_TRASH, rec['id']))
                n += 1
            except Exception as e:
                log.warning('corbeille : purge de %s impossible : %s', rec.get('name'), e)
    if n:
        _px_log('file.expire', '', count=n)
    return n


def _px_trash_loop():
    _px_time.sleep(600)
    while True:
        try:
            _px_trash_expire()
        except Exception as e:
            log.warning('corbeille : %s', e)
        _px_time.sleep(3 * 3600)


# ── Versions précédentes : le même fichier dans les snapshots de son dataset ──
# Les snapshots se lisent sur le NAS lui-même (le dossier « .zfs » ne se monte pas de façon fiable dans un conteneur).
def _px_rel_ok(rel):
    return not any(part in ('..', '') for part in str(rel).split('/')) if rel else True


_PX_VERS_SCRIPT = r'''
P=%(path)s
d=$P; while [ ! -e "$d" ] && [ "$d" != / ]; do d=$(dirname -- "$d"); done
root=$(stat -c %%m -- "$d" 2>/dev/null) || exit 3
case "$root" in %(base)s/*) ;; *) echo "NOSNAP"; exit 0;; esac
rel=${P#"$root"}; rel=${rel#/}
echo "ROOT|$root"
cur=$(stat -c '%%s|%%Y|%%F' -- "$P" 2>/dev/null) && echo "CUR|$cur"
ds=$(zfs list -H -o name "$root" 2>/dev/null) || { echo "NOSNAP"; exit 0; }
zfs list -H -p -t snapshot -o name,creation -S creation -d 1 "$ds" 2>/dev/null | head -n %(limit)d | while IFS="$(printf '\t')" read -r full created; do
  s=${full#*@}
  st=$(stat -c '%%s|%%Y|%%F' -- "$root/.zfs/snapshot/$s/$rel" 2>/dev/null) && echo "S|$s|$created|$st" || echo "N|$s|$created"
done
'''


def _px_versions(path, limit=200):
    p = os.path.abspath(str(path)).rstrip('/')
    if not _px_under_base(p) or p == _PX_BASE:
        raise ValueError('Les versions précédentes ne concernent que les fichiers des pools')
    out, err, code = _px_host(_PX_VERS_SCRIPT % {'path': shq(p), 'base': _PX_BASE.rstrip('/'), 'limit': max(1, min(500, int(limit)))}, timeout=180)
    if code != 0:
        raise RuntimeError((err or out or 'lecture des snapshots impossible').strip()[:300])
    res = {'path': p, 'root': '', 'current': None, 'versions': [], 'snapshots': 0, 'supported': True}
    for ln in out.splitlines():
        parts = ln.split('|')
        if parts[0] == 'NOSNAP':
            res['supported'] = False
        elif parts[0] == 'ROOT' and len(parts) >= 2:
            res['root'] = '|'.join(parts[1:])
        elif parts[0] == 'CUR' and len(parts) >= 4:
            res['current'] = {'size': int(parts[1]), 'mtime': int(parts[2]), 'is_dir': parts[3] == 'directory'}
        elif parts[0] in ('S', 'N') and len(parts) >= 3:
            res['snapshots'] += 1
            if parts[0] == 'S' and len(parts) >= 6:
                try:
                    res['versions'].append({'snapshot': parts[1], 'created': int(parts[2]), 'size': int(parts[3]),
                                            'mtime': int(parts[4]), 'is_dir': parts[5] == 'directory'})
                except ValueError:
                    pass
    return res


def _px_snap_path(root, snap, rel):
    root = os.path.abspath(str(root or '')).rstrip('/')
    snap = str(snap or '')
    rel = str(rel or '').strip('/')
    if not _px_under_base(root) or root == _PX_BASE or not _PX_SNAP_RE.match(snap) or snap in ('.', '..') or not _px_rel_ok(rel):
        raise ValueError('Version invalide')
    return root + '/.zfs/snapshot/' + snap + ('/' + rel if rel else '')


def _px_version_list(root, snap, rel):
    """Contenu d'un dossier tel qu'il était dans un snapshot."""
    src = _px_snap_path(root, snap, rel)
    script = 'cd -- ' + shq(src) + ' 2>/dev/null || exit 4; find . -mindepth 1 -maxdepth 1 -printf \'%y|%s|%T@|%f\\n\' | head -n 5000'
    out, err, code = _px_host(script, timeout=120)
    if code == 4:
        raise ValueError('Ce dossier n\'existe pas dans cette version')
    if code != 0:
        raise RuntimeError((err or out or 'lecture impossible').strip()[:300])
    now_dir = os.path.join(os.path.abspath(str(root)), str(rel or '').strip('/'))
    entries = []
    for ln in out.splitlines():
        parts = ln.split('|', 3)
        if len(parts) < 4 or not parts[3]:
            continue
        try:
            entries.append({'name': parts[3], 'is_dir': parts[0] == 'd', 'size': 0 if parts[0] == 'd' else int(parts[1]),
                            'mtime': int(float(parts[2])), 'exists_now': os.path.lexists(os.path.join(now_dir, parts[3]))})
        except ValueError:
            pass
    entries.sort(key=lambda e: (not e['is_dir'], e['name'].lower()))
    return {'root': os.path.abspath(str(root)), 'snapshot': snap, 'rel': str(rel or '').strip('/'), 'entries': entries}


def _px_version_restore(root, snap, rel, mode='beside', created=0, by=''):
    """Recopie une version depuis un snapshot. « beside » : à côté de l'actuel, sous un nom daté. « replace » : à la place
    de l'actuel, qui part d'abord à la corbeille."""
    src = _px_snap_path(root, snap, rel)
    if not str(rel or '').strip('/'):
        raise ValueError('Un dataset entier se restaure depuis la fenêtre Snapshots')
    dst = os.path.join(os.path.abspath(str(root)), str(rel).strip('/'))
    trashed = None
    if mode == 'replace':
        if os.path.lexists(dst):
            trashed = _px_trash(dst, by)
    elif os.path.lexists(dst):
        stamp = _px_time.strftime('%d-%m-%Y %Hh%M', _px_time.localtime(int(created))) if created else snap
        base = os.path.basename(dst)
        low = base.lower()
        for comp in ('.tar.gz', '.tar.bz2', '.tar.xz'):
            if low.endswith(comp):
                stem, ext = base[:-len(comp)], base[-len(comp):]
                break
        else:
            stem, ext = os.path.splitext(base)
        dst = _unique_path(os.path.join(os.path.dirname(dst), stem + ' (version du ' + stamp + ')' + ext))
    parent = os.path.dirname(dst)
    script = '[ -e ' + shq(src) + ' ] || exit 4; mkdir -p -- ' + shq(parent) + ' && cp -a -- ' + shq(src) + ' ' + shq(dst)
    out, err, code = _px_host(script, timeout=7200)
    if code == 4:
        raise ValueError('Cet élément n\'existe pas dans cette version')
    if code != 0:
        raise RuntimeError((err or out or 'copie impossible').strip()[:300])
    _px_log('file.version', by, path=dst, snapshot=snap, replaced=bool(trashed))
    return {'dst': dst, 'name': os.path.basename(dst), 'trashed': bool(trashed)}


# ── Lecteur : une adresse temporaire pour lire un fichier dans le navigateur ──
# Une balise <img>, <video> ou <iframe> ne peut pas envoyer le jeton du service. Le service signe donc une adresse qui
# ne vaut que pour ce fichier et pour quelques heures. La clé change à chaque démarrage du service.
_PX_MEDIA_KEY = _px_secrets.token_bytes(32)
_PX_MEDIA_TTL = 6 * 3600
_PX_MEDIA_TYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp',
    'bmp': 'image/bmp', 'avif': 'image/avif', 'ico': 'image/x-icon', 'svg': 'image/svg+xml',
    'mp4': 'video/mp4', 'm4v': 'video/mp4', 'webm': 'video/webm', 'mov': 'video/quicktime', 'mkv': 'video/x-matroska',
    'ogv': 'video/ogg',
    'mp3': 'audio/mpeg', 'm4a': 'audio/mp4', 'aac': 'audio/aac', 'ogg': 'audio/ogg', 'oga': 'audio/ogg',
    'opus': 'audio/ogg', 'wav': 'audio/wav', 'flac': 'audio/flac', 'weba': 'audio/webm',
    'pdf': 'application/pdf',
}


def _px_media_type(path):
    return _PX_MEDIA_TYPES.get(os.path.splitext(str(path))[1].lower().lstrip('.'))


def _px_media_ticket(path, ttl=None):
    p = os.path.abspath(str(path))
    if not _px_media_type(p):
        raise ValueError('Ce type de fichier ne se lit pas dans le navigateur')
    if not os.path.isfile(p):
        raise ValueError('Fichier introuvable')
    raw = json.dumps([p, int(_px_time.time()) + int(ttl or _PX_MEDIA_TTL)], ensure_ascii=False).encode('utf-8')
    sig = _px_hmac.new(_PX_MEDIA_KEY, raw, _px_hash.sha256).digest()[:16]
    return _px_b64.urlsafe_b64encode(sig + raw).decode('ascii').rstrip('=')


def _px_media_path(ticket):
    try:
        blob = _px_b64.urlsafe_b64decode(str(ticket) + '=' * (-len(str(ticket)) % 4))
        sig, raw = blob[:16], blob[16:]
        if not _px_hmac.compare_digest(sig, _px_hmac.new(_PX_MEDIA_KEY, raw, _px_hash.sha256).digest()[:16]):
            return None
        p, exp = json.loads(raw.decode('utf-8'))
        if int(exp) < _px_time.time() or not _px_media_type(p):
            return None
        return str(p)
    except Exception:
        return None


def _px_range(header, size):
    """« bytes=a-b » → (début, fin incluse), None si absent, False si irrecevable."""
    m = re.match(r'^\s*bytes=(\d*)-(\d*)\s*$', str(header or ''))
    if not header:
        return None
    if not m or (m.group(1) == '' and m.group(2) == ''):
        return False
    if m.group(1) == '':
        n = int(m.group(2))
        if n <= 0:
            return False
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) != '' else size - 1
    if start >= size or end < start:
        return False
    return start, min(end, size - 1)


def _px_media_serve(handler, url_path):
    parts = [x for x in str(url_path).split('/') if x]     # ['media', <billet>, (nom)]
    p = _px_media_path(parts[1]) if len(parts) >= 2 else None
    if not p or not os.path.isfile(p):
        handler.send_response(404)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return
    ctype = _px_media_type(p)
    size = os.path.getsize(p)
    rng = _px_range(handler.headers.get('Range'), size)
    if rng is False:
        handler.send_response(416)
        handler.send_header('Content-Range', 'bytes */%d' % size)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return
    start, end = rng if rng else (0, size - 1)
    length = max(0, end - start + 1)
    handler.send_response(206 if rng else 200)
    handler.send_header('Content-Type', ctype)
    handler.send_header('Content-Length', str(length))
    handler.send_header('Accept-Ranges', 'bytes')
    if rng:
        handler.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
    handler.send_header('Content-Disposition', "inline; filename*=UTF-8''" + _px_quote(os.path.basename(p)))
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Cache-Control', 'private, max-age=3600')
    if ctype == 'image/svg+xml':     # un SVG peut porter du script : ouvert seul, il ne doit rien pouvoir exécuter
        handler.send_header('Content-Security-Policy', "sandbox; default-src 'none'; style-src 'unsafe-inline'")
    handler.end_headers()
    try:
        with open(p, 'rb') as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                left -= len(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:
        log.warning('lecteur : %s', e)


# ── Analyse de l'espace : la taille de chaque dossier d'un emplacement ──
_px_scans = {}            # chemin -> {'state', 'started', 'result' | 'error'}
_PX_SCAN_TTL = 15 * 60
_PX_SCAN_MAX = 2          # « du » pèse sur les disques : deux analyses à la fois, pas plus


def _px_scan_run(path):
    t0 = _px_time.time()
    try:
        proc = _px_sp.run(['du', '-x', '-k', '-d', '1', '--', path], capture_output=True, text=True, timeout=1800)
        sizes = {}
        for ln in proc.stdout.splitlines():
            kb, _tab, full = ln.partition('\t')
            try:
                sizes[full.rstrip('/') or '/'] = int(kb) * 1024
            except ValueError:
                pass
        base = path.rstrip('/') or '/'
        total = sizes.get(base)
        if total is None:
            raise RuntimeError((proc.stderr or 'analyse impossible').strip().splitlines()[-1][:200])
        entries, dirs_sum = [], 0
        dev = _px_dev(path)
        with os.scandir(path) as it:
            for e in it:
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    other = st.st_dev != dev
                    sz = None if other else sizes.get(os.path.join(base, e.name), 0)
                    if sz:
                        dirs_sum += sz
                    entries.append({'name': e.name, 'is_dir': True, 'size': sz, 'other_fs': other, 'trash': e.name == _PX_TRASH})
                else:
                    entries.append({'name': e.name, 'is_dir': False, 'size': st.st_blocks * 512})
        entries.sort(key=lambda x: -(x['size'] or 0))
        files = [x for x in entries if not x['is_dir']]
        result = {'path': base, 'total': total, 'dirs': [x for x in entries if x['is_dir']], 'files': files[:60],
                  'files_count': len(files), 'files_size': sum(x['size'] for x in files),
                  'partial': bool(proc.returncode != 0 and proc.stderr), 'scanned_at': int(_px_time.time()),
                  'took': round(_px_time.time() - t0, 1)}
        with _px_lock:
            _px_scans[path] = {'state': 'done', 'started': t0, 'result': result}
    except _px_sp.TimeoutExpired:
        with _px_lock:
            _px_scans[path] = {'state': 'error', 'started': t0, 'error': 'Analyse trop longue : essayez un sous-dossier.'}
    except Exception as e:
        with _px_lock:
            _px_scans[path] = {'state': 'error', 'started': t0, 'error': str(e)[:300]}


def _px_scan(path, fresh=False):
    p = os.path.abspath(str(path)).rstrip('/') or '/'
    if not _px_under_base(p) or p == _PX_BASE:
        raise ValueError('L\'analyse porte sur les dossiers des pools')
    if not os.path.isdir(p):
        raise ValueError('Dossier introuvable')
    now = _px_time.time()
    with _px_lock:
        for k in [k for k, v in _px_scans.items() if v['state'] != 'running' and now - v['started'] > _PX_SCAN_TTL]:
            del _px_scans[k]
        cur = _px_scans.get(p)
        if cur and (cur['state'] == 'running' or (cur['state'] == 'done' and not fresh)):
            pass
        elif sum(1 for v in _px_scans.values() if v['state'] == 'running') >= _PX_SCAN_MAX:
            return {'state': 'busy', 'path': p}
        else:
            cur = _px_scans[p] = {'state': 'running', 'started': now}
            _px_th.Thread(target=_px_scan_run, args=(p,), daemon=True).start()
        out = {'state': cur['state'], 'path': p, 'elapsed': round(now - cur['started'], 1)}
        if cur['state'] == 'done':
            out.update(cur['result'])
        elif cur['state'] == 'error':
            out['error'] = cur.get('error', '')
        return out


# ── Recherche globale : un nom de fichier, dans tous les pools ──
_PX_FIND_SKIP = ('.zfs', _PX_TRASH, 'ix-applications', '.ix-apps', 'ix-apps', '.system', 'lost+found')


def _px_find(q, limit=40, seconds=10):
    q = str(q or '').strip()
    if len(q) < 2:
        return {'results': [], 'truncated': False}
    limit = max(1, min(200, int(limit or 40)))
    esc = ''.join(('\\' + c) if c in '*?[]\\' else c for c in q)
    cmd = ['find', _PX_BASE, '-mindepth', '2', '(']
    for i, nm in enumerate(_PX_FIND_SKIP):
        cmd += (['-o'] if i else []) + ['-name', nm]
    cmd += ['-o', '-name', '.*', ')', '-prune', '-o', '-iname', '*' + esc + '*', '-print']
    results, truncated = [], False
    t0 = _px_time.time()
    proc = _px_sp.Popen(cmd, stdout=_px_sp.PIPE, stderr=_px_sp.DEVNULL, text=True, errors='replace')
    killer = _px_th.Timer(seconds, proc.kill)
    killer.start()
    try:
        for ln in proc.stdout:
            full = ln.rstrip('\n')
            if not full:
                continue
            isd, sz, mt = False, 0, 0
            try:
                st = os.stat(full)
                isd = os.path.isdir(full)
                mt = st.st_mtime
                sz = 0 if isd else st.st_size
            except OSError:
                pass
            results.append({'name': os.path.basename(full), 'path': full, 'is_dir': isd, 'size': sz, 'mtime': mt})
            if len(results) >= limit:
                truncated = True
                break
    finally:
        killer.cancel()
        try:
            proc.kill()
        except Exception:
            pass
    if _px_time.time() - t0 >= seconds:
        truncated = True
    return {'results': results, 'truncated': truncated}


# ── Routes ──
def _px_get(handler, path, qs):
    """Rend True si la requête était pour ce bloc."""
    try:
        if path == '/trash/config':
            handler._json(200, dict(_px_trash_conf(), ok=True))
        elif path == '/trash/list':
            items = _px_trash_list()
            disk = _px_trash_disk()
            handler._json(200, dict(_px_trash_conf(), ok=True, items=items, total=sum(int(i.get('size') or 0) for i in items), disk=disk,
                                    pending=disk is None or any(i.get('size_complete') is False and i.get('present') for i in items)))
        elif path == '/versions':
            handler._json(200, dict(_px_versions(qs.get('path', ''), qs.get('limit', '200') or 200), ok=True))
        elif path == '/versions/list':
            handler._json(200, dict(_px_version_list(qs.get('root', ''), qs.get('snapshot', ''), qs.get('rel', '')), ok=True))
        elif path == '/media/ticket':
            p = qs.get('path', '')
            t = _px_media_ticket(p)
            handler._json(200, {'ok': True, 'url': '/media/' + t + '/' + _px_quote(os.path.basename(p)), 'ttl': _PX_MEDIA_TTL,
                                'type': _px_media_type(p), 'size': os.path.getsize(p)})
        elif path == '/space/scan':
            handler._json(200, dict(_px_scan(qs.get('path', ''), qs.get('fresh') == '1'), ok=True))
        elif path == '/activity':
            handler._json(200, {'ok': True, 'events': _px_activity(qs.get('limit', '500') or 500, int(qs.get('since', '0') or 0))})
        elif path == '/search/global':
            handler._json(200, dict(_px_find(qs.get('q', ''), qs.get('limit', '40') or 40), ok=True))
        else:
            return False
    except ValueError as e:
        handler._json(400, {'error': str(e)})
    except Exception as e:
        handler._json(500, {'error': str(e)})
    return True


_PX_POST = ('/trash/config', '/trash/restore', '/trash/purge', '/versions/restore', '/activity/note')


def _px_post(handler, path):
    try:
        body = handler._body()
        by = _px_by(handler)
        if path == '/trash/config':
            handler._json(200, dict(_px_trash_conf_set(body), ok=True))
        elif path == '/trash/restore':
            done, failed = [], []
            for it in (body.get('items') or [])[:500]:
                try:
                    dst = _px_trash_restore(it.get('root'), it.get('id'))
                    done.append(dst)
                    _px_log('file.restore', by, path=dst)
                except Exception as e:
                    failed.append({'id': it.get('id'), 'error': str(e)})
            handler._json(200, {'ok': True, 'restored': done, 'failed': failed})
        elif path == '/trash/purge':
            items = _px_trash_list() if body.get('all') else (body.get('items') or [])[:2000]
            done, failed = 0, []
            for it in items:
                try:
                    _px_trash_purge(it.get('root'), it.get('id'))
                    done += 1
                except Exception as e:
                    failed.append({'id': it.get('id'), 'error': str(e)})
            if done:
                _px_log('file.purge', by, count=done)
            handler._json(200, {'ok': True, 'purged': done, 'failed': failed})
        elif path == '/versions/restore':
            handler._json(200, dict(_px_version_restore(body.get('root'), body.get('snapshot'), body.get('rel'),
                                                        str(body.get('mode') or 'beside'), int(body.get('created') or 0), by), ok=True))
        elif path == '/activity/note':
            # Ce que la page a fait par TrueNAS plutôt que par le service (nouveau dossier, par exemple)
            kind = str(body.get('kind') or '')
            if not re.match(r'^file\.(mkdir|upload)$', kind):
                raise ValueError('Action inconnue')
            _px_log(kind, by, path=str(body.get('path') or '')[:1000], count=int(body.get('count') or 0) or None)
            handler._json(200, {'ok': True})
        else:
            return False
    except ValueError as e:
        handler._json(400, {'error': str(e)})
    except Exception as e:
        handler._json(500, {'error': str(e)})
    return True
# ── MDM-FILES-PLUS-V1 (fin) ─────────────────────────────────────────


def _npm_status():
    """Détecte un reverse-proxy NPMplus / Nginx Proxy Manager sur l'hôte.
    Matching sur le nom, l'image et le port admin 81. Utilise sudo (socket Docker root)."""
    info = {'running': False, 'containers': [], 'nas_ip': (_truenas_ip() if SSH_VIA_GATEWAY else SSH_HOST)}
    cmd = (
        "sudo -n docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null "
        "|| docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null"
    )
    try:
        out, _err, _code = ssh_exec(cmd, timeout=20)
        matches = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            name = line.split('|', 1)[0]
            if ('npmplus' in low or 'nginx-proxy-manager' in low
                    or ':81->' in low or ':81/' in low):
                if name and name not in matches:
                    matches.append(name)
        info['running'] = bool(matches)
        info['containers'] = matches
    except Exception as e:
        info['error'] = str(e)
    try:   # module Desktroo Apps : port de l'admin connu, et l'interface propose l'installation ou la reprise
        info['module'] = STORE_NPM_ID in store_installed_get() or bool(store_npm_linked())
        info['admin_port'] = int(((_lic_read_json(STORE_NPM_FILE) or {}).get('ports') or {}).get('admin') or 0) or None
    except Exception:
        pass
    return info


def _web_dataset_path():
    return '/mnt/%s/%s' % (WEB_DATASET_POOL, WEB_DATASET_NAME)


def _web_create_dataset(pool=None, name=None):
    """Crée le dataset Web et applique des droits adaptés à l'hébergement :
    propriétaire root, groupe www-data (gid 82) avec setgid, group rwx, other r-x.
    nginx (lecture via 'other') et php-fpm (lecture/écriture via le groupe) accèdent
    tous deux aux fichiers."""
    pool = str(pool or WEB_DATASET_POOL).strip()
    name = str(name or WEB_DATASET_NAME).strip()
    if not _WEB_DS_RE.match(pool) or not _WEB_DS_RE.match(name):
        raise ValueError('Nom de pool ou de dataset invalide.')
    gid = str(WEB_WWW_GID)
    if not re.match(r'^\d+$', gid):
        gid = '82'
    ds = pool + '/' + name
    path = '/mnt/' + ds
    script = (
        'if sudo -n zfs list -H -o name ' + shq(ds) + ' >/dev/null 2>&1; then echo EXISTS; '
        'else sudo -n zfs create ' + shq(ds) + ' && echo CREATED; fi && '
        'sudo -n chown 0:' + gid + ' ' + shq(path) + ' && '
        'sudo -n chmod 2775 ' + shq(path) + ' && echo DONE'
    )
    out, err, code = ssh_exec(script, timeout=60)
    if code != 0 or 'DONE' not in out:
        raise RuntimeError(
            (err or out or 'Échec de création du dataset').strip()
            + ' — vérifiez que l\'utilisateur SSH a les droits sudo (zfs/chown/chmod).'
        )
    return {'path': path, 'dataset': ds, 'existed': 'EXISTS' in out}


def _web_mkdir(parent, name):
    """Crée un sous-dossier de site (dans le dataset Web) avec les bons droits.
    fileops tourne en root avec /mnt monté : création directe, sans SSH."""
    name = str(name or '').strip()
    if name in ('.', '..') or not re.match(r'^[A-Za-z0-9_.-]{1,64}$', name):
        raise ValueError('Nom de dossier invalide (lettres, chiffres, . _ - ).')
    parent_real = os.path.realpath(str(parent or '').strip() or _web_dataset_path())
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not (parent_real == base or parent_real.startswith(base + '/')):
        raise PermissionError('Le dossier doit être sous %s/.' % WEB_ROOT_ALLOWED)
    target = os.path.join(parent_real, name)
    existed = os.path.isdir(target)
    os.makedirs(target, exist_ok=True)
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    try:
        os.chown(target, 0, gid)
    except OSError:
        pass
    try:
        os.chmod(target, 0o2775)
    except OSError:
        pass
    return {'path': target, 'existed': existed}


def _web_adopt(src, dry_run=False):
    """Deplace un dossier existant dans le dataset Web et lui donne les droits
    des sites. Sert au « Servir sur le web » du gestionnaire de fichiers : un site
    vit dans le dataset Web, pas ailleurs — c'est la seule zone que _web_reimport()
    sait retrouver, et la seule ou les droits sont garantis.

    Deplacement et non copie : pas de doublon, et c'est instantane dans le meme
    pool. Vers un autre pool, os.rename echoue et shutil.move recopie : long, d'ou
    le 'cross' renvoye en dry_run pour prevenir avant de lancer.
    """
    src_real = os.path.realpath(str(src or '').strip())
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not src_real.startswith(base + '/'):
        raise PermissionError('Le dossier doit etre sous %s/.' % WEB_ROOT_ALLOWED)
    if not os.path.isdir(src_real):
        raise ValueError('Dossier introuvable : %s' % src)
    ds = os.path.realpath(_web_dataset_path())
    if not os.path.isdir(ds):
        raise ValueError('Dataset Web introuvable (%s). Cree-le depuis Sites web.' % ds)
    # Deja a sa place : rien a deplacer, on rend le chemin tel quel.
    if src_real == ds or os.path.dirname(src_real) == ds:
        return {'path': src_real, 'moved': False, 'already': True, 'cross': False}
    if ds == src_real or ds.startswith(src_real + '/'):
        raise ValueError('Ce dossier contient le dataset Web : il ne peut pas y etre deplace.')
    name = os.path.basename(src_real)
    if not re.match(r'^[A-Za-z0-9_.-]{1,64}$', name):
        raise ValueError('Nom de dossier invalide pour un site (lettres, chiffres, . _ - ).')
    if os.path.ismount(src_real):
        raise ValueError('Ce dossier est un dataset ZFS monte : il ne se deplace pas comme un dossier. '
                         'Cree un site vide puis copie son contenu, ou deplace le dataset depuis Stockage.')
    dst = os.path.join(ds, name)
    if os.path.exists(dst):
        raise ValueError('Un dossier « %s » existe deja dans le dataset Web.' % name)
    try:
        cross = os.stat(src_real).st_dev != os.stat(ds).st_dev
    except OSError:
        cross = True
    if dry_run:
        return {'path': dst, 'moved': False, 'already': False, 'cross': bool(cross)}
    _sh_shutil.move(src_real, dst)
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    try:
        os.chown(dst, 0, gid)
    except OSError:
        pass
    try:
        os.chmod(dst, 0o2775)
    except OSError:
        pass
    return {'path': dst, 'moved': True, 'already': False, 'cross': bool(cross), 'from': src_real}


def _web_write_user_ini(root, ini):
    """Écrit un .user.ini dans le dossier du site : réglages php.ini propres au
    site (priment sur le profil de la version). Directives 'par répertoire'."""
    root = os.path.realpath(str(root or ''))
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not root.startswith(base + '/'):
        raise PermissionError('Chemin hors zone autorisée.')
    path = os.path.join(root, '.user.ini')
    if not ini:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    lines = ['; Généré par Desktroo — réglages PHP du site']
    for k, v in ini.items():
        lines.append('%s = %s' % (k, v))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    try:
        os.chown(path, 0, gid)
        os.chmod(path, 0o664)
    except OSError:
        pass


_WEB_NAME_RE = re.compile(r'^[A-Za-z0-9 _.\-]{1,48}$')
_WEB_SN_RE = re.compile(r'^[A-Za-z0-9*._\-]{1,253}$')
_WEB_UP_RE = re.compile(r'^https?://[A-Za-z0-9._\-]+(:\d+)?(/\S*)?$')


def _web_read():
    with _web_lock:
        data = _access_read_json(WEB_FILE, {})
        return data if isinstance(data, dict) else {}


def _web_write(data):
    with _web_lock:
        _access_write_json(WEB_FILE, data)


def _web_used_ports(data, exclude=None):
    used = set()
    for sid, s in data.items():
        if sid == exclude:
            continue
        if s.get('enabled', True) and s.get('port'):
            used.add(int(s['port']))
    return used


def _web_auto_port(data):
    used = _web_used_ports(data)
    for p in range(WEB_PORT_MIN, WEB_PORT_MAX + 1):
        if p not in used:
            return p
    raise RuntimeError('Aucun port libre dans la plage %d-%d' % (WEB_PORT_MIN, WEB_PORT_MAX))


def _web_validate(site, data, sid=None):
    name = str(site.get('name', '')).strip()
    if not _WEB_NAME_RE.match(name):
        raise ValueError('Nom invalide (lettres, chiffres, espace, . _ - ; 48 max).')
    typ = site.get('type')
    if typ not in ('static', 'php', 'proxy'):
        raise ValueError('Type invalide.')
    port = site.get('port')
    if port in (None, '', 0, '0'):
        port = _web_auto_port(data)
    port = int(port)
    if not (WEB_PORT_MIN <= port <= WEB_PORT_MAX):
        raise ValueError('Port hors plage %d-%d.' % (WEB_PORT_MIN, WEB_PORT_MAX))
    sn = str(site.get('server_name', '') or '').strip()
    if sn and not _WEB_SN_RE.match(sn):
        raise ValueError('Nom de domaine invalide.')
    for other_id, o in data.items():
        if other_id == sid:
            continue
        if not o.get('enabled', True):
            continue
        if int(o.get('port', 0)) == port and str(o.get('server_name', '') or '') == sn:
            raise ValueError('Le port %d est déjà utilisé%s.' % (port, (' pour ce domaine' if sn else '')))
    out = {'name': name, 'type': typ, 'port': port, 'server_name': sn}
    if typ in ('static', 'php'):
        root = os.path.realpath(str(site.get('root', '')))
        base = WEB_ROOT_ALLOWED.rstrip('/')
        if not (root == base or root.startswith(base + '/')):
            raise ValueError('Le dossier doit être sous %s/.' % WEB_ROOT_ALLOWED)
        if not os.path.isdir(root):
            raise ValueError('Dossier introuvable : %s' % site.get('root'))
        out['root'] = root
        if typ == 'php':
            ver = str(site.get('php_version') or WEB_PHP_DEFAULT)
            if ver not in WEB_PHP_VERSIONS:
                raise ValueError('Version PHP indisponible : %s' % ver)
            out['php_version'] = ver
    else:
        up = str(site.get('upstream', '')).strip()
        if not _WEB_UP_RE.match(up):
            raise ValueError('Cible proxy invalide (ex: http://192.168.0.50:3000).')
        out['upstream'] = up
    # Conserve l'appli d'origine (installeur) pour les actions màj/SSL/clone.
    app = str(site.get('app') or '')
    if app in APP_CATALOG:
        out['app'] = app
    return out


def _web_conf(site, proxy_port=None, sid=None):
    sn = site.get('server_name') or ''
    sn_line = ('    server_name %s;\n' % sn) if sn else ''
    listen_lines = '    listen %d;\n' % int(site['port'])
    if proxy_port and sn:
        listen_lines += '    listen %d;\n' % int(proxy_port)
    log_lines = ''
    if sid:
        log_lines = (
            '    access_log %s/site-%s.access.log;\n' % (WEB_LOG_DIR, sid) +
            '    error_log %s/site-%s.error.log;\n' % (WEB_LOG_DIR, sid)
        )
    t = site['type']
    if t == 'static':
        body = (
            '    root %s;\n' % site['root'] +
            '    index index.html index.htm;\n'
            '    location / { try_files $uri $uri/ =404; }\n'
            '    autoindex off;\n'
        )
    elif t == 'php':
        body = (
            '    root %s;\n' % site['root'] +
            '    index index.php index.html;\n'
            '    location / { try_files $uri $uri/ /index.php?$query_string; }\n'
            '    location ~ \\.php$ {\n'
            '        include fastcgi_params;\n'
            '        fastcgi_pass %s;\n' % WEB_PHP_VERSIONS.get(site.get('php_version'), WEB_PHP_UPSTREAM) +
            '        fastcgi_index index.php;\n'
            '        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;\n'
            # HTTPS derrière reverse-proxy : détection locale (sans map globale, vhost autonome).
            '        set $fcgi_https "";\n'
            '        if ($http_x_forwarded_proto = "https") { set $fcgi_https "on"; }\n'
            '        fastcgi_param HTTPS $fcgi_https if_not_empty;\n'
            # Back-offices lourds (PrestaShop…) : 1er chargement long + gros en-têtes.
            '        fastcgi_read_timeout 300s;\n'
            '        fastcgi_send_timeout 300s;\n'
            '        fastcgi_connect_timeout 60s;\n'
            '        fastcgi_buffer_size 32k;\n'
            '        fastcgi_buffers 16 16k;\n'
            '        fastcgi_busy_buffers_size 64k;\n'
            '    }\n'
        )
    else:
        body = (
            '    location / {\n'
            '        proxy_pass %s;\n' % site['upstream'] +
            '        proxy_http_version 1.1;\n'
            '        proxy_set_header Host $host;\n'
            '        proxy_set_header X-Real-IP $remote_addr;\n'
            '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
            '        proxy_set_header X-Forwarded-Proto $scheme;\n'
            '        proxy_set_header Upgrade $http_upgrade;\n'
            '        proxy_set_header Connection "upgrade";\n'
            '    }\n'
        )
    return (
        'server {\n'
        + listen_lines +
        sn_line +
        log_lines +
        '    client_max_body_size 1024m;\n' +
        body +
        '}\n'
    )


def _web_regenerate():
    """Réécrit toutes les confs des sites activés et signale un reload au watcher."""
    data = _web_read()
    proxy = _web_proxy_get()
    proxy_port = proxy['port'] if proxy['enabled'] else None
    os.makedirs(WEB_CONF_DIR, exist_ok=True)
    os.makedirs(WEB_LOG_DIR, exist_ok=True)
    try:
        for fn in os.listdir(WEB_CONF_DIR):
            if (fn.startswith('site-') or fn in ('00-proxy-default.conf', '00-maps.conf')) and fn.endswith('.conf'):
                try:
                    os.unlink(os.path.join(WEB_CONF_DIR, fn))
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    # (La détection HTTPS derrière proxy est désormais locale à chaque vhost PHP,
    #  via set/if — plus de map globale : chaque conf est autonome et robuste.)
    # Reverse-proxy intégré : serveur par défaut sur le port partagé (hôte inconnu -> 404)
    if proxy_port:
        try:
            with open(os.path.join(WEB_CONF_DIR, '00-proxy-default.conf'), 'w', encoding='utf-8') as f:
                f.write('server {\n    listen %d default_server;\n    server_name _;\n'
                        '    return 404;\n}\n' % proxy_port)
        except OSError as e:
            log.warning('web proxy default write error: %s', e)
    for sid, s in data.items():
        if not isinstance(s, dict) or not s.get('enabled', True):
            continue
        try:
            conf = _web_conf(s, proxy_port=proxy_port, sid=sid)
        except Exception as e:
            log.warning('web conf gen error for %s: %s', sid, e)
            continue
        try:
            with open(os.path.join(WEB_CONF_DIR, 'site-%s.conf' % sid), 'w', encoding='utf-8') as f:
                f.write(conf)
        except OSError as e:
            log.warning('web conf write error for %s: %s', sid, e)
    try:
        with open(os.path.join(WEB_CONF_DIR, '.reload'), 'w', encoding='utf-8') as f:
            f.write(str(_sh_time.time()))
    except OSError as e:
        log.warning('web reload signal error: %s', e)
    # Rechargement direct et fiable via le host (indépendant du watcher interne du
    # conteneur, qui peut être cassé selon l'échappement de la compose).
    try:
        ssh_exec("sudo -n docker exec desktroo-websites sh -c "
                 + shq("nginx -t && nginx -s reload"), timeout=30)
    except Exception as e:
        log.warning('web nginx reload (ssh) error: %s', e)


def _web_public(site, sid):
    v = dict(site)
    v['id'] = sid
    v['enabled'] = site.get('enabled', True)
    return v


def _web_create(site):
    data = _web_read()
    clean = _web_validate(site, data)
    sid = _sh_secrets.token_hex(4)
    clean['enabled'] = True
    clean['created_at'] = int(_sh_time.time())
    data[sid] = clean
    _web_write(data)
    _web_regenerate()
    return sid, clean


def _web_reimport():
    """Recrée les entrées de sites à partir des dossiers du dataset Web non encore
    enregistrés. Type 'php' si un fichier .php est présent, sinon 'static'. Port
    auto-assigné. Retourne la liste des noms importés."""
    base = _web_dataset_path()
    data = _web_read()
    existing = set()
    for s in data.values():
        if isinstance(s, dict) and s.get('root'):
            existing.add(os.path.realpath(str(s['root'])))
    try:
        entries = sorted(os.listdir(base))
    except OSError as e:
        raise RuntimeError('Dataset Web introuvable (%s) : %s' % (base, e))
    imported = []
    for name in entries:
        full = os.path.join(base, name)
        if name.startswith('.') or not os.path.isdir(full):
            continue
        real = os.path.realpath(full)
        if real in existing:
            continue
        is_php = os.path.exists(os.path.join(full, 'index.php'))
        if not is_php:
            try:
                is_php = any(f.endswith('.php') for f in os.listdir(full))
            except OSError:
                is_php = False
        site = {'name': name[:48], 'type': ('php' if is_php else 'static'),
                'root': real, 'server_name': ''}
        if is_php:
            site['php_version'] = WEB_PHP_DEFAULT
        try:
            clean = _web_validate(site, data)
            sid = _sh_secrets.token_hex(4)
            clean['enabled'] = True
            clean['created_at'] = int(_sh_time.time())
            clean['imported'] = True
            data[sid] = clean
            imported.append(clean['name'])
        except Exception as e:
            log.warning('web reimport skip %s: %s', name, e)
    if imported:
        _web_write(data)
        _web_regenerate()
    return imported


# ── Profils PHP : réglages php.ini + extensions, par version ────────────────
def _php_profiles_read():
    with _web_lock:
        data = _access_read_json(WEB_PHP_PROFILE_FILE, {})
        return data if isinstance(data, dict) else {}


def _php_profiles_write(data):
    with _web_lock:
        _access_write_json(WEB_PHP_PROFILE_FILE, data)


def _php_profile_get(ver):
    p = _php_profiles_read().get(ver, {})
    return {'ini': p.get('ini', {}) if isinstance(p.get('ini'), dict) else {},
            'extensions': p.get('extensions', []) if isinstance(p.get('extensions'), list) else []}


def _php_write_conf(ver, bump_ext=False):
    prof = _php_profile_get(ver)
    base = os.path.join(WEB_PHP_DIR, ver)
    ini_dir = os.path.join(base, 'ini')
    os.makedirs(ini_dir, exist_ok=True)
    lines = ['; Généré par Desktroo — ne pas éditer']
    for k, v in prof['ini'].items():
        lines.append('%s = %s' % (k, v))
    with open(os.path.join(ini_dir, 'zz-truenas.ini'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    exts = prof['extensions']
    with open(os.path.join(base, 'extensions.txt'), 'w', encoding='utf-8') as f:
        f.write(' '.join(exts) + ('\n' if exts else ''))
    with open(os.path.join(base, '.reload'), 'w', encoding='utf-8') as f:
        f.write(str(_sh_time.time()))
    if bump_ext:
        with open(os.path.join(base, '.extreload'), 'w', encoding='utf-8') as f:
            f.write(str(_sh_time.time()))


def _php_save(version, ini, extensions):
    version = str(version)
    if version not in WEB_PHP_VERSIONS:
        raise ValueError('Version PHP indisponible : %s' % version)
    clean_ini = {}
    for k, v in (ini or {}).items():
        k = str(k).strip()
        if not _PHP_INI_KEY_RE.match(k):
            raise ValueError('Directive ini invalide : %s' % k)
        vs = str(v).replace('\r', '').replace('\n', '').strip()[:256]
        if vs == '':
            continue
        clean_ini[k] = vs
    clean_ext = []
    for e in (extensions or []):
        e = str(e).strip()
        if not e:
            continue
        if not _PHP_EXT_RE.match(e):
            raise ValueError('Nom d\'extension invalide : %s' % e)
        if e not in clean_ext:
            clean_ext.append(e)
    data = _php_profiles_read()
    prev = data.get(version, {})
    changed_ext = set(prev.get('extensions', []) if isinstance(prev.get('extensions'), list) else []) != set(clean_ext)
    data[version] = {'ini': clean_ini, 'extensions': clean_ext}
    _php_profiles_write(data)
    _php_write_conf(version, bump_ext=changed_ext)
    # Applique directement via le host (le watcher interne du conteneur peut être
    # cassé selon l'échappement de la compose). install-php-extensions compile :
    # on lance en tâche de fond puis on recharge php-fpm (kill -USR2 1).
    cont = 'desktroo-php' + version.replace('.', '')

    def _php_apply_bg():
        try:
            if clean_ext:
                inner = 'install-php-extensions ' + ' '.join(clean_ext) + ' >/dev/null 2>&1; kill -USR2 1 2>/dev/null || true'
            else:
                inner = 'kill -USR2 1 2>/dev/null || true'
            ssh_exec('sudo -n docker exec ' + cont + ' sh -c ' + shq(inner), timeout=600)
        except Exception as e:
            log.warning('php ext apply (ssh) error: %s', e)

    _threading.Thread(target=_php_apply_bg, daemon=True).start()
    return data[version], changed_ext
# ── MDM-WEBSITES-V1-END ─────────────────────────────────────────────────────


# ── MDM-APPS-V1-BEGIN : bases de données + installeur d'applications ─────────
DB_HOST = os.environ.get('DB_HOST', 'mariadb')
DB_PORT = int(os.environ.get('DB_PORT', '3306'))
DB_ROOT_PASSWORD = os.environ.get('DB_ROOT_PASSWORD', '')
try:
    import pymysql as _pymysql
    _HAS_PYMYSQL = True
except Exception:
    _HAS_PYMYSQL = False
_DB_NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,63}$')
_DB_USER_RE = re.compile(r'^[A-Za-z0-9_]{1,32}$')
DB_CRED_FILE = os.path.join(ACCESS_DATA_DIR, 'databases.json')


def _db_creds_read():
    d = _access_read_json(DB_CRED_FILE, {})
    return d if isinstance(d, dict) else {}


def _db_creds_save(rec):
    d = _db_creds_read()
    d[rec['name']] = {
        'name': rec['name'], 'user': rec['user'], 'password': rec['password'],
        'host': rec['host'], 'port': rec['port'], 'created_at': int(_sh_time.time()),
    }
    _access_write_json(DB_CRED_FILE, d)


def _db_creds_remove(name):
    d = _db_creds_read()
    if name in d:
        del d[name]
        _access_write_json(DB_CRED_FILE, d)


def _db_conn():
    if not _HAS_PYMYSQL:
        raise RuntimeError('pymysql non disponible — relancez le conteneur fileops')
    return _pymysql.connect(host=DB_HOST, port=DB_PORT, user='root',
                            password=DB_ROOT_PASSWORD, connect_timeout=8, autocommit=True)


# ── MDM-DB-REPAIR-V1 : rétablir l'accès root quand le mot de passe enregistré ne correspond plus ──
# La base garde le mot de passe root de sa création (MARIADB_ROOT_PASSWORD n'est lu qu'à
# l'initialisation du dossier de données). Si la configuration en porte un autre — ancienne
# réinstallation par l'assistant —, fileops reçoit « 1045 Access denied » et ne peut plus gérer
# les bases. La réparation arrête MariaDB, le relance un instant sans contrôle d'accès ni réseau
# (conteneur éphémère sur le même volume), y pose le mot de passe de la configuration, puis
# redémarre le conteneur normal. Les bases et leurs comptes ne sont pas touchés.
DB_CONTAINER = os.environ.get('DB_CONTAINER', 'desktroo-mariadb')
_db_repair_lock = _threading.Lock()

# Exécuté dans le conteneur éphémère. Le SQL arrive en base64 dans DK_SQL.
_DB_REPAIR_INNER = r"""
S=/tmp/dk-repair.sock
mariadbd --user=mysql --skip-grant-tables --skip-networking --socket=$S --pid-file=/tmp/dk-repair.pid >/tmp/dk-repair.log 2>&1 &
PID=$!
ok=0; i=0
while [ $i -lt 120 ]; do
  if mariadb --socket=$S -e "SELECT 1" >/dev/null 2>&1; then ok=1; break; fi
  kill -0 $PID 2>/dev/null || break
  i=$((i+1)); sleep 1
done
if [ $ok != 1 ]; then
  tail -n 15 /tmp/dk-repair.log
  kill -TERM $PID 2>/dev/null; wait $PID 2>/dev/null
  echo DK_REPAIR_FAIL; exit 1
fi
printf %s "$DK_SQL" | base64 -d | mariadb --socket=$S; rc=$?
kill -TERM $PID 2>/dev/null; wait $PID 2>/dev/null
if [ $rc = 0 ]; then echo DK_REPAIR_OK; else echo DK_REPAIR_FAIL; fi
exit $rc
"""


def _db_is_access_denied(e):
    a = getattr(e, 'args', None) or ()
    return bool(a) and a[0] == 1045


def _db_repair_sql(password):
    p = password.replace('\\', '\\\\').replace("'", "\\'")
    return ("FLUSH PRIVILEGES;\n"
            "CREATE USER IF NOT EXISTS 'root'@'%%' IDENTIFIED BY '%(p)s';\n"
            "ALTER USER 'root'@'%%' IDENTIFIED BY '%(p)s';\n"
            "ALTER USER IF EXISTS 'root'@'localhost' IDENTIFIED BY '%(p)s';\n"
            "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%%' WITH GRANT OPTION;\n"
            "FLUSH PRIVILEGES;\n") % {'p': p}


def _db_repair_root():
    import base64 as _b64
    if not DB_ROOT_PASSWORD or '\n' in DB_ROOT_PASSWORD or '\x00' in DB_ROOT_PASSWORD:
        raise ValueError('Mot de passe MariaDB absent de la configuration (DB_ROOT_PASSWORD).')
    if not (SSH_HOST and SSH_USER and SSH_PASS):
        raise ValueError('Accès SSH au NAS non configuré : réparation impossible depuis le bureau.')
    try:
        _db_conn().close()
        return {'ok': True, 'already': True}
    except Exception as e:
        if not _db_is_access_denied(e):
            raise RuntimeError('MariaDB ne répond pas (%s) : la réparation ne concerne que le mot de passe.' % e)
    if not _db_repair_lock.acquire(False):
        raise RuntimeError('Une réparation est déjà en cours.')
    try:
        sql = _b64.b64encode(_db_repair_sql(DB_ROOT_PASSWORD).encode('utf-8')).decode('ascii')
        c = shq(DB_CONTAINER)
        script = (
            "IMG=$(docker inspect -f '{{.Config.Image}}' " + c + " 2>/dev/null)\n"
            '[ -n "$IMG" ] || { echo DK_NO_CONTAINER; exit 0; }\n'
            'docker stop -t 90 ' + c + ' >/dev/null 2>&1\n'
            'docker run --rm --network none --volumes-from ' + c + ' -e DK_SQL=' + sql
            + ' --entrypoint sh "$IMG" -c ' + shq(_DB_REPAIR_INNER) + ' 2>&1\n'
            'docker start ' + c + ' >/dev/null 2>&1 && echo DK_STARTED\n'
        )
        out, err, _code = ssh_exec('sudo -n sh -c ' + shq(script), timeout=300)
        out = out or ''
        if 'DK_NO_CONTAINER' in out:
            raise RuntimeError('Conteneur MariaDB introuvable (%s).' % DB_CONTAINER)
        if 'DK_REPAIR_OK' not in out:
            tail = ' | '.join([l for l in (out + '\n' + (err or '')).splitlines() if l.strip() and not l.startswith('DK_')][-4:])
            raise RuntimeError('Réparation non aboutie' + (' : ' + tail if tail else '') + '.')
        if 'DK_STARTED' not in out:
            raise RuntimeError('Mot de passe rétabli, mais MariaDB n\'a pas redémarré (docker start %s).' % DB_CONTAINER)
        last = None
        for _i in range(40):
            try:
                _db_conn().close()
                log.info('MariaDB : accès root rétabli.')
                return {'ok': True, 'repaired': True}
            except Exception as e:
                last = e
                _sh_time.sleep(1.5)
        raise RuntimeError('MariaDB redémarre encore (%s) — réessayez dans un instant.' % last)
    finally:
        _db_repair_lock.release()
# ── MDM-DB-REPAIR-V1-END ────────────────────────────────────────────────────


def _db_list():
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute('SHOW DATABASES')
        sysdbs = {'mysql', 'information_schema', 'performance_schema', 'sys'}
        return [r[0] for r in cur.fetchall() if r[0] not in sysdbs]
    finally:
        conn.close()


def _db_create(name=None, user=None, password=None):
    name = str(name or ('db_' + _sh_secrets.token_hex(4)))
    user = str(user or name)[:32]
    password = password or _sh_secrets.token_urlsafe(12)
    if not _DB_NAME_RE.match(name):
        raise ValueError('Nom de base invalide (lettres, chiffres, _ ).')
    if not _DB_USER_RE.match(user):
        raise ValueError('Nom d\'utilisateur invalide.')
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute('CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci' % name)
        cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s", (user, password))
        cur.execute("ALTER USER %s@'%%' IDENTIFIED BY %s", (user, password))
        cur.execute('GRANT ALL PRIVILEGES ON `' + name + "`.* TO %s@'%%'", (user,))
        cur.execute('FLUSH PRIVILEGES')
        rec = {'name': name, 'user': user, 'password': password, 'host': DB_HOST, 'port': DB_PORT}
        try:
            _db_creds_save(rec)
        except Exception as e:
            log.warning('db creds save error: %s', e)
        return rec
    finally:
        conn.close()


def _db_delete(name, user=None):
    if not _DB_NAME_RE.match(str(name)):
        raise ValueError('Nom de base invalide.')
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute('DROP DATABASE IF EXISTS `%s`' % name)
        if user and _DB_USER_RE.match(str(user)):
            cur.execute("DROP USER IF EXISTS %s@'%%'", (user,))
            cur.execute('FLUSH PRIVILEGES')
        try:
            _db_creds_remove(str(name))
        except Exception as e:
            log.warning('db creds remove error: %s', e)
        return True
    finally:
        conn.close()


# Catalogue d'applications (URL par défaut surchargeable côté client).
APP_CATALOG = {
    'wordpress': {'label': 'WordPress', 'url': 'https://wordpress.org/latest.zip',
                  'strip': True, 'db': True, 'php': '8.2', 'post': 'wordpress',
                  'ext': ['mysqli', 'gd', 'exif', 'zip', 'intl', 'mbstring']},
    'phpmyadmin': {'label': 'phpMyAdmin', 'url': 'https://www.phpmyadmin.net/downloads/phpMyAdmin-latest-all-languages.zip',
                   'strip': True, 'db': False, 'php': '8.2', 'post': 'phpmyadmin',
                   'ext': ['mysqli', 'mbstring', 'zip', 'gd']},
    'nextcloud': {'label': 'Nextcloud', 'url': 'https://download.nextcloud.com/server/releases/latest.zip',
                  'strip': True, 'db': True, 'php': '8.2', 'post': 'nextcloud',
                  'ext': ['pdo_mysql', 'gd', 'zip', 'intl', 'mbstring', 'bcmath', 'gmp', 'exif']},
    'grav': {'label': 'Grav', 'url': 'https://getgrav.org/download/core/grav-admin/latest',
             'strip': True, 'db': False, 'php': '8.2', 'post': None,
             'ext': ['gd', 'zip', 'mbstring', 'curl']},
    'joomla': {'label': 'Joomla', 'url': 'https://downloads.joomla.org/cms/joomla5/5-2-4/Joomla_5-2-4-Stable-Full_Package.zip?format=zip',
               'strip': False, 'db': True, 'php': '8.2', 'post': 'joomla',
               'ext': ['mysqli', 'gd', 'zip', 'intl', 'mbstring', 'curl']},
    'prestashop': {'label': 'PrestaShop', 'url': 'https://github.com/PrestaShop/PrestaShop/releases/download/8.2.0/prestashop_8.2.0.zip',
                   'strip': False, 'db': True, 'php': '8.1', 'post': 'prestashop',
                   'ext': ['pdo_mysql', 'mysqli', 'gd', 'zip', 'intl', 'mbstring', 'curl']},
}


def _app_move_into(exdir, dest, strip):
    entries = os.listdir(exdir)
    src = exdir
    if strip:
        dirs = [e for e in entries if os.path.isdir(os.path.join(exdir, e))]
        files = [e for e in entries if os.path.isfile(os.path.join(exdir, e))]
        if len(dirs) == 1 and not files:
            src = os.path.join(exdir, dirs[0])
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dest, item)
        if os.path.exists(d):
            if os.path.isdir(d) and not os.path.islink(d):
                _sh_shutil.rmtree(d)
            else:
                os.remove(d)
        _sh_shutil.move(s, d)


def _app_fetch_into(url, dest, strip):
    import tempfile, urllib.request, zipfile, tarfile
    os.makedirs(dest, exist_ok=True)
    tmpd = tempfile.mkdtemp(prefix='appdl-')
    try:
        arch = os.path.join(tmpd, 'archive')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 Desktroo'})
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(arch, 'wb') as f:
                _sh_shutil.copyfileobj(resp, f)
        exdir = os.path.join(tmpd, 'ex')
        os.makedirs(exdir)
        if zipfile.is_zipfile(arch):
            with zipfile.ZipFile(arch) as z:
                z.extractall(exdir)
        elif tarfile.is_tarfile(arch):
            with tarfile.open(arch) as t:
                t.extractall(exdir)
        else:
            raise RuntimeError('Archive téléchargée non reconnue (ni zip ni tar).')
        _app_move_into(exdir, dest, strip)
    finally:
        try:
            _sh_shutil.rmtree(tmpd)
        except OSError:
            pass


def _app_fix_perms(root, gid):
    def _set(p, mode):
        try:
            os.chown(p, 0, gid)
        except OSError:
            pass
        try:
            os.chmod(p, mode)
        except OSError:
            pass
    _set(root, 0o2775)
    for r, dirs, files in os.walk(root):
        for d in dirs:
            _set(os.path.join(r, d), 0o2775)
        for fn in files:
            _set(os.path.join(r, fn), 0o0664)


def _wp_salt(n=64):
    import string
    chars = string.ascii_letters + string.digits + '!@#$%^&*()-_ []{}<>~`+=,.;:/?|'
    return ''.join(_sh_secrets.choice(chars) for _ in range(n))


def _app_wp_config(root, db):
    sample = os.path.join(root, 'wp-config-sample.php')
    target = os.path.join(root, 'wp-config.php')
    if not os.path.exists(sample) or os.path.exists(target):
        return
    with open(sample, 'r', encoding='utf-8', errors='replace') as f:
        cfg = f.read()
    cfg = cfg.replace('database_name_here', db['name'])
    cfg = cfg.replace('username_here', db['user'])
    cfg = cfg.replace('password_here', db['password'])
    cfg = cfg.replace("'localhost'", "'%s'" % db['host'])
    while "'put your unique phrase here'" in cfg:
        cfg = cfg.replace("'put your unique phrase here'", "'" + _wp_salt() + "'", 1)
    # Derrière un reverse-proxy HTTPS (NPMplus) : faire confiance à X-Forwarded-Proto.
    # Et URL dynamiques (WP_HOME/WP_SITEURL d'après l'hôte demandé) : le site répond
    # aussi bien en IP:port qu'en domaine, sans redirection canonique (évite le timeout
    # quand on accède par IP alors que le domaine est enregistré en base).
    proxy_fix = (
        "\n/* Accès multi-hôte + reverse-proxy HTTPS (Desktroo) */\n"
        "if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {\n"
        "    $_SERVER['HTTPS'] = 'on';\n"
        "}\n"
        "if (!empty($_SERVER['HTTP_HOST'])) {\n"
        "    $_tnd_proto = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';\n"
        "    define('WP_HOME', $_tnd_proto . '://' . $_SERVER['HTTP_HOST']);\n"
        "    define('WP_SITEURL', $_tnd_proto . '://' . $_SERVER['HTTP_HOST']);\n"
        "}\n"
    )
    marker = '<?php'
    idx = cfg.find(marker)
    if idx >= 0:
        cut = idx + len(marker)
        cfg = cfg[:cut] + '\n' + proxy_fix + cfg[cut:]
    else:
        cfg = '<?php' + proxy_fix + cfg
    with open(target, 'w', encoding='utf-8') as f:
        f.write(cfg)


def _app_pma_config(root, db):
    target = os.path.join(root, 'config.inc.php')
    if os.path.exists(target):
        return
    secret = _sh_secrets.token_hex(16)
    cfg = (
        "<?php\n"
        "$cfg['blowfish_secret'] = '" + secret + "';\n"
        "$i = 0;\n"
        "$i++;\n"
        "$cfg['Servers'][$i]['host'] = '" + DB_HOST + "';\n"
        "$cfg['Servers'][$i]['port'] = '" + str(DB_PORT) + "';\n"
        "$cfg['Servers'][$i]['auth_type'] = 'cookie';\n"
        "$cfg['Servers'][$i]['AllowNoPassword'] = false;\n"
    )
    with open(target, 'w', encoding='utf-8') as f:
        f.write(cfg)


def _app_prestashop_extract(root):
    """Le paquet PrestaShop contient un zip interne (prestashop.zip) + un
    index.php d'auto-décompression. On extrait le zip interne et on nettoie."""
    import zipfile
    inner = os.path.join(root, 'prestashop.zip')
    if os.path.exists(inner):
        try:
            with zipfile.ZipFile(inner) as z:
                z.extractall(root)
            os.remove(inner)
        except Exception as e:
            log.warning('prestashop inner extract error: %s', e)
    for junk in ('Install_PrestaShop.html',):
        p = os.path.join(root, junk)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _app_ensure_extensions(ver, exts):
    if ver not in WEB_PHP_VERSIONS or not exts:
        return
    prof = _php_profile_get(ver)
    merged = list(prof['extensions'])
    changed = False
    for e in exts:
        if e not in merged:
            merged.append(e)
            changed = True
    if changed:
        _php_save(ver, prof['ini'], merged)


# Profil de réponses (pré-remplit les installs, évite de tout ressaisir)
INSTALL_PROFILE_FILE = os.path.join(ACCESS_DATA_DIR, 'install_profile.json')
WEB_BIN_DIR = os.environ.get('WEB_BIN_DIR', os.path.join(os.path.dirname(WEB_PHP_DIR.rstrip('/')), 'bin'))


def _install_profile_read():
    d = _access_read_json(INSTALL_PROFILE_FILE, {})
    return d if isinstance(d, dict) else {}


def _install_profile_save(p):
    keep = {k: str(p.get(k, '') or '') for k in ('admin_user', 'admin_email', 'language', 'title')}
    _access_write_json(INSTALL_PROFILE_FILE, keep)
    return keep


def _php_container(ver):
    return 'desktroo-php' + str(ver).replace('.', '')


def _docker_exec(container, inner_cmd, timeout=240, user=None):
    """Exécute une commande shell dans un conteneur via SSH (docker exec, repli sudo)."""
    u = ('-u ' + shq(user) + ' ') if user else ''
    full = 'docker exec ' + u + shq(container) + ' sh -c ' + shq(inner_cmd)
    out, err, code = ssh_exec(full, timeout=timeout)
    if code != 0:
        out2, err2, code2 = ssh_exec('sudo -n ' + full, timeout=timeout)
        if code2 == 0:
            return out2, err2, 0
        return out, (err or err2), code
    return out, err, code


def _wp_cli_ensure():
    os.makedirs(WEB_BIN_DIR, exist_ok=True)
    phar = os.path.join(WEB_BIN_DIR, 'wp-cli.phar')
    if not os.path.exists(phar) or os.path.getsize(phar) < 1000000:
        import urllib.request
        url = 'https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar'
        req = urllib.request.Request(url, headers={'User-Agent': 'Desktroo'})
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(phar, 'wb') as f:
                _sh_shutil.copyfileobj(resp, f)
        try:
            os.chmod(phar, 0o644)
        except OSError:
            pass
    return phar


def _app_wp_install(root, ver, url, title, admin_user, admin_password, admin_email, language):
    """Installe WordPress sans assistant via WP-CLI (dans le conteneur php de la version)."""
    phar = _wp_cli_ensure()
    container = _php_container(ver)
    inner = (
        'install-php-extensions mysqli >/dev/null 2>&1 || true; '
        'php ' + shq(phar) + ' --allow-root --path=' + shq(root) + ' core install'
        ' --url=' + shq(url) +
        ' --title=' + shq(title) +
        ' --admin_user=' + shq(admin_user) +
        ' --admin_password=' + shq(admin_password) +
        ' --admin_email=' + shq(admin_email) +
        ' --skip-email'
    )
    out, err, code = _docker_exec(container, inner, timeout=300)
    combined = (out or '') + '\n' + (err or '')
    ok = (code == 0 and 'success' in combined.lower()) or 'already installed' in combined.lower()
    if ok and language:
        _docker_exec(container,
                     'php ' + shq(phar) + ' --allow-root --path=' + shq(root) +
                     ' language core install ' + shq(language) + ' --activate >/dev/null 2>&1 || true',
                     timeout=120)
    return ok, combined.strip()[-1500:]


def _app_nextcloud_install(root, ver, host, domain, db, admin_user, admin_password):
    """Installe Nextcloud sans assistant via occ (exécuté en www-data)."""
    container = _php_container(ver)
    occ = 'php -d memory_limit=512M ' + shq(root.rstrip('/') + '/occ')
    pre = ('install-php-extensions pdo_mysql gd zip intl mbstring bcmath gmp exif '
           '>/dev/null 2>&1 || true; ')
    install = (
        occ + ' maintenance:install --no-interaction'
        ' --database mysql'
        ' --database-host ' + shq(db['host']) +
        ' --database-name ' + shq(db['name']) +
        ' --database-user ' + shq(db['user']) +
        ' --database-pass ' + shq(db['password']) +
        ' --admin-user ' + shq(admin_user) +
        ' --admin-pass ' + shq(admin_password)
    )
    out, err, code = _docker_exec(container, pre + install, timeout=420, user='www-data')
    combined = (out or '') + '\n' + (err or '')
    ok = code == 0 and ('successfully installed' in combined.lower() or 'already been installed' in combined.lower())
    if ok:
        url = ('https://' + domain) if domain else ('http://' + host)
        cmds = []
        idx = 1
        for hn in ([domain, host] if domain else [host]):
            if hn:
                cmds.append(occ + ' config:system:set trusted_domains ' + str(idx) + ' --value=' + shq(hn))
                idx += 1
        cmds.append(occ + ' config:system:set overwrite.cli.url --value=' + shq(url))
        if domain:
            cmds.append(occ + ' config:system:set overwriteprotocol --value=https')
        _docker_exec(container, ' ; '.join(cmds) + ' ; true', timeout=120, user='www-data')
    return ok, combined.strip()[-1500:]


def _app_joomla_install(root, ver, db, admin_user, admin_password, admin_email, site_name):
    """Installe Joomla sans assistant via installation/joomla.php (en www-data)."""
    container = _php_container(ver)
    jphp = 'php -d memory_limit=512M ' + shq(root.rstrip('/') + '/installation/joomla.php')
    prefix = 'j' + _sh_secrets.token_hex(2) + '_'
    pre = 'install-php-extensions mysqli gd zip intl mbstring curl >/dev/null 2>&1 || true; '
    install = (
        jphp + ' install'
        ' --site-name=' + shq(site_name or 'Joomla') +
        ' --admin-user=' + shq(admin_user) +
        ' --admin-username=' + shq(admin_user) +
        ' --admin-password=' + shq(admin_password) +
        ' --admin-email=' + shq(admin_email) +
        ' --db-type=mysqli'
        ' --db-host=' + shq(db['host']) +
        ' --db-user=' + shq(db['user']) +
        ' --db-pass=' + shq(db['password']) +
        ' --db-name=' + shq(db['name']) +
        ' --db-prefix=' + shq(prefix) +
        ' --db-encryption=0'
    )
    out, err, code = _docker_exec(container, pre + install, timeout=420, user='www-data')
    combined = (out or '') + '\n' + (err or '')
    ok = (code == 0) and ('error' not in combined.lower() or 'successfully' in combined.lower())
    if ok:
        # Le dossier installation doit être retiré après installation
        _docker_exec(container, 'rm -rf ' + shq(root.rstrip('/') + '/installation'), timeout=60, user='www-data')
    return ok, combined.strip()[-1500:]


def _app_prestashop_install(root, ver, domain_host, db, admin_email, admin_password, name, language):
    """Installe PrestaShop sans assistant via install/index_cli.php (en www-data)."""
    container = _php_container(ver)
    cli = 'php -d memory_limit=512M ' + shq(root.rstrip('/') + '/install/index_cli.php')
    pre = 'install-php-extensions pdo_mysql mysqli gd zip intl mbstring curl >/dev/null 2>&1 || true; '
    install = (
        cli +
        ' --domain=' + shq(domain_host) +
        ' --db_server=' + shq(db['host']) +
        ' --db_name=' + shq(db['name']) +
        ' --db_user=' + shq(db['user']) +
        ' --db_password=' + shq(db['password']) +
        ' --prefix=ps_'
        ' --db_create=0'
        ' --name=' + shq(name or 'PrestaShop') +
        ' --email=' + shq(admin_email) +
        ' --password=' + shq(admin_password) +
        ' --firstname=Admin --lastname=Admin'
        ' --language=' + shq((language or 'fr').split('_')[0]) +
        ' --country=fr --newsletter=0 --send_email=0'
    )
    out, err, code = _docker_exec(container, pre + install, timeout=480, user='www-data')
    combined = (out or '') + '\n' + (err or '')
    ok = (code == 0)
    if ok:
        # Le dossier install doit être retiré après installation
        _docker_exec(container, 'rm -rf ' + shq(root.rstrip('/') + '/install'), timeout=60, user='www-data')
    return ok, combined.strip()[-1500:]


def _app_install(app_key, root, php_version=None, port=None, server_name=None,
                 name=None, url_override=None, create_db=True,
                 auto=False, site_host=None, title=None,
                 admin_user=None, admin_password=None, admin_email=None, language=None,
                 db_name=None, db_user=None, db_password=None):
    meta = APP_CATALOG.get(str(app_key))
    if not meta:
        raise ValueError('Application inconnue : %s' % app_key)
    root = os.path.realpath(str(root or ''))
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not (root == base or root.startswith(base + '/')):
        raise PermissionError('Le dossier doit être sous %s/.' % WEB_ROOT_ALLOWED)
    os.makedirs(root, exist_ok=True)
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    url = str(url_override or '').strip() or meta['url']
    _app_fetch_into(url, root, meta.get('strip'))
    if meta.get('post') == 'prestashop':
        _app_prestashop_extract(root)
    db = None
    if meta.get('db') and create_db:
        db = _db_create(db_name or None, db_user or None, db_password or None)
    if meta.get('post') == 'wordpress' and db:
        _app_wp_config(root, db)
    if meta.get('post') == 'phpmyadmin':
        _app_pma_config(root, db)
    _app_fix_perms(root, gid)
    ver = str(php_version or meta.get('php') or WEB_PHP_DEFAULT)
    site = {'name': name or meta['label'], 'type': 'php', 'root': root,
            'php_version': ver if ver in WEB_PHP_VERSIONS else WEB_PHP_DEFAULT,
            'app': str(app_key)}
    if port:
        site['port'] = port
    if server_name:
        site['server_name'] = server_name
    sid, clean = _web_create(site)
    if db:
        d = _web_read()
        if sid in d:
            d[sid]['db'] = {'name': db['name'], 'user': db['user']}
            _web_write(d)
        clean['db'] = {'name': db['name'], 'user': db['user']}
    _app_ensure_extensions(clean.get('php_version'), meta.get('ext') or [])
    result = {'ok': True, 'app': app_key, 'site': _web_public(clean, sid), 'db': db,
              'ext_installing': bool(meta.get('ext'))}
    # Installation sans assistant (Softaculous-like). WordPress d'abord (WP-CLI).
    if auto and db and meta.get('post') == 'wordpress':
        try:
            apw = admin_password or _sh_secrets.token_urlsafe(10)
            auser = admin_user or 'admin'
            if clean.get('server_name'):
                aurl = 'https://' + clean['server_name']
            else:
                aurl = 'http://' + (site_host or 'localhost') + ':' + str(int(clean['port']))
            ok, msg = _app_wp_install(
                root, clean.get('php_version'), aurl,
                title or name or meta['label'], auser, apw,
                admin_email or 'admin@example.com', language or 'fr_FR')
            result['auto_installed'] = ok
            result['auto_message'] = msg
            if ok:
                result['login_url'] = aurl + '/wp-admin/'
                result['admin'] = {'user': auser, 'password': apw}
        except Exception as e:
            result['auto_installed'] = False
            result['auto_message'] = str(e)
    elif auto and db and meta.get('post') == 'nextcloud':
        try:
            apw = admin_password or _sh_secrets.token_urlsafe(10)
            auser = admin_user or 'admin'
            domain = clean.get('server_name') or ''
            host = (site_host or 'localhost') + ':' + str(int(clean['port']))
            ok, msg = _app_nextcloud_install(root, clean.get('php_version'), host, domain, db, auser, apw)
            result['auto_installed'] = ok
            result['auto_message'] = msg
            if ok:
                result['login_url'] = ('https://' + domain) if domain else ('http://' + host)
                result['admin'] = {'user': auser, 'password': apw}
        except Exception as e:
            result['auto_installed'] = False
            result['auto_message'] = str(e)
    elif auto and db and meta.get('post') == 'joomla':
        try:
            apw = admin_password or _sh_secrets.token_urlsafe(10)
            auser = admin_user or 'admin'
            ok, msg = _app_joomla_install(
                root, clean.get('php_version'), db, auser, apw,
                admin_email or 'admin@example.com', title or name or 'Joomla')
            result['auto_installed'] = ok
            result['auto_message'] = msg
            if ok:
                domain = clean.get('server_name') or ''
                result['login_url'] = (('https://' + domain) if domain
                                       else ('http://' + (site_host or 'localhost') + ':' + str(int(clean['port'])))) + '/administrator/'
                result['admin'] = {'user': auser, 'password': apw}
        except Exception as e:
            result['auto_installed'] = False
            result['auto_message'] = str(e)
    elif auto and db and meta.get('post') == 'prestashop':
        try:
            apw = admin_password or _sh_secrets.token_urlsafe(10)
            domain = clean.get('server_name') or ''
            domain_host = domain or ((site_host or 'localhost') + ':' + str(int(clean['port'])))
            ok, msg = _app_prestashop_install(
                root, clean.get('php_version'), domain_host, db,
                admin_email or 'admin@example.com', apw,
                title or name or 'PrestaShop', language or 'fr')
            result['auto_installed'] = ok
            result['auto_message'] = msg
            if ok:
                result['login_url'] = (('https://' + domain) if domain
                                       else ('http://' + domain_host)) + '/admin/'
                result['admin'] = {'user': admin_email or 'admin@example.com', 'password': apw}
        except Exception as e:
            result['auto_installed'] = False
            result['auto_message'] = str(e)
    elif auto and meta.get('db'):
        result['auto_supported'] = False
    # Persiste les identifiants pour les afficher plus tard dans la liste des sites.
    if result.get('admin') or result.get('login_url'):
        try:
            d = _web_read()
            if sid in d:
                if result.get('admin'):
                    d[sid]['admin'] = {'user': result['admin'].get('user'),
                                       'password': result['admin'].get('password')}
                if result.get('login_url'):
                    d[sid]['login_url'] = result['login_url']
                _web_write(d)
        except Exception as e:
            log.warning('persist admin creds: %s', e)
    return result
# ── MDM-APPS-V1-END ─────────────────────────────────────────────────────────


# ── MDM-WEB-MANAGE-V1 : logs, mise à jour, clone, SSL PrestaShop ─────────────
_WEB_LOG_LINES_MAX = 2000


def _web_get(sid):
    data = _web_read()
    site = data.get(sid)
    if not isinstance(site, dict):
        raise ValueError('Site introuvable.')
    return data, site


def _web_detect_app(site):
    """Clé d'appli du site ('wordpress', 'nextcloud', 'prestashop', 'joomla',
    'phpmyadmin') ou None. Utilise le champ 'app' puis une détection fichiers."""
    a = str(site.get('app') or '').strip().lower()
    if a in APP_CATALOG:
        return a
    root = site.get('root') or ''
    if not root or not os.path.isdir(root):
        return None
    def has(*parts):
        return os.path.exists(os.path.join(root, *parts))
    if has('wp-config.php') or has('wp-login.php') or has('wp-load.php'):
        return 'wordpress'
    if has('occ') and has('config', 'config.php'):
        return 'nextcloud'
    if has('configuration.php') and os.path.isdir(os.path.join(root, 'administrator')):
        return 'joomla'
    if (has('classes', 'PrestaShopAutoload.php') or has('config', 'settings.inc.php')
            or has('app', 'config', 'parameters.php')):
        return 'prestashop'
    if has('config.inc.php') and os.path.isdir(os.path.join(root, 'libraries')):
        return 'phpmyadmin'
    return None


def _web_tail_log(sid, kind='error', lines=200):
    kind = 'access' if kind == 'access' else 'error'
    lines = max(1, min(int(lines or 200), _WEB_LOG_LINES_MAX))
    path = os.path.join(WEB_LOG_DIR, 'site-%s.%s.log' % (sid, kind))
    if not os.path.isfile(path):
        return {'ok': True, 'kind': kind, 'lines': [], 'exists': False, 'path': path}
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 256 * 1024)
            f.seek(size - block)
            data = f.read().decode('utf-8', 'replace')
        rows = data.splitlines()[-lines:]
    except OSError as e:
        raise RuntimeError('Lecture du log impossible : %s' % e)
    return {'ok': True, 'kind': kind, 'lines': rows, 'exists': True, 'path': path, 'size': size}


def _app_update(sid):
    data, site = _web_get(sid)
    app = _web_detect_app(site)
    if not app:
        raise ValueError("Type d'application non reconnu — mise à jour automatique indisponible.")
    root = (site.get('root') or '').rstrip('/')
    ver = site.get('php_version') or WEB_PHP_DEFAULT
    container = _php_container(ver)
    if app == 'wordpress':
        phar = _wp_cli_ensure()
        wp = 'php ' + shq(phar) + ' --allow-root --path=' + shq(root) + ' '
        cmd = ('install-php-extensions mysqli >/dev/null 2>&1 || true; '
               + wp + 'core update 2>&1; '
               + wp + 'core update-db 2>&1; '
               + wp + 'plugin update --all 2>&1; '
               + wp + 'theme update --all 2>&1; '
               + 'echo "--- version ---"; ' + wp + 'core version 2>&1')
        out, err, code = _docker_exec(container, cmd, timeout=600)
        combined = ((out or '') + '\n' + (err or '')).strip()
        return {'ok': code == 0, 'app': app, 'log': combined[-4000:]}
    if app == 'nextcloud':
        occ = 'php ' + shq(root + '/occ') + ' '
        updater = root + '/updater/updater.phar'
        cmd = ('if [ -f ' + shq(updater) + ' ]; then php ' + shq(updater) + ' --no-interaction 2>&1 || true; fi; '
               + occ + 'upgrade 2>&1 || true; '
               + occ + 'maintenance:mode --off 2>&1 || true; '
               + occ + 'status 2>&1')
        out, err, code = _docker_exec(container, cmd, timeout=900, user='www-data')
        combined = ((out or '') + '\n' + (err or '')).strip()
        return {'ok': True, 'app': app, 'log': combined[-4000:]}
    if app == 'prestashop':
        con = 'php ' + shq(root + '/bin/console') + ' '
        cmd = ('if [ -f ' + shq(root + '/bin/console') + ' ]; then ' + con + 'cache:clear --no-warmup 2>&1 || true; fi; echo done')
        out, err, code = _docker_exec(container, cmd, timeout=300, user='www-data')
        combined = ((out or '') + '\n' + (err or '')).strip()
        return {'ok': True, 'app': app, 'partial': True, 'log': combined[-2000:],
                'message': ("La mise à jour du cœur PrestaShop se fait via le module « Mise à niveau "
                            "en 1 clic » (autoupgrade) du back-office. Le cache a été vidé.")}
    return {'ok': False, 'app': app, 'partial': True,
            'message': "Mise à jour automatique non prise en charge pour cette application ; "
                       "utilisez son mécanisme de mise à jour intégré."}


def _app_rewrite_db_config(app, root, db):
    """Réécrit les identifiants de base dans la config de l'appli clonée."""
    import re as _re
    root = root.rstrip('/')
    def rw(path, subs):
        if not os.path.isfile(path):
            return False
        try:
            s = open(path, encoding='utf-8', errors='replace').read()
        except OSError:
            return False
        for pat, rep in subs:
            s = _re.sub(pat, rep, s)
        open(path, 'w', encoding='utf-8').write(s)
        return True
    if app == 'wordpress':
        return rw(os.path.join(root, 'wp-config.php'), [
            (r"(define\(\s*'DB_NAME'\s*,\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"(define\(\s*'DB_USER'\s*,\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"(define\(\s*'DB_PASSWORD'\s*,\s*)'[^']*'", r"\1'%s'" % db['password']),
        ])
    if app == 'nextcloud':
        return rw(os.path.join(root, 'config', 'config.php'), [
            (r"('dbname'\s*=>\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"('dbuser'\s*=>\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"('dbpassword'\s*=>\s*)'[^']*'", r"\1'%s'" % db['password']),
        ])
    if app == 'prestashop':
        done = rw(os.path.join(root, 'app', 'config', 'parameters.php'), [
            (r"('database_name'\s*=>\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"('database_user'\s*=>\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"('database_password'\s*=>\s*)'[^']*'", r"\1'%s'" % db['password']),
        ])
        done = rw(os.path.join(root, 'config', 'settings.inc.php'), [
            (r"(_DB_NAME_'\s*,\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"(_DB_USER_'\s*,\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"(_DB_PASSWD_'\s*,\s*)'[^']*'", r"\1'%s'" % db['password']),
        ]) or done
        return done
    if app == 'joomla':
        return rw(os.path.join(root, 'configuration.php'), [
            (r"(\$db\s*=\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"(\$user\s*=\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"(\$password\s*=\s*)'[^']*'", r"\1'%s'" % db['password']),
        ])
    return False


def _db_clone(src_name, dst_name):
    if not (_DB_NAME_RE.match(str(src_name)) and _DB_NAME_RE.match(str(dst_name))):
        raise ValueError('Nom de base invalide.')
    tmp = '/tmp/clone_%s.sql' % dst_name
    inner = ('export MYSQL_PWD=%s; mysqldump -uroot --no-tablespaces --single-transaction '
             '--routines --triggers %s > %s && mysql -uroot %s < %s; rc=$?; rm -f %s; exit $rc') % (
        shq(DB_ROOT_PASSWORD), shq(src_name), shq(tmp), shq(dst_name), shq(tmp), shq(tmp))
    out, err, code = _docker_exec(DB_CONTAINER, inner, timeout=600)
    if code != 0:
        inner2 = inner.replace('mysqldump ', 'mariadb-dump ').replace('mysql -uroot', 'mariadb -uroot')
        out, err, code = _docker_exec(DB_CONTAINER, inner2, timeout=600)
    if code != 0:
        raise RuntimeError('Copie de la base échouée : ' + ((err or out or '').strip()[-500:]))
    return True


def _app_clone(sid, new_name=None):
    data, site = _web_get(sid)
    src_root = (site.get('root') or '').rstrip('/')
    if not src_root or not os.path.isdir(src_root):
        raise ValueError('Ce site n\'a pas de dossier clonable.')
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not (src_root == base or src_root.startswith(base + '/')):
        raise PermissionError('Dossier hors zone autorisée.')
    parent = os.path.dirname(src_root)
    stem = os.path.basename(src_root)
    dst_root = os.path.join(parent, stem + '-clone')
    i = 2
    while os.path.exists(dst_root):
        dst_root = os.path.join(parent, '%s-clone%d' % (stem, i)); i += 1
    _sh_shutil.copytree(src_root, dst_root, symlinks=True)
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    try:
        _app_fix_perms(dst_root, gid)
    except Exception as e:
        log.warning('clone fix perms: %s', e)
    app = _web_detect_app(site)
    new_db = None
    src_db = site.get('db') or {}
    if src_db.get('name'):
        new_db = _db_create()
        try:
            _db_clone(src_db['name'], new_db['name'])
        except Exception:
            try:
                _db_delete(new_db['name'], new_db['user'])
            except Exception:
                pass
            try:
                _sh_shutil.rmtree(dst_root)
            except Exception:
                pass
            raise
        if app:
            try:
                _app_rewrite_db_config(app, dst_root, new_db)
            except Exception as e:
                log.warning('clone db config rewrite: %s', e)
    nm = (new_name or (str(site.get('name', 'Site')) + ' (copie)'))[:48]
    newsite = {'name': nm, 'type': 'php', 'root': dst_root,
               'php_version': site.get('php_version') or WEB_PHP_DEFAULT}
    if app:
        newsite['app'] = app
    nsid, clean = _web_create(newsite)
    if new_db:
        d = _web_read()
        if nsid in d:
            d[nsid]['db'] = {'name': new_db['name'], 'user': new_db['user']}
            _web_write(d)
        clean['db'] = {'name': new_db['name'], 'user': new_db['user']}
    return {'ok': True, 'site': _web_public(clean, nsid), 'db': new_db,
            'src_root': src_root, 'dst_root': dst_root}


def _ps_table_prefix(root):
    import re as _re
    for rel in (('app', 'config', 'parameters.php'), ('config', 'settings.inc.php')):
        p = os.path.join((root or '').rstrip('/'), *rel)
        if os.path.isfile(p):
            try:
                s = open(p, encoding='utf-8', errors='replace').read()
            except OSError:
                continue
            m = _re.search(r"'database_prefix'\s*=>\s*'([^']*)'", s) or _re.search(r"_DB_PREFIX_'\s*,\s*'([^']*)'", s)
            if m:
                return m.group(1)
    return 'ps_'


def _joomla_prefix(root):
    import re as _re
    p = os.path.join((root or '').rstrip('/'), 'configuration.php')
    if os.path.isfile(p):
        try:
            s = open(p, encoding='utf-8', errors='replace').read()
            m = _re.search(r"\$dbprefix\s*=\s*'([^']*)'", s)
            if m:
                return m.group(1)
        except OSError:
            pass
    return 'jos_'


def _app_admin_reset(sid):
    data, site = _web_get(sid)
    app = _web_detect_app(site)
    if not app:
        raise ValueError("Application non reconnue.")
    root = (site.get('root') or '').rstrip('/')
    ver = site.get('php_version') or WEB_PHP_DEFAULT
    container = _php_container(ver)
    newpw = _sh_secrets.token_urlsafe(10)
    user = None
    if app == 'wordpress':
        phar = _wp_cli_ensure()
        wp = 'php ' + shq(phar) + ' --allow-root --path=' + shq(root) + ' '
        out, _e, _c = _docker_exec(container, wp + 'user list --role=administrator --field=user_login 2>/dev/null', timeout=180)
        lines = [l.strip() for l in (out or '').splitlines() if l.strip()]
        user = lines[0] if lines else 'admin'
        o2, e2, c2 = _docker_exec(container, wp + 'user update ' + shq(user) + ' --user_pass=' + shq(newpw) + ' 2>&1', timeout=180)
        if c2 != 0:
            raise RuntimeError((e2 or o2 or 'échec wp-cli').strip()[-400:])
    elif app == 'nextcloud':
        occ = 'php ' + shq(root + '/occ') + ' '
        out, _e, _c = _docker_exec(container, occ + 'user:list 2>/dev/null', timeout=120, user='www-data')
        for line in (out or '').splitlines():
            s = line.strip()
            if s.startswith('- '):
                user = s[2:].split(':')[0].strip()
                break
        user = user or 'admin'
        o2, e2, c2 = _docker_exec(container, 'OC_PASS=' + shq(newpw) + ' ' + occ + 'user:resetpassword --password-from-env ' + shq(user) + ' 2>&1', timeout=120, user='www-data')
        if c2 != 0:
            raise RuntimeError((e2 or o2 or 'échec occ').strip()[-400:])
    elif app in ('prestashop', 'joomla'):
        db = site.get('db') or {}
        if not db.get('name'):
            raise ValueError('Base de données du site inconnue.')
        hout, herr, hcode = _docker_exec(container, 'NP=' + shq(newpw) + ' php -r ' + shq("echo password_hash(getenv('NP'), PASSWORD_BCRYPT);"), timeout=60)
        phash = ((hout or '').strip().splitlines() or [''])[-1].strip()
        if not phash.startswith('$2'):
            raise RuntimeError('Impossible de générer le hash du mot de passe (PHP indisponible ?).')
        conn = _db_conn()
        try:
            cur = conn.cursor()
            cur.execute('USE `%s`' % db['name'])
            if app == 'prestashop':
                prefix = _ps_table_prefix(root)
                if not re.match(r'^[A-Za-z0-9_]{0,16}$', prefix or ''):
                    prefix = 'ps_'
                t = prefix + 'employee'
                cur.execute('SELECT email FROM `%s` WHERE id_profile=1 ORDER BY id_employee LIMIT 1' % t)
                r = cur.fetchone()
                if not r:
                    cur.execute('SELECT email FROM `%s` ORDER BY id_employee LIMIT 1' % t)
                    r = cur.fetchone()
                user = r[0] if r else 'admin@example.com'
                cur.execute('UPDATE `%s` SET passwd=%%s WHERE email=%%s' % t, (phash, user))
            else:
                prefix = _joomla_prefix(root)
                if not re.match(r'^[A-Za-z0-9_]{0,16}$', prefix or ''):
                    prefix = 'jos_'
                t = prefix + 'users'
                cur.execute('SELECT username FROM `%s` ORDER BY id LIMIT 1' % t)
                r = cur.fetchone()
                user = r[0] if r else 'admin'
                cur.execute('UPDATE `%s` SET password=%%s WHERE username=%%s' % t, (phash, user))
        finally:
            conn.close()
    else:
        raise ValueError('Réinitialisation non prise en charge pour cette application.')
    d = _web_read()
    if sid in d:
        d[sid]['admin'] = {'user': user, 'password': newpw}
        _web_write(d)
    return {'ok': True, 'app': app, 'admin': {'user': user, 'password': newpw},
            'login_url': site.get('login_url')}


def _web_admin_path(app, root):
    """Chemin (relatif) de l'interface d'administration selon l'appli.
    PrestaShop renomme son dossier admin -> on le détecte sur le disque."""
    root = (root or '').rstrip('/')
    if app == 'wordpress':
        return '/wp-admin/'
    if app == 'joomla':
        return '/administrator/'
    if app == 'phpmyadmin':
        return '/'
    if app == 'prestashop':
        try:
            for name in sorted(os.listdir(root)):
                if name.lower().startswith('admin') and os.path.isdir(os.path.join(root, name)) \
                        and os.path.exists(os.path.join(root, name, 'index.php')):
                    return '/' + name + '/'
        except OSError:
            pass
        return '/admin/'
    return '/'


def _web_credentials(sid):
    data, site = _web_get(sid)
    app = _web_detect_app(site)
    out = {'ok': True, 'id': sid, 'name': site.get('name'),
           'login_url': site.get('login_url'),
           'admin_path': _web_admin_path(app, site.get('root') or '') if app else '/',
           'admin': site.get('admin') or None, 'db': None,
           'app': app, 'can_reset': app in ('wordpress', 'nextcloud', 'prestashop', 'joomla')}
    dbinfo = site.get('db') or {}
    if dbinfo.get('name'):
        creds = _db_creds_read().get(dbinfo['name'])
        if creds:
            out['db'] = {'host': creds.get('host'), 'port': creds.get('port'),
                         'name': creds.get('name'), 'user': creds.get('user'),
                         'password': creds.get('password')}
        else:
            out['db'] = {'name': dbinfo['name'], 'user': dbinfo.get('user')}
    return out


def _app_prestashop_ssl(sid, enable=True):
    data, site = _web_get(sid)
    app = _web_detect_app(site)
    if app != 'prestashop':
        raise ValueError("Ce site n'est pas une boutique PrestaShop.")
    db = site.get('db') or {}
    if not db.get('name'):
        raise ValueError("Base de données du site inconnue — impossible de modifier le SSL.")
    prefix = _ps_table_prefix(site.get('root') or '')
    if not re.match(r'^[A-Za-z0-9_]{0,16}$', prefix or ''):
        prefix = 'ps_'
    table = prefix + 'configuration'
    val = '1' if enable else '0'
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute('USE `%s`' % db['name'])
        updates = {'PS_SSL_ENABLED': val, 'PS_SSL_ENABLED_EVERYWHERE': val}
        sn = site.get('server_name') or ''
        if sn and enable:
            updates['PS_SHOP_DOMAIN_SSL'] = sn
        applied = []
        for k, v in updates.items():
            cur.execute('UPDATE `%s` SET value=%%s, date_upd=NOW() WHERE name=%%s' % table, (v, k))
            if cur.rowcount == 0:
                cur.execute('INSERT INTO `%s` (name, value, date_add, date_upd) VALUES (%%s,%%s,NOW(),NOW())' % table, (k, v))
            applied.append(k)
    finally:
        conn.close()
    _web_regenerate()  # s'assure que la map HTTPS + fastcgi param sont en place
    # Vide le cache PrestaShop : purge les URL mises en cache (rompt une boucle de redirection).
    root = (site.get('root') or '').rstrip('/')
    try:
        ver = site.get('php_version') or WEB_PHP_DEFAULT
        _docker_exec(_php_container(ver),
                     'rm -rf ' + shq(root + '/var/cache') + '/prod ' + shq(root + '/var/cache') + '/dev 2>/dev/null || true',
                     timeout=60)
    except Exception as e:
        log.warning('ps ssl cache clear: %s', e)
    msg = ('SSL activé' if enable else 'SSL désactivé') + ' dans PrestaShop (cache vidé).'
    if enable and sn:
        msg += ' Accédez à la boutique via https://' + sn + ' (via le reverse-proxy).'
    return {'ok': True, 'app': app, 'enabled': enable, 'db': db['name'],
            'prefix': prefix, 'applied': applied, 'message': msg}
# ── MDM-WEB-MANAGE-V1-END ────────────────────────────────────────────────────


# ── MDM-PREMIUM-V1 : comptes premium / débrideurs (résolution de liens) ─────
PREMIUM_FILE = os.path.join(ACCESS_DATA_DIR, 'premium.json')
_prem_lock = _threading.RLock()
_PREM_PROVIDERS = ('alldebrid', 'realdebrid', 'debridlink', 'premiumize', 'megadebrid',
                   'onefichier', 'rapidgator', 'nitroflare', 'ddownload')
# Débrideurs « généralistes » : résolvent des liens de n'importe quel hébergeur
# supporté (utilisés par le filet de sécurité auto ci-dessous).
_PREM_DEBRIDERS = ('alldebrid', 'realdebrid', 'debridlink', 'premiumize', 'megadebrid')
_PREM_HOSTERS = ('1fichier.com', 'rapidgator.net', 'uptobox.com', 'uptostream.com', 'turbobit.net',
                 'nitroflare.com', 'uploaded.net', 'ul.to', 'mediafire.com', 'katfile.com',
                 'fikper.com', 'ddownload.com', 'filefactory.com', 'wupfile.com', 'hexload.com',
                 'rapidgator.asia', 'mega.nz', 'usersdrive.com', 'file.al',
                 'turbobit.cc', 'trbt.cc', 'turb.cc', 'turb.pw', 'uploady.io',
                 'dailyuploads.io')


def _prem_read():
    with _prem_lock:
        d = _access_read_json(PREMIUM_FILE, {})
        return d if isinstance(d, dict) else {}


def _prem_save(d):
    with _prem_lock:
        _access_write_json(PREMIUM_FILE, d)


def _prem_set(provider, fields):
    if provider not in _PREM_PROVIDERS:
        raise ValueError('Fournisseur inconnu.')
    d = _prem_read()
    cur = d.get(provider) or {}
    for k, v in (fields or {}).items():
        if isinstance(v, str):
            v = v.strip()
        if v in ('', None):
            continue
        cur[str(k)] = v
    d[provider] = cur
    _prem_save(d)
    return True


def _prem_remove(provider):
    d = _prem_read()
    if provider in d:
        del d[provider]
        _prem_save(d)
    return True


def _prem_configured(provider, cfg=None):
    c = (cfg or _prem_read()).get(provider) or {}
    return bool(c.get('apikey') or c.get('token') or (c.get('login') and c.get('password')))


def _prem_status():
    d = _prem_read()
    return {p: {'configured': _prem_configured(p, d)} for p in _PREM_PROVIDERS}


def _prem_get(provider):
    """Renvoie les champs enregistrés d'un fournisseur (pour l'édition/révélation).
    Accès protégé par le jeton fileops (NAS personnel, root)."""
    if provider not in _PREM_PROVIDERS:
        raise ValueError('Fournisseur inconnu.')
    c = _prem_read().get(provider) or {}
    return {k: c.get(k, '') for k in ('login', 'password', 'apikey', 'token')}


def _prem_http(method, url, headers=None, data=None, timeout=45):
    import urllib.request, urllib.parse
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header('User-Agent', 'Desktroo')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
    except Exception as e:
        # tenter de lire le corps d'erreur JSON (HTTPError)
        body = getattr(e, 'read', None)
        if body:
            try:
                return json.loads(e.read().decode('utf-8', 'replace'))
            except Exception:
                pass
        raise
    try:
        return json.loads(raw)
    except Exception:
        return {'_raw': raw}


def _host_of(url):
    from urllib.parse import urlparse
    h = (urlparse(url).hostname or '').lower()
    return h[4:] if h.startswith('www.') else h


def _is_known_hoster(url):
    h = _host_of(url)
    return any(h == d or h.endswith('.' + d) for d in _PREM_HOSTERS)


def _resolve_alldebrid(url, cfg):
    import urllib.parse
    key = cfg.get('apikey')
    if not key:
        raise ValueError('AllDebrid non configuré.')
    j = _prem_http('GET', 'https://api.alldebrid.com/v4/link/unlock?agent=Desktroo&apikey='
                   + urllib.parse.quote(key) + '&link=' + urllib.parse.quote(url, safe=''))
    if j.get('status') == 'success':
        dd = j.get('data') or {}
        return {'link': dd.get('link'), 'filename': dd.get('filename'), 'size': dd.get('filesize') or 0}
    raise RuntimeError('AllDebrid: ' + str((j.get('error') or {}).get('message') or j))


def _resolve_realdebrid(url, cfg):
    token = cfg.get('token')
    if not token:
        raise ValueError('Real-Debrid non configuré.')
    j = _prem_http('POST', 'https://api.real-debrid.com/rest/1.0/unrestrict/link',
                   headers={'Authorization': 'Bearer ' + token}, data={'link': url})
    if j.get('download'):
        return {'link': j['download'], 'filename': j.get('filename'), 'size': j.get('filesize') or 0}
    raise RuntimeError('Real-Debrid: ' + str(j.get('error') or j))


def _resolve_debridlink(url, cfg):
    key = cfg.get('apikey')
    if not key:
        raise ValueError('Debrid-Link non configuré.')
    j = _prem_http('POST', 'https://debrid-link.com/api/v2/downloader/add',
                   headers={'Authorization': 'Bearer ' + key}, data={'url': url})
    if j.get('success') and j.get('value'):
        v = j['value']
        return {'link': v.get('downloadUrl'), 'filename': v.get('name'), 'size': v.get('size') or 0}
    raise RuntimeError('Debrid-Link: ' + str(j.get('error') or j))


# 1fichier limite fortement la fréquence des appels à son API et répond « 429 Too Many Requests » quand ils se
# suivent de trop près (plusieurs liens envoyés d'un coup, tests de clé répétés). Les appels passent donc un par un,
# espacés ; sur un 429 on attend puis on réessaie ; s'il persiste, on cesse d'appeler pendant une minute.
_ONEF_GAP = 1.5            # secondes entre deux appels
_ONEF_WAITS = (4, 10)      # attentes avant les nouveaux essais après un 429
_ONEF_REST = 60            # repos après un 429 qui persiste
_onef_lock = _threading.Lock()
_onef_state = {'next': 0.0, 'rest_until': 0.0, 'test': None}
_ONEF_BUSY = "1fichier : trop d'appels rapprochés (HTTP 429). Réessayez dans quelques minutes."


def _onefichier_call(endpoint, payload, key, timeout=45):
    import urllib.request, urllib.error
    with _onef_lock:
        for i in range(len(_ONEF_WAITS) + 1):
            now = _sh_time.time()
            if now < _onef_state['rest_until']:
                raise RuntimeError(_ONEF_BUSY)
            if now < _onef_state['next']:
                _sh_time.sleep(_onef_state['next'] - now)
            req = urllib.request.Request('https://api.1fichier.com/v1/' + endpoint,
                                         data=json.dumps(payload).encode(), method='POST',
                                         headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                                                  'User-Agent': 'Desktroo'})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read().decode('utf-8', 'replace')
                _onef_state['next'] = _sh_time.time() + _ONEF_GAP
                return json.loads(raw)
            except urllib.error.HTTPError as e:
                _onef_state['next'] = _sh_time.time() + _ONEF_GAP
                if e.code == 429:
                    if i < len(_ONEF_WAITS):
                        wait = _ONEF_WAITS[i]
                        try:
                            wait = max(wait, min(30, int(e.headers.get('Retry-After') or 0)))
                        except (TypeError, ValueError):
                            pass
                        _sh_time.sleep(wait)
                        continue
                    _onef_state['rest_until'] = _sh_time.time() + _ONEF_REST
                    raise RuntimeError(_ONEF_BUSY)
                try:   # 1fichier détaille ses refus en JSON ({"status":"KO","message":…})
                    return json.loads(e.read().decode('utf-8', 'replace'))
                except Exception:
                    raise e


def _onefichier_url(url):
    """Lien 1fichier réduit à « https://1fichier.com/?<identifiant> ». Les liens copiés sur les sites de partage portent
    souvent un paramètre d'affiliation (« &af=5010551 ») : le navigateur l'accepte, l'API répond « Resource not found »."""
    from urllib.parse import urlparse
    try:
        p = urlparse(str(url or '').strip())
        host = (p.hostname or '').lower()
        if host in ('1fichier.com', 'www.1fichier.com'):
            fid = (p.query or '').split('&')[0]
            if re.match(r'^[A-Za-z0-9]{5,}$', fid):
                return 'https://1fichier.com/?' + fid
    except Exception:
        pass
    return url


def _resolve_onefichier(url, cfg):
    key = cfg.get('apikey')
    if not key:
        raise ValueError('1fichier non configuré.')
    j = _onefichier_call('download/get_token.cgi', {'url': _onefichier_url(url)}, key)
    if j.get('status') == 'OK' and j.get('url'):
        return {'link': j['url'], 'filename': None, 'size': 0}
    raise RuntimeError('1fichier: ' + str(j.get('message') or j))


def _resolve_rapidgator(url, cfg):
    import urllib.parse
    login, pw = cfg.get('login'), cfg.get('password')
    if not (login and pw):
        raise ValueError('Rapidgator non configuré.')
    token, ts = cfg.get('token'), cfg.get('token_ts') or 0
    if not token or (_sh_time.time() - ts) > 3000:
        j = _prem_http('GET', 'https://rapidgator.net/api/v2/user/login?login='
                       + urllib.parse.quote(login) + '&password=' + urllib.parse.quote(pw))
        token = (j.get('response') or {}).get('token')
        if not token:
            raise RuntimeError('Rapidgator login: ' + str(j.get('details') or j))
        d = _prem_read()
        d.setdefault('rapidgator', {})
        d['rapidgator']['token'] = token
        d['rapidgator']['token_ts'] = int(_sh_time.time())
        _prem_save(d)
    j = _prem_http('GET', 'https://rapidgator.net/api/v2/file/download?token='
                   + urllib.parse.quote(token) + '&url=' + urllib.parse.quote(url, safe=''))
    dl = (j.get('response') or {}).get('download_url')
    if dl:
        return {'link': dl, 'filename': None, 'size': 0}
    raise RuntimeError('Rapidgator: ' + str(j.get('details') or j))


def _resolve_premiumize(url, cfg):
    key = cfg.get('apikey')
    if not key:
        raise ValueError('Premiumize non configuré.')
    j = _prem_http('POST', 'https://www.premiumize.me/api/transfer/directdl',
                   data={'apikey': key, 'src': url})
    if j.get('status') == 'success':
        if j.get('location'):
            return {'link': j['location'], 'filename': j.get('filename'), 'size': j.get('filesize') or 0}
        content = j.get('content') or []
        if content and content[0].get('link'):
            c = content[0]
            return {'link': c.get('link'), 'filename': c.get('path') or j.get('filename'), 'size': c.get('size') or 0}
    raise RuntimeError('Premiumize: ' + str(j.get('message') or j))


def _resolve_megadebrid(url, cfg):
    import urllib.parse
    login, pw = cfg.get('login'), cfg.get('password')
    if not (login and pw):
        raise ValueError('MegaDebrid non configuré.')
    token, ts = cfg.get('token'), cfg.get('token_ts') or 0
    if not token or (_sh_time.time() - ts) > 3000:
        j = _prem_http('GET', 'https://www.mega-debrid.eu/api.php?action=connectUser&login='
                       + urllib.parse.quote(login) + '&password=' + urllib.parse.quote(pw))
        if j.get('response_code') != 'ok' or not j.get('token'):
            raise RuntimeError('MegaDebrid login: ' + str(j.get('response_text') or j))
        token = j['token']
        d = _prem_read(); d.setdefault('megadebrid', {})
        d['megadebrid']['token'] = token; d['megadebrid']['token_ts'] = int(_sh_time.time()); _prem_save(d)
    j = _prem_http('POST', 'https://www.mega-debrid.eu/api.php?action=getLink&token=' + urllib.parse.quote(token),
                   data={'link': url})
    if j.get('response_code') == 'ok' and j.get('debridLink'):
        return {'link': str(j['debridLink']).strip().strip('"'), 'filename': j.get('filename'), 'size': 0}
    raise RuntimeError('MegaDebrid: ' + str(j.get('response_text') or j))


def _resolve_nitroflare(url, cfg):
    import urllib.parse, re as _re
    email, key = cfg.get('login'), cfg.get('password')
    if not (email and key):
        raise ValueError('Nitroflare non configuré.')
    m = _re.search(r'nitroflare\.com/(?:view|watch)/([A-Za-z0-9]+)', url) or _re.search(r'/([A-Za-z0-9]{10,})', url)
    fid = m.group(1) if m else url
    j = _prem_http('GET', 'https://nitroflare.com/api/v2/getDownloadLink?user='
                   + urllib.parse.quote(email) + '&premiumKey=' + urllib.parse.quote(key)
                   + '&file=' + urllib.parse.quote(fid))
    if j.get('type') == 'success':
        res = j.get('result') or {}
        if res.get('url'):
            return {'link': res['url'], 'filename': res.get('name'), 'size': res.get('size') or 0}
    raise RuntimeError('Nitroflare: ' + str(j.get('messages') or j.get('result') or j))


def _resolve_ddownload(url, cfg):
    import urllib.parse, re as _re
    key = cfg.get('apikey')
    if not key:
        raise ValueError('DDownload non configuré.')
    m = _re.search(r'ddownload\.com/(?:d/)?([A-Za-z0-9]+)', url)
    code = m.group(1) if m else url
    j = _prem_http('GET', 'https://ddownload.com/api/file/direct_link?key='
                   + urllib.parse.quote(key) + '&file_code=' + urllib.parse.quote(code))
    if str(j.get('status')) == '200':
        res = j.get('result') or {}
        if res.get('url'):
            return {'link': res['url'], 'filename': None, 'size': res.get('size') or 0}
    raise RuntimeError('DDownload: ' + str(j.get('msg') or j))


_PREM_RESOLVERS = {'alldebrid': _resolve_alldebrid, 'realdebrid': _resolve_realdebrid,
                   'debridlink': _resolve_debridlink, 'onefichier': _resolve_onefichier,
                   'rapidgator': _resolve_rapidgator, 'premiumize': _resolve_premiumize,
                   'megadebrid': _resolve_megadebrid, 'nitroflare': _resolve_nitroflare,
                   'ddownload': _resolve_ddownload}


def _resolve_premium(url, prefer=None):
    """Résout un lien via compte direct de l'hôte puis débrideurs configurés.
    Si `prefer` est un fournisseur précis (choisi par l'utilisateur), on l'utilise
    exclusivement. Retourne {'link','filename','size','via'} ou lève."""
    cfg = _prem_read()
    host = _host_of(url)
    order = []
    if prefer and prefer in _PREM_PROVIDERS:
        if not _prem_configured(prefer, cfg):
            raise RuntimeError('Le fournisseur choisi (' + prefer + ") n'est pas configuré.")
        order = [prefer]
    elif host.endswith('1fichier.com') and _prem_configured('onefichier', cfg):
        order.append('onefichier')
    if host.endswith('rapidgator.net') and _prem_configured('rapidgator', cfg):
        order.append('rapidgator')
    if host.endswith('nitroflare.com') and _prem_configured('nitroflare', cfg):
        order.append('nitroflare')
    if host.endswith('ddownload.com') and _prem_configured('ddownload', cfg):
        order.append('ddownload')
    for name in ('alldebrid', 'realdebrid', 'debridlink', 'premiumize', 'megadebrid'):
        if _prem_configured(name, cfg):
            order.append(name)
    if not order:
        raise RuntimeError('Aucun compte premium/débrideur configuré.')
    errs = []
    for name in order:
        try:
            r = _PREM_RESOLVERS[name](url, cfg.get(name) or {})
            if r.get('link'):
                r['via'] = name
                return r
            errs.append(name + ': réponse vide')
        except Exception as e:
            errs.append(str(e))
    raise RuntimeError('Résolution échouée — ' + ' | '.join(errs)[:400])


def _prem_test(provider):
    import urllib.parse, urllib.request
    cfg = _prem_read().get(provider) or {}
    if provider == 'alldebrid':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        j = _prem_http('GET', 'https://api.alldebrid.com/v4/user?agent=Desktroo&apikey=' + urllib.parse.quote(cfg['apikey']))
        if j.get('status') == 'success':
            u = (j.get('data') or {}).get('user') or {}
            return {'ok': True, 'account': u.get('username'), 'premium': bool(u.get('isPremium')), 'until': u.get('premiumUntil')}
        raise RuntimeError(str((j.get('error') or {}).get('message') or j))
    if provider == 'realdebrid':
        if not cfg.get('token'):
            raise ValueError('Token manquant.')
        j = _prem_http('GET', 'https://api.real-debrid.com/rest/1.0/user', headers={'Authorization': 'Bearer ' + cfg['token']})
        if j.get('username'):
            return {'ok': True, 'account': j.get('username'), 'premium': (j.get('type') == 'premium'), 'until': j.get('expiration')}
        raise RuntimeError(str(j.get('error') or j))
    if provider == 'debridlink':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        j = _prem_http('GET', 'https://debrid-link.com/api/v2/account/infos', headers={'Authorization': 'Bearer ' + cfg['apikey']})
        if j.get('success'):
            v = j.get('value') or {}
            return {'ok': True, 'account': v.get('email') or v.get('username'), 'premium': bool(v.get('premiumLeft')), 'until': v.get('premiumLeft')}
        raise RuntimeError(str(j.get('error') or j))
    if provider == 'onefichier':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        last = _onef_state.get('test')
        if last and last[0] == cfg['apikey'] and _sh_time.time() - last[1] < 600:
            return dict(last[2], cached=True)
        j = _onefichier_call('user/info.cgi', {}, cfg['apikey'], timeout=30)
        if j.get('email') or j.get('status') == 'OK':
            res = {'ok': True, 'account': j.get('email'), 'premium': True, 'until': j.get('premium_until') or j.get('offer')}
            _onef_state['test'] = (cfg['apikey'], _sh_time.time(), res)
            return res
        raise RuntimeError(str(j.get('message') or j))
    if provider == 'rapidgator':
        if not (cfg.get('login') and cfg.get('password')):
            raise ValueError('Identifiants manquants.')
        j = _prem_http('GET', 'https://rapidgator.net/api/v2/user/login?login='
                       + urllib.parse.quote(cfg['login']) + '&password=' + urllib.parse.quote(cfg['password']))
        resp = j.get('response') or {}
        if resp.get('token'):
            d = _prem_read()
            d.setdefault('rapidgator', {})
            d['rapidgator']['token'] = resp['token']
            d['rapidgator']['token_ts'] = int(_sh_time.time())
            _prem_save(d)
            u = resp.get('user') or {}
            return {'ok': True, 'account': u.get('email') or cfg['login'], 'premium': bool(u.get('is_premium')), 'until': u.get('premium_end_time')}
        raise RuntimeError(str(j.get('details') or j))
    if provider == 'premiumize':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        j = _prem_http('GET', 'https://www.premiumize.me/api/account/info?apikey=' + urllib.parse.quote(cfg['apikey']))
        if j.get('status') == 'success':
            return {'ok': True, 'account': j.get('customer_id'), 'premium': bool(j.get('premium_until')), 'until': j.get('premium_until')}
        raise RuntimeError(str(j.get('message') or j))
    if provider == 'megadebrid':
        if not (cfg.get('login') and cfg.get('password')):
            raise ValueError('Identifiants manquants.')
        j = _prem_http('GET', 'https://www.mega-debrid.eu/api.php?action=connectUser&login='
                       + urllib.parse.quote(cfg['login']) + '&password=' + urllib.parse.quote(cfg['password']))
        if j.get('response_code') == 'ok' and j.get('token'):
            d = _prem_read(); d.setdefault('megadebrid', {})
            d['megadebrid']['token'] = j['token']; d['megadebrid']['token_ts'] = int(_sh_time.time()); _prem_save(d)
            return {'ok': True, 'account': cfg['login'], 'premium': True, 'until': j.get('vip_end') or j.get('premium_left')}
        raise RuntimeError(str(j.get('response_text') or j))
    if provider == 'nitroflare':
        if not (cfg.get('login') and cfg.get('password')):
            raise ValueError('E‑mail + clé premium requis.')
        j = _prem_http('GET', 'https://nitroflare.com/api/v2/getKeyInfo?user='
                       + urllib.parse.quote(cfg['login']) + '&premiumKey=' + urllib.parse.quote(cfg['password']))
        if j.get('type') == 'success':
            res = j.get('result') or {}
            return {'ok': True, 'account': cfg['login'],
                    'premium': (str(res.get('status')).lower() in ('active', 'premium', '1', 'true')) or bool(res.get('expiryDate')),
                    'until': res.get('expiryDate')}
        raise RuntimeError(str(j.get('messages') or j))
    if provider == 'ddownload':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        j = _prem_http('GET', 'https://ddownload.com/api/account/info?key=' + urllib.parse.quote(cfg['apikey']))
        if str(j.get('status')) == '200':
            res = j.get('result') or {}
            return {'ok': True, 'account': res.get('email'),
                    'premium': (str(res.get('premium')) in ('1', 'true')) or bool(res.get('premium_expire')),
                    'until': res.get('premium_expire')}
        raise RuntimeError(str(j.get('msg') or j))
    raise ValueError('Fournisseur inconnu.')
# ── MDM-PREMIUM-V1-END ───────────────────────────────────────────────────────


# ── MDM-DOWNLOADS-V1 : gestionnaire de téléchargements HTTP/HTTPS ────────────
def _install_pool_root():
    """« /mnt/<pool> » de cette installation, déduit du dossier d'installation (à défaut, de celui des VM)."""
    for p in (APP_DIR, os.environ.get('VM_DIR', ''), os.environ.get('ISO_DIR', '')):
        parts = str(p or '').split('/')
        if len(parts) > 2 and parts[0] == '' and parts[1] == 'mnt' and parts[2]:
            return '/mnt/' + parts[2]
    return ''


# Dossier de téléchargement quand rien n'est précisé (extension du navigateur, par exemple) : celui choisi dans
# Desktroo (réglage « default_dir »), à défaut « Downloads » à la racine du pool de l'installation. Jusqu'à la 2.28.2
# c'était un chemin écrit en dur, « /mnt/Truenas_Stockage/Downloads » : sur un NAS dont le pool porte un autre nom,
# le dossier était créé hors de tout pool, sur le disque système.
DOWNLOAD_DIR = os.environ.get('DOWNLOAD_DIR') or ((_install_pool_root() + '/Downloads') if _install_pool_root() else '')
DOWNLOAD_ALLOWED_ROOT = os.environ.get('DOWNLOAD_ALLOWED_ROOT', '/mnt')
_DL_DEFAULT_DIR = ''        # choisi dans Desktroo ; vide : DOWNLOAD_DIR
_DL_AUTO_EXTRACT = False    # décompresser à la fin, quand la demande ne le précise pas
_DL_AUTO_REMOVE = False     # retirer de la liste une fois terminé, idem


def _dl_default_dir():
    return _DL_DEFAULT_DIR or DOWNLOAD_DIR


def _dl_check_dir(path):
    """Chemin réel d'un dossier de destination, ou une erreur claire. Sous /mnt, le premier niveau (le pool) doit
    déjà exister : on ne crée jamais « /mnt/<nom> », ce serait écrire sur le disque système."""
    path = str(path or '').strip()
    if not path.startswith('/'):
        raise ValueError('Dossier de destination invalide.')
    real = os.path.realpath(path)
    base = os.path.realpath(DOWNLOAD_ALLOWED_ROOT).rstrip('/')
    if not (real == base or real.startswith(base + '/')):
        raise PermissionError('Destination hors zone autorisée (%s).' % DOWNLOAD_ALLOWED_ROOT)
    parts = real.split('/')
    if len(parts) > 2 and parts[1] == 'mnt':
        if not os.path.isdir('/mnt/' + parts[2]):
            raise PermissionError("Le pool « %s » n'existe pas sur ce NAS : choisissez un dossier d'un pool existant." % parts[2])
    if real == '/mnt':
        raise PermissionError('Choisissez un dossier dans un pool, pas /mnt lui-même.')
    return real


def _dl_set_default_dir(path):
    global _DL_DEFAULT_DIR
    _DL_DEFAULT_DIR = _dl_check_dir(path)
    return _DL_DEFAULT_DIR


def _dl_set_auto(extract=None, remove=None):
    global _DL_AUTO_EXTRACT, _DL_AUTO_REMOVE
    if extract is not None:
        _DL_AUTO_EXTRACT = bool(extract)
    if remove is not None:
        _DL_AUTO_REMOVE = bool(remove)
DOWNLOADS_FILE = os.path.join(ACCESS_DATA_DIR, 'downloads.json')
_DL_CHUNK = 256 * 1024
_DL_MAX_CONCURRENT = int(os.environ.get('DOWNLOAD_MAX_CONCURRENT', '3'))
_DL_CONNECTIONS = int(os.environ.get('DOWNLOAD_CONNECTIONS', '4'))   # sockets par lien (défaut)
_DL_MIN_SEG = 1024 * 1024   # taille min d'un segment (sinon on réduit le nb de connexions)
_dl_lock = _threading.RLock()
_dl_items = {}
_dl_threads = {}
_dl_flags = {}
class _ResizableSemaphore:
    """Sémaphore dont la capacité (nombre de jetons) est ajustable à chaud.
    Réduire la limite pendant des téléchargements en cours prend effet dès
    qu'un jeton se libère ; augmenter réveille aussitôt les threads en attente."""

    def __init__(self, value):
        self._cond = _threading.Condition(_threading.Lock())
        self._limit = int(value)
        self._in_use = 0

    def acquire(self, blocking=True, timeout=None):
        with self._cond:
            while self._in_use >= self._limit:
                if not blocking:
                    return False
                if not self._cond.wait(timeout):
                    return False
            self._in_use += 1
            return True

    def release(self):
        with self._cond:
            if self._in_use > 0:
                self._in_use -= 1
            self._cond.notify_all()

    def set_limit(self, n):
        with self._cond:
            self._limit = int(n)
            self._cond.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


_dl_sema = _ResizableSemaphore(_DL_MAX_CONCURRENT)

# MDM-DL-SETTINGS-V1 : réglages persistants du gestionnaire de téléchargements.
# Le nombre de téléchargements simultanés est ajustable à chaud depuis l'UI
# (endpoint POST /downloads/settings) et persiste dans data/dl-settings.json.
DL_SETTINGS_FILE = os.path.join(ACCESS_DATA_DIR, 'dl-settings.json')
_DL_MAX_CONCURRENT_MIN = 1
_DL_MAX_CONCURRENT_MAX = 20


def _dl_settings_get():
    return {'max_concurrent': _DL_MAX_CONCURRENT,
            'default_connections': _DL_CONNECTIONS,
            'default_dir': _DL_DEFAULT_DIR,
            'auto_extract': _DL_AUTO_EXTRACT,
            'auto_remove': _DL_AUTO_REMOVE}


def _dl_settings_save():
    try:
        _access_write_json(DL_SETTINGS_FILE, _dl_settings_get())
    except Exception as e:
        log.warning('dl settings save: %s', e)


def _dl_set_max_concurrent(n):
    """Ajuste le nombre de téléchargements simultanés à chaud.
    Augmenter libère aussitôt des jetons du sémaphore ; diminuer prend effet
    au fur et à mesure que les téléchargements en cours se terminent."""
    global _DL_MAX_CONCURRENT
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ValueError('Nombre de téléchargements simultanés invalide.')
    n = max(_DL_MAX_CONCURRENT_MIN, min(_DL_MAX_CONCURRENT_MAX, n))
    with _dl_lock:
        _DL_MAX_CONCURRENT = n
    _dl_sema.set_limit(n)
    return n


def _dl_set_default_connections(n):
    global _DL_CONNECTIONS
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ValueError('Nombre de connexions par lien invalide.')
    _DL_CONNECTIONS = max(1, min(16, n))
    return _DL_CONNECTIONS


def _dl_settings_load():
    try:
        d = _access_read_json(DL_SETTINGS_FILE, {})
        if isinstance(d, dict):
            if d.get('max_concurrent') is not None:
                _dl_set_max_concurrent(d.get('max_concurrent'))
            if d.get('default_connections') is not None:
                _dl_set_default_connections(d.get('default_connections'))
            global _DL_DEFAULT_DIR, _DL_AUTO_EXTRACT, _DL_AUTO_REMOVE
            if d.get('default_dir'):
                _DL_DEFAULT_DIR = str(d.get('default_dir'))   # vérifié à l'usage : le pool peut être absent au démarrage
            _DL_AUTO_EXTRACT = bool(d.get('auto_extract'))
            _DL_AUTO_REMOVE = bool(d.get('auto_remove'))
    except Exception as e:
        log.warning('dl settings load: %s', e)


def _dl_sanitize_name(name):
    name = os.path.basename(str(name or '')).strip() or 'download'
    name = re.sub(r'[\\/:*?"<>|]+', '_', name)
    return name[:200]


def _dl_pick_path(dest_dir, filename):
    cand = os.path.join(dest_dir, filename)
    if not os.path.exists(cand) and not os.path.exists(cand + '.part'):
        return cand
    stem, ext = os.path.splitext(filename)
    i = 1
    while True:
        c = os.path.join(dest_dir, '%s-%d%s' % (stem, i, ext))
        if not os.path.exists(c) and not os.path.exists(c + '.part'):
            return c
        i += 1


def _dl_save():
    try:
        with _dl_lock:
            data = {k: dict(v) for k, v in _dl_items.items()}
        _access_write_json(DOWNLOADS_FILE, data)
    except Exception as e:
        log.warning('downloads save: %s', e)


def _dl_load():
    try:
        d = _access_read_json(DOWNLOADS_FILE, {})
        if isinstance(d, dict):
            with _dl_lock:
                for k, it in d.items():
                    if isinstance(it, dict):
                        if it.get('status') in ('downloading', 'queued'):
                            it['status'] = 'paused'  # à reprendre manuellement
                        it['speed'] = 0
                        _dl_items[k] = it
    except Exception as e:
        log.warning('downloads load: %s', e)


def _dl_probe(url):
    """Sonde l'URL (GET Range 0-0) : retourne (total, ranges_ok, filename_cd)."""
    import urllib.request
    from urllib.parse import unquote
    req = urllib.request.Request(url, headers={'User-Agent': 'Desktroo', 'Range': 'bytes=0-0'})
    with urllib.request.urlopen(req, timeout=45) as r:
        status = getattr(r, 'status', None) or r.getcode()
        cr = r.headers.get('Content-Range') or ''
        ar = (r.headers.get('Accept-Ranges') or '').lower()
        clen = r.headers.get('Content-Length')
        cd = r.headers.get('Content-Disposition') or ''
        ct = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    fn = None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        fn = _dl_sanitize_name(unquote(m.group(1)))
    if status == 206 and cr:
        mm = re.search(r'/(\d+)\s*$', cr)
        return (int(mm.group(1)) if mm else 0), True, fn, ct
    return (int(clen) if clen else 0), (ar == 'bytes'), fn, ct


_DL_CT_EXT = {
    'application/zip': '.zip', 'application/x-zip-compressed': '.zip',
    'application/x-rar-compressed': '.rar', 'application/vnd.rar': '.rar',
    'application/x-7z-compressed': '.7z', 'application/x-tar': '.tar',
    'application/gzip': '.gz', 'application/x-gzip': '.gz',
}


def _dl_has_ext(name):
    base = str(name or '')
    dot = base.rfind('.')
    if dot <= 0 or dot >= len(base) - 1:
        return False
    ext = base[dot + 1:]
    return 1 <= len(ext) <= 6 and ext.isalnum()


def _dl_maybe_rename(it, fn, ctype=None):
    """Donne un nom correct au fichier. Beaucoup d'hebergeurs resolus renvoient
    un lien direct dont le basename est un id sans extension (ex. p2211932672) :
    on prefere alors le nom fourni par Content-Disposition ; a defaut on deduit
    au moins l'extension du Content-Type, pour que l'extraction auto fonctionne."""
    cur = it.get('filename', '')
    if fn and (cur.startswith('download') or (not _dl_has_ext(cur) and _dl_has_ext(fn))):
        newpath = _dl_pick_path(it['dir'], fn)
        with _dl_lock:
            it['filename'] = os.path.basename(newpath)
            it['path'] = newpath
        cur = os.path.basename(newpath)
    if not _dl_has_ext(cur) and ctype in _DL_CT_EXT:
        newpath = _dl_pick_path(it['dir'], cur + _DL_CT_EXT[ctype])
        with _dl_lock:
            it['filename'] = os.path.basename(newpath)
            it['path'] = newpath


def _dl_single(it, fl, total):
    """Repli mono-connexion, reprise via .part."""
    import urllib.request
    path = it['path']
    part = path + '.part'
    existing = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(it['url'], headers={'User-Agent': 'Desktroo'})
    if existing > 0:
        req.add_header('Range', 'bytes=%d-' % existing)
    resp = urllib.request.urlopen(req, timeout=60)
    status = getattr(resp, 'status', None) or resp.getcode()
    clen = resp.headers.get('Content-Length')
    if status == 206 and existing > 0:
        total = existing + (int(clen) if clen else 0)
        mode = 'ab'
    else:
        total = int(clen) if clen else (total or 0)
        existing = 0
        mode = 'wb'
    with _dl_lock:
        it['total'] = total
        it['downloaded'] = existing
    downloaded = existing
    last_t = _sh_time.time()
    last_b = downloaded
    with open(part, mode) as f:
        while True:
            if fl.get('cancel'):
                with _dl_lock:
                    it['status'] = 'canceled'; it['speed'] = 0
                _dl_save()
                try:
                    f.close(); os.remove(part)
                except OSError:
                    pass
                return
            if fl.get('pause'):
                with _dl_lock:
                    it['status'] = 'paused'; it['speed'] = 0
                _dl_save()
                return
            chunk = resp.read(_DL_CHUNK)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            now = _sh_time.time()
            if now - last_t >= 0.5:
                spd = (downloaded - last_b) / (now - last_t)
                with _dl_lock:
                    it['downloaded'] = downloaded; it['speed'] = spd
                    it['eta'] = int((total - downloaded) / spd) if (spd > 0 and total) else 0
                last_t = now; last_b = downloaded
    os.replace(part, path)
    with _dl_lock:
        it['downloaded'] = downloaded
        if not total or downloaded > total:
            it['total'] = downloaded
        it['status'] = 'done'; it['speed'] = 0; it['eta'] = 0
    _dl_save()


def _dl_multi(it, fl, total, n):
    """Téléchargement segmenté : n connexions parallèles (requêtes Range) écrites
    DIRECTEMENT dans le fichier final à leur offset. Plus aucune passe de
    réassemblage en fin de téléchargement (gain majeur sur les gros fichiers)."""
    import urllib.request, glob
    path = it['path']
    meta = path + '.parts'          # progression par segment (pour la reprise)
    seg = (total + n - 1) // n
    errs = [None] * n

    # Bornes [start, end) de chaque segment.
    bounds = [(i * seg, min((i + 1) * seg, total)) for i in range(n)]

    # Reprise : réutilise le fichier existant + la progression sauvegardée.
    seg_bytes = [0] * n
    resume = False
    try:
        if os.path.exists(path) and os.path.getsize(path) == total and os.path.exists(meta):
            with open(meta, 'r') as mf:
                saved = json.load(mf)
            if isinstance(saved, list) and len(saved) == n:
                seg_bytes = [max(0, int(x)) for x in saved]
                resume = True
    except Exception:
        resume = False

    if not resume:
        # Pré-alloue le fichier final (fichier creux : instantané) pour pouvoir
        # écrire chaque segment à sa position définitive.
        with open(path, 'wb') as f:
            if total:
                f.truncate(total)
        seg_bytes = [0] * n
        # Nettoie d'éventuels anciens fragments .partN (ancien format).
        for old in glob.glob(path + '.part[0-9]*'):
            try:
                os.remove(old)
            except OSError:
                pass

    def _persist_meta():
        try:
            with open(meta, 'w') as mf:
                json.dump(seg_bytes, mf)
        except Exception:
            pass

    def _seg(i):
        start, end = bounds[i]
        done = seg_bytes[i]
        if start >= end or start + done >= end:
            return
        try:
            req = urllib.request.Request(it['url'], headers={
                'User-Agent': 'Desktroo',
                'Range': 'bytes=%d-%d' % (start + done, end - 1)})
            resp = urllib.request.urlopen(req, timeout=60)
            with open(path, 'r+b') as f:
                f.seek(start + done)
                while True:
                    if fl.get('cancel') or fl.get('pause'):
                        return
                    chunk = resp.read(_DL_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    seg_bytes[i] += len(chunk)
        except Exception as e:
            errs[i] = str(e)

    with _dl_lock:
        it['total'] = total
        it['downloaded'] = sum(seg_bytes)
    threads = []
    for i in range(n):
        t = _threading.Thread(target=_seg, args=(i,), daemon=True)
        threads.append(t)
        t.start()
    last_t = _sh_time.time()
    last_b = sum(seg_bytes)
    while any(t.is_alive() for t in threads):
        _sh_time.sleep(0.25)
        now = _sh_time.time()
        if now - last_t >= 0.5:
            downloaded = sum(seg_bytes)
            spd = (downloaded - last_b) / (now - last_t)
            with _dl_lock:
                it['downloaded'] = downloaded; it['speed'] = spd
                it['eta'] = int((total - downloaded) / spd) if (spd > 0 and total) else 0
            last_t = now
            last_b = downloaded
            _persist_meta()
    for t in threads:
        t.join()
    downloaded = sum(seg_bytes)
    with _dl_lock:
        it['downloaded'] = downloaded
        it['speed'] = 0
    if fl.get('cancel'):
        for pth in (path, meta):
            try:
                os.remove(pth)
            except OSError:
                pass
        with _dl_lock:
            it['status'] = 'canceled'
        _dl_save()
        return
    if fl.get('pause'):
        _persist_meta()
        with _dl_lock:
            it['status'] = 'paused'
        _dl_save()
        return
    if downloaded < total or any(errs):
        _persist_meta()
        msg = '; '.join([e for e in errs if e][:2]) or 'segments incomplets'
        raise RuntimeError('Téléchargement incomplet : ' + msg)
    # Terminé : le fichier final est déjà complet et à sa place — aucune
    # recopie/réassemblage. On retire simplement la métadonnée de progression.
    try:
        os.remove(meta)
    except OSError:
        pass
    with _dl_lock:
        it['status'] = 'done'
        it['eta'] = 0
    _dl_save()


def _dl_is_archive(name):
    n = str(name or '').lower()
    return n.endswith(('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2',
                       '.tar.xz', '.txz', '.gz', '.bz2', '.xz', '.lzma',
                       '.rar', '.7z'))


def _dl_sniff_archive(path):
    """Detecte une archive par ses octets de tete (si l'extension manque ou ment)."""
    try:
        with open(path, 'rb') as fh:
            head = fh.read(8)
    except OSError:
        return False
    sigs = (b'PK\x03\x04', b'PK\x05\x06', b'Rar!\x1a\x07',
            b'7z\xbc\xaf\x27\x1c', b'\x1f\x8b', b'\xfd7zXZ\x00', b'BZh')
    return any(head.startswith(s) for s in sigs)


def _dl_extract_external(src, d, dest=None):
    """Extrait rar/7z via un binaire externe. Essaie tous les outils disponibles
    (7z/7za/bsdtar/unrar/unar) jusqu'à réussite. Ces outils assainissent les
    chemins (anti path-traversal). Retourne le dossier de destination."""
    import shutil as _sh, subprocess as _sp
    low = src.lower()
    stem = os.path.splitext(os.path.basename(src))[0]
    if dest:
        dest = os.path.realpath(dest)
    else:
        dest = os.path.join(d, stem or 'extrait')
        k = 1
        while os.path.exists(dest):
            dest = os.path.join(d, (stem or 'extrait') + '.' + str(k)); k += 1
    os.makedirs(dest, exist_ok=True)

    def run(cmd):
        p = _sp.run(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT)
        return p.returncode, (p.stdout or b'').decode('utf-8', 'replace')

    cands = []
    if _sh.which('7z'):
        cands.append(['7z', 'x', '-y', '-o' + dest, src])
    if _sh.which('7za'):
        cands.append(['7za', 'x', '-y', '-o' + dest, src])
    if not low.endswith('.rar') and _sh.which('7zr'):
        cands.append(['7zr', 'x', '-y', '-o' + dest, src])
    if _sh.which('bsdtar'):
        cands.append(['bsdtar', '-x', '-f', src, '-C', dest])
    if low.endswith('.rar') and _sh.which('unrar'):
        cands.append(['unrar', 'x', '-y', '-o+', src, dest + os.sep])
    if _sh.which('unar'):
        cands.append(['unar', '-f', '-D', '-o', dest, src])

    if not cands:
        raise ValueError('Aucun outil (7z/bsdtar/unrar/unar) installé dans le conteneur.')
    last = ''
    for cmd in cands:
        try:
            rc, out = run(cmd)
        except Exception as e:
            last = cmd[0] + ': ' + str(e); continue
        if rc == 0 and os.path.isdir(dest) and os.listdir(dest):
            return dest
        last = cmd[0] + ' (code ' + str(rc) + ') : ' + out.strip()[-160:]
    raise ValueError('Échec extraction — ' + last)


def _dl_extract_archive(src):
    """Extrait une archive locale (zip/tar/gz/bz2/xz), protégé contre le zip-slip."""
    import zipfile as _zf, tarfile as _tf, shutil as _shutil
    if not os.path.isfile(src):
        raise ValueError('Fichier introuvable')
    d = os.path.dirname(src)

    def decide_dest(names):
        tops = set()
        for n in names:
            n = str(n).replace('\\', '/').lstrip('/')
            if n:
                tops.add(n.split('/')[0])
        if len(tops) == 1:
            return d
        low = src.lower()
        if low.endswith('.tar.gz'):
            stem = os.path.basename(src)[:-7]
        elif low.endswith('.tgz'):
            stem = os.path.basename(src)[:-4]
        else:
            stem = os.path.splitext(os.path.basename(src))[0]
        return os.path.join(d, stem or 'extrait')

    def do(names, extract_all):
        dest = decide_dest(names)
        os.makedirs(dest, exist_ok=True)
        dr = os.path.realpath(dest)
        for name in names:
            p = os.path.realpath(os.path.join(dr, name))
            if not (p == dr or p.startswith(dr + os.sep)):
                raise ValueError('Archive non sûre (zip-slip)')
        extract_all(dr)
        return dr

    if _zf.is_zipfile(src):
        with _zf.ZipFile(src) as z:
            return do(z.namelist(), z.extractall)
    if _tf.is_tarfile(src):
        with _tf.open(src) as t:
            return do(t.getnames(), t.extractall)
    low = src.lower()
    if low.endswith(('.rar', '.7z')):
        return _dl_extract_external(src, d)
    if low.endswith('.gz'):
        import gzip as _c; out = src[:-3]
    elif low.endswith('.bz2'):
        import bz2 as _c; out = src[:-4]
    elif low.endswith(('.xz', '.lzma')):
        import lzma as _c; out = os.path.splitext(src)[0]
    else:
        raise ValueError('Format non pris en charge (zip/tar/gz/bz2/xz).')
    cand = out or (src + '.out')
    k = 1
    while os.path.exists(cand):
        cand = out + '.' + str(k); k += 1
    with _c.open(src, 'rb') as fi, open(cand, 'wb') as fo:
        _shutil.copyfileobj(fi, fo)
    return cand


def _dl_postprocess(did):
    """Décompression et/ou retrait auto après un téléchargement terminé."""
    with _dl_lock:
        it = _dl_items.get(did)
    if not it:
        return
    if it.get('auto_extract') and (_dl_is_archive(it.get('filename', '')) or _dl_sniff_archive(it.get('path', ''))):
        with _dl_lock:
            it['status'] = 'extracting'; it['speed'] = 0
        _dl_save()
        try:
            _dl_extract_archive(it['path'])
        except Exception as e:
            with _dl_lock:
                it['error'] = ('extraction : ' + str(e))[:250]
        with _dl_lock:
            if it.get('status') == 'extracting':
                it['status'] = 'done'
        _dl_save()
    if it.get('auto_remove'):
        with _dl_lock:
            _dl_items.pop(did, None)
            _dl_flags.pop(did, None)
        _dl_save()


def _dl_worker(did):
    with _dl_sema:
        with _dl_lock:
            it = _dl_items.get(did)
            fl = _dl_flags.get(did)
        if not it or not fl or fl.get('cancel'):
            with _dl_lock:
                _dl_threads.pop(did, None)
            return
        try:
            with _dl_lock:
                it['status'] = 'downloading'
                it['error'] = ''
            _dl_save()
            total, ranges, fn, ctype = _dl_probe(it['url'])
            # Filet de sécurité : un hébergeur non reconnu répond souvent par une
            # page HTML au lieu du fichier. Si le lien n'a pas déjà été résolu et
            # qu'un débrideur généraliste est configuré (premium != off), on le
            # fait passer par le débrideur plutôt que d'enregistrer la page HTML.
            _dl_prefer = it.get('premium') if it.get('premium') in _PREM_PROVIDERS else None
            if (not it.get('via')
                    and str(it.get('premium') or 'auto') != 'off'
                    and 'html' in (ctype or '')
                    and (_dl_prefer is not None
                         or any(_prem_configured(pr) for pr in _PREM_DEBRIDERS))):
                r = _resolve_premium(it.get('source_url') or it['url'], prefer=_dl_prefer)
                newname = _dl_sanitize_name(r['filename']) if r.get('filename') else None
                with _dl_lock:
                    it['url'] = r['link']
                    it['via'] = r.get('via')
                    if r.get('size'):
                        it['total'] = r['size']
                    if newname:
                        newpath = _dl_pick_path(it['dir'], newname)
                        it['filename'] = os.path.basename(newpath)
                        it['path'] = newpath
                _dl_save()
                total, ranges, fn, ctype = _dl_probe(it['url'])
            _dl_maybe_rename(it, fn, ctype)
            n = int(it.get('connections') or _DL_CONNECTIONS)
            if ranges and total and total > _DL_MIN_SEG and n > 1:
                n = max(1, min(n, total // _DL_MIN_SEG))
                with _dl_lock:
                    it['connections'] = n
                _dl_multi(it, fl, total, n)
            else:
                with _dl_lock:
                    it['connections'] = 1
                _dl_single(it, fl, total)
            with _dl_lock:
                _st = (_dl_items.get(did) or {}).get('status')
            if _st == 'done':
                _dl_postprocess(did)
        except Exception as e:
            with _dl_lock:
                cur = _dl_items.get(did)
                if cur:
                    cur['status'] = 'error'
                    cur['error'] = str(e)[:300]
                    cur['speed'] = 0
            _dl_save()
        finally:
            with _dl_lock:
                _dl_threads.pop(did, None)


def _dl_start_thread(did):
    with _dl_lock:
        if did in _dl_threads:
            return
        _dl_flags[did] = {'pause': False, 'cancel': False}
        t = _threading.Thread(target=_dl_worker, args=(did,), daemon=True)
        _dl_threads[did] = t
    t.start()


def _dl_add(url, dest_dir=None, filename=None, premium='auto', connections=None,
            auto_extract=None, auto_remove=None):
    url = str(url or '').strip()
    if not re.match(r'^https?://', url, re.I):
        raise ValueError('URL invalide (http/https attendu).')
    if not (dest_dir or _dl_default_dir()):
        raise ValueError('Aucun dossier de destination : choisissez-en un dans Téléchargements, Options.')
    dest_dir = _dl_check_dir(dest_dir or _dl_default_dir())
    os.makedirs(dest_dir, exist_ok=True)
    if auto_extract is None:
        auto_extract = _DL_AUTO_EXTRACT
    if auto_remove is None:
        auto_remove = _DL_AUTO_REMOVE
    # Résolution premium (débrideur / compte direct) si demandé.
    source_url = url
    via = None
    resolved_name = None
    resolved_size = 0
    premium = str(premium or 'auto')
    prefer = premium if premium in _PREM_PROVIDERS else None
    if premium != 'off':
        want = (premium == 'force') or (prefer is not None) or _is_known_hoster(url)
        if prefer is not None:
            has_provider = _prem_configured(prefer)
        else:
            has_provider = any(_prem_configured(p) for p in _PREM_PROVIDERS)
        if want and has_provider:
            r = _resolve_premium(url, prefer=prefer)  # peut lever -> remonte à l'appelant
            url = r['link']
            via = r.get('via')
            resolved_name = r.get('filename')
            resolved_size = r.get('size') or 0
        elif want and prefer is not None:
            raise ValueError('Débrideur choisi (' + prefer + ") non configuré dans « Comptes premium ».")
        elif want and not has_provider:
            raise ValueError('Lien d\'hébergeur premium détecté mais aucun compte/débrideur configuré.')
    if not filename:
        from urllib.parse import urlparse, unquote
        filename = resolved_name or unquote(os.path.basename(urlparse(url).path)) or 'download'
    filename = _dl_sanitize_name(filename)
    path = _dl_pick_path(dest_dir, filename)
    did = _sh_secrets.token_hex(5)
    try:
        conn = int(connections) if connections else _DL_CONNECTIONS
    except (TypeError, ValueError):
        conn = _DL_CONNECTIONS
    conn = max(1, min(conn, 16))
    it = {'id': did, 'url': url, 'source_url': source_url, 'via': via, 'dir': dest_dir,
          'filename': os.path.basename(path), 'path': path,
          'total': resolved_size or 0, 'downloaded': 0, 'speed': 0, 'eta': 0,
          'connections': conn, 'premium': premium,
          'auto_extract': bool(auto_extract), 'auto_remove': bool(auto_remove),
          'status': 'queued', 'error': '', 'added_at': int(_sh_time.time())}
    with _dl_lock:
        _dl_items[did] = it
    _dl_save()
    _dl_start_thread(did)
    return it


def _dl_add_batch(urls, dest_dir=None, premium='auto', connections=None,
                  auto_extract=None, auto_remove=None):
    """Ajoute plusieurs URLs d'un coup. Retourne un résultat par URL (une erreur
    sur un lien n'empêche pas les autres)."""
    out = []
    seen = set()
    for u in (urls or []):
        u = str(u or '').strip()
        if not u or u in seen:
            continue
        seen.add(u)
        try:
            it = _dl_add(u, dest_dir, None, premium, connections, auto_extract, auto_remove)
            out.append({'url': u, 'ok': True, 'id': it['id']})
        except Exception as e:
            out.append({'url': u, 'ok': False, 'error': str(e)})
    return out


def _dl_pause(did):
    with _dl_lock:
        fl = _dl_flags.get(did)
        it = _dl_items.get(did)
    if not it:
        raise ValueError('Téléchargement introuvable.')
    if fl:
        fl['pause'] = True
    return {'ok': True}


def _dl_resume(did):
    with _dl_lock:
        it = _dl_items.get(did)
        if not it:
            raise ValueError('Téléchargement introuvable.')
        if it['status'] in ('downloading', 'done'):
            return {'ok': True}
        it['status'] = 'queued'
        it['error'] = ''
    _dl_start_thread(did)
    return {'ok': True}


def _dl_cancel(did, delete=True):
    with _dl_lock:
        fl = _dl_flags.get(did)
        it = _dl_items.get(did)
    if not it:
        return {'ok': True}
    if fl:
        fl['cancel'] = True
    if did not in _dl_threads:  # pas en cours -> nettoyer soi-même
        with _dl_lock:
            it['status'] = 'canceled'
            it['speed'] = 0
        if delete:
            # multi-part non terminé : fichier final partiel + métadonnée .parts.
            incomplete_multi = (it.get('status') != 'done'
                                and os.path.exists(it['path'] + '.parts'))
            for suffix in ('.part', '.parts'):
                try:
                    os.remove(it['path'] + suffix)
                except OSError:
                    pass
            if incomplete_multi:
                try:
                    os.remove(it['path'])
                except OSError:
                    pass
        _dl_save()
    return {'ok': True}


def _dl_remove(did):
    _dl_cancel(did, delete=True)
    with _dl_lock:
        _dl_items.pop(did, None)
        _dl_flags.pop(did, None)
    _dl_save()
    return {'ok': True}


def _dl_clear():
    with _dl_lock:
        rem = [k for k, v in _dl_items.items() if v.get('status') in ('done', 'error', 'canceled')]
        for k in rem:
            _dl_items.pop(k, None)
            _dl_flags.pop(k, None)
    _dl_save()
    return {'ok': True, 'removed': len(rem)}


def _dl_list():
    with _dl_lock:
        items = [dict(v) for v in _dl_items.values()]
    items.sort(key=lambda x: x.get('added_at', 0), reverse=True)
    return {'ok': True, 'downloads': items, 'default_dir': _dl_default_dir(), 'default_dir_set': bool(_DL_DEFAULT_DIR),
            'max_concurrent': _DL_MAX_CONCURRENT, 'default_connections': _DL_CONNECTIONS,
            'auto_extract': _DL_AUTO_EXTRACT, 'auto_remove': _DL_AUTO_REMOVE}


_dl_load()
_dl_settings_load()
# ── MDM-DOWNLOADS-V1-END ─────────────────────────────────────────────────────


# ── MDM-LICENSE-V1 : essai + abonnement (vérif RSA-SHA256 PKCS#1 v1.5, pur-Python) ──
import base64 as _lic_b64
import hashlib as _lic_hashlib
import hmac as _lic_hmac
import time as _lic_time

TRIAL_DAYS    = int(os.environ.get('TRIAL_DAYS', '30'))
GRACE_DAYS    = int(os.environ.get('GRACE_DAYS', '3'))   # tolérance après expiration d'un abonnement payant
LICENSE_PRICE = os.environ.get('LICENSE_PRICE', '3.99')
# Desktroo est gratuit depuis le 23/10/2026. Le mecanisme de licence reste en place
# (enregistrement des installations, cle gratuite) mais il ne bloque plus rien.
# Repasser a False pour reactiver essai + lecture seule.
LICENSE_FREE = os.environ.get('LICENSE_FREE', '1') not in ('0', 'false', 'False')
LICENSE_FILE  = os.path.join(ACCESS_DATA_DIR, 'license.json')
TRIAL_FILE    = os.path.join(ACCESS_DATA_DIR, 'trial.json')
ACTIVATION_KEY_FILE = os.path.join(ACCESS_DATA_DIR, 'activation-key')
LIC_REFRESH_FILE    = os.path.join(ACCESS_DATA_DIR, 'license-refresh.json')

# Serveur de licences (emission/refresh en ligne) + lien de contribution.
# Depuis la 3.0.0 Desktroo est gratuit : SUBSCRIBE_URL n'est plus un lien d'abonnement,
# c'est la destination du bouton « Soutenir Desktroo » de la fenetre Licence. Il pointe sur la
# section « Soutenez Desktroo ! » du site, qui porte le lien de paiement a montant libre : changer
# de prestataire ne demande alors aucune mise a jour de l'application.
# MANAGE_URL (ancien portail client Stripe) n'est plus atteignable : licManage() ne s'affiche que
# pour une licence payante, et il n'en existe plus. Garde pour un eventuel retour en arriere.
LICENSE_SERVER = os.environ.get('LICENSE_SERVER', 'https://desktroo.fr/wp-json/desktroo/v1').rstrip('/')
SUBSCRIBE_URL  = os.environ.get('SUBSCRIBE_URL', 'https://desktroo.fr/#tarifs')
FREE_KEY_URL   = os.environ.get('FREE_KEY_URL', 'https://desktroo.fr/cle-gratuite/')   # page de la clé gratuite
MANAGE_URL     = os.environ.get('MANAGE_URL', 'https://billing.stripe.com/p/login/bJe4gy5Z0eCxbFZdbh38400')
LIC_REFRESH_INTERVAL = int(os.environ.get('LIC_REFRESH_INTERVAL', '43200'))  # 12 h entre deux refresh auto

# Clé publique de licence (RSA-2048). La clé privée reste hors dépôt chez l'éditeur.
LICENSE_PUBKEY_N_HEX = (
    "B92F624CA18A65506AE6D78AEC008F9E561C3C6BD591658303FD0892B914B2C4"
    "33D615D27A8B1775D4F2668EC8CFCAB9A4B6741253B124B7D14F0655C0C4E48C7"
    "525CA1D31417EED0E50C53F7CE562880E05AA0B04C909708BC13BA85FC424DE29"
    "8CA9FA0239CA78A2DBECCA2EC8C31CA0545B3677668163F83846C8A081510EBFB"
    "8B578000BB37F77DC584634DC23754EDB835BED0FCF4AD200142FE7C92DF5F6AF"
    "A38E21A902CF426D3930D673145C10E92FE56D66627B60A62635001F32E3C3092"
    "535A87280C87E8C406B6926C55396193DACD2D3A1DC138EB62E66EE0384E2B3D5"
    "3A68993595E969B6968B8C9617BBA1CF575886154E97EEADDD12986341")
_LIC_N = int(LICENSE_PUBKEY_N_HEX, 16)
_LIC_E = 65537
_LIC_SHA256_DI = bytes.fromhex('3031300d060960864801650304020105000420')


def _lic_b64url_dec(s):
    s = s + '=' * (-len(s) % 4)
    return _lic_b64.urlsafe_b64decode(s.encode('ascii'))


def _lic_verify(token):
    """Retourne le payload (dict) si la signature est valide, sinon None.
    Ne vérifie PAS l'expiration (l'appelant compare exp/now)."""
    try:
        p_b64, s_b64 = token.strip().split('.')
        payload = _lic_b64url_dec(p_b64)
        sig = _lic_b64url_dec(s_b64)
    except Exception:
        return None
    k = (_LIC_N.bit_length() + 7) // 8
    if len(sig) > k:
        return None
    if len(sig) < k:
        sig = b'\x00' * (k - len(sig)) + sig
    try:
        em = pow(int.from_bytes(sig, 'big'), _LIC_E, _LIC_N).to_bytes(k, 'big')
    except Exception:
        return None
    t = _LIC_SHA256_DI + _lic_hashlib.sha256(payload).digest()
    ps_len = k - 3 - len(t)
    if ps_len < 8:
        return None
    expected = b'\x00\x01' + b'\xff' * ps_len + b'\x00' + t
    if not _lic_hmac.compare_digest(em, expected):
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


INSTALL_ID_FILE = os.path.join(ACCESS_DATA_DIR, 'install-id')


def _lic_compute_install_id():
    """Ancienne logique deterministe — sert uniquement de graine initiale."""
    parts = []
    try:
        if SSH_HOST:
            parts.append(str(SSH_HOST))
    except Exception:
        pass
    for p in ('/etc/machine-id', '/data/machine-id'):
        try:
            with open(p) as fh:
                parts.append(fh.read().strip())
                break
        except Exception:
            pass
    try:
        parts.append(os.uname().nodename)
    except Exception:
        pass
    # NB : 'truenas-desktop' est une graine de hachage historique, pas un nom affiche.
    # Ne PAS la renommer : elle entre dans la derivation de l'install_id (licences liees).
    raw = '|'.join([x for x in parts if x]) or 'truenas-desktop'
    return _lic_hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _lic_install_id():
    """Identifiant d'installation STABLE, persiste dans le volume de donnees.
    Genere une seule fois puis relu : survit a la recreation du conteneur (et
    donc aux mises a jour), contrairement au hostname du conteneur qui change
    a chaque recreation. ACCESS_DATA_DIR est un volume hote (/mnt/...)."""
    try:
        with open(INSTALL_ID_FILE, 'r', encoding='utf-8') as fh:
            v = fh.read().strip()
            if v:
                return v
    except Exception:
        pass
    v = _lic_compute_install_id()
    try:
        os.makedirs(os.path.dirname(INSTALL_ID_FILE), exist_ok=True)
        with open(INSTALL_ID_FILE, 'w', encoding='utf-8') as fh:
            fh.write(v)
    except Exception as e:
        log.warning('install-id persist failed: %s', e)
    return v


def _lic_read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _lic_write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh)
        return True
    except Exception as e:
        log.warning('license write failed %s: %s', path, e)
        return False


TRIAL_PROV_DAYS = int(os.environ.get('TRIAL_PROV_DAYS', '3'))   # essai provisoire si desktroo.fr est injoignable
TRIAL_FP_FILE   = os.path.join(ACCESS_DATA_DIR, 'trial-fp.json')
TRIAL_MARK_FILE = os.path.join(APP_DIR or ACCESS_DATA_DIR, '.dk-t0')   # 2e témoin : survit à la suppression de trial.json
TRIAL_RETRY     = 600


def _lic_fingerprint():
    """Empreinte IRRÉVERSIBLE du NAS (SHA-256), stable après réinstallation de Desktroo.
    Basée sur l'UUID matériel (DMI) quand il existe, sinon GUID du pool de démarrage + machine-id de l'hôte.
    Seul ce hachage quitte le NAS. None si l'hôte est injoignable."""
    c = _lic_read_json(TRIAL_FP_FILE)
    if c and re.match(r'^[0-9a-f]{64}$', str(c.get('fp', ''))):
        return c['fp']
    key = ''
    try:
        out, _e, _c = _px_host("cat /sys/class/dmi/id/product_uuid 2>/dev/null; echo '|'; "
                               "zpool get -H -o value guid boot-pool 2>/dev/null; echo '|'; "
                               "cat /etc/machine-id 2>/dev/null", timeout=15)
        parts = [x.strip() for x in (out or '').split('|')]
        dmi = (parts[0] if parts else '').lower()
        if re.match(r'^[0-9a-f-]{32,36}$', dmi) and len(set(dmi.replace('-', ''))) > 2:
            key = 'dmi:' + dmi
        elif len(parts) >= 3 and parts[1] and parts[2]:
            key = 'zfs:' + parts[1] + ':' + parts[2]
    except Exception:
        key = ''
    if not key:
        return None
    fp = _lic_hashlib.sha256(('desktroo-trial-v1|' + key).encode('utf-8')).hexdigest()
    _lic_write_json(TRIAL_FP_FILE, {'fp': fp})
    return fp


def _lic_trial_token_ok(tok, fp):
    p = _lic_verify(tok) if tok else None
    if not p or p.get('kind') != 'trial' or not isinstance(p.get('start'), (int, float)):
        return None
    if fp and p.get('fingerprint') != fp:
        return None
    return p


def _lic_trial_state(now):
    """-> dict(start, exp, provisional, now). Gère jeton signé, mode provisoire et recul d'horloge."""
    rec = _lic_read_json(TRIAL_FILE) or {}
    mark = _lic_read_json(TRIAL_MARK_FILE) or {}
    fp = _lic_fingerprint() if rec.get('tok') else None
    changed = False
    hw = max(int(rec.get('hw') or 0), int(mark.get('hw') or 0))
    if now < hw:
        now = hw                      # horloge reculée : on ne rend pas de temps
    elif now - hw > 600:
        rec['hw'] = mark['hw'] = now
        changed = True
    p = _lic_trial_token_ok(rec.get('tok'), fp)
    if p:
        out = {'start': int(p['start']), 'exp': int(p.get('exp') or int(p['start']) + TRIAL_DAYS * 86400),
               'provisional': False, 'now': now}
    else:
        prov = int(rec.get('prov') or 0) or int(mark.get('prov') or 0)
        if not prov:
            prov = now
        if rec.get('prov') != prov or mark.get('prov') != prov:
            rec['prov'] = mark['prov'] = prov
            changed = True
        legacy = rec.get('start')
        prov_end = prov + TRIAL_PROV_DAYS * 86400
        if isinstance(legacy, (int, float)):       # installation antérieure : date locale non signée, plafonnée
            exp = min(int(legacy) + TRIAL_DAYS * 86400, prov_end)
            start = int(legacy)
        else:
            exp, start = prov_end, prov
        out = {'start': start, 'exp': exp, 'provisional': True, 'now': now}
    if changed:
        _lic_write_json(TRIAL_FILE, rec)
        _lic_write_json(TRIAL_MARK_FILE, mark)
    return out


def license_trial_sync(force=False):
    """Enregistre / relit l'essai auprès de desktroo.fr. True si un jeton d'essai valide est en place."""
    now = int(_lic_time.time())
    rec = _lic_read_json(TRIAL_FILE) or {}
    fp = _lic_fingerprint()
    if not fp:
        return False
    p = _lic_trial_token_ok(rec.get('tok'), fp)
    if p and not force and now - int(rec.get('sync') or 0) < LIC_REFRESH_INTERVAL:
        return True
    if not force and now - int(rec.get('try') or 0) < TRIAL_RETRY:
        return bool(p)
    rec['try'] = now
    _lic_write_json(TRIAL_FILE, rec)
    body = {'install_id': _lic_install_id(), 'fingerprint': fp}
    claim = rec.get('start') if isinstance(rec.get('start'), (int, float)) else (rec.get('prov') or 0)
    if claim:
        body['claim'] = int(claim)
    resp = _lic_server_post('trial', body, timeout=6)
    if resp and resp.get('ok') and resp.get('token'):
        q = _lic_trial_token_ok(str(resp['token']), fp)
        if q:
            rec['tok'] = str(resp['token'])
            rec['sync'] = now
            rec['hw'] = max(int(rec.get('hw') or 0), int(q.get('iat') or 0), now)
            _lic_write_json(TRIAL_FILE, rec)
            return True
    return bool(p)


def _lic_registered():
    """Clé gratuite activée sur ce NAS : jeton signé (pas d'essai), lié à cette installation, encore valable.
    Lu sur le disque, sans aucun appel réseau. Donne accès aux mises à jour et à la bêta (3.1.0)."""
    try:
        rec = _lic_read_json(LICENSE_FILE)
        p = _lic_verify(rec['token']) if (rec and rec.get('token')) else None
    except Exception:
        p = None
    if not p or p.get('kind') == 'trial':
        return False
    if p.get('install_id') and p.get('install_id') != _lic_install_id():
        return False
    try:
        return int(p.get('exp', 0)) > int(_lic_time.time())
    except Exception:
        return False


def _update_gate():
    """La mise à jour de Desktroo peut-elle partir ? Avec une clé, toujours ; sans clé, pour un correctif de sécurité,
    ou pour réinstaller la version en place. Rend {ok} ou {ok: False, need_key, error}."""
    if _lic_registered():
        return {'ok': True}
    v = _version_status()
    if not v.get('latest'):
        return {'ok': False, 'error': 'Impossible de vérifier la dernière version de Desktroo pour le moment. Réessayez plus tard.'}
    if not v.get('update_available') or v.get('update_allowed'):
        return {'ok': True}
    return {'ok': False, 'need_key': True, 'latest': v.get('latest'),
            'error': 'Les mises à jour de Desktroo demandent une clé gratuite. Demandez-la sur desktroo.fr, puis activez-la dans « Licence ». '
                     'Desktroo continue de fonctionner entièrement sans.'}


def license_status():
    now = int(_lic_time.time())
    install = _lic_install_id()
    if LICENSE_FREE:
        rec = _lic_read_json(LICENSE_FILE)
        p = _lic_verify(rec['token']) if (rec and rec.get('token')) else None
        return {'state': 'active', 'plan': 'free', 'exp': now + 3650 * 86400,
                'days_left': 3650, 'customer': (p.get('customer', '') if p else ''),
                'registered': _lic_registered(), 'free_key_url': FREE_KEY_URL,
                'readonly': False, 'install_id': install, 'price': LICENSE_PRICE}
    rec = _lic_read_json(LICENSE_FILE)
    if rec and rec.get('token'):
        p = _lic_verify(rec['token'])
        if p and p.get('kind') == 'trial':
            p = None
        if p and int(p.get('exp', 0)) > now and (not p.get('install_id') or p.get('install_id') == install):
            exp = int(p['exp'])
            return {'state': 'active', 'plan': p.get('plan', 'monthly'), 'exp': exp,
                    'days_left': max(0, (exp - now) // 86400), 'customer': p.get('customer', ''),
                    'readonly': False, 'install_id': install, 'price': LICENSE_PRICE}
        if p and int(p.get('exp', 0)) <= now:
            exp = int(p.get('exp', 0))
            grace_end = exp + GRACE_DAYS * 86400
            if GRACE_DAYS > 0 and now < grace_end:
                return {'state': 'grace', 'plan': p.get('plan', 'monthly'), 'exp': exp,
                        'grace_end': grace_end, 'days_left': max(0, (grace_end - now + 86399) // 86400),
                        'customer': p.get('customer', ''), 'readonly': False,
                        'install_id': install, 'price': LICENSE_PRICE}
            return {'state': 'expired', 'plan': p.get('plan', 'monthly'), 'exp': exp,
                    'days_left': 0, 'customer': p.get('customer', ''), 'readonly': True,
                    'install_id': install, 'price': LICENSE_PRICE}
    ts = _lic_trial_state(now)
    exp = ts['exp']
    now = ts['now']
    if now < exp:
        return {'state': 'trial', 'plan': 'trial', 'exp': exp, 'provisional': ts['provisional'],
                'days_left': max(0, (exp - now + 86399) // 86400), 'readonly': False,
                'install_id': install, 'price': LICENSE_PRICE}
    return {'state': 'expired', 'plan': 'trial', 'exp': exp, 'days_left': 0,
            'readonly': True, 'install_id': install, 'price': LICENSE_PRICE}


def license_activate(token):
    now = int(_lic_time.time())
    install = _lic_install_id()
    p = _lic_verify(token)
    if not p or p.get('kind') == 'trial':
        return (400, {'ok': False, 'error': 'Jeton invalide (signature non reconnue).'})
    if int(p.get('exp', 0)) <= now:
        return (400, {'ok': False, 'error': 'Jeton expiré.'})
    if p.get('install_id') and p.get('install_id') != install:
        return (400, {'ok': False, 'error': 'Jeton lié à une autre installation.'})
    _lic_write_json(LICENSE_FILE, {'token': token})
    return (200, {'ok': True, 'status': license_status()})


def _lic_server_post(ep, payload, timeout=20):
    """POST JSON vers le serveur de licences. Retourne un dict (corps JSON) ou None."""
    import urllib.request
    url = LICENSE_SERVER + '/' + ep.lstrip('/')
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'Desktroo'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        body = getattr(e, 'read', None)
        if body:
            try:
                return json.loads(e.read().decode('utf-8', 'replace'))
            except Exception:
                pass
        log.warning('license server call failed (%s): %s', ep, e)
        return None


def _lic_save_activation_key(key):
    try:
        os.makedirs(os.path.dirname(ACTIVATION_KEY_FILE), exist_ok=True)
        with open(ACTIVATION_KEY_FILE, 'w', encoding='utf-8') as fh:
            fh.write((key or '').strip())
    except Exception as e:
        log.warning('activation-key persist failed: %s', e)


def _lic_read_activation_key():
    try:
        with open(ACTIVATION_KEY_FILE, 'r', encoding='utf-8') as fh:
            return fh.read().strip()
    except Exception:
        return ''


def license_activate_key(key):
    """Active via le serveur : clé d'activation -> liaison install_id -> jeton signé."""
    key = (key or '').strip().upper()
    if not key:
        return (400, {'ok': False, 'error': "Saisissez une clé d'activation."})
    install = _lic_install_id()
    resp = _lic_server_post('activate', {'activation_key': key, 'install_id': install})
    if resp is None:
        return (502, {'ok': False, 'error': 'Serveur de licences injoignable. Vérifiez la connexion et réessayez.'})
    if not resp.get('ok') or not resp.get('token'):
        return (400, {'ok': False, 'error': resp.get('error', "Échec de l'activation.")})
    code, out = license_activate(str(resp['token']))
    if code == 200 and out.get('ok'):
        _lic_save_activation_key(key)
        _lic_write_json(LIC_REFRESH_FILE, {'ts': int(_lic_time.time())})
    return (code, out)


def license_do_refresh():
    """Rafraîchit le jeton depuis le serveur si une clé d'activation est connue."""
    key = _lic_read_activation_key()
    if not key:
        return False
    install = _lic_install_id()
    resp = _lic_server_post('refresh', {'activation_key': key, 'install_id': install})
    _lic_write_json(LIC_REFRESH_FILE, {'ts': int(_lic_time.time())})
    if resp and resp.get('ok') and resp.get('token'):
        code, _out = license_activate(str(resp['token']))
        return code == 200
    return False


def license_maybe_refresh():
    """Déclenche un refresh en tâche de fond, au plus une fois par LIC_REFRESH_INTERVAL."""
    if not _lic_read_activation_key():
        return
    rec = _lic_read_json(LIC_REFRESH_FILE) or {}
    last = rec.get('ts', 0)
    last = int(last) if isinstance(last, (int, float)) else 0
    now = int(_lic_time.time())
    if now - last < LIC_REFRESH_INTERVAL:
        return
    _lic_write_json(LIC_REFRESH_FILE, {'ts': now})  # jalon posé tout de suite (évite les doublons)
    try:
        import threading
        threading.Thread(target=license_do_refresh, daemon=True).start()
    except Exception:
        try:
            license_do_refresh()
        except Exception:
            pass


def _lic_is_readonly():
    if LICENSE_FREE:
        return False
    try:
        return bool(license_status().get('readonly'))
    except Exception:
        return False


# Routes POST dont l'écriture reste autorisée même en lecture seule (on doit pouvoir signaler un problème sans abonnement).
_LIC_READONLY_ALLOW_POST = {'/license/activate', '/license/activate-key', '/license/refresh', '/report', '/terms/accept',
                            '/uninstall/run', '/store/uninstall', '/store/start', '/store/access', '/store/wake', '/store/idle',
                            '/dkbackup/run', '/dkbackup/settings', '/dkbackup/delete', '/dkbackup/restore', '/dkbackup/restore-local'}


# ── Licence d'utilisation (LICENSE.md) : l'accord de l'installation ─────────────────────────────
# À ne pas confondre avec l'abonnement (« /license/… », ci-dessus). Ici, il s'agit de l'accord donné
# au texte de LICENSE.md : les installeurs le recueillent avant d'installer et l'inscrivent dans
# data/licence-acceptee.json ; le bureau le demande à la première connexion s'il manque (installation
# antérieure, fichiers copiés à la main) ou si le texte a changé de version.
# TERMS_VERSION suit le numéro de version écrit en tête de LICENSE.md : les deux changent ensemble.
TERMS_VERSION = '1.3'
TERMS_FILE = os.path.join(ACCESS_DATA_DIR, 'licence-acceptee.json')


def _terms_read():
    try:
        with open(TERMS_FILE, 'r', encoding='utf-8') as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _terms_status():
    rec = _terms_read()
    ok = str(rec.get('version') or '') == TERMS_VERSION and bool(rec.get('accepted_at'))
    return {'version': TERMS_VERSION, 'accepted': ok,
            'accepted_at': rec.get('accepted_at') if ok else None,
            'by': rec.get('by') if ok else None, 'via': rec.get('via') if ok else None}


def _terms_accept(b):
    b = b if isinstance(b, dict) else {}
    if b.get('accept') is not True:
        raise ValueError("L'accord doit être donné explicitement.")
    if str(b.get('version') or '') != TERMS_VERSION:
        raise ValueError("La licence a changé de version : recharge la page pour lire la bonne.")
    old = _terms_read()
    hist = old.get('history') if isinstance(old.get('history'), list) else []
    if old.get('accepted_at'):
        hist = (hist + [{k: old.get(k) for k in ('version', 'accepted_at', 'by', 'via')}])[-20:]
    rec = {'version': TERMS_VERSION,
           'accepted_at': _login_time.strftime('%Y-%m-%dT%H:%M:%SZ', _login_time.gmtime()),
           'by': re.sub(r'[^\w .@-]', '', str(b.get('by') or ''))[:64],
           'via': 'bureau', 'app_version': APP_VERSION, 'history': hist}
    os.makedirs(ACCESS_DATA_DIR, exist_ok=True)
    tmp = TERMS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, TERMS_FILE)
    return _terms_status()


# ── Signalements : bug, idée ou question, envoyés depuis le bureau ───────────
# Le bureau dépose le signalement ici ; le service y ajoute sa version et l'identifiant d'installation, puis le
# relaie à desktroo.fr (même serveur que les licences), qui le transmet par e-mail. Passer par le service évite au
# navigateur d'appeler un autre site, et marche même quand le poste n'a pas d'accès direct à Internet.
REPORT_URL = os.environ.get('REPORT_URL') or (LICENSE_SERVER + '/report')
REPORT_MAX_BYTES = 6 * 1024 * 1024
_REPORT_CATEGORIES = ('bug', 'idea', 'question')


def _report_clean(v, depth=0):
    """Informations techniques : textes, nombres et listes courtes seulement, bornés en taille."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return v[:600]
    if depth >= 2:
        return str(v)[:200]
    if isinstance(v, (list, tuple)):
        return [_report_clean(x, depth + 1) for x in list(v)[:30]]
    if isinstance(v, dict):
        return {str(k)[:40]: _report_clean(x, depth + 1) for k, x in list(v.items())[:30]}
    return str(v)[:200]


def _report_send(b):
    import platform
    import urllib.error
    import urllib.request
    b = b if isinstance(b, dict) else {}
    message = str(b.get('message') or '').strip()
    if len(message) < 10:
        raise ValueError('Décrivez le problème en quelques mots.')
    category = str(b.get('category') or 'bug')
    if category not in _REPORT_CATEGORIES:
        category = 'bug'
    images = [x for x in (b.get('images') or []) if isinstance(x, str) and x.startswith('data:image/')][:3]
    try:
        lic = license_status()
    except Exception:
        lic = {}
    payload = {
        'category': category,
        'message': message[:5000],
        'contact': str(b.get('contact') or '').strip()[:200],
        'diag': _report_clean(b.get('diag') if isinstance(b.get('diag'), dict) else {}),
        'images': images,
        'service': {'version': APP_VERSION, 'install_id': str(lic.get('install_id') or ''),
                    'license': str(lic.get('state') or ''), 'plan': str(lic.get('plan') or ''),
                    'python': platform.python_version()},
    }
    data = json.dumps(payload).encode('utf-8')
    if len(data) > REPORT_MAX_BYTES:
        raise ValueError('Signalement trop volumineux : retirez une capture.')
    req = urllib.request.Request(REPORT_URL, data=data, method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'Desktroo/' + APP_VERSION})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            j = json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode('utf-8', 'replace'))
        except Exception:
            j = None
        if isinstance(j, dict) and j.get('code') == 'rest_no_route':
            raise RuntimeError("La réception des signalements n'est pas encore en place sur desktroo.fr.")
        why = (j.get('message') or j.get('error')) if isinstance(j, dict) else None
        raise RuntimeError("desktroo.fr n'a pas pris le signalement (%s)." % (why or 'HTTP %s' % e.code))
    except Exception as e:
        raise RuntimeError('desktroo.fr est injoignable depuis le NAS (%s).' % e)
    if not (isinstance(j, dict) and j.get('ok')):
        raise RuntimeError("desktroo.fr n'a pas pris le signalement (%s)."
                           % ((j.get('message') or j.get('error') or 'réponse inattendue') if isinstance(j, dict) else 'réponse inattendue'))
    return {'ok': True, 'id': j.get('id')}


# ── MDM-HA-V1 : lampes et prises de Home Assistant sur le bureau ─────────────
# Le bureau ne parle jamais à Home Assistant : c'est ce service qui le fait, avec un jeton d'accès
# que l'utilisateur saisit une fois et qui reste ici (fichier lisible du seul service). Le jeton n'est
# jamais renvoyé au navigateur. Seuls les appareils choisis comme favoris peuvent être commandés.
import socket as _ha_socket
import time as _ha_time
from concurrent.futures import ThreadPoolExecutor as _HaPool

HA_FILE = os.path.join(ACCESS_DATA_DIR, 'homeassistant.json')
_ha_lock = _threading.RLock()
_HA_ENTITY = re.compile(r'^(light|switch)\.[a-z0-9_]{1,200}$')
_HA_MAX = 24
_HA_CACHE = {'t': 0.0, 'v': None}
_HA_ERRORS = {
    'unconfigured': "Home Assistant n'est pas relié.",
    'auth': 'Home Assistant refuse le jeton.',
    'unreachable': 'Home Assistant ne répond pas à cette adresse.',
    'not_ha': "Cette adresse ne répond pas comme un Home Assistant.",
    'tls': 'Le certificat de Home Assistant est refusé.',
    'http': 'Home Assistant a renvoyé une erreur.',
    'missing': 'Home Assistant ne connaît pas cet appareil.',
}


class _HaError(Exception):
    def __init__(self, code, detail=''):
        Exception.__init__(self, _HA_ERRORS.get(code, code))
        self.code, self.detail = code, str(detail or '')[:200]


def _ha_fail(e):
    """Réponse d'échec lisible par le bureau : un code qu'il traduit, et le détail technique."""
    if isinstance(e, _HaError):
        return {'ok': False, 'code': e.code, 'error': str(e), 'detail': e.detail}
    return {'ok': False, 'code': 'http', 'error': _HA_ERRORS['http'], 'detail': str(e)[:200]}


def _ha_read():
    with _ha_lock:
        d = _access_read_json(HA_FILE, {})
        d = d if isinstance(d, dict) else {}
    ents = [e for e in (d.get('entities') or []) if isinstance(e, str) and _HA_ENTITY.match(e)]
    return {'url': str(d.get('url') or ''), 'token': str(d.get('token') or ''), 'entities': ents[:_HA_MAX]}


def _ha_save(cfg):
    with _ha_lock:
        _access_write_json(HA_FILE, {'url': cfg.get('url') or '', 'token': cfg.get('token') or '',
                                     'entities': list(cfg.get('entities') or [])[:_HA_MAX]})
        _HA_CACHE['v'] = None


def _ha_public(cfg=None):
    cfg = cfg or _ha_read()
    return {'ok': True, 'configured': bool(cfg['url'] and cfg['token']), 'url': cfg['url'],
            'has_token': bool(cfg['token']), 'entities': cfg['entities'], 'max': _HA_MAX}


def _ha_clean_url(value):
    url = str(value or '').strip().rstrip('/')
    if url and '://' not in url:
        url = 'http://' + url
    p = urlparse(url)
    if p.scheme not in ('http', 'https') or not p.hostname or len(url) > 200:
        raise ValueError("Adresse attendue sous la forme http://adresse:8123")
    if p.username or p.password or p.query or p.fragment:
        raise ValueError("L'adresse ne doit contenir ni identifiant ni paramètre.")
    return p.scheme + '://' + p.netloc + p.path.rstrip('/')


def _ha_http(cfg, method, path, body=None, timeout=8):
    import urllib.request, urllib.error, ssl
    if not (cfg.get('url') and cfg.get('token')):
        raise _HaError('unconfigured')

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):   # le jeton ne suit jamais une redirection vers une autre adresse
            return None

    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(cfg['url'] + path, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + cfg['token'])
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'Desktroo')
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            raw = r.read(32 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise _HaError('auth', 'HTTP %d' % e.code)
        if e.code == 404:
            raise _HaError('missing', 'HTTP 404')
        raise _HaError('not_ha' if 300 <= e.code < 400 else 'http', 'HTTP %d' % e.code)
    except urllib.error.URLError as e:
        if isinstance(getattr(e, 'reason', None), ssl.SSLError):
            raise _HaError('tls', e.reason)
        raise _HaError('unreachable', getattr(e, 'reason', e))
    except (_ha_socket.timeout, OSError) as e:
        raise _HaError('unreachable', e)
    try:
        return json.loads(raw.decode('utf-8', 'replace') or 'null')
    except ValueError:
        raise _HaError('not_ha', 'réponse illisible')


def _ha_item(st, eid=None):
    st = st if isinstance(st, dict) else {}
    eid = str(st.get('entity_id') or eid or '')
    attrs = st.get('attributes') if isinstance(st.get('attributes'), dict) else {}
    state = str(st.get('state') or 'unavailable')
    return {'id': eid, 'domain': eid.split('.', 1)[0], 'name': str(attrs.get('friendly_name') or eid)[:80],
            'state': state if state in ('on', 'off') else 'unavailable'}


def _ha_test(cfg):
    try:
        res = _ha_http(cfg, 'GET', '/api/')
    except _HaError as e:
        if e.code == 'missing':
            raise _HaError('not_ha', e.detail)
        raise
    if not isinstance(res, dict) or 'message' not in res:
        raise _HaError('not_ha', 'réponse inattendue')
    return True


def _ha_connect(body):
    """Enregistre l'adresse et le jeton, une fois la liaison vérifiée.

    Sans jeton saisi, celui déjà gardé sert — y compris quand l'adresse change.
    C'est le cas courant : le même Home Assistant passe d'une IP locale à un nom de
    domaine derrière un reverse proxy, et le jeton, lui, ne bouge pas. L'ancienne
    version refusait ce changement et obligeait à recréer un jeton sans raison.
    Reconduire le jeton n'ouvre rien : seul l'administrateur du NAS modifie cette
    adresse, et c'est lui qui a créé ce jeton. Si l'adresse désigne un autre Home
    Assistant, _ha_test échoue en 401 et l'erreur le dit.
    """
    cur = _ha_read()
    url = _ha_clean_url(body.get('url'))
    typed = str(body.get('token') or '').strip()
    token = typed or cur['token']
    if not token:
        raise ValueError("Jeton d'accès attendu.")
    if len(token) > 2000 or any(ch.isspace() for ch in token):
        raise ValueError("Jeton d'accès invalide : il ne doit contenir ni espace ni retour à la ligne.")
    # Jeton reconduit, ou adresse inchangée : c'est le même Home Assistant, les favoris restent.
    same_ha = (not typed) or url == cur['url']
    cfg = {'url': url, 'token': token, 'entities': cur['entities'] if same_ha else []}
    _ha_test(cfg)
    _ha_save(cfg)
    return _ha_public(cfg)


def _ha_set_favorites(ids):
    cfg = _ha_read()
    out = []
    for e in (ids if isinstance(ids, list) else []):
        if isinstance(e, str) and _HA_ENTITY.match(e) and e not in out:
            out.append(e)
    if len(out) > _HA_MAX:
        raise ValueError('%d appareils au plus.' % _HA_MAX)
    cfg['entities'] = out
    _ha_save(cfg)
    return _ha_public(cfg)


def _ha_entities():
    """Toutes les lampes et prises que Home Assistant connaît, pour le choix des favoris."""
    states = _ha_http(_ha_read(), 'GET', '/api/states', timeout=15)
    items = [_ha_item(s) for s in (states if isinstance(states, list) else [])
             if isinstance(s, dict) and _HA_ENTITY.match(str(s.get('entity_id') or ''))]
    items.sort(key=lambda x: (x['domain'], x['name'].lower()))
    return {'ok': True, 'entities': items}


def _ha_states():
    cfg = _ha_read()
    if not (cfg['url'] and cfg['token']):
        raise _HaError('unconfigured')
    now = _ha_time.time()
    with _ha_lock:
        hit = _HA_CACHE['v']
        if hit is not None and now - _HA_CACHE['t'] < 1.5:
            return hit

    def one(eid):
        try:
            return _ha_item(_ha_http(cfg, 'GET', '/api/states/' + eid, timeout=6), eid)
        except _HaError as e:
            if e.code == 'missing':   # appareil retiré de Home Assistant : il reste affiché, indisponible
                return _ha_item({}, eid)
            raise

    if cfg['entities']:
        with _HaPool(max_workers=6) as pool:
            items = list(pool.map(one, cfg['entities']))
    else:
        _ha_test(cfg)
        items = []
    res = {'ok': True, 'items': items, 'url': cfg['url']}
    with _ha_lock:
        _HA_CACHE['t'], _HA_CACHE['v'] = _ha_time.time(), res
    return res


def _ha_switch(eid, on):
    cfg = _ha_read()
    eid = str(eid or '')
    if eid not in cfg['entities']:
        raise ValueError("Cet appareil ne fait pas partie des favoris.")
    domain = eid.split('.', 1)[0]
    changed = _ha_http(cfg, 'POST', '/api/services/%s/%s' % (domain, 'turn_on' if on else 'turn_off'), {'entity_id': eid})
    with _ha_lock:
        _HA_CACHE['v'] = None
    item = None
    for st in (changed if isinstance(changed, list) else []):
        if isinstance(st, dict) and st.get('entity_id') == eid:
            item = _ha_item(st)
    return {'ok': True, 'item': item}


# ── MDM-MEMORY-V1 : qui occupe la mémoire du NAS ─────────────────────────────
# Résumé (jauge du bureau) : /proc/meminfo et les compteurs du cache ZFS se lisent d'ici, le conteneur
# voit ceux de l'hôte. Détail (Informations système) : la liste des processus, elle, se lit sur le NAS,
# par SSH. Les lignes de commande restent sur le NAS : elles peuvent contenir des mots de passe ; seul
# le nom du programme en sort (et, pour un interpréteur, le nom du script lancé).
import time as _mem_time
import threading as _mem_threading

_MEM_LOCK = _mem_threading.Lock()
_MEM_CACHE = {}
_MEM_TOP = 30

_MEM_HOST_PY = r'''
import json, os, re
def rd(p):
    try:
        with open(p, 'rb') as f:
            return f.read().decode('utf-8', 'replace')
    except Exception:
        return ''
SH = re.compile(r'^(python[0-9.]*|node|nodejs|java|perl|ruby|php[0-9.]*|sh|bash|dash|zsh|busybox)$')
ps = []
for d in os.listdir('/proc'):
    if not d.isdigit():
        continue
    st = rd('/proc/' + d + '/status')
    m = re.search(r'^VmRSS:\s+(\d+)', st, re.M)
    if not m:
        continue
    n = re.search(r'^Name:\s+(.*)$', st, re.M)
    comm = n.group(1).strip() if n else ''
    argv = [a for a in rd('/proc/' + d + '/cmdline').split('\0') if a]
    label, vm, alloc = comm, '', 0
    if argv:
        first = argv[0]
        exe = os.path.basename(first.split(' ')[0]) if '/' in first else first
        label = exe or comm
        if exe.startswith('qemu-system') or comm.startswith('qemu-system'):
            line = ' '.join(argv)
            g = re.search(r'-name\s+(?:guest=)?([^,\s]+)', line)
            vm = g.group(1) if g else 'qemu'
            a = re.search(r'\s-m\s+(?:size=)?(\d+)([kKmMgG]?)', line)
            if a:
                alloc = int(a.group(1)) * {'k': 1024, 'm': 1048576, 'g': 1073741824}.get(a.group(2).lower(), 1048576)
        elif SH.match(exe):
            skip = False
            for a in argv[1:]:
                if skip:
                    skip = False
                    continue
                if a in ('-c', '-e', '-m'):
                    skip = a != '-m'
                    continue
                if a.startswith('-'):
                    continue
                if ' ' not in a and len(a) < 120:
                    label = exe + ' ' + os.path.basename(a)
                break
    cg = rd('/proc/' + d + '/cgroup')
    c = re.search(r'docker[-/]([0-9a-f]{12,64})', cg)
    x = re.search(r'lxc\.payload\.([^/\s]+)', cg)
    cid = c.group(1) if c else ('lxc:' + x.group(1) if x else '')
    ps.append([int(d), int(m.group(1)) * 1024, label[:60], cid, vm[:60], alloc])
print(json.dumps({'meminfo': rd('/proc/meminfo'), 'arc': rd('/proc/spl/kstat/zfs/arcstats'), 'ps': ps}))
'''


def _mem_file(path):
    try:
        with open(path, 'rb') as f:
            return f.read().decode('utf-8', 'replace')
    except Exception:
        return ''


def _mem_meminfo(text):
    out = {}
    for line in str(text or '').splitlines():
        parts = line.replace(':', ' ').split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1]) * 1024
    return out


def _mem_arc(text):
    out = {}
    for line in str(text or '').splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] in ('size', 'c_min', 'c_max') and parts[2].isdigit():
            out[parts[0]] = int(parts[2])
    return out


def _mem_totals(mi, arc):
    """Répartition de la mémoire : prise (applications et système), cache de fichiers, cache ZFS, libre.
    « arc » vaut None quand le cache ZFS n'a pas pu être lu : la page n'affiche alors pas de répartition."""
    total = int(mi.get('MemTotal') or 0)
    if total <= 0:
        raise RuntimeError('mémoire du NAS illisible')
    free = min(total, int(mi.get('MemFree') or 0))
    known = bool(arc) and 'size' in arc
    arc_size = min(int(arc.get('size') or 0), total - free) if known else 0
    cache = max(0, int(mi.get('Buffers') or 0) + int(mi.get('Cached') or 0) - int(mi.get('Shmem') or 0))
    cache = min(cache, total - free - arc_size)
    swap_total = int(mi.get('SwapTotal') or 0)
    return {
        'total': total, 'free': free, 'available': int(mi.get('MemAvailable') or 0),
        'arc': arc_size if known else None,
        'arc_min': int(arc.get('c_min') or 0) if known else 0, 'arc_max': int(arc.get('c_max') or 0) if known else 0,
        'file_cache': cache, 'used': max(0, total - free - arc_size - cache),
        'swap_total': swap_total, 'swap_used': max(0, swap_total - int(mi.get('SwapFree') or 0)),
    }


def _mem_cached(key, ttl, make):
    now = _mem_time.time()
    with _MEM_LOCK:
        hit = _MEM_CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = make()
    with _MEM_LOCK:
        _MEM_CACHE[key] = (_mem_time.time(), value)
    return value


def _mem_summary():
    def make():
        mi = _mem_meminfo(_mem_file('/proc/meminfo'))
        arc = _mem_arc(_mem_file('/proc/spl/kstat/zfs/arcstats'))
        with _MEM_LOCK:
            down = _MEM_CACHE.get('ssh_down') or 0
        if (not mi or not arc) and _mem_time.time() - down > 120:
            # Compteurs absents d'ici : on les lit sur le NAS. Sans réponse, la jauge se passera de la répartition,
            # et on ne redemande pas avant deux minutes pour ne pas faire attendre le bureau à chaque passage.
            try:
                out = ssh_ok("sh -c 'cat /proc/meminfo; echo @@ARC@@; cat /proc/spl/kstat/zfs/arcstats 2>/dev/null; true'", timeout=15)
                a, _sep, b = out.partition('@@ARC@@')
                mi = _mem_meminfo(a) or mi
                arc = _mem_arc(b)
            except Exception:
                with _MEM_LOCK:
                    _MEM_CACHE['ssh_down'] = _mem_time.time()
        res = _mem_totals(mi, arc if arc else None)
        res['ok'] = True
        return res
    return _mem_cached('summary', 5, make)


def _mem_app_name(project):
    p = str(project or '')
    return p[3:] if p.startswith('ix-') and len(p) > 3 else p


def _mem_overview():
    def make():
        raw = json.loads(ssh_ok('python3 -c ' + shq(_MEM_HOST_PY), timeout=40) or '{}')
        arc = _mem_arc(raw.get('arc'))
        res = _mem_totals(_mem_meminfo(raw.get('meminfo')), arc if arc else None)
        names, docker_error = {}, ''
        try:
            fmt = '{{.ID}}|{{.Names}}|{{.Label "com.docker.compose.project"}}'
            for line in ssh_ok(_ct_sh('$D ps -a --no-trunc --format ' + shq(fmt)), timeout=30).splitlines():
                cid, _s, rest = line.strip().partition('|')
                name, _s, project = rest.partition('|')
                if cid:
                    names[cid] = (name, project)
        except Exception as e:
            docker_error = str(e)

        def docker_info(cid):
            if cid in names:
                return names[cid]
            for full, info in names.items():
                if full.startswith(cid) or cid.startswith(full):
                    return info
            return None

        groups = {}
        for row in raw.get('ps') or []:
            if not isinstance(row, list) or len(row) < 5:
                continue
            _pid, rss, label, cid, vm = row[:5]
            rss = int(rss or 0)
            alloc = int(row[5] or 0) if len(row) > 5 else 0
            part = ''
            if vm:
                key, kind, name = ('vm', vm), 'vm', vm
            elif str(cid).startswith('lxc:'):
                key, kind, name = ('container', cid), 'container', cid[4:]
            elif cid:
                info = docker_info(cid)
                if info and info[1]:
                    key, kind, name, part = ('app', info[1]), 'app', _mem_app_name(info[1]), info[0]
                else:
                    key, kind, name = ('container', cid), 'container', (info[0] if info else cid[:12])
            else:
                key, kind, name = ('process', label), 'process', label or '?'
            g = groups.setdefault(key, {'kind': kind, 'name': name, 'mem': 0, 'count': 0, 'parts': set(), 'alloc': 0})
            g['mem'] += rss
            g['alloc'] += alloc
            g['count'] += 1
            if part:
                g['parts'].add(part)

        items = []
        for g in groups.values():
            n = len(g['parts']) if g['kind'] == 'app' else g['count']
            item = {'kind': g['kind'], 'name': g['name'], 'mem': g['mem'], 'count': n}
            if g['alloc']:
                item['alloc'] = g['alloc']   # machine virtuelle : la mémoire qui lui a été donnée au démarrage
            items.append(item)
        items.sort(key=lambda x: -x['mem'])
        apps = sum(x['mem'] for x in items if x['kind'] in ('app', 'container'))
        vms = sum(x['mem'] for x in items if x['kind'] == 'vm')
        # La mémoire d'un processus compte aussi les pages qu'il partage : la somme peut dépasser ce qui est pris.
        used = res['used']
        if apps + vms > used and apps + vms > 0:
            k = used / float(apps + vms)
            apps, vms = int(apps * k), int(vms * k)
        res.update({
            'ok': True, 'apps': apps, 'vms': vms, 'system': max(0, used - apps - vms),
            'consumers': items[:_MEM_TOP],
            'others': max(0, len(items) - _MEM_TOP), 'others_mem': sum(x['mem'] for x in items[_MEM_TOP:]),
            'docker_error': docker_error,
        })
        return res
    return _mem_cached('overview', 5, make)


# ── MDM-WALLPAPERS-V1 : dossier de fonds d'écran ─────────────────────────────
# Les images vivent dans <install>/wallpapers (volume hôte, donc conservées aux
# mises à jour). Le bureau les liste, les lit et en ajoute par les routes
# /wallpapers* ; on peut aussi y déposer des fichiers directement.
WALLPAPER_DIR = os.environ.get('WALLPAPER_DIR') or (os.path.join(APP_DIR, 'wallpapers') if APP_DIR else '')
WALLPAPER_MAX_BYTES = int(os.environ.get('WALLPAPER_MAX_BYTES', str(20 * 1024 * 1024)))
_WALLPAPER_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.webp': 'image/webp', '.avif': 'image/avif', '.gif': 'image/gif',
}


def _wallpaper_dir(create=False):
    """Chemin réel du dossier de fonds d'écran ; le crée à la demande."""
    if not WALLPAPER_DIR:
        raise RuntimeError("Dossier de fonds d'écran indisponible (APP_DIR non défini).")
    if create:
        os.makedirs(WALLPAPER_DIR, exist_ok=True)
    return os.path.realpath(WALLPAPER_DIR)


def _wallpaper_name(name):
    """Valide un nom de fichier : pas de sous-dossier, pas de fichier caché, image connue."""
    name = os.path.basename(str(name or '').replace('\\', '/')).strip()
    if not name or name.startswith('.') or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError('Nom de fichier invalide.')
    if os.path.splitext(name)[1].lower() not in _WALLPAPER_TYPES:
        raise ValueError('Format non pris en charge : JPG, PNG, WebP, AVIF ou GIF.')
    return name


def _wallpaper_path(name):
    """Chemin d'une image existante du dossier, sans jamais en sortir (liens compris)."""
    base = _wallpaper_dir()
    full = os.path.realpath(os.path.join(base, _wallpaper_name(name)))
    if os.path.dirname(full) != base or not os.path.isfile(full):
        raise FileNotFoundError('Image introuvable.')
    return full


def _wallpaper_item(full):
    st = os.stat(full)
    return {'name': os.path.basename(full), 'size': st.st_size, 'mtime': int(st.st_mtime)}


def _wallpaper_list():
    base = _wallpaper_dir(create=True)
    items = []
    for fn in os.listdir(base):
        full = os.path.join(base, fn)
        if fn.startswith('.') or os.path.splitext(fn)[1].lower() not in _WALLPAPER_TYPES:
            continue
        # isfile suit les liens : on écarte ceux qui pointent hors du dossier.
        if os.path.isfile(full) and os.path.dirname(os.path.realpath(full)) == base:
            items.append(_wallpaper_item(full))
    items.sort(key=lambda it: it['name'].lower())
    return {'ok': True, 'dir': WALLPAPER_DIR, 'max_bytes': WALLPAPER_MAX_BYTES, 'items': items}


def _wallpaper_free_name(base, name):
    """Évite d'écraser une image existante : photo.jpg → photo-2.jpg."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while os.path.exists(os.path.join(base, candidate)):
        candidate = '%s-%d%s' % (stem, n, ext)
        n += 1
    return candidate


# ── MDM-LANGPACKS-V1 : langues ajoutées ──────────────────────────────────────
# Une langue ajoutée est un fichier <install>/lang/<code>.json : un objet dont les clés sont
# les textes français de Desktroo et les valeurs leur traduction, plus une entrée
# « _desktroo » qui la décrit (code, name, locale, version, author). Un texte absent garde
# l'anglais. La liste des textes à traduire est le dictionnaire anglais de desktroo.html
# (bloc dk-i18n-en). Lire la liste et un fichier ne demande pas de jeton : l'écran de
# connexion en a besoin. Importer et retirer en demandent un.
LANG_DIR = os.environ.get('LANG_DIR') or (os.path.join(APP_DIR, 'lang') if APP_DIR else '')
LANG_MAX_BYTES = 4 * 1024 * 1024
LANG_MAX_TEXT = 4000
_LP_CODE_RE = re.compile(r'^[a-z]{2,3}(-[a-z0-9]{2,8})?$')
_LP_LOCALE_RE = re.compile(r'^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$')
_LP_TAGS = ('b', 'strong', 'i', 'em', 'u', 'code', 'kbd', 'small', 'sub', 'sup', 'mark', 'br')
_LP_TAG_RE = re.compile(r'<\s*(/?)\s*([A-Za-z][A-Za-z0-9]*)\b[^<>]*>')
_LP_KEEP_RE = re.compile(r'(</?(?:b|strong|i|em|u|code|kbd|small|sub|sup|mark)>|<br>)')
_LP_AMP_RE = re.compile(r'&(?!(?:amp|lt|gt|nbsp|hellip|mdash|ndash|laquo|raquo|thinsp);)')
_LP_HOLE_RE = re.compile(r'\{\d\}')
_lp_base_cache = {'key': None, 'd': {}}
_lp_stat_cache = {}


def _lp_clean(v):
    """Rend un texte sûr partout où le bureau l'insère (HTML, attributs, onclick) : guillemets
    typographiques, balises de mise en forme nues seulement, entités courantes seulement."""
    v = ''.join(c for c in str(v) if c in '\n\t\x1b' or ord(c) >= 32)   # ESC : couleurs du terminal
    v = re.sub(r'\\(?!n)', '＼', v.replace('"', '”').replace("'", '’'))   # « \n » des clés gardé

    def tag(m):
        t = m.group(2).lower()
        if t not in _LP_TAGS:
            return ''
        return '<br>' if t == 'br' else '<%s%s>' % (m.group(1), t)
    v = _LP_TAG_RE.sub(tag, v)
    parts = _LP_KEEP_RE.split(v)
    for i in range(0, len(parts), 2):
        parts[i] = _LP_AMP_RE.sub('&amp;', parts[i]).replace('<', '&lt;').replace('>', '&gt;')
    return ''.join(parts)[:LANG_MAX_TEXT]


def _lp_base():
    """Dictionnaire anglais de référence (clé française → anglais), relu si desktroo.html change."""
    p = os.path.join(APP_DIR, 'desktroo.html') if APP_DIR else ''
    try:
        st = os.stat(p)
    except OSError:
        return {}
    key = (st.st_mtime, st.st_size)
    if _lp_base_cache['key'] != key:
        d = {}
        try:
            with open(p, encoding='utf-8') as fh:
                m = re.search(r'<script type="application/json" id="dk-i18n-en">(.*?)</script>', fh.read(), re.S)
            if m:
                d = json.loads(m.group(1))
        except Exception as e:
            log.warning('langues : dictionnaire anglais illisible : %s', e)
        _lp_base_cache.update(key=key, d=d if isinstance(d, dict) else {})
    return _lp_base_cache['d']


def _lp_code(code):
    code = str(code or '').strip().lower().replace('_', '-')
    if not _LP_CODE_RE.match(code):
        raise ValueError('Code de langue invalide : deux ou trois lettres, éventuellement suivies d\'une variante (« de », « pt-br »).')
    if code in ('fr', 'en'):
        raise ValueError('Le français et l\'anglais sont fournis avec Desktroo : choisissez le code d\'une autre langue.')
    return code


def _lp_path(code):
    if not LANG_DIR:
        raise RuntimeError('Dossier des langues indisponible (APP_DIR non défini).')
    return os.path.join(LANG_DIR, _lp_code(code) + '.json')


def _lp_parse(data):
    """Valide et nettoie un fichier de langue. Rend (meta, textes, rapport)."""
    if not isinstance(data, dict):
        raise ValueError('Le fichier doit contenir un objet JSON { "texte français": "traduction", … }.')
    meta = data.get('_desktroo')
    if not isinstance(meta, dict):
        raise ValueError('Entrée « _desktroo » absente : partez du modèle à traduire, qui la contient.')
    code = _lp_code(meta.get('code'))
    name = ' '.join(str(meta.get('name') or '').split())
    if not name or len(name) > 40:
        raise ValueError('Donnez à la langue un nom de 1 à 40 caractères (« name », par exemple « Deutsch »).')
    loc = str(meta.get('locale') or '').strip()
    clean_meta = {'code': code, 'name': _lp_clean(name), 'locale': loc if _LP_LOCALE_RE.match(loc) else code,
                  'version': _lp_clean(str(meta.get('version') or ''))[:20], 'author': _lp_clean(str(meta.get('author') or ''))[:120]}
    base = _lp_base()
    out = {}
    rep = {'translated': 0, 'total': len(base), 'unchanged': 0, 'empty': 0, 'ignored': 0, 'unknown': 0, 'cleaned': 0, 'holes': []}
    for k, v in data.items():
        if k == '_desktroo' or k.startswith('_'):
            continue
        if not isinstance(v, str) or not k.strip():
            rep['ignored'] += 1
            continue
        if not v.strip():
            rep['empty'] += 1
            continue
        c = _lp_clean(v)
        if c != v:
            rep['cleaned'] += 1
        if k in base and c == base[k]:
            rep['unchanged'] += 1   # encore en anglais : inutile de le garder, l'anglais s'affiche déjà
            continue
        if k not in base:
            rep['unknown'] += 1
        elif sorted(_LP_HOLE_RE.findall(k)) != sorted(_LP_HOLE_RE.findall(c)) and len(rep['holes']) < 20:
            rep['holes'].append(k)
        out[k] = c
    rep['translated'] = sum(1 for k in out if k in base)
    return clean_meta, out, rep


def _lp_read(code):
    with open(_lp_path(code), encoding='utf-8') as fh:
        return _lp_parse(json.load(fh))


def lang_list():
    langs = []
    total = len(_lp_base())
    if LANG_DIR and os.path.isdir(LANG_DIR):
        for fn in sorted(os.listdir(LANG_DIR)):
            if not fn.endswith('.json') or fn.startswith('.'):
                continue
            full = os.path.join(LANG_DIR, fn)
            try:
                st = os.stat(full)
                key = (fn, st.st_mtime, st.st_size, total)
                item = _lp_stat_cache.get(fn)
                if not item or item[0] != key:
                    meta, _texts, rep = _lp_read(fn[:-5])
                    if meta['code'] != fn[:-5]:
                        raise ValueError('code « %s » différent du nom du fichier' % meta['code'])
                    item = (key, dict(meta, translated=rep['translated'], unknown=rep['unknown'], mtime=int(st.st_mtime)))
                    _lp_stat_cache[fn] = item
                langs.append(item[1])
            except Exception as e:
                log.warning('langues : %s ignoré : %s', fn, e)
    return {'ok': True, 'total': total, 'langs': langs}


def lang_import(body):
    raw = body.get('content')
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('Fichier vide.')
    if len(raw.encode('utf-8')) > LANG_MAX_BYTES:
        raise ValueError('Fichier trop gros (4 Mo au plus).')
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError('Ce fichier n\'est pas du JSON valide : %s' % e)
    meta, texts, rep = _lp_parse(data)
    if not rep['translated']:
        raise ValueError('Aucun texte traduit dans ce fichier : chaque valeur est vide ou encore en anglais.')
    path = _lp_path(meta['code'])
    os.makedirs(LANG_DIR, exist_ok=True)
    existed = os.path.exists(path)
    # Par défaut, le fichier complète la langue déjà en place (travail par lots) ; « replace » repart de zéro.
    merge = existed and str(body.get('mode') or 'merge') != 'replace'
    added, changed = len(texts), 0
    if merge:
        try:
            old_meta, old_texts, _r = _lp_read(meta['code'])
        except Exception:
            old_meta, old_texts = {}, {}
        added = sum(1 for k in texts if k not in old_texts)
        changed = sum(1 for k in texts if k in old_texts and old_texts[k] != texts[k])
        merged = dict(old_texts)
        merged.update(texts)
        texts = merged
        for f in ('author', 'version'):
            if not meta.get(f):
                meta[f] = old_meta.get(f, '')
        if meta.get('locale') == meta['code'] and old_meta.get('locale'):
            meta['locale'] = old_meta['locale']
    base = _lp_base()
    rep['translated'] = sum(1 for k in texts if k in base)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(dict({'_desktroo': meta}, **texts), fh, ensure_ascii=False, indent=1)
        fh.write('\n')
    os.replace(tmp, path)
    _lp_stat_cache.pop(meta['code'] + '.json', None)
    return dict(rep, ok=True, code=meta['code'], name=meta['name'], replaced=existed, merged=merge, added=added, changed=changed)


def lang_delete(code):
    path = _lp_path(code)
    if not os.path.exists(path):
        raise FileNotFoundError('Langue introuvable.')
    os.unlink(path)
    _lp_stat_cache.pop(os.path.basename(path), None)
    return {'ok': True}


def _lang_serve(handler, code):
    """GET /lang/<code>.json : le fichier nettoyé, revalidé par le navigateur à chaque ouverture."""
    try:
        full = _lp_path(code)
        st = os.stat(full)
    except (ValueError, RuntimeError, OSError):
        return handler._json(404, {'error': 'Langue introuvable.'})
    etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
    if handler.headers.get('If-None-Match') == etag:
        handler.send_response(304)
        handler.send_header('ETag', etag)
        handler.end_headers()
        return
    try:
        meta, texts, _rep = _lp_read(code)
    except Exception as e:
        return handler._json(422, {'error': 'Fichier de langue illisible : %s' % e})
    body = json.dumps(dict({'_desktroo': meta}, **texts), ensure_ascii=False).encode('utf-8')
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-cache')
    handler.send_header('ETag', etag)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


# ── MDM-STORE-V1 : Magasin d'apps Desktroo ───────────────────────────────────────────────────────
# Phase 0 : session des apps (cookie dt_app), vérification pour nginx (/store/auth), kit des apps (SDK),
# catalogue signé, réseau Docker partagé et bloc nginx /a/<id>/. Plan : « Plan — Magasin d'apps Desktroo ».
import hashlib as _st_hashlib, hmac as _st_hmac, base64 as _st_b64, time as _st_time

STORE_URL        = (os.environ.get('STORE_URL') or 'https://raw.githubusercontent.com/Nabief/desktroo-store/main').rstrip('/')
STORE_NET        = 'desktroo-apps'          # réseau partagé entre le nginx du bureau et les apps (aucun port publié)
STORE_APP_PORT   = 8080                     # port interne imposé à toutes les apps du Magasin
STORE_DIR        = os.path.join(ACCESS_DATA_DIR, 'store')
STORE_CATALOG_CACHE = os.path.join(STORE_DIR, 'catalog.json')
STORE_ACCESS_FILE   = os.path.join(STORE_DIR, 'access.json')
STORE_REFRESH    = 43200                    # 12 h entre deux lectures du catalogue
STORE_COOKIE     = 'dt_app'
STORE_COOKIE_TTL = 12 * 3600
# Clé publique du catalogue (RSA-2048), distincte de la clé d'activation. Posée par tools/store/store-keygen.py.
STORE_PUBKEY_N_HEX = 'F512261EA90C6D026B2F558CB305C4364F46B03222F6BAD240549D0EC8ADA61E6286FCF45456657CBBC912F5B90108584A130D990179CD29EF697492DC70B504A2C225FD565BF58C65F6D9620A64416891670D4E7688DC4D1429E2513E0249A953782E8DC583E03874EE712086EAC82242DC3062557949CEF8F09415E11C128DA1B4E0B62933B39891E5D4AAC4576C9B6BCD77DA0967561FAE914E52A956326FD5107B46AAC22F58762051EBCE308662D9DE6B99097567CD65B1397B84F3123EBB90A0BE8DD4AB64D126350B9CFAC358D63CCA120503163667909CE73A8CD5ECB950F4F8C2D9D27540D68564252070500F5A3443000F2A42617F534CCF4057DD'
_STORE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')
_STORE_SECRET = _st_hmac.new(TOKEN.encode('utf-8'), b'desktroo-store-v1', _st_hashlib.sha256).digest()
_STORE_LOCK = _threading.Lock()
_store_state = {'checked': 0.0, 'error': ''}


def _store_b64e(b):
    return _st_b64.urlsafe_b64encode(b).decode('ascii').rstrip('=')


def _store_b64d(s):
    s = str(s or '').strip()
    return _st_b64.urlsafe_b64decode((s + '=' * (-len(s) % 4)).encode('ascii'))


def _store_sign(text):
    return _store_b64e(_st_hmac.new(_STORE_SECRET, text.encode('utf-8'), _st_hashlib.sha256).digest())


def store_cookie_make(user, ttl=None):
    """Valeur du cookie de session des apps : utilisateur + échéance, signés."""
    body = _store_b64e(json.dumps({'u': str(user or '')[:64], 'exp': int(_st_time.time()) + int(ttl or STORE_COOKIE_TTL)},
                                  separators=(',', ':')).encode('utf-8'))
    return body + '.' + _store_sign('cookie.' + body)


def store_cookie_header(handler, value, max_age):
    secure = '; Secure' if (handler.headers.get('X-Forwarded-Proto', '') or '').lower() == 'https' else ''
    return '%s=%s; Path=/a/; Max-Age=%d; HttpOnly; SameSite=Strict%s' % (STORE_COOKIE, value, int(max_age), secure)


def store_cookie_user(cookie_header):
    """Utilisateur porté par un cookie dt_app valide, sinon None. Lecture à la main : un autre cookie
    mal formé (portail d'authentification…) ne doit pas faire échouer la lecture."""
    val = ''
    for part in str(cookie_header or '').split(';'):
        k, _, v = part.strip().partition('=')
        if k == STORE_COOKIE:
            val = v.strip().strip('"')
    try:
        body, sig = val.split('.')
        if not _st_hmac.compare_digest(sig, _store_sign('cookie.' + body)):
            return None
        d = json.loads(_store_b64d(body).decode('utf-8'))
        if int(d.get('exp', 0)) < _st_time.time():
            return None
        return str(d.get('u') or '') or None
    except Exception:
        return None


def store_app_secret(app_id):
    """Secret propre à une app sur cette installation : posé dans son conteneur (DESKTROO_APP_SECRET)
    et transmis par nginx à chaque requête (X-Desktroo-Secret). Sans lui, l'app ne fait confiance à personne."""
    return _st_hmac.new(_STORE_SECRET, ('app.' + str(app_id)).encode('utf-8'), _st_hashlib.sha256).hexdigest()[:48]


def store_access_get():
    d = _lic_read_json(STORE_ACCESS_FILE)
    return d if isinstance(d, dict) else {}


# ── MDM-ACCESS-UNIFIED-V1 : un seul réglage d'accès aux apps, celui de « Droits d'accès » ─────────────────────────
# Une app de Desktroo Apps y porte le nom de son app TrueNAS (dt-<id>). Même règle que le bureau : le choix du compte
# (Autorisé / Refusé), sinon ceux de ses groupes (un refus l'emporte), sinon autorisé. Un nouveau compte a donc accès
# tant qu'on ne le lui refuse pas. Les anciennes listes « Qui peut l'utiliser » (data/store/access.json) sont reprises
# une fois, en refus explicites pour les administrateurs qui n'y figuraient pas : personne ne gagne ni ne perd d'accès.
_ACC_IDS = {'at': 0.0, 'fail_at': 0.0, 'users': {}, 'busy': False}
_ACC_IDS_LOCK = _threading.Lock()
_ACC_ALWAYS = ('root', 'truenas_admin')   # comptes propriétaires du NAS : toujours autorisés, comme sur le bureau


def _access_ids_load():
    users = _store_midclt('user.query', [], {'select': ['username', 'uid', 'groups', 'builtin', 'roles']}, timeout=60) or []
    groups = _store_midclt('group.query', [], {'select': ['id', 'gid']}, timeout=60) or []
    gids = {g['id']: int(g['gid']) for g in groups if isinstance(g, dict) and g.get('id') is not None and g.get('gid') is not None}
    rows = [u for u in users if isinstance(u, dict) and u.get('username') and u.get('uid') is not None]
    any_role = any(u.get('roles') for u in rows)
    out = {}
    for u in rows:
        admin = ('FULL_ADMIN' in (u.get('roles') or [])) if any_role else not u.get('builtin')
        out[str(u['username']).lower()] = {'username': str(u['username']), 'uid': int(u['uid']), 'builtin': bool(u.get('builtin')),
                                           'admin': admin, 'groups': [gids[i] for i in (u.get('groups') or []) if i in gids]}
    with _ACC_IDS_LOCK:
        _ACC_IDS.update(at=_st_time.time(), users=out)
    return out


def _access_ids():
    """Comptes du NAS (uid, groupes, administrateur ou non), relus au plus une fois par minute, en arrière-plan quand une copie
    existe déjà : le contrôle d'accès passe à chaque requête vers une app et ne doit pas attendre TrueNAS."""
    now = _st_time.time()
    with _ACC_IDS_LOCK:
        have = bool(_ACC_IDS['users'])
        refresh = have and now - _ACC_IDS['at'] > 60 and not _ACC_IDS['busy']
        if refresh:
            _ACC_IDS['busy'] = True
        recent_fail = now - _ACC_IDS['fail_at'] < 30
    if not have:
        if recent_fail:
            return {}
        try:
            return _access_ids_load()
        except Exception as e:
            with _ACC_IDS_LOCK:
                _ACC_IDS['fail_at'] = now
            log.warning('Droits d\'accès : comptes du NAS illisibles : %s', e)
            return {}
    if refresh:
        def run():
            try:
                _access_ids_load()
            except Exception as e:
                log.warning('Droits d\'accès : comptes du NAS illisibles : %s', e)
            finally:
                with _ACC_IDS_LOCK:
                    _ACC_IDS['busy'] = False
        _threading.Thread(target=run, name='access-ids', daemon=True).start()
    return _ACC_IDS['users']


def _access_app_key(app_id):
    return 'dt-' + str(app_id)


def access_app_effective(app_name, username, subjects=None, ids=None, direct=True):
    """ALLOW ou DENY, exactement comme le calcule la fenêtre Droits d'accès (acEffectiveApplication)."""
    u = str(username or '').lower()
    if u in _ACC_ALWAYS:
        return 'ALLOW'
    ids = _access_ids() if ids is None else ids
    info = ids.get(u)
    if not info:
        return 'ALLOW'
    if subjects is None:
        subjects = access_policy_get().get('subjects') or {}

    def mode(key):
        v = ((subjects.get(key) or {}).get('applications') or {}).get(app_name)
        return v if v in ('ALLOW', 'DENY') else None
    if direct:
        d = mode('user:%d' % info['uid'])
        if d:
            return d
    if 'DENY' in [mode('group:%d' % g) for g in info['groups']]:
        return 'DENY'
    return 'ALLOW'


def _store_access_migrate():
    """Reprise unique des listes « Qui peut l'utiliser ». Rend False si elle n'a pas encore pu se faire (comptes illisibles)."""
    if not os.path.exists(STORE_ACCESS_FILE):
        return True
    old = store_access_get()
    ids = _access_ids() if old else {}
    if old and not ids:
        return False
    n = 0
    if old:
        with _access_lock:
            policy = access_policy_get()
            subjects = policy.setdefault('subjects', {})
            for app_id, allowed in old.items():
                if not isinstance(allowed, list):
                    continue
                ok = {str(x).lower() for x in allowed}
                for u, info in ids.items():
                    if info['builtin'] or not info['admin'] or u in _ACC_ALWAYS or u in ok:
                        continue
                    apps = subjects.setdefault('user:%d' % info['uid'], {}).setdefault('applications', {})
                    if apps.get(_access_app_key(app_id)) not in ('ALLOW', 'DENY'):
                        apps[_access_app_key(app_id)] = 'DENY'
                        n += 1
            policy['updated_at'] = _access_datetime.now(_access_timezone.utc).isoformat()
            _access_write_json(ACCESS_POLICY_FILE, policy)
    try:
        os.replace(STORE_ACCESS_FILE, STORE_ACCESS_FILE + '.repris')
    except OSError as e:
        log.warning('Droits d\'accès : %s non renommé : %s', STORE_ACCESS_FILE, e)
        return True
    if old:
        access_history_add({'actor': 'Desktroo', 'action': "Reprise des accès de Desktroo Apps", 'subject': '',
                            'resource': ', '.join(sorted(old)), 'before': None, 'after': '%d refus' % n})
        log.info('Droits d\'accès : listes de Desktroo Apps reprises (%d refus explicites)', n)
    return True


def store_user_allowed(app_id, user):
    """Le compte peut-il ouvrir cette app de Desktroo Apps ? Décidé par Droits d'accès (MDM-ACCESS-UNIFIED-V1)."""
    if not _store_access_migrate():   # comptes du NAS illisibles avant la reprise : l'ancienne liste vaut encore
        acl = store_access_get().get(app_id)
        return acl is None or (isinstance(acl, list) and user in acl)
    return access_app_effective(_access_app_key(app_id), user) != 'DENY'


def store_access_users(app_id):
    """Administrateurs qui ont accès à l'app (pour sa fiche dans Desktroo Apps)."""
    _store_access_migrate()
    ids = _access_ids()
    subjects = access_policy_get().get('subjects') or {}
    key = _access_app_key(app_id)
    out = [i['username'] for u, i in ids.items() if (i['admin'] or u in _ACC_ALWAYS) and access_app_effective(key, u, subjects, ids) != 'DENY']
    return sorted(set(out + [u for u in _ACC_ALWAYS if u in ids]))


def _store_access_apply(app_id, allowed, actor='Desktroo'):
    """La fiche de l'app (« Qui peut l'utiliser ») écrit dans Droits d'accès : un choix explicite pour un compte seulement
    quand il diffère de ce que lui donnent ses groupes ; sinon le compte reste sur « Hérité »."""
    _store_access_migrate()
    ids = _access_ids()
    if not ids:
        raise RuntimeError('Comptes du NAS illisibles : réessayez dans un instant.')
    key, ok, changed = _access_app_key(app_id), {str(x).lower() for x in allowed or []}, []
    with _access_lock:
        policy = access_policy_get()
        subjects = policy.setdefault('subjects', {})
        for u, info in ids.items():
            if info['builtin'] or not info['admin'] or u in _ACC_ALWAYS:
                continue
            base = access_app_effective(key, u, subjects, ids, direct=False)
            want = 'ALLOW' if u in ok else 'DENY'
            skey = 'user:%d' % info['uid']
            entry = dict(subjects.get(skey) or {})
            apps = dict(entry.get('applications') or {})
            before = apps.get(key, 'INHERIT')
            if want == base:
                apps.pop(key, None)
            else:
                apps[key] = want
            if apps.get(key, 'INHERIT') != before:
                changed.append((skey, before, apps.get(key, 'INHERIT')))
            if apps:
                entry['applications'] = apps
                subjects[skey] = entry
            else:
                entry.pop('applications', None)
                if entry:
                    subjects[skey] = entry
                else:
                    subjects.pop(skey, None)
        policy['updated_at'] = _access_datetime.now(_access_timezone.utc).isoformat()
        _access_write_json(ACCESS_POLICY_FILE, policy)
    for skey, before, after in changed:
        access_history_add({'actor': actor, 'action': 'Accès à une app modifié depuis Desktroo Apps', 'subject': skey,
                            'resource': key, 'before': before, 'after': after})


def _store_access_forget(app_id):
    """App désinstallée : ses choix disparaissent de Droits d'accès."""
    key = _access_app_key(app_id)
    with _access_lock:
        policy = access_policy_get()
        subjects = policy.get('subjects') or {}
        for skey in list(subjects):
            apps = (subjects[skey] or {}).get('applications') or {}
            if key in apps:
                apps.pop(key)
                if not apps:
                    subjects[skey].pop('applications', None)
                    if not subjects[skey]:
                        subjects.pop(skey)
        _access_write_json(ACCESS_POLICY_FILE, policy)


def store_auth_check(handler):
    """GET /store/auth, appelé par nginx (auth_request) avant chaque requête vers /a/<id>/."""
    uri = handler.headers.get('X-Original-URI', '') or ''
    m = re.match(r'^/a/([a-z0-9][a-z0-9-]{0,40})(?:[/?]|$)', uri)
    if not m:
        return 403, None, None
    app = m.group(1)
    user = store_cookie_user(handler.headers.get('Cookie', ''))
    if not user:
        return 401, None, None
    if not store_user_allowed(app, user):
        return 403, None, None
    store_activity_touch(app)   # MDM-STORE-IDLE-V1
    return 200, user, store_app_secret(app)


def store_gate(action='install'):
    """LE contrôle du Magasin, à un seul endroit. MDM-STORE-KEY-V1 (3.1.0) : installer une app, relier ou reprendre une app
    existante et mettre à jour une app demandent la clé gratuite (un correctif de sécurité passe sans clé : voir
    _store_update_allowed) ; voir, ouvrir, relancer, régler et désinstaller restent toujours permis."""
    free_ok = action in ('view', 'open', 'start', 'uninstall')
    if LICENSE_FREE:
        reg = _lic_registered()
        return {'ok': bool(reg or free_ok), 'action': action, 'state': 'active', 'registered': reg,
                'need_key': not (reg or free_ok), 'free_key_url': FREE_KEY_URL}
    try:
        st = license_status()
    except Exception:
        st = {'state': 'expired'}
    state = st.get('state')
    ok = state in ('trial', 'active', 'grace') or free_ok
    return {'ok': bool(ok), 'action': action, 'state': state, 'days_left': st.get('days_left'),
            'subscribe_url': SUBSCRIBE_URL}


def _store_update_allowed(app, inst, reg=None):
    """Mise à jour d'une app permise ? Avec la clé gratuite, oui ; sans clé, seulement si le catalogue marque comme
    correctif de sécurité (« security » : version) une version plus récente que celle installée."""
    if not LICENSE_FREE:
        return store_gate('update')['ok']
    if (reg if reg is not None else _lic_registered()):
        return True
    sec = (app or {}).get('security')
    return bool(sec) and _store_vtuple(sec) > _store_vtuple((inst or {}).get('version'))


def _store_rsa_ok(data, sig_b64):
    if not STORE_PUBKEY_N_HEX:
        return False
    try:
        n = int(STORE_PUBKEY_N_HEX, 16)
        k = (n.bit_length() + 7) // 8
        sig = _store_b64d(sig_b64)
        if len(sig) > k:
            return False
        sig = b'\x00' * (k - len(sig)) + sig
        em = pow(int.from_bytes(sig, 'big'), 65537, n).to_bytes(k, 'big')
        t = _LIC_SHA256_DI + _st_hashlib.sha256(data).digest()
        if k - 3 - len(t) < 8:
            return False
        return _st_hmac.compare_digest(em, b'\x00\x01' + b'\xff' * (k - 3 - len(t)) + b'\x00' + t)
    except Exception:
        return False


def store_catalog(force=False):
    """Catalogue vérifié (signature, format, anti-retour arrière), mis en cache 12 h.
    En cas d'échec, le dernier catalogue valide reste servi. Rend (catalogue ou None, message d'erreur)."""
    cached = _lic_read_json(STORE_CATALOG_CACHE)
    now = _st_time.time()
    if not force and cached and now - _store_state['checked'] < STORE_REFRESH:
        return cached, _store_state['error']
    with _STORE_LOCK:
        err = ''
        try:
            if not STORE_PUBKEY_N_HEX:
                raise ValueError("Clé du catalogue non configurée.")
            import urllib.request

            def _get(name):
                req = urllib.request.Request(STORE_URL + '/' + name + '?t=%d' % int(now), headers={'User-Agent': 'Desktroo'})
                with urllib.request.urlopen(req, timeout=15) as r:
                    return r.read(4 * 1024 * 1024)

            data = _get('catalog.json')
            sig = _get('catalog.json.sig').decode('ascii', 'replace')
            if not _store_rsa_ok(data, sig):
                raise ValueError('Signature du catalogue invalide : catalogue ignoré.')
            cat = json.loads(data.decode('utf-8'))
            if not isinstance(cat, dict) or int(cat.get('schema', 0)) != 1 or not isinstance(cat.get('apps'), list):
                raise ValueError('Format de catalogue inconnu.')
            if cached and str(cat.get('published', '')) < str(cached.get('published', '')):
                raise ValueError('Catalogue plus ancien que celui déjà reçu : ignoré.')
            cat['apps'] = [a for a in cat['apps'] if isinstance(a, dict) and _STORE_ID_RE.match(str(a.get('id', '')))]
            os.makedirs(STORE_DIR, exist_ok=True)
            _lic_write_json(STORE_CATALOG_CACHE, cat)
            cached = cat
            try:
                _store_icons_fetch(cat)
            except Exception as e:
                log.warning('Magasin : icônes non récupérées : %s', e)
        except Exception as e:
            err = str(e)
            log.warning('Magasin : %s', err)
        _store_state.update(checked=now, error=err)
        return cached, err


# Bloc nginx des apps. Posé par fileops dans nginx.conf (toutes les méthodes d'installation), une seule fois.
_STORE_NGINX_MARK = 'MDM-STORE-NGINX-V6'   # à changer à chaque évolution du bloc : fileops remplace l'ancien
_STORE_NGINX = r'''    # ── MDM-STORE-V1 · MDM-STORE-NGINX-V6 : apps du Magasin Desktroo, derrière la session du bureau (posé par fileops) ──
    location = /_store_auth {
        internal;
        proxy_pass              http://fileops:8765/store/auth;
        proxy_pass_request_body off;
        proxy_set_header        Content-Length "";
        proxy_set_header        X-Original-URI $request_uri;
        proxy_set_header        X-Forwarded-Proto $scheme;
    }

    location /a/_sdk/ {
        proxy_pass         http://fileops:8765/store/sdk/;
        proxy_http_version 1.1;
    }

    location = /a/_login {
        rewrite            ^ /store/login break;
        proxy_pass         http://fileops:8765;
        proxy_set_header   X-Forwarded-Proto $scheme;
        client_max_body_size 16k;
    }

    location ~ "^/a/([a-z0-9][a-z0-9-]{0,40})/desktroo\.webmanifest$" {
        rewrite            ^/a/([^/]+)/ /store/manifest/$1 break;
        proxy_pass         http://fileops:8765;
    }

    location ~ "^/a/([a-z0-9][a-z0-9-]{0,40})$" {
        return 301 $uri/$is_args$args;
    }

    # Bureautique : fichiers statiques de l'éditeur (code, polices, dictionnaires), sans la session. Le service worker
    # d'OnlyOffice les relit sans cookie ; ils ne contiennent aucune donnée. Lecture seule.
    location ~ "^/a/(onlyoffice|eurooffice)/([0-9]+\.[0-9]+\.[0-9]+[.-][A-Za-z0-9._-]+/)?(web-apps|sdkjs|sdkjs-plugins|fonts|dictionaries)/" {
        set                   $dt_app $1;
        limit_except GET HEAD { deny all; }
        resolver              127.0.0.11 valid=10s ipv6=off;
        rewrite               ^/a/[^/]+(/.*)$ $1 break;
        proxy_pass            http://dt-$dt_app:80;
        proxy_http_version    1.1;
        proxy_set_header      Host               $http_host;
        proxy_set_header      X-Forwarded-Host   $http_host/a/$dt_app;
        proxy_set_header      X-Forwarded-Proto  $dt_fwd_proto;
        proxy_set_header      Cookie             "";
        error_page 502 504 = @dks_502;
    }

    # Bureautique (OnlyOffice, Euro-Office) : le serveur écoute sur 80 et doit connaître son adresse publique
    # (X-Forwarded-Host avec le chemin /a/<app>) pour les adresses qu'il donne au navigateur.
    location ~ "^/a/(onlyoffice|eurooffice)/" {
        set                   $dt_app $1;
        set                   $dt_orig $uri;
        auth_request          /_store_auth;
        resolver              127.0.0.11 valid=10s ipv6=off;
        rewrite               ^/a/[^/]+(/.*)$ $1 break;
        proxy_pass            http://dt-$dt_app:80;
        proxy_http_version    1.1;
        proxy_set_header      Host               $http_host;
        proxy_set_header      X-Forwarded-Host   $http_host/a/$dt_app;
        proxy_set_header      X-Forwarded-Proto  $dt_fwd_proto;
        proxy_set_header      X-Forwarded-For    $proxy_add_x_forwarded_for;
        proxy_set_header      Upgrade            $http_upgrade;
        proxy_set_header      Connection         $http_connection;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
        client_max_body_size  1g;
        error_page 401 = @dks_401;
        error_page 403 = @dks_403;
        error_page 502 504 = @dks_502;
    }

    location ~ "^/a/([a-z0-9][a-z0-9-]{0,40})/" {
        set                   $dt_app $1;
        set                   $dt_orig $uri;     # adresse demandée, avant réécriture : retour après connexion
        auth_request          /_store_auth;
        auth_request_set      $dt_user   $upstream_http_x_desktroo_user;
        auth_request_set      $dt_secret $upstream_http_x_desktroo_secret;
        resolver              127.0.0.11 valid=10s ipv6=off;
        rewrite               ^/a/[^/]+(/.*)$ $1 break;
        proxy_pass            http://dt-$dt_app:8080;
        proxy_http_version    1.1;
        proxy_set_header      Host               $host;
        proxy_set_header      X-Desktroo-User    $dt_user;
        proxy_set_header      X-Desktroo-Secret  $dt_secret;
        proxy_set_header      X-Forwarded-Prefix /a/$dt_app;
        proxy_set_header      Upgrade            $http_upgrade;
        proxy_set_header      Connection         $http_connection;
        proxy_read_timeout    3600s;
        client_max_body_size  1g;
        error_page 401 = @dks_401;
        error_page 403 = @dks_403;
        error_page 502 504 = @dks_502;
    }

    location @dks_401 {
        if ($http_accept ~* "text/html") { return 302 /a/_login?next=$dt_orig; }
        rewrite ^ /store/error/401 break;
        proxy_pass http://fileops:8765;
    }
    location @dks_403 { rewrite ^ /store/error/403 break; proxy_pass http://fileops:8765; }
    location @dks_502 { rewrite ^ /store/error/502/$dt_app break; proxy_pass http://fileops:8765; }

'''


# Protocole vu par le navigateur : celui annoncé par un proxy placé devant (NPMplus en https), sinon celui de la requête.
_STORE_NGINX_MAP_MARK = 'MDM-STORE-MAP-V1'
_STORE_NGINX_MAP = '''# ── MDM-STORE-MAP-V1 : protocole vu par le navigateur, pour les apps du Magasin (posé par fileops) ──
map $http_x_forwarded_proto $dt_fwd_proto {
    default $http_x_forwarded_proto;
    ""      $scheme;
}

'''


def store_nginx_ensure():
    """Ajoute le bloc /a/ à nginx.conf s'il manque, vérifie la config (nginx -t) et recharge ; en cas d'erreur,
    remet le fichier d'origine. Réécriture SUR PLACE : le fichier est monté seul dans le conteneur du bureau."""
    if not APP_DIR:
        return False
    conf = os.path.join(APP_DIR, 'nginx.conf')
    try:
        txt = open(conf, encoding='utf-8').read()
    except Exception:
        return False
    if _STORE_NGINX_MARK in txt and _STORE_NGINX_MAP_MARK in txt:
        return True
    anchor = '    location /fileops/ {'
    if anchor not in txt:
        log.warning('Magasin : nginx.conf sans « location /fileops/ », bloc des apps non posé')
        return False
    base = txt
    old = base.find('    # ── MDM-STORE-V1')
    if old >= 0 and base.find(anchor, old) > old:      # bloc d'une version précédente : on le remplace
        base = base[:old] + base[base.find(anchor, old):]
    block = _STORE_NGINX
    if _STORE_NGINX_MAP_MARK not in base:
        m = re.search(r'^server\s*\{', base, re.M)
        if m:
            base = base[:m.start()] + _STORE_NGINX_MAP + base[m.start():]
        else:   # pas de bloc server en tête de fichier : sans la table, le protocole de la requête suffit
            block = block.replace('$dt_fwd_proto', '$scheme')
    new = base.replace(anchor, block + anchor, 1)

    def _write(s):
        with open(conf, 'r+', encoding='utf-8') as fh:
            fh.seek(0); fh.write(s); fh.truncate()

    try:
        _write(new)
        out, err, code = _px_host('docker exec desktroo nginx -t 2>&1', timeout=30)
        if code != 0:
            _write(txt)
            log.warning('Magasin : nginx -t refuse le bloc des apps, nginx.conf remis : %s', (out or err)[-400:])
            return False
        _px_host('docker exec desktroo nginx -s reload', timeout=30)
        log.info('Magasin : bloc nginx des apps posé')
        return True
    except Exception as e:
        try:
            _write(txt)
        except Exception:
            pass
        log.warning('Magasin : bloc nginx non posé : %s', e)
        return False


def store_net_ensure():
    """Crée le réseau partagé s'il manque et y branche le nginx du bureau (testé : tient au redémarrage).
    À rappeler avant chaque installation : un conteneur recréé perd son branchement."""
    cmd = ("docker network inspect {n} >/dev/null 2>&1 || docker network create {n} >/dev/null; "
           "docker inspect desktroo --format '{{{{json .NetworkSettings.Networks}}}}' | grep -q '\"{n}\"' "
           "|| docker network connect {n} desktroo; "
           "docker inspect desktroo-fileops --format '{{{{json .NetworkSettings.Networks}}}}' | grep -q '\"{n}\"' "
           "|| docker network connect {n} desktroo-fileops").format(n=STORE_NET)
    try:
        out, err, code = _px_host(cmd, timeout=60)
        if code != 0:
            log.warning('Magasin : réseau %s non prêt : %s', STORE_NET, (err or out)[-300:])
        return code == 0
    except Exception as e:
        log.warning('Magasin : réseau %s non prêt : %s', STORE_NET, e)
        return False


def _store_boot():
    _st_time.sleep(20)   # laisser le bureau et SSH démarrer
    if not (SSH_HOST and SSH_USER and SSH_PASS):
        return
    store_net_ensure()
    store_nginx_ensure()
    # nginx ne résout le nom « fileops » qu'à son démarrage : si fileops redémarre avec une autre adresse sur le
    # réseau de la pile (des conteneurs l'ont quittée, après la migration du serveur web par exemple), le bureau
    # n'obtiendrait plus que des 502. Un rechargement lui fait relire l'adresse, sans couper personne.
    _px_host('docker exec desktroo nginx -t >/dev/null 2>&1 && docker exec desktroo nginx -s reload', timeout=30)
    try:
        store_migrate_webserver()
    except Exception as e:
        log.warning('Magasin : migration du serveur web : %s', e)


def _store_get(handler, path, qs):
    """Routes GET du Magasin protégées par le jeton du bureau."""
    if path == '/store/session':
        user = _px_unquote(handler.headers.get('X-Desktroo-User', '') or '')[:64] or 'admin'
        body = json.dumps({'ok': True, 'user': user, 'expires_in': STORE_COOKIE_TTL}).encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Set-Cookie', store_cookie_header(handler, store_cookie_make(user), STORE_COOKIE_TTL))
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path == '/store/nas/choices':
        handler._json(200, store_nas_choices())
        return True
    if path == '/store/sms/hook':   # MDM-STORE-SMS-V1 : adresse où TrueNAS poste ses alertes pour l'app SMS
        handler._json(200, store_sms_hook())
        return True
    if path == '/store/kiosk-icons':
        handler._json(200, {'ok': True, 'ids': store_kiosk_icon_ids()})
        return True
    if path == '/store/cameras/media':
        store_cameras_media(handler, qs)
        return True
    if path == '/store/navidrome/img':
        code, ctype, data = store_navidrome_img(qs.get('i', ''), qs.get('s', ''))
        handler.send_response(code)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Cache-Control', 'private, max-age=86400' if code == 200 else 'no-store')
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return True
    if path == '/store/jellyfin/img':
        code, ctype, data = store_jellyfin_img(qs.get('i', ''), qs.get('s', ''))
        handler.send_response(code)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Cache-Control', 'private, max-age=3600' if code == 200 else 'no-store')
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return True
    if path == '/store/photoprism/img':
        code, ctype, data = store_photoprism_img(qs.get('h', ''), qs.get('s', ''))
        handler.send_response(code)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Cache-Control', 'private, max-age=86400' if code == 200 else 'no-store')
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        return True
    if path == '/store/pass/state':
        handler._json(200, store_pass_state())
        return True
    if path == '/store/npm/state':
        handler._json(200, store_npm_state())
        return True
    if path == '/store/wake':
        handler._json(200, store_wake_status(qs.get('id', '')))
        return True
    if path == '/store/view':
        if qs.get('refresh') == '1':
            store_catalog(force=True)
        handler._json(200, store_view())
        return True
    if path == '/store/installed':
        handler._json(200, {'ok': True, 'apps': store_installed_list()})
        return True
    if path == '/store/users':
        try:
            handler._json(200, {'ok': True, 'users': store_admin_users()})
        except Exception as e:
            handler._json(502, {'ok': False, 'error': str(e)})
        return True
    if path == '/store/job':
        handler._json(200, {'ok': True, 'job': store_job_get(qs.get('id', ''))})
        return True
    if path == '/store/catalog':
        cat, err = store_catalog(force=qs.get('refresh') == '1')
        handler._json(200, {'ok': cat is not None, 'catalog': cat, 'error': err,
                            'gate': store_gate('install'), 'net': STORE_NET})
        return True
    return False


_STORE_POST = {
    '/store/kiosk-icon': lambda b: store_kiosk_icon_put(b),
    '/store/install': lambda b: store_install(b),
    '/store/update': lambda b: store_install(b, update=True),
    '/store/uninstall': lambda b: store_uninstall(b),     # lambdas : les fonctions sont définies plus bas
    '/store/start': lambda b: store_start(b),
    '/store/wake': lambda b: store_wake(b),
    '/store/idle': lambda b: store_idle_set(b),
    '/store/access': lambda b: store_access_set(b),
    '/store/adopt': lambda b: store_npm_adopt(b),
    '/store/npm/credentials': lambda b: store_npm_credentials(b),
    '/store/npm/secret': lambda b: store_npm_secret(b),
    '/store/npm/host': lambda b: store_npm_host_add({k: v for k, v in (b or {}).items() if k != 'advanced_config'}),
    '/store/npm/host/delete': lambda b: store_npm_host_delete(b),
    '/store/npm/host/toggle': lambda b: store_npm_host_toggle(b),
    '/store/npm/cert/renew': lambda b: store_npm_cert_renew(b),
    '/store/npm/job/clear': lambda b: store_npm_job_clear(b),
    '/store/npm/link': lambda b: store_npm_link(b),
    '/store/npm/unlink': lambda b: store_npm_unlink(b),
    '/store/adguard/link': lambda b: store_adguard_link(b),
    '/store/adguard/unlink': lambda b: store_adguard_unlink(b),
    '/store/adguard/api': lambda b: store_adguard_api(b),
    '/store/pihole/link': lambda b: store_pihole_link(b),
    '/store/pihole/unlink': lambda b: store_pihole_unlink(b),
    '/store/pihole/api': lambda b: store_pihole_api(b),
    '/store/wireguard/api': lambda b: store_wireguard_api(b),
    '/store/uptime-kuma/api': lambda b: store_uptimekuma_api(b),
    '/store/photoprism/api': lambda b: store_photoprism_api(b),
    '/store/jellyfin/api': lambda b: store_jellyfin_api(b),
    '/store/navidrome/api': lambda b: store_navidrome_api(b),
    '/store/cameras/api': lambda b: store_cameras_api(b),
    '/store/pass/invite': lambda b: store_pass_invite(b),
    '/store/pass/user': lambda b: store_pass_user(b),
    '/store/pass/domain': lambda b: store_pass_domain(b),
    '/store/pass/mail': lambda b: store_pass_mail(b),
    '/store/pass/test-mail': lambda b: store_pass_test_mail(b),
    '/store/nas': lambda b: store_nas_update(b),
}


def _store_post(handler, path):
    fn = _STORE_POST.get(path)
    if not fn:
        return False
    try:
        body = handler._body()
    except Exception:
        handler._json(400, {'ok': False, 'error': 'Requête invalide.'})
        return True
    code, resp = fn(body if isinstance(body, dict) else {})
    handler._json(code, resp)
    return True


_STORE_ERRORS = {
    401: ('Session expirée', 'Votre session Desktroo a expiré. Reconnectez-vous au bureau, puis rouvrez cette app.'),
    403: ('Accès non autorisé', "Votre compte n'a pas accès à cette app. Un administrateur peut vous l'ouvrir dans Desktroo Apps : "
                                "fiche de l'app, « Qui peut l'utiliser »."),
    502: ("L'app ne répond pas", "Elle est peut-être arrêtée ou en train de démarrer. Patientez quelques secondes, ou ouvrez Desktroo Apps "
                                 "pour la démarrer."),
}


def _store_error_page(code):
    import html as _h
    title, text = _STORE_ERRORS.get(code, _STORE_ERRORS[502])
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title><link rel="stylesheet" href="/a/_sdk/desktroo-app.css"><script src="/a/_sdk/desktroo-app.js"></script>'
            '<style>body{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}'
            '.box{max-width:420px;text-align:center}.ic{font-size:34px;margin-bottom:10px}</style></head>'
            '<body><div class="box"><div class="ic" aria-hidden="true">%s</div><h1>%s</h1><p class="dk-muted">%s</p>'
            '<p><button class="dk-btn" onclick="location.reload()">Réessayer</button></p></div></body></html>'
            % (_h.escape(title), {401: '🔑', 403: '🔒'}.get(code, '⏳'), _h.escape(title), _h.escape(text)))


def _store_waking_page():
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="refresh" content="5"><title>Démarrage…</title><link rel="stylesheet" href="/a/_sdk/desktroo-app.css">'
            '<script src="/a/_sdk/desktroo-app.js"></script><style>body{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}'
            '.box{max-width:420px;text-align:center}.ic{font-size:34px;margin-bottom:10px}</style></head>'
            '<body><div class="box"><div class="ic" aria-hidden="true">⏳</div><h1>L’app démarre…</h1>'
            '<p class="dk-muted">Elle était en veille pour économiser la mémoire du NAS. Cette page se recharge toute seule '
            'dès qu’elle répond, en général en moins d’une minute.</p></div></body></html>')


def _store_public_get(handler, path):
    """Routes GET du Magasin sans jeton : la vérification pour nginx, le kit des apps, la fin de session."""
    if path == '/store/auth':
        code, user, secret = store_auth_check(handler)
        handler.send_response(code)
        if code == 200:
            handler.send_header('X-Desktroo-User', _px_quote(user, safe=''))
            handler.send_header('X-Desktroo-Secret', secret)
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return True
    if path.startswith('/store/navidrome/stream/'):
        _nd_stream_serve(handler, path[len('/store/navidrome/stream/'):].split('/')[0])
        return True
    if path.startswith('/store/jellyfin/stream/'):
        _jf_stream_serve(handler, path[len('/store/jellyfin/stream/'):].split('/')[0])
        return True
    if path.startswith('/store/jellyfin/sub/'):
        _jf_stream_serve(handler, path[len('/store/jellyfin/sub/'):].split('/')[0])
        return True
    if path.startswith('/store/kiosk-icon/'):
        m = _KIOSK_ICON_NAME.match(path[len('/store/kiosk-icon/'):])
        body = b''
        if m:
            try:
                with open(os.path.join(KIOSK_ICON_DIR, m.group(0)), 'rb') as f:
                    body = f.read()
            except OSError:
                body = b''
        handler.send_response(200 if body else 404)
        if body:
            handler.send_header('Content-Type', 'image/png')
        handler.send_header('Cache-Control', 'public, max-age=3600')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path.startswith('/store/sys-icon/'):
        body = store_sys_icon(path[len('/store/sys-icon/'):]) or b''
        handler.send_response(200 if body else 404)
        if body:
            handler.send_header('Content-Type', 'image/svg+xml')
        handler.send_header('Cache-Control', 'public, max-age=86400')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path.startswith('/store/kiosk-manifest/'):
        man = store_kiosk_manifest(path[len('/store/kiosk-manifest/'):])
        body = json.dumps(man, ensure_ascii=False).encode('utf-8') if man else b''
        handler.send_response(200 if man else 404)
        if man:
            handler.send_header('Content-Type', 'application/manifest+json; charset=utf-8')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path.startswith('/store/sdk/'):
        item = _STORE_SDK.get(path[len('/store/sdk/'):])
        if not item:
            handler.send_response(404); handler.send_header('Content-Length', '0'); handler.end_headers()
            return True
        ctype, body = item
        handler.send_response(200)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Cache-Control', 'no-cache')   # petit fichier : toujours revalidé, une mise à jour du kit s'applique tout de suite
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path == '/store/login':
        body = _STORE_LOGIN_HTML.encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Frame-Options', 'SAMEORIGIN')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path.startswith('/store/manifest/'):
        man = store_manifest(path[len('/store/manifest/'):])
        if not man:
            handler.send_response(404); handler.send_header('Content-Length', '0'); handler.end_headers()
            return True
        body = json.dumps(man, ensure_ascii=False).encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/manifest+json; charset=utf-8')
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path.startswith('/store/error/'):
        parts = path[len('/store/error/'):].split('/')
        code = {'401': 401, '403': 403, '502': 502}.get(parts[0], 502)
        body = None
        app_id = parts[1] if len(parts) > 1 else ''
        # App en veille, ouverte par son adresse : on la réveille et la page se recharge jusqu'à ce qu'elle réponde.
        if code == 502 and _STORE_ID_RE.match(app_id) and store_cookie_user(handler.headers.get('Cookie', '')):
            try:
                wc, wr = store_wake({'id': app_id})
                if wc == 200 and wr.get('state') in ('starting', 'ready'):
                    body = _store_waking_page().encode('utf-8')
            except Exception as e:
                log.warning('Veille : réveil de %s depuis son adresse : %s', app_id, e)
        body = body or _store_error_page(code).encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path.startswith('/store/icon/'):
        p, ctype = store_icon_path(path[len('/store/icon/'):])
        if not p:
            handler.send_response(404); handler.send_header('Content-Length', '0'); handler.end_headers()
            return True
        body = open(p, 'rb').read()
        handler.send_response(200)
        handler.send_header('Content-Type', ctype)
        handler.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'")
        handler.send_header('Cache-Control', 'no-cache')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    if path == '/store/logout':
        handler.send_response(200)
        handler.send_header('Set-Cookie', store_cookie_header(handler, '', 0))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return True
    return False


# ── MDM-STORE-V1 · phase 1 : installer, désinstaller, droits d'accès ────────────────────────────────────
STORE_INSTALLED_FILE = os.path.join(STORE_DIR, 'installed.json')
STORE_ICON_DIR = os.path.join(STORE_DIR, 'icons')
STORE_BETA_FILE = os.path.join(STORE_DIR, 'beta')     # présent : NAS inscrit au programme bêta (Desktroo et apps en test)
_STORE_ICON_TYPES = {'.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp'}
_store_jobs = {}            # id d'app → {'action', 'state': running|done|error, 'step', 'error', 'at'}
_STORE_JOBS_LOCK = _threading.Lock()


def _store_vtuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', str(v or ''))[:3]) or (0,)


def _store_midclt(method, *args, job=False, timeout=120):
    """Appel du middleware TrueNAS sur le NAS (SSH). Rend le résultat JSON décodé ; lève RuntimeError sinon."""
    cmd = 'midclt call ' + ('-j ' if job else '') + method + ''.join(' ' + shq(json.dumps(a)) for a in args)
    out, err, code = _px_host(cmd + ' 2>&1', timeout=timeout)
    text = (out or '').strip()
    if code != 0:
        lines = [l for l in text.splitlines() if l.strip() and not l.startswith(('Status:', 'Total Progress'))]
        raise RuntimeError((lines[-1] if lines else (err or 'erreur inconnue'))[-400:])
    last = text.splitlines()[-1] if text else 'null'
    try:
        return json.loads(last)
    except Exception:
        return last


def _store_beta_on():
    """Inscrit au programme bêta : le fichier bêta et une clé gratuite (l'inscription n'est possible qu'avec la clé)."""
    return os.path.exists(STORE_BETA_FILE) and _lic_registered()


def store_visible_apps(cat):
    # 3.1.0 : toutes les apps du catalogue apparaissent, celles du canal test aussi (badge « Test »). Les installer demande
    # d'être inscrit au programme bêta : voir store_install.
    return [a for a in (cat or {}).get('apps', []) if isinstance(a, dict)]


def store_app_entry(app_id):
    cat, _err = store_catalog()
    for a in store_visible_apps(cat):
        if a.get('id') == app_id:
            return a
    return None


def store_installed_get():
    d = _lic_read_json(STORE_INSTALLED_FILE)
    return d if isinstance(d, dict) else {}


def _store_installed_set(d):
    os.makedirs(STORE_DIR, exist_ok=True)
    _lic_write_json(STORE_INSTALLED_FILE, d)


def store_installed_list():
    """Apps du Magasin présentes sur ce NAS : registre local croisé avec l'état réel des apps TrueNAS."""
    reg = store_installed_get()
    states = {}
    try:
        rows = _store_midclt('app.query', [], {'select': ['name', 'state']}, timeout=60) or []
        states = {r.get('name'): r.get('state') for r in rows if isinstance(r, dict)}
    except Exception as e:
        log.warning('Magasin : état des apps illisible : %s', e)
    out = []
    if STORE_WEB_ID not in reg:
        try:
            legacy = store_webserver_legacy()
        except Exception:            # NAS injoignable : dans le doute, on garde la fenêtre Sites Web visible
            legacy = {'running': False}
        if legacy:
            mig = _lic_read_json(STORE_WEB_MIGRATION_FILE) or {}
            out.append({'id': STORE_WEB_ID, 'name': {'fr': 'Serveur web', 'en': 'Web server'}, 'kind': 'module', 'legacy': True,
                        'version': '', 'state': 'RUNNING' if legacy['running'] else 'STOPPED', 'users': None,
                        'migration': mig.get('skipped') or mig.get('error') or ''})
    linked = (_lic_read_json(STORE_NPM_FILE) or {}).get('linked') if STORE_NPM_ID not in reg else None
    if linked:   # NPMplus relié : une icône « Domaines » au bureau, sans que Desktroo Apps le gère
        try:
            running = True if linked.get('remote') else _store_container_info(linked.get('container') or '')[1]
        except Exception:
            running = False
        out.append({'id': STORE_NPM_ID, 'name': {'fr': 'NPMplus', 'en': 'NPMplus'}, 'kind': 'module', 'linked': True,
                    'version': '', 'state': 'RUNNING' if running else 'STOPPED', 'users': None,
                    'container': linked.get('container'), 'remote': linked.get('url') if linked.get('remote') else '',
                    'ports': store_npm_settings()['ports']})
    ag = store_ag_linked() if STORE_AG_ID not in reg else None
    if ag:   # AdGuard Home relié : icône et fenêtre au bureau, sans que Desktroo Apps le gère
        out.append({'id': STORE_AG_ID, 'name': {'fr': 'AdGuard Home', 'en': 'AdGuard Home'}, 'kind': 'app', 'linked': True,
                    'version': ag.get('version') or '', 'state': 'RUNNING', 'users': None, 'remote': ag['url']})
    ph = store_ph_linked() if STORE_PH_ID not in reg else None
    if ph:   # Pi-hole relié : icône et fenêtre au bureau, sans que Desktroo Apps le gère
        out.append({'id': STORE_PH_ID, 'name': {'fr': 'Pi-hole', 'en': 'Pi-hole'}, 'kind': 'app', 'linked': True,
                    'version': ph.get('version') or '', 'state': 'RUNNING', 'users': None, 'remote': ph['url']})
    for app_id, rec in reg.items():
        r = dict(rec or {}); r['id'] = app_id
        r['state'] = states.get('dt-' + app_id, 'MISSING' if states else 'UNKNOWN')
        r['users'] = store_access_users(app_id)
        if app_id == STORE_NPM_ID:   # ports seulement (le secret des cookies reste sur le NAS)
            r['ports'] = store_npm_settings()['ports']
        out.append(r)
    return out


def store_admin_users():
    """Comptes qui peuvent se connecter à Desktroo (administrateurs complets de TrueNAS)."""
    rows = _store_midclt('user.query', [['locked', '=', False]], {'select': ['username', 'builtin', 'roles', 'full_name']}, timeout=60) or []
    admins = [r for r in rows if isinstance(r, dict) and 'FULL_ADMIN' in (r.get('roles') or [])]
    if not admins:
        admins = [r for r in rows if isinstance(r, dict) and not r.get('builtin')]
    return [{'username': r.get('username'), 'full_name': r.get('full_name') or ''} for r in admins if r.get('username')]


def _store_pool():
    m = re.match(r'^/mnt/([^/]+)/', (APP_DIR or '') + '/')
    if not m:
        raise RuntimeError("Pool de Desktroo introuvable (APP_DIR).")
    return m.group(1)


_STORE_DS_CACHE = {}
_STORE_DS_RE = re.compile(r'^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+){1,4}$')


def _store_apps_base():
    """Dossier de données des apps Desktroo : <pool>/apps/desktroo quand le dataset « apps » du pool existe (là où TrueNAS
    et l'utilisateur rangent déjà leurs apps), sinon l'ancien <pool>/desktroo-apps."""
    pool = _store_pool()
    if pool in _STORE_DS_CACHE:
        return _STORE_DS_CACHE[pool]
    try:
        out, _e, code = _px_host('zfs list -H -o name ' + shq(pool + '/apps') + ' 2>/dev/null', timeout=30)
        if code == 0 and (out or '').strip() == pool + '/apps':
            _STORE_DS_CACHE[pool] = pool + '/apps/desktroo'
            return _STORE_DS_CACHE[pool]
    except Exception as e:
        log.warning('Magasin : dataset « apps » illisible : %s', e)
    return pool + '/desktroo-apps'          # pas mis en cache : on relira au prochain appel


def _store_dataset(app_id):
    """Dataset des données d'une app. Une app déjà installée garde celui que le registre a retenu ; des données gardées à la
    désinstallation dans l'ancien dossier sont reprises ; une nouvelle installation va dans apps/desktroo/<app>."""
    ds = str((store_installed_get().get(app_id) or {}).get('dataset') or '')
    if _STORE_DS_RE.match(ds) and not any(part in ('.', '..') for part in ds.split('/')):
        return ds
    legacy = '%s/desktroo-apps/%s' % (_store_pool(), app_id)
    if os.path.isdir('/mnt/' + legacy):
        return legacy
    return '%s/%s' % (_store_apps_base(), app_id)


def _store_timezone():
    try:
        return (_store_midclt('system.general.config', timeout=30) or {}).get('timezone') or 'Europe/Paris'
    except Exception:
        return 'Europe/Paris'


STORE_CAP_ALLOWED = ('NET_BIND_SERVICE', 'NET_ADMIN', 'NET_RAW', 'SYS_MODULE',   # capacités qu'une app signée peut demander (ports < 1024 ; VPN ;
                      'CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'SETUID', 'SETGID',   # apps qui gèrent leurs propres fichiers et comptes : sauvegarde ;
                      'KILL', 'FSETID', 'SETPCAP', 'SETFCAP', 'SYS_CHROOT', 'AUDIT_WRITE')   # serveurs à plusieurs services : jeu par défaut de Docker)
STORE_SYSCTL_ALLOWED = ('net.ipv4.ip_forward', 'net.ipv4.conf.all.src_valid_mark', 'net.ipv6.conf.all.disable_ipv6',
                        'net.ipv6.conf.all.forwarding', 'net.ipv6.conf.default.forwarding')
STORE_HOST_MOUNTS_ALLOWED = ('/lib/modules',)   # dossiers de l'hôte qu'une app peut voir, en lecture seule
STORE_FIELDS_DIR = os.path.join(STORE_DIR, 'fields')
_STORE_FIELD_KINDS = {
    'host': re.compile(r'^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$|^\d{1,3}(?:\.\d{1,3}){3}$'),
    'text': re.compile(r'^[^\r\n\x00]{1,120}$'),
}


def _store_install_fields(app):
    """Réglages demandés avant l'installation (adresse publique d'un VPN, par exemple) : [{key, env, kind, required, ...}]."""
    out = []
    for f in (app.get('install_fields') or [])[:6]:
        if (isinstance(f, dict) and re.match(r'^[a-z][a-z0-9_]{0,23}$', str(f.get('key') or ''))
                and re.match(r'^[A-Z_][A-Z0-9_]*$', str(f.get('env') or '')) and f.get('kind') in _STORE_FIELD_KINDS):
            out.append(f)
    return out


def _store_fields_get(app_id):
    d = _lic_read_json(os.path.join(STORE_FIELDS_DIR, app_id + '.json'))
    return d if isinstance(d, dict) else {}


def _store_fields_set(app_id, vals):
    p = os.path.join(STORE_FIELDS_DIR, app_id + '.json')
    if vals is None:
        try:
            os.remove(p)
        except OSError:
            pass
        return
    os.makedirs(STORE_FIELDS_DIR, exist_ok=True)
    _lic_write_json(p, vals)


def _store_fields_validate(app, raw):
    """Valeurs saisies pour les champs d'installation ; ValueError avec un message lisible sinon."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for f in _store_install_fields(app):
        v = str(raw.get(f['key']) or '').strip()
        lab = (f.get('label') or {}).get('fr') or f['key']
        if not v:
            if f.get('required', True):
                raise ValueError('Indiquez : %s.' % lab)
            continue
        if not _STORE_FIELD_KINDS[f['kind']].match(v):
            raise ValueError('Valeur invalide pour « %s ».' % lab)
        out[f['key']] = v
    return out


def _store_derive(app_id, label):
    """Valeur secrète propre à cette installation, recalculable (mot de passe d'une app, par exemple) : rien à mémoriser."""
    return _st_hmac.new(_STORE_SECRET, ('app.%s.%s' % (app_id, label)).encode('utf-8'), _st_hashlib.sha256).hexdigest()[:32]


def _store_host_ports(app):
    """Ports que l'app ouvre sur le réseau du NAS : [{port, proto: tcp|udp|both, label}], vérifiés."""
    out = []
    for x in (app.get('host_ports') or [])[:6]:
        if not isinstance(x, dict):
            continue
        try:
            port = int(x.get('port'))
        except (TypeError, ValueError):
            continue
        proto = str(x.get('proto') or 'tcp').lower()
        if 1 <= port <= 65535 and proto in ('tcp', 'udp', 'both') and port not in (22, 80, 443):
            try:
                to = int(x.get('to') or port)
            except (TypeError, ValueError):
                to = port
            if not 1 <= to <= 65535:
                to = port
            out.append({'port': port, 'to': to, 'proto': proto, 'label': x.get('label') or {}, 'bind': 'all' if x.get('bind') == 'all' else 'lan'})
    return out


def _store_bind_ip():
    """Adresse du NAS sur le réseau local, pour n'ouvrir un port que là (et non sur toutes les adresses : le réseau
    interne des machines virtuelles, par exemple, a déjà son propre serveur DNS). '' si elle est introuvable."""
    for fn in (_truenas_ip, _nas_lan_ip):
        try:
            ip = str(fn() or '')
        except Exception:
            continue
        if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', ip) and not ip.startswith(('127.', '0.')):
            return ip
    return ''


def _store_proto_list(x):
    return ('tcp', 'udp') if x['proto'] == 'both' else (x['proto'],)


def _store_ports_busy(specs, ip=''):
    """Ports (parmi ceux demandés) déjà pris sur le NAS, en TCP ou en UDP : {port: nom du programme ou ''}."""
    out, _e, _c = _px_host("ss -Hltunp 2>/dev/null", timeout=30)
    used = {}
    for line in (out or '').splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        m = re.search(r'^(.*):(\d+)$', parts[4])
        if m:
            addr = m.group(1).strip('[]')
            pm = re.search(r'users:\(\("([^"]+)"', line)
            for x in specs:   # une adresse précise ailleurs que la nôtre ne gêne pas ; l'adresse « toutes » gêne toujours
                if int(m.group(2)) == x['port'] and (x['bind'] == 'all' or not ip or addr in ('*', '0.0.0.0', '::', ip)):
                    used[(parts[0], x['port'])] = pm.group(1) if pm else ''
    busy = {}
    for x in specs:
        for pr in _store_proto_list(x):
            if (pr, x['port']) in used and (x['port'] not in busy or not busy[x['port']]):
                busy[x['port']] = used[(pr, x['port'])]
    return busy


def _store_seed_cmds(app, owner):
    """Fichiers de départ de l'app (configuration prête à l'emploi), écrits seulement s'ils n'existent pas encore."""
    import base64
    cmds, base = [], '/mnt/' + _store_dataset(app['id'])
    vols = {str(v.get('name', '')) for v in app.get('volumes') or []}
    for f in (app.get('seed') or [])[:6]:
        vol, rel, text = str(f.get('volume', '')), str(f.get('path', '')), str(f.get('content', ''))
        if vol not in vols or not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(/[A-Za-z0-9][A-Za-z0-9._-]{0,80}){0,3}$', rel) or len(text) > 32768:
            continue
        path = '%s/%s/%s' % (base, vol, rel)
        b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
        cmds.append('[ -e {p} ] || {{ mkdir -p "$(dirname {p})" && printf %s {b} | base64 -d > {p} && chown {o} {p} "$(dirname {p})"; }}'.format(
            p=shq(path), b=shq(b64), o=owner))
    return cmds


def store_compose(app):
    """Compose d'une app, fabriqué ici à partir du catalogue : l'utilisateur ne voit ni YAML ni port."""
    app_id = app['id']
    if not app.get('image') or not app.get('digest'):
        raise RuntimeError("Fiche de l'app incomplète dans le catalogue (image) : contactez Mdm-services.fr.")
    if re.match(r'^sha256:0{64}$', str(app.get('digest'))):
        raise RuntimeError("Cette app n'est pas encore prête dans le catalogue (image non figée) : réessayez plus tard.")
    base = '/mnt/' + _store_dataset(app_id)
    env = {}
    for k, v in (app.get('env') or {}).items():
        if re.match(r'^[A-Z_][A-Z0-9_]*$', str(k)):
            sv = str(v)
            if v == '@host' and k == 'TZ':
                sv = _store_timezone()
            elif sv.startswith('@derive:') and re.match(r'^[a-z][a-z0-9-]{0,30}$', sv[8:]):
                sv = _store_derive(app_id, sv[8:])
            env[str(k)] = sv
    fvals = _store_fields_get(app_id)
    for f in _store_install_fields(app):
        if fvals.get(f['key']):
            env[f['env']] = str(fvals[f['key']])
    env['DESKTROO_APP_SECRET'] = store_app_secret(app_id)
    env['DESKTROO_APP_ID'] = app_id
    vols = []
    for v in app.get('volumes') or []:
        name, mount = str(v.get('name', '')), str(v.get('mount', ''))
        if re.match(r'^[a-z0-9][a-z0-9_-]{0,30}$', name) and mount.startswith('/'):
            vols.append('%s/%s:%s' % (base, name, mount))
    for hm in app.get('host_mounts') or []:   # dossier de l'hôte, toujours en lecture seule
        if hm in STORE_HOST_MOUNTS_ALLOWED:
            vols.append('%s:%s:ro' % (hm, hm))
    res = app.get('resources') or {}
    svc = {
        'image': '%s@%s' % (app['image'], app['digest']),
        'container_name': 'dt-' + app_id,
        'restart': 'unless-stopped',
        'user': str(app.get('run_as') or '568:568'),
        'environment': env,
        'volumes': vols,
        'networks': [STORE_NET],
        'read_only': bool(app.get('read_only', True)),
        'tmpfs': ['/tmp'],
        'security_opt': ['no-new-privileges:true'],
        'cap_drop': ['ALL'],
        'labels': {'fr.desktroo.store': app_id, 'fr.desktroo.version': str(app.get('version', ''))},
    }
    if app.get('tmpfs', True) is False:   # /tmp dans le conteneur : gros fichiers temporaires (conversion de documents)
        svc.pop('tmpfs', None)
    if res.get('memory_mb'):
        svc['mem_limit'] = '%dm' % int(res['memory_mb'])
    if res.get('cpus'):
        svc['cpus'] = float(res['cpus'])
    try:   # mémoire partagée (/dev/shm) : Frigate y échange les images des caméras
        if 64 <= int(res.get('shm_mb') or 0) <= 4096:
            svc['shm_size'] = '%dm' % int(res['shm_mb'])
    except (TypeError, ValueError):
        pass
    if isinstance(app.get('command'), list) and app['command']:
        svc['command'] = [str(x) for x in app['command']]
    hp = _store_host_ports(app)
    if hp:   # services que l'app offre au réseau local (DNS, par exemple) : seuls ces ports sont ouverts
        ip = _store_bind_ip()
        svc['ports'] = [('%s:' % ip if (ip and x['bind'] == 'lan') else '') + '%d:%d/%s' % (x['port'], x['to'], pr)
                        for x in hp for pr in _store_proto_list(x)]
    caps = [c for c in (app.get('cap_add') or []) if c in STORE_CAP_ALLOWED]
    if caps:
        svc['cap_add'] = caps
    sysc = {str(k): str(v) for k, v in (app.get('sysctls') or {}).items() if k in STORE_SYSCTL_ALLOWED and str(v) in ('0', '1')}
    if sysc:
        svc['sysctls'] = sysc
    if _store_nas_all(app):
        _store_nas_apply_all(svc, env, app_id)
    elif app.get('nas_access'):
        _store_nas_apply(svc, env, app_id, app)
    return {'services': {'app': svc}, 'networks': {STORE_NET: {'external': True}}}


def _store_job_set(app_id, **kw):
    with _STORE_JOBS_LOCK:
        j = _store_jobs.setdefault(app_id, {})
        j.update(kw); j['at'] = int(_st_time.time())


def store_job_get(app_id):
    with _STORE_JOBS_LOCK:
        return dict(_store_jobs.get(app_id) or {})


def _store_running(app_id):
    return store_job_get(app_id).get('state') == 'running'


def _store_ds_create(name):
    """Crée un dataset. Sous un parent en ACL POSIX (le dataset « apps » de TrueNAS), TrueNAS refuse l'aclmode hérité :
    il doit valoir DISCARD. On réessaie alors avec cette valeur."""
    try:
        return _store_midclt('pool.dataset.create', {'name': name}, timeout=120)
    except RuntimeError as e:
        if 'aclmode' not in str(e):
            raise
        return _store_midclt('pool.dataset.create', {'name': name, 'aclmode': 'DISCARD'}, timeout=120)


def _store_prepare_data(app):
    ds = _store_dataset(app['id'])
    parent = ds.rsplit('/', 1)[0]
    for d in (parent, ds):
        out, _e, code = _px_host('zfs list -H -o name ' + shq(d) + ' 2>/dev/null', timeout=30)
        if code != 0 or not out.strip():
            if d == ds and re.match(r'^[A-Za-z0-9_.-]+/(desktroo-apps|apps/desktroo)/[a-z0-9][a-z0-9-]{0,40}$', d):
                # Un dossier ordinaire porte déjà ce nom (reste d'une désinstallation, ou dossier recréé par Docker) : un dataset
                # ne peut pas le remplacer. Vide de fichiers, on le retire pour créer le dataset ; sinon on garde son contenu.
                p = '/mnt/' + d
                _o, _e, c2 = _px_host('[ -d %s ]' % shq(p), timeout=15)
                if c2 == 0:
                    _o, _e, c3 = _px_host('[ -z "$(find %s -mindepth 1 ! -type d 2>/dev/null | head -n 1)" ] && rm -rf %s' % (shq(p), shq(p)), timeout=60)
                    if c3 != 0:
                        log.warning('Magasin : %s existe déjà et contient des fichiers : utilisé tel quel, sans dataset.', p)
                        continue
            _store_ds_create(d)
    owner = str(app.get('run_as') or '568:568')
    nas = _store_nas_get(app['id']) if (app.get('nas_access') and not _store_nas_all(app)) else None
    if _store_nas_all(app):
        owner = '0:0'
    if nas:   # l'app tourne sous le compte prêté : ses propres données lui appartiennent
        owner = '%d:%d' % (int(nas['uid']), int(nas['gid']))
    if not re.match(r'^\d+:\d+$', owner):
        raise RuntimeError('run_as invalide dans le catalogue.')
    base = '/mnt/' + ds
    cmds = []
    for v in app.get('volumes') or []:
        name = str(v.get('name', ''))
        if re.match(r'^[a-z0-9][a-z0-9_-]{0,30}$', name):
            p = base + '/' + name
            # Propriétaire changé (autre compte, ou passage en « tout le NAS ») : tout le contenu suit, sinon l'app
            # ne peut plus écrire dans ses propres fichiers.
            if v.get('keep_owner'):   # l'app donne elle-même ce dossier à ses services (base de données…) : on n'y touche pas
                cmds.append('mkdir -p {p}'.format(p=shq(p)))
                continue
            cmds.append('mkdir -p {p} && {{ [ "$(stat -c %u:%g {p})" = {o} ] || chown -R {o} {p}; }}'.format(p=shq(p), o=owner))
    cmds += _store_seed_cmds(app, owner)
    if cmds:
        _o, err, code = _px_host(' && '.join(cmds), timeout=60)
        if code != 0:
            raise RuntimeError('Dossiers de données : ' + (err or '')[-300:])
    return ds


def _store_health(app, tries=30):
    path = str(app.get('health') or '/')
    if not path.startswith('/'):
        path = '/' + path
    port = STORE_APP_PORT
    if app.get('id') in OFFICE_APP_IDS:   # seul port autre que 8080 permis : celui du serveur de documents
        port = int(app.get('port') or OFFICE_PORT)
    if app.get('health_port'):   # app pilotée par sa fenêtre Desktroo (Caméras) : le moteur écoute sur son propre port
        port = max(1, min(65535, int(app['health_port'])))
    if tries > 1:   # premier démarrage long (base de données, polices) : « health_wait » secondes au plus
        tries = max(tries, min(200, int(app.get('health_wait') or 0) // 6))
    for i in range(tries):
        _o, _e, code = _px_host('docker exec desktroo wget -q -T 4 -O /dev/null %s 2>/dev/null'
                                % shq('http://dt-%s:%d%s' % (app['id'], port, path)), timeout=20)
        if code == 0:
            return True
        if i < tries - 1:
            _st_time.sleep(2)
    return False


def _store_install_job(app, users, update=False):
    app_id = app['id']
    is_module = app.get('kind') == 'module' or app_id in STORE_MODULE_IDS
    npm_s = pass_s = None
    try:
        _store_job_set(app_id, step='Préparation du réseau des apps')
        if not store_net_ensure():
            raise RuntimeError("Réseau des apps indisponible.")
        store_nginx_ensure()
        if update and not is_module:
            _store_job_set(app_id, step='Instantané des données avant mise à jour')
            ds = _store_dataset(app_id)
            snap = '%s@desktroo-maj-%s' % (ds, _st_time.strftime('%Y%m%d-%H%M%S'))
            _px_host('zfs snapshot ' + shq(snap), timeout=60)
        if app_id == STORE_PASS_ID:
            _store_job_set(app_id, step='Création du dossier de données')
            pass_s = _store_pass_prepare(app)
            ds = _store_dataset(app_id)
            compose = store_pass_compose(app, pass_s)
        elif app_id == STORE_NPM_ID:
            _store_job_set(app_id, step='Création du dossier de données')
            npm_s = _store_npm_prepare(app)
            ds = _store_dataset(app_id)
            compose = store_npm_compose(app, npm_s)
        elif is_module:
            ds = ''
            compose = store_webserver_compose(app)
        else:
            _store_job_set(app_id, step='Création des dossiers de données')
            ds = _store_prepare_data(app)
            compose = store_compose(app)
        if update:
            _store_job_set(app_id, step='Téléchargement et redémarrage')
            _store_midclt('app.update', 'dt-' + app_id, {'custom_compose_config': compose}, job=True, timeout=1800)
        else:
            _store_job_set(app_id, step='Téléchargement et démarrage')
            try:
                _store_midclt('app.create', {'custom_app': True, 'app_name': 'dt-' + app_id,
                                             'custom_compose_config': compose}, job=True, timeout=1800)
            except Exception:
                # TrueNAS retire en général lui-même une installation ratée ; si l'app est restée à moitié
                # créée, elle bloquerait l'essai suivant : on la retire (les données restent).
                try:
                    left = _store_midclt('app.query', [['name', '=', 'dt-' + app_id]], {'select': ['name']}, timeout=60)
                    if left:
                        _store_midclt('app.delete', 'dt-' + app_id, {'remove_images': True}, job=True, timeout=600)
                except Exception:
                    pass
                raise
        reg = store_installed_get()
        reg[app_id] = {'name': app.get('name'), 'version': app.get('version'), 'digest': app.get('digest'),
                       'image': app.get('image'), 'dataset': ds, 'window': app.get('window') or {},
                       'standalone': bool(app.get('standalone')), 'kind': 'module' if is_module else 'app',
                       'installed_at': (reg.get(app_id) or {}).get('installed_at') or int(_st_time.time()),
                       'updated_at': int(_st_time.time())}
        _store_installed_set(reg)
        if users is not None:   # MDM-ACCESS-UNIFIED-V1 : les comptes décochés à l'installation sont refusés dans Droits d'accès
            try:
                _store_access_apply(app_id, users)
            except Exception as e:
                log.warning('Magasin : accès de %s non enregistrés : %s', app_id, e)
        _store_job_set(app_id, step="Vérification que l'app répond")
        if npm_s and _store_npm_wait(npm_s):
            try:
                store_npm_setup_admin()
            except Exception as e:
                log.warning('NPMplus : compte administrateur non créé : %s', e)
        if not (_store_pass_wait(pass_s) if pass_s else _store_npm_wait(npm_s) if npm_s else _store_web_wait_running() if is_module else _store_health(app)):
            _store_job_set(app_id, state='done', step='Installée, mais elle ne répond pas encore', error='')
            return
        if app_id == STORE_ND_ID:
            try:
                _nd_setup()
            except Exception as e:
                log.warning('Navidrome : configuration de départ non faite (reprise à l’ouverture de la fenêtre) : %s', e)
        if app_id == STORE_JF_ID:
            try:
                _jf_setup()   # compte administrateur prêt : l'assistant de Jellyfin n'a pas à être suivi
            except Exception as e:
                log.warning('Jellyfin : configuration de départ non faite (reprise à l’ouverture de la fenêtre) : %s', e)
        _store_job_set(app_id, state='done', step='Installée', error='')
        log.info('Magasin : %s %s (%s)', 'mise à jour' if update else 'installation', app_id, app.get('version'))
    except Exception as e:
        log.warning('Magasin : %s de %s en échec : %s', 'mise à jour' if update else 'installation', app_id, e, exc_info=not isinstance(e, RuntimeError))
        _store_job_set(app_id, state='error', error=_store_explain(e))


def _store_explain(e):
    """Message lisible pour les pannes connues ; le détail technique est gardé dans le journal."""
    t = str(e)
    if re.search(r'unauthorized|denied|manifest unknown|not found', t, re.I) and 'ghcr.io' in t:
        return "L'image de l'app n'a pas pu être téléchargée (refusée par le registre). Réessayez plus tard ; si cela persiste, contactez Mdm-services.fr."
    if re.search(r'no such host|timeout|i/o timeout|connection refused|TLS handshake', t, re.I):
        return "Le NAS n'arrive pas à télécharger l'app : vérifiez son accès à Internet, puis réessayez."
    if re.search(r'no space left', t, re.I):
        return "Plus assez de place sur le NAS pour installer l'app."
    return t


def store_install(body, update=False):
    app_id = str((body or {}).get('id') or '')
    if not _STORE_ID_RE.match(app_id):
        return 400, {'ok': False, 'error': 'Identifiant d’app invalide.'}
    gate = store_gate('update' if update else 'install')
    app = store_app_entry(app_id)
    if not gate['ok'] and not (update and app and _store_update_allowed(app, store_installed_get().get(app_id))):
        return 402, {'ok': False, 'code': 'need_key', 'gate': gate, 'error': (
            'Mettre à jour une app demande une clé gratuite : demandez-la sur desktroo.fr, puis activez-la dans « À propos » › « Licence ». '
            'Les correctifs de sécurité arrivent sans clé.' if update else
            'Installer une app demande une clé gratuite : demandez-la sur desktroo.fr, puis activez-la dans « À propos » › « Licence ». '
            'Vos apps déjà installées restent utilisables.')}
    if not app:
        return 404, {'ok': False, 'error': 'App absente du catalogue.'}
    if not update and (app.get('channel') or 'stable') != 'stable' and not _store_beta_on():
        return 403, {'ok': False, 'code': 'need_beta', 'error': 'Cette app est en test : l’installer demande d’être inscrit au programme bêta '
                     '(« À propos » › « Programme bêta »), réservé aux installations qui ont une clé gratuite.'}
    if _store_vtuple(app.get('min_desktroo')) > _store_vtuple(APP_VERSION):
        return 409, {'ok': False, 'error': 'Cette app demande Desktroo %s ou plus récent : mettez Desktroo à jour.' % app.get('min_desktroo')}
    nas_cfg = None
    if app.get('nas_access') and not update and not _store_nas_all(app):
        try:
            nas_cfg = store_nas_validate(app, (body or {}).get('nas'))
        except ValueError as e:
            return 400, {'ok': False, 'error': str(e)}
    fields_cfg = None
    if _store_install_fields(app) and not update:
        try:
            fields_cfg = _store_fields_validate(app, (body or {}).get('fields'))
        except ValueError as e:
            return 400, {'ok': False, 'error': str(e)}
    if app_id == STORE_AG_ID and not update and store_ag_linked():
        return 409, {'ok': False, 'error': 'AdGuard Home est déjà relié (autre machine) : déliez-le d’abord pour l’installer ici.'}
    if app_id == STORE_PH_ID and not update and store_ph_linked():
        return 409, {'ok': False, 'error': 'Pi-hole est déjà relié (autre machine) : déliez-le d’abord pour l’installer ici.'}
    if app_id == STORE_WEB_ID and not update and store_webserver_legacy():
        return 409, {'ok': False, 'error': 'Le serveur web tourne encore dans Desktroo : il sera migré automatiquement.'}
    if app_id == STORE_NPM_ID and not update and app_id not in store_installed_get():
        why = store_npm_check_install()
        if why:
            return 409, {'ok': False, 'error': why}
    installed = app_id in store_installed_get()
    if update and not installed:
        return 409, {'ok': False, 'error': 'App non installée.'}
    if not update and installed:
        return 409, {'ok': False, 'error': 'App déjà installée.'}
    if _store_running(app_id):
        return 409, {'ok': False, 'error': 'Une opération est déjà en cours sur cette app.'}
    if not update:
        busy = _store_ports_busy(_store_host_ports(app), _store_bind_ip()) if _store_host_ports(app) else {}
        if busy and _store_dns_conflict(app_id, busy):
            return 409, {'ok': False, 'error': _store_dns_conflict(app_id, busy)}
        if busy:
            who = ', '.join('%d (%s)' % (k, v or 'programme inconnu') for k, v in sorted(busy.items()))
            return 409, {'ok': False, 'error': 'Port déjà utilisé sur ce NAS : %s. Arrêtez ce service avant d’installer cette app.' % who}
    users = body.get('users') if isinstance(body.get('users'), list) else None
    if users is not None:
        users = [str(u)[:64] for u in users if u][:50]
        if not users:
            return 400, {'ok': False, 'error': 'Choisissez au moins un utilisateur.'}
    if nas_cfg:
        _store_nas_set(app_id, nas_cfg)
    if fields_cfg is not None:
        _store_fields_set(app_id, fields_cfg)
    _store_job_set(app_id, action='update' if update else 'install', state='running', step='Démarrage', error='')
    _threading.Thread(target=_store_install_job, args=(app, users, update), name='store-' + app_id, daemon=True).start()
    return 202, {'ok': True, 'job': store_job_get(app_id)}


def store_uninstall(body):
    app_id = str((body or {}).get('id') or '')
    if not _STORE_ID_RE.match(app_id) or app_id not in store_installed_get():
        return 404, {'ok': False, 'error': 'App non installée.'}
    if _store_running(app_id):
        return 409, {'ok': False, 'error': 'Une opération est en cours sur cette app : attendez qu’elle se termine.'}
    keep = bool((body or {}).get('keep_data', True))
    module = (store_installed_get().get(app_id) or {}).get('kind') == 'module' or app_id in STORE_MODULE_IDS
    try:
        try:
            # Un module utilise des images publiques (nginx, php, mariadb) : on les garde pour une réinstallation rapide.
            _store_midclt('app.delete', 'dt-' + app_id, {'remove_images': not module}, job=True, timeout=600)
        except RuntimeError as e:
            if 'does not exist' not in str(e):
                raise
        if not keep and not module:
            _store_midclt('pool.dataset.delete', _store_dataset(app_id), {'recursive': True}, timeout=300)
        reg = store_installed_get(); reg.pop(app_id, None); _store_installed_set(reg)
        _store_nas_set(app_id, None)
        _store_fields_set(app_id, None)
        try:
            _store_access_forget(app_id)
        except Exception as e:
            log.warning('Magasin : droits de %s non retirés : %s', app_id, e)
        log.info('Magasin : %s désinstallée (données %s)', app_id, 'conservées' if keep else 'supprimées')
        return 200, {'ok': True}
    except Exception as e:
        return 502, {'ok': False, 'error': str(e)}


def store_start(body):
    app_id = str((body or {}).get('id') or '')
    if not _STORE_ID_RE.match(app_id) or app_id not in store_installed_get():
        return 404, {'ok': False, 'error': 'App non installée.'}
    try:
        store_net_ensure()
        _store_midclt('app.start', 'dt-' + app_id, job=True, timeout=600)
        store_activity_touch(app_id)
        if (_store_idle_all().get(app_id) or {}).get('asleep'):
            _store_idle_put(app_id, asleep=None)
        return 200, {'ok': True}
    except Exception as e:
        return 502, {'ok': False, 'error': str(e)}


def store_access_set(body):
    app_id = str((body or {}).get('id') or '')
    users = (body or {}).get('users')
    if not _STORE_ID_RE.match(app_id) or app_id not in store_installed_get():
        return 404, {'ok': False, 'error': 'App non installée.'}
    if not isinstance(users, list):
        return 400, {'ok': False, 'error': 'Liste des comptes invalide.'}
    try:
        _store_access_apply(app_id, [str(u)[:64] for u in users if u][:200], actor=str((body or {}).get('actor') or 'Desktroo')[:64])
    except RuntimeError as e:
        return 502, {'ok': False, 'error': str(e)}
    return 200, {'ok': True, 'users': store_access_users(app_id)}


def _store_icons_fetch(cat):
    """Copie locale des icônes du catalogue : le bureau les sert lui-même, sans appel à GitHub depuis le navigateur."""
    import urllib.request
    os.makedirs(STORE_ICON_DIR, exist_ok=True)
    for a in (cat or {}).get('apps', []):
        icon, app_id = str(a.get('icon') or ''), str(a.get('id') or '')
        ext = os.path.splitext(icon)[1].lower()
        if not icon or ext not in _STORE_ICON_TYPES or '..' in icon or icon.startswith('/') or not _STORE_ID_RE.match(app_id):
            continue
        try:
            req = urllib.request.Request(STORE_URL + '/' + icon, headers={'User-Agent': 'Desktroo'})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read(256 * 1024 + 1)
            if len(data) > 256 * 1024:
                continue
            for old in os.listdir(STORE_ICON_DIR):
                if old.startswith(app_id + '.'):
                    os.remove(os.path.join(STORE_ICON_DIR, old))
            with open(os.path.join(STORE_ICON_DIR, app_id + ext), 'wb') as fh:
                fh.write(data)
        except Exception as e:
            log.warning('Magasin : icône de %s non récupérée : %s', app_id, e)


def store_icon_path(app_id):
    if not _STORE_ID_RE.match(app_id or '') or not os.path.isdir(STORE_ICON_DIR):
        return None, None
    for ext, ctype in _STORE_ICON_TYPES.items():
        p = os.path.join(STORE_ICON_DIR, app_id + ext)
        if os.path.isfile(p):
            return p, ctype
    return None, None


def store_view():
    """Ce que montre la fenêtre Magasin : catalogue visible, apps installées, mises à jour, état de la licence."""
    cat, err = store_catalog()
    installed = {r['id']: r for r in store_installed_list()}
    apps = []
    reg = _lic_registered()
    for a in store_visible_apps(cat):
        a = dict(a)
        inst = installed.get(a['id'])
        a['installed'] = inst
        a['update_available'] = bool(inst and not inst.get('legacy') and not inst.get('linked') and _store_vtuple(a.get('version')) > _store_vtuple(inst.get('version')))
        a['update_allowed'] = bool(a['update_available']) and _store_update_allowed(a, inst, reg)
        a['security_update'] = bool(a['update_available'] and a.get('security') and _store_vtuple(a.get('security')) > _store_vtuple(inst.get('version')))
        a['compatible'] = _store_vtuple(a.get('min_desktroo')) <= _store_vtuple(APP_VERSION)
        j = store_job_get(a['id'])
        if inst and j.get('state') == 'done' and j.get('step') == 'Installée, mais elle ne répond pas encore' \
                and a.get('kind') != 'module' and inst.get('state') == 'RUNNING':
            try:   # l'app a fini de démarrer depuis : l'avertissement de l'installation tombe tout seul
                if _store_health(a, tries=1):
                    _store_job_set(a['id'], step='Installée')
                    j = store_job_get(a['id'])
            except Exception:
                pass
        a['job'] = j or None
        if a['id'] == STORE_NPM_ID:
            a['npm'] = store_npm_info()
        if a['id'] == STORE_NPM_ID or _store_host_ports(a):
            a['nas_ip'] = _truenas_ip() or SSH_HOST
        if inst and _store_install_fields(a):
            a['fields'] = _store_fields_get(a['id'])
        if inst and a.get('kind') != 'module':
            a['idle'] = store_idle_get(a['id'], a)
        cr = a.get('credentials')
        if inst and isinstance(cr, dict) and re.match(r'^[a-z][a-z0-9-]{0,30}$', str(cr.get('derive') or '')):
            a['credentials'] = {'user': str(cr.get('user') or ''), 'password': _store_derive(a['id'], cr['derive'])}
        else:
            a.pop('credentials', None)
        if a.get('nas_access') and inst and _store_nas_all(a):
            a['nas'] = {'mode': 'all'}
        elif a.get('nas_access') and inst:
            cfg = _store_nas_get(a['id']) or {}
            a['nas'] = {'user': cfg.get('user'), 'mode': cfg.get('mode'), 'paths': [m['path'] for m in cfg.get('mounts') or []]}
        apps.append(a)
    # Une app installée mais retirée du catalogue reste visible : on peut toujours l'ouvrir et la désinstaller.
    known = {a['id'] for a in apps}
    for app_id, inst in installed.items():
        if app_id not in known:
            apps.append({'id': app_id, 'name': inst.get('name') or app_id, 'version': inst.get('version'),
                         'kind': inst.get('kind') or 'app', 'installed': inst, 'orphan': True, 'update_available': False, 'compatible': True,
                         'job': store_job_get(app_id) or None})
    return {'ok': cat is not None, 'error': err, 'apps': apps, 'gate': store_gate('install'), 'beta': _store_beta_on(),
            'published': (cat or {}).get('published'), 'version': APP_VERSION}


# ── MDM-STORE-V1 · accès direct aux apps (sans ouvrir le bureau) ───────────────────────────────────────
# Une app « standalone » s'ouvre aussi dans un onglet ou depuis l'écran d'accueil du téléphone, à l'adresse du bureau
# (/a/<id>/). Sans session, nginx renvoie vers /a/_login : identifiants TrueNAS (et code de double authentification),
# comme pour le bureau. « Rester connecté » garde la session des apps 30 jours ; elle n'ouvre jamais le bureau.
STORE_REMEMBER_TTL = 30 * 86400


async def _store_login_async(username, password, otp):
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect('wss://%s/api/current' % TRUENAS_API_HOST, ssl=ctx, open_timeout=10,
                                  max_size=4 * 1024 * 1024) as ws:
        async def call(ident, method, params):
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 12))
                if isinstance(msg, dict) and msg.get('id') == ident:
                    return msg
        opts = {'login_options': {'user_info': False}}
        r = await call(1, 'auth.login_ex', [dict({'mechanism': 'PASSWORD_PLAIN', 'username': username, 'password': password}, **opts)])
        if r.get('error'):      # TrueNAS sans « login_ex » : l'ancien appel prend aussi le code
            r = await call(2, 'auth.login', [username, password, otp or None])
            if r.get('error') or r.get('result') is not True:
                return 'bad', None
        else:
            kind = (r.get('result') or {}).get('response_type')
            if kind == 'OTP_REQUIRED':
                if not otp:
                    return 'otp', None
                r = await call(3, 'auth.login_ex', [dict({'mechanism': 'OTP_TOKEN', 'otp_token': otp}, **opts)])
                kind = (r.get('result') or {}).get('response_type')
            if kind != 'SUCCESS':
                return 'bad', None
        me = await call(4, 'auth.me', [])
        return 'ok', (None if me.get('error') else me.get('result'))


def _store_safe_next(nxt):
    nxt = str(nxt or '')
    return nxt if re.match(r'^/a/[a-z0-9][a-z0-9-]{0,40}/[A-Za-z0-9._~/%-]*$', nxt) else ''


def store_login(handler):
    """POST /store/login (via /a/_login) : ouvre la session des apps. Rend toujours du JSON."""
    def reply(code, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Cache-Control', 'no-store')
        if cookie:
            handler.send_header('Set-Cookie', cookie)
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    if _login_blocked():
        return reply(429, {'ok': False, 'error': 'Trop de tentatives. Réessayez dans quelques minutes.'})
    try:
        n = int(handler.headers.get('Content-Length', 0) or 0)
        b = json.loads(handler.rfile.read(min(n, 8192)).decode('utf-8')) if n > 0 else {}
    except Exception:
        return reply(400, {'ok': False, 'error': 'Requête invalide.'})
    user = str(b.get('username') or '').strip()[:64]
    password = str(b.get('password') or '')
    otp = re.sub(r'\s', '', str(b.get('otp') or ''))[:12]
    if not user or not password:
        return reply(400, {'ok': False, 'error': 'Identifiant et mot de passe requis.'})
    try:
        state, me = asyncio.run(asyncio.wait_for(_store_login_async(user, password, otp), 30))
    except Exception as e:
        log.warning('Connexion aux apps : TrueNAS injoignable : %s', e)
        return reply(502, {'ok': False, 'error': 'TrueNAS injoignable : réessayez dans un instant.'})
    if state == 'otp':
        return reply(200, {'ok': False, 'need_otp': True})
    if state != 'ok':
        _login_note_failure()
        log.warning('Connexion aux apps refusée pour %s', user)
        return reply(401, {'ok': False, 'error': "Identifiant ou mot de passe incorrect, ou compte sans droits d'administration du NAS." if not otp else "Identifiant, mot de passe ou code incorrect, ou compte sans droits d'administration du NAS."})
    if not _truenas_is_full_admin(me):
        return reply(403, {'ok': False, 'error': "Ce compte n'a pas accès à Desktroo."})
    name = str((me or {}).get('pw_name') or user)[:64]
    ttl = STORE_REMEMBER_TTL if b.get('remember') else STORE_COOKIE_TTL
    nxt = _store_safe_next(b.get('next'))
    if nxt:
        m = re.match(r'^/a/([a-z0-9][a-z0-9-]{0,40})/', nxt)
        if m and not store_user_allowed(m.group(1), name):
            return reply(403, {'ok': False, 'error': "Votre compte n'a pas accès à cette app."})
    log.info('Connexion aux apps : %s%s', name, ' (30 jours)' if ttl == STORE_REMEMBER_TTL else '')
    return reply(200, {'ok': True, 'next': nxt or ''}, store_cookie_header(handler, store_cookie_make(name, ttl), ttl))


# Toute app installée peut s'installer comme une application de l'ordinateur (adresse /?app=<id>, sans le bureau autour).
# Applications du bureau Desktroo (Fichiers, Stockage, Terminal…) : mêmes adresses /?app=<id>, icône dessinée ici.
_KIOSK_SYS = json.loads(r'''{"filestation": {"l": "Fichiers", "a": "#1e7fe8", "b": "#0ea5e9", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2z\"/></svg>"}, "storage": {"l": "Stockage", "a": "#7c3aed", "b": "#a855f7", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><ellipse cx=\"12\" cy=\"5\" rx=\"9\" ry=\"3\"/><path d=\"M21 12c0 1.66-4.03 3-9 3S3 13.66 3 12M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5\"/></svg>"}, "apps": {"l": "Applications", "a": "#059669", "b": "#10b981", "s": "<svg viewBox=\"0 0 24 24\" fill=\"rgba(255,255,255,0.95)\"><rect x=\"3\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"14\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"3\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"14\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.5\"/></svg>"}, "docker": {"l": "Conteneurs", "a": "#0ea5e9", "b": "#38bdf8", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z\"/><polyline points=\"3.27 6.96 12 12.01 20.73 6.96\"/><line x1=\"12\" y1=\"22.08\" x2=\"12\" y2=\"12\"/></svg>"}, "catalog": {"l": "Catalogue d’applications", "a": "#c026d3", "b": "#f0abfc", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"2\" y=\"3\" width=\"9\" height=\"9\" rx=\"1.5\"/><rect x=\"13\" y=\"3\" width=\"9\" height=\"9\" rx=\"1.5\"/><rect x=\"2\" y=\"13\" width=\"9\" height=\"9\" rx=\"1.5\"/><rect x=\"13\" y=\"13\" width=\"9\" height=\"9\" rx=\"1.5\"/></svg>"}, "control": {"l": "Panneau de configuration", "a": "#dc2626", "b": "#f87171", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><line x1=\"4\" y1=\"6\" x2=\"20\" y2=\"6\"/><line x1=\"4\" y1=\"12\" x2=\"20\" y2=\"12\"/><line x1=\"4\" y1=\"18\" x2=\"20\" y2=\"18\"/><circle cx=\"8\" cy=\"6\" r=\"2\" fill=\"rgba(255,255,255,0.95)\" stroke=\"none\"/><circle cx=\"16\" cy=\"12\" r=\"2\" fill=\"rgba(255,255,255,0.95)\" stroke=\"none\"/><circle cx=\"10\" cy=\"18\" r=\"2\" fill=\"rgba(255,255,255,0.95)\" stroke=\"none\"/></svg>"}, "sysinfo": {"l": "Informations système", "a": "#334155", "b": "#64748b", "s": "<svg viewBox=\"0 0 24 24\" fill=\"rgba(255,255,255,0.95)\"><rect x=\"3\" y=\"13\" width=\"4\" height=\"8\" rx=\"1\"/><rect x=\"10\" y=\"8\" width=\"4\" height=\"13\" rx=\"1\"/><rect x=\"17\" y=\"4\" width=\"4\" height=\"17\" rx=\"1\"/></svg>"}, "network": {"l": "Réseau", "a": "#0d9488", "b": "#2dd4bf", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"12\" cy=\"5\" r=\"2.4\"/><circle cx=\"5\" cy=\"18\" r=\"2.4\"/><circle cx=\"19\" cy=\"18\" r=\"2.4\"/><line x1=\"12\" y1=\"7.2\" x2=\"6.2\" y2=\"15.9\"/><line x1=\"12\" y1=\"7.2\" x2=\"17.8\" y2=\"15.9\"/><line x1=\"7.4\" y1=\"18\" x2=\"16.6\" y2=\"18\"/></svg>"}, "update": {"l": "Mises à jour TrueNAS", "a": "#d97706", "b": "#fbbf24", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><polyline points=\"16 12 12 8 8 12\"/><line x1=\"12\" y1=\"16\" x2=\"12\" y2=\"8\"/></svg>"}, "notifs": {"l": "Alertes", "a": "#e11d48", "b": "#fb7185", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9\"/><path d=\"M13.73 21a2 2 0 01-3.46 0\"/></svg>"}, "reports": {"l": "Rapports", "a": "#4f46e5", "b": "#22d3ee", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><line x1=\"18\" y1=\"20\" x2=\"18\" y2=\"10\"/><line x1=\"12\" y1=\"20\" x2=\"12\" y2=\"4\"/><line x1=\"6\" y1=\"20\" x2=\"6\" y2=\"14\"/></svg>"}, "users": {"l": "Utilisateurs", "a": "#2563eb", "b": "#60a5fa", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2\"/><circle cx=\"9\" cy=\"7\" r=\"4\"/><path d=\"M23 21v-2a4 4 0 00-3-3.87\"/><path d=\"M16 3.13a4 4 0 010 7.75\"/></svg>"}, "sharing": {"l": "Partages", "a": "#ea580c", "b": "#fb923c", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"18\" cy=\"5\" r=\"3\"/><circle cx=\"6\" cy=\"12\" r=\"3\"/><circle cx=\"18\" cy=\"19\" r=\"3\"/><line x1=\"8.59\" y1=\"13.51\" x2=\"15.42\" y2=\"17.49\"/><line x1=\"15.41\" y1=\"6.51\" x2=\"8.59\" y2=\"10.49\"/></svg>"}, "websites": {"l": "Sites Web", "a": "#0ea5e9", "b": "#22c55e", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><line x1=\"3\" y1=\"12\" x2=\"21\" y2=\"12\"/><path d=\"M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z\"/></svg>"}, "downloads": {"l": "Téléchargements", "a": "#4f46e5", "b": "#818cf8", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4\"/><polyline points=\"7 10 12 15 17 10\"/><line x1=\"12\" y1=\"15\" x2=\"12\" y2=\"3\"/></svg>"}, "license": {"l": "Licence", "a": "#f59e0b", "b": "#fbbf24", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"7.5\" cy=\"15.5\" r=\"3.5\"/><path d=\"M10 13l6-6\"/><path d=\"M14.5 4.5l4 0 0 4\"/><path d=\"M15 9l2.5 2.5\"/></svg>"}, "services": {"l": "Services", "a": "#475569", "b": "#94a3b8", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"12\" cy=\"12\" r=\"3\"/><path d=\"M19.07 4.93l-1.41 1.41M5.34 17.66l-1.41 1.41M20 12h-2M6 12H4M19.07 19.07l-1.41-1.41M5.34 6.34L3.93 4.93M12 20v-2M12 6V4\"/></svg>"}, "certs": {"l": "Certificats", "a": "#b45309", "b": "#facc15", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3\" y=\"11\" width=\"18\" height=\"11\" rx=\"2\"/><path d=\"M7 11V7a5 5 0 0110 0v4\"/></svg>"}, "health": {"l": "Bilan de santé", "a": "#0a8451", "b": "#3ecf8e", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M3 12h4l2.5-7 4 14 2.5-7H21\"/></svg>"}, "activity": {"l": "Journal d'activité", "a": "#0f766e", "b": "#5eead4", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><polyline points=\"22 12 18 12 15 20 9 4 6 12 2 12\"/></svg>"}, "dataprotect": {"l": "Protection des données", "a": "#15803d", "b": "#4ade80", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z\"/></svg>"}, "terminal": {"l": "Terminal", "a": "#0f172a", "b": "#1e293b", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><polyline points=\"4 17 10 11 4 5\"/><line x1=\"12\" y1=\"19\" x2=\"20\" y2=\"19\"/></svg>"}, "about": {"l": "À propos", "a": "#6366f1", "b": "#a855f7", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"12\" cy=\"12\" r=\"10\"/><line x1=\"12\" y1=\"16\" x2=\"12\" y2=\"12\"/><line x1=\"12\" y1=\"8\" x2=\"12.01\" y2=\"8\"/></svg>"}, "vms": {"l": "VMs TrueNAS (ancien)", "a": "#0369a1", "b": "#0ea5e9", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"2\" y=\"3\" width=\"20\" height=\"14\" rx=\"2\"/><path d=\"M8 21h8M12 17v4\"/><path d=\"M7 8l2 2-2 2M13 10h4\"/></svg>"}, "cron": {"l": "Tâches planifiées", "a": "#0ea5e9", "b": "#6366f1", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><polyline points=\"12 7 12 12 15 14\"/></svg>"}, "bootenv": {"l": "Environnements de démarrage", "a": "#f59e0b", "b": "#ef4444", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M12 3v8\"/><path d=\"M6.8 6.4a8 8 0 1 0 10.4 0\"/></svg>"}, "snapshots": {"l": "Snapshots ZFS", "a": "#8b5cf6", "b": "#06b6d4", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M12 3 2 8l10 5 10-5-10-5z\"/><path d=\"M2 12l10 5 10-5\"/><path d=\"M2 16l10 5 10-5\"/></svg>"}, "maintenance": {"l": "Disques & Scrub", "a": "#14b8a6", "b": "#0ea5e9", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><ellipse cx=\"12\" cy=\"6\" rx=\"8\" ry=\"3\"/><path d=\"M4 6v6c0 1.66 3.58 3 8 3s8-1.34 8-3V6\"/><path d=\"M4 12v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6\"/></svg>"}, "libvirt": {"l": "VMs QEMU/KVM", "a": "#0369a1", "b": "#0ea5e9", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"2\" y=\"3\" width=\"20\" height=\"14\" rx=\"2\"/><path d=\"M8 21h8M12 17v4\"/><circle cx=\"7\" cy=\"8\" r=\"1.5\" fill=\"rgba(255,255,255,0.95)\"/><circle cx=\"12\" cy=\"8\" r=\"1.5\" fill=\"rgba(255,255,255,0.95)\"/><circle cx=\"17\" cy=\"8\" r=\"1.5\" fill=\"rgba(255,255,255,0.95)\"/></svg>"}}''')
# MDM-KIOSK-SYS-2 : Desktroo Apps et Droits d'accès, installables comme les autres apps du bureau (sans icône jusqu'en 2.60.2).
_KIOSK_SYS.update(json.loads(r'''{"store": {"l": "Desktroo Apps", "a": "#0a8451", "b": "#3ecf8e", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M5 8h14l-1.2 11.2a2 2 0 0 1-2 1.8H8.2a2 2 0 0 1-2-1.8z\"/><path d=\"M9 8V6.5a3 3 0 0 1 6 0V8\"/><path d=\"M9.5 13.5l1.8 1.8 3.2-3.6\"/></svg>"}, "access": {"l": "Droits d’accès", "a": "#0078d4", "b": "#7c3aed", "s": "<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"rgba(255,255,255,0.95)\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z\"/><path d=\"M8.5 12l2.2 2.2 4.8-5\"/></svg>"}}'''))


def store_sys_icon(app_id):
    d = _KIOSK_SYS.get(app_id)
    if not d or not re.match(r'^#[0-9a-fA-F]{3,6}$', d['a']) or not re.match(r'^#[0-9a-fA-F]{3,6}$', d['b']):
        return None
    glyph = d['s'].replace('<svg ', '<svg x="116" y="116" width="280" height="280" ', 1)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient></defs>'
            '<rect width="512" height="512" rx="112" fill="url(#g)"/>%s</svg>' % (d['a'], d['b'], glyph)).encode('utf-8')


# Icônes PNG des apps installables (Windows ne fabrique pas le raccourci d'une SVG : il met une simple lettre).
# Le navigateur de la personne les dessine à partir des icônes SVG et les dépose ici ; elles servent dans les manifestes.
KIOSK_ICON_DIR = os.path.join(STORE_DIR, 'kiosk-icons')
_KIOSK_ICON_NAME = re.compile(r'^([a-z0-9][a-z0-9-]{0,40})-(192|512)\.png$')


def _store_kiosk_inst(app_id):
    """Fiche d'une app pour son manifeste « Desktroo <app> » : app installée par Desktroo Apps, ou app RELIÉE
    (NPMplus, AdGuard Home, Pi-hole sur une autre machine). Sans cela, une app reliée n'avait ni manifeste ni icône
    à elle : le navigateur installait la page avec l'icône de Desktroo, ou proposait d'ouvrir l'app Desktroo."""
    inst = store_installed_get().get(app_id)
    if inst:
        return inst
    try:
        for aid, label, fn in ((STORE_NPM_ID, 'NPMplus', store_npm_linked), (STORE_AG_ID, 'AdGuard Home', store_ag_linked),
                               (STORE_PH_ID, 'Pi-hole', store_ph_linked)):
            if app_id == aid and fn():
                return {'name': {'fr': label, 'en': label}, 'linked': True}
    except Exception:
        pass
    return None


def store_kiosk_icon_put(b):
    import base64 as _b64
    b = b if isinstance(b, dict) else {}
    app = str(b.get('id') or '')
    if not re.match(r'^[a-z0-9][a-z0-9-]{0,40}$', app) or not (app in _KIOSK_SYS or _store_kiosk_inst(app)):
        return 400, {'ok': False, 'error': 'App inconnue.'}
    os.makedirs(KIOSK_ICON_DIR, exist_ok=True)
    saved = 0
    for size in (192, 512):
        try:
            data = _b64.b64decode(str(b.get('png%d' % size) or ''), validate=True)
        except Exception:
            continue
        if len(data) > 300000 or len(data) < 70 or not data.startswith(b'\x89PNG\r\n\x1a\n'):
            continue
        if struct.unpack('>II', data[16:24]) != (size, size):
            continue
        path = os.path.join(KIOSK_ICON_DIR, '%s-%d.png' % (app, size))
        with open(path + '.tmp', 'wb') as f:
            f.write(data)
        os.replace(path + '.tmp', path)
        saved += 1
    return 200, {'ok': True, 'saved': saved}


def store_kiosk_icon_ids():
    try:
        have = set(os.listdir(KIOSK_ICON_DIR))
    except OSError:
        return []
    return sorted(i for i in {m.group(1) for m in (_KIOSK_ICON_NAME.match(n) for n in have) if m}
                  if ('%s-192.png' % i) in have and ('%s-512.png' % i) in have)


def _kiosk_png_icons(app_id):
    out = []
    for size in (192, 512):
        p = os.path.join(KIOSK_ICON_DIR, '%s-%d.png' % (app_id, size))
        try:
            v = int(os.path.getmtime(p))
        except OSError:
            return []
        out.append({'src': '/fileops/store/kiosk-icon/%s-%d.png?v=%d' % (app_id, size, v), 'sizes': '%dx%d' % (size, size), 'type': 'image/png', 'purpose': 'any'})
    return out


def store_kiosk_manifest(app_id):
    """Manifeste « Desktroo <app> » : la fenêtre de l'app, seule, dans sa propre fenêtre Windows/Mac/Linux."""
    if not _STORE_ID_RE.match(app_id or ''):
        return None
    inst = _store_kiosk_inst(app_id)
    if not inst and app_id in _KIOSK_SYS:
        d = _KIOSK_SYS[app_id]
        return {'name': 'Desktroo ' + d['l'], 'short_name': d['l'][:12], 'id': '/k/' + app_id + '/', 'start_url': '/k/' + app_id + '/',
                'scope': '/k/' + app_id + '/', 'display': 'standalone', 'background_color': '#0a1220', 'theme_color': '#0a1220',
                'icons': _kiosk_png_icons(app_id) + [{'src': '/fileops/store/sys-icon/' + app_id, 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any'}]}
    if not inst:
        return None
    name = inst.get('name')
    if isinstance(name, dict):
        name = name.get('fr') or name.get('en')
    name = str(name or app_id)
    p, ctype = store_icon_path(app_id)
    icons = _kiosk_png_icons(app_id) + ([{'src': '/fileops/store/icon/' + app_id, 'sizes': 'any', 'type': ctype, 'purpose': 'any'}] if p else [])
    return {'name': 'Desktroo ' + name, 'short_name': name[:12], 'id': '/k/' + app_id + '/', 'start_url': '/k/' + app_id + '/',
            'scope': '/k/' + app_id + '/', 'display': 'standalone', 'background_color': '#0a1220', 'theme_color': '#0a1220', 'icons': icons}


def store_manifest(app_id):
    """Manifeste d'une app « standalone » : elle s'installe comme une application sur le téléphone ou l'ordinateur."""
    inst = store_installed_get().get(app_id)
    if not inst or not inst.get('standalone'):
        return None
    name = inst.get('name')
    if isinstance(name, dict):
        name = name.get('fr') or name.get('en')
    p, ctype = store_icon_path(app_id)
    icons = _kiosk_png_icons(app_id) + ([{'src': '/fileops/store/icon/' + app_id, 'sizes': 'any', 'type': ctype, 'purpose': 'any'}] if p else [])
    return {'name': str(name or app_id), 'short_name': str(name or app_id)[:12], 'id': '/a/%s/' % app_id,
            'start_url': '/a/%s/' % app_id, 'scope': '/a/%s/' % app_id, 'display': 'standalone',
            'background_color': '#0a1220', 'theme_color': '#0a1220', 'icons': icons}


_STORE_LOGIN_HTML = r'''<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Connexion — Desktroo</title><link rel="stylesheet" href="/a/_sdk/desktroo-app.css">
<script>try{var t=localStorage.getItem('tn-theme');if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t)}catch(e){}</script>
<style>
body{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.box{width:100%;max-width:360px;background:var(--surface);border:1px solid var(--glass-border);border-radius:var(--radius);padding:28px 26px}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:6px;font:700 22px var(--font-display)}
.logo svg{width:34px;height:34px}.sub{margin:0 0 20px;color:var(--text-dim);font-size:13px}
label{display:block;font-size:12px;color:var(--text-dim);margin:12px 0 5px}
.row{display:flex;align-items:center;gap:8px;margin:16px 0 18px;font-size:13px;color:var(--text-dim)}
.row label{display:flex;align-items:center;gap:8px;margin:0;font-size:13px;cursor:pointer}
.err{min-height:18px;color:var(--danger);font-size:13px;margin:0 0 10px}.dk-btn{width:100%}
.hidden{display:none}
</style></head><body>
<form class="box" id="f" autocomplete="on">
 <div class="logo"><svg viewBox="4 4 56 56" aria-hidden="true"><rect x="4" y="4" width="56" height="56" rx="15" fill="#0f1a2e"/><circle cx="26" cy="37" r="11" fill="none" stroke="#e9eef7" stroke-width="6"/><circle cx="26" cy="37" r="11" fill="none" stroke="#3ecf8e" stroke-width="6" stroke-linecap="round" stroke-dasharray="17 52.2"/><rect x="37" y="12" width="6.5" height="38" rx="3.25" fill="#e9eef7"/><circle cx="26" cy="37" r="4.6" fill="#3ecf8e"/></svg>Desktroo</div>
 <p class="sub" id="sub">Connectez-vous pour ouvrir l'app.</p>
 <div id="step1"><label for="u">Identifiant TrueNAS</label><input class="dk-input" id="u" name="username" autocomplete="username" required autofocus>
 <label for="p">Mot de passe</label><input class="dk-input" id="p" name="password" type="password" autocomplete="current-password" required></div>
 <div id="step2" class="hidden"><label for="o">Code de double authentification</label>
 <input class="dk-input" id="o" name="otp" inputmode="numeric" autocomplete="one-time-code" maxlength="12"></div>
 <div class="row"><label><input type="checkbox" id="r"> Rester connecté 30 jours sur cet appareil</label></div>
 <p class="err" id="e" role="alert"></p>
 <button class="dk-btn primary" id="b" type="submit">Se connecter</button>
</form>
<script>
(function(){
 var q=new URLSearchParams(location.search), next=q.get('next')||'';
 if(!/^\/a\/[a-z0-9][a-z0-9-]{0,40}\//.test(next)) next='';
 var f=document.getElementById('f'), e=document.getElementById('e'), b=document.getElementById('b');
 f.onsubmit=function(ev){
  ev.preventDefault(); e.textContent=''; b.disabled=true; b.textContent='Vérification…';
  fetch('/a/_login',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',
   body:JSON.stringify({username:f.u.value.trim(),password:f.p.value,otp:f.o.value,remember:document.getElementById('r').checked,next:next})})
  .then(function(r){return r.json().catch(function(){return {ok:false,error:'Réponse inattendue du NAS.'}})})
  .then(function(d){
   b.disabled=false; b.textContent='Se connecter';
   if(d.ok){ location.replace(d.next||next||'/'); return; }
   if(d.need_otp){ document.getElementById('step2').classList.remove('hidden'); document.getElementById('sub').textContent='Votre compte est protégé par la double authentification.'; f.o.focus(); return; }
   e.textContent=d.error||'Connexion impossible.';
  }).catch(function(){ b.disabled=false; b.textContent='Se connecter'; e.textContent='NAS injoignable.'; });
 };
})();
</script></body></html>'''


# ── MDM-STORE-V1 · module « Serveur web » (dt-webserver) ───────────────────────────────────────────────
# Le serveur web de Desktroo (nginx des sites, PHP 8.3/8.2/8.1/7.4, MariaDB) n'est plus dans la pile de base :
# c'est un module de Desktroo Apps. Mêmes noms de conteneurs et mêmes dossiers de données qu'avant, pour que
# tout le code des Sites Web reste valable. Les installations existantes sont migrées une fois, avec retour arrière.
STORE_WEB_ID = 'webserver'
STORE_WEB_CONTAINERS = ['desktroo-websites', 'desktroo-php83', 'desktroo-php82', 'desktroo-php81', 'desktroo-php74', 'desktroo-mariadb']
STORE_WEB_SERVICES = ['websites', 'php83', 'php82', 'php81', 'php74', 'mariadb']
STORE_WEB_MIGRATION_FILE = os.path.join(STORE_DIR, 'migration-webserver.json')

# Commandes reprises telles quelles de l'assistant d'installation ('$$' = '$' littéral pour docker compose).
_STORE_PHP_RELOAD = (
    "apk add --no-cache curl >/dev/null 2>&1 || true; "
    "[ -x /usr/local/bin/install-php-extensions ] || { curl -sSLf https://github.com/mlocati/docker-php-extension-installer/releases/latest/download/install-php-extensions -o /usr/local/bin/install-php-extensions && chmod +x /usr/local/bin/install-php-extensions; }; "
    "[ -s /conf/extensions.txt ] && install-php-extensions $$(cat /conf/extensions.txt) || true; "
    "( last=''; lastext=''; while true; do e=$$(cat /conf/.extreload 2>/dev/null); if [ \"$$e\" != \"$$lastext\" ]; then lastext=\"$$e\"; { [ -s /conf/extensions.txt ] && install-php-extensions $$(cat /conf/extensions.txt) >/dev/null 2>&1; } || true; kill -USR2 1 2>/dev/null || true; fi; v=$$(cat /conf/.reload 2>/dev/null); if [ \"$$v\" != \"$$last\" ]; then last=\"$$v\"; kill -USR2 1 2>/dev/null || true; fi; sleep 3; done ) & exec php-fpm"
)
_STORE_WEB_RELOAD = "last=''; ( while true; do v=$$(cat /etc/nginx/conf.d/.reload 2>/dev/null); if [ \"$$v\" != \"$$last\" ]; then last=\"$$v\"; nginx -t && nginx -s reload; fi; sleep 3; done ) & exec nginx -g 'daemon off;'"


def store_webserver_compose(app):
    """Configuration du module, fabriquée ici (comme l'assistant le faisait dans la pile de base)."""
    if not APP_DIR or not WEB_CONF_DIR or not WEB_PHP_DIR:
        raise RuntimeError("Dossier d'installation de Desktroo introuvable.")
    images = (app or {}).get('images') or {}
    img = lambda k, d: str(images.get(k) or d)
    services = {}
    for ver in ('8.3', '8.2', '8.1', '7.4'):
        n = 'php' + ver.replace('.', '')
        services[n] = {
            'image': img(n, 'php:%s-fpm-alpine' % ver), 'container_name': 'desktroo-' + n, 'restart': 'unless-stopped',
            'environment': {'PHP_INI_SCAN_DIR': ':/conf/ini'},
            'volumes': ['/mnt:/mnt', '%s/%s:/conf' % (WEB_PHP_DIR.rstrip('/'), ver)],
            'command': ['sh', '-c', _STORE_PHP_RELOAD],
        }
    services['websites'] = {
        'image': img('nginx', 'nginx:alpine'), 'container_name': 'desktroo-websites', 'restart': 'unless-stopped',
        'ports': ['%d:8080' % WEB_PROXY_PORT, '%d-%d:%d-%d' % (WEB_PORT_MIN, WEB_PORT_MAX, WEB_PORT_MIN, WEB_PORT_MAX)],
        'volumes': ['/mnt:/mnt', WEB_CONF_DIR.rstrip('/') + ':/etc/nginx/conf.d'],
        'command': ['sh', '-c', _STORE_WEB_RELOAD],
        'depends_on': ['php83', 'php82', 'php81', 'php74'],
    }
    services['mariadb'] = {
        'image': img('mariadb', 'mariadb:11'), 'container_name': 'desktroo-mariadb', 'restart': 'unless-stopped',
        'environment': {'MARIADB_ROOT_PASSWORD': DB_ROOT_PASSWORD, 'MARIADB_AUTO_UPGRADE': '1'},
        'volumes': [APP_DIR.rstrip('/') + '/mariadb:/var/lib/mysql'],
        # « mariadb » reste joignable par Desktroo (fileops) à travers le réseau partagé des apps.
        'networks': {'default': {}, STORE_NET: {'aliases': ['mariadb']}},
    }
    return {'services': services, 'networks': {STORE_NET: {'external': True}}}


def _store_container_info(name):
    """(existe, en marche, projet compose, fichier compose) d'un conteneur."""
    fmt = '{{.State.Running}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}'
    out, _e, code = _px_host('docker inspect --format %s %s 2>/dev/null' % (shq(fmt), shq(name)), timeout=30)
    if code != 0 or not out.strip():
        return False, False, '', ''
    run, proj, files = (out.strip().split('|') + ['', '', ''])[:3]
    return True, run == 'true', proj, files


def store_webserver_legacy():
    """Le serveur web tourne-t-il encore dans la pile de base (installation d'avant le module) ?"""
    exists, running, proj, files = _store_container_info('desktroo-websites')
    if not exists or proj == 'ix-dt-' + STORE_WEB_ID:
        return None
    return {'running': running, 'project': proj, 'compose': files.split(',')[0] if files else ''}


def _store_compose_drop_services(text, names):
    """Retire des services d'un docker-compose.yml écrit par l'assistant (blocs indentés de 2 espaces sous services:)."""
    out, skip, in_services = [], False, False
    for line in text.splitlines(True):
        if re.match(r'^\S', line):
            in_services = line.startswith('services:')
            skip = False
        elif in_services:
            m = re.match(r'^  ([A-Za-z0-9_.-]+):\s*$', line)
            if m:
                skip = m.group(1) in names
        if not skip:
            out.append(line)
    return ''.join(out)


def _store_web_wait_running(timeout=120):
    end = _st_time.time() + timeout
    while _st_time.time() < end:
        if all(_store_container_info(n)[1] for n in ('desktroo-websites', 'desktroo-mariadb')):
            return True
        _st_time.sleep(3)
    return False


def store_migrate_webserver():
    """Déplace le serveur web de la pile de base vers le module dt-webserver. Une fois ; retour arrière complet."""
    legacy = store_webserver_legacy()
    if not legacy or STORE_WEB_ID in store_installed_get():
        return
    state = _lic_read_json(STORE_WEB_MIGRATION_FILE) or {}
    if state.get('done') or int(state.get('attempts', 0)) >= 3:
        return
    state['attempts'] = int(state.get('attempts', 0)) + 1
    os.makedirs(STORE_DIR, exist_ok=True)
    _lic_write_json(STORE_WEB_MIGRATION_FILE, state)
    compose_file = legacy.get('compose') or ''
    if legacy['project'].startswith('ix-') or not compose_file or not os.path.isfile(compose_file):   # fichier lu à travers /mnt
        state.update(skipped='Pile installée autrement que par l’assistant (Custom App) : migration à faire à part.')
        _lic_write_json(STORE_WEB_MIGRATION_FILE, state)
        log.warning('Magasin : serveur web non migré : %s', state['skipped'])
        return
    log.info('Magasin : migration du serveur web vers le module (essai %d)', state['attempts'])
    _store_job_set(STORE_WEB_ID, action='migrate', state='running', step='Migration du serveur web', error='')
    backup = compose_file + '.avant-module-web'
    renamed = []
    try:
        store_net_ensure()
        # 1. La pile de base perd ses services web (copie gardée, fichier vérifié par docker compose).
        original = open(compose_file, encoding='utf-8').read()
        new = _store_compose_drop_services(original, STORE_WEB_SERVICES)
        if new == original:
            raise RuntimeError('services web introuvables dans ' + compose_file)
        with open(backup, 'w', encoding='utf-8') as fh:
            fh.write(original)
        with open(compose_file + '.module-web.tmp', 'w', encoding='utf-8') as fh:
            fh.write(new)
        _o, err, code = _px_host('cd %s && docker compose -f %s config -q' % (shq(os.path.dirname(compose_file)), shq(compose_file + '.module-web.tmp')), timeout=60)
        if code != 0:
            raise RuntimeError('fichier compose refusé : ' + (err or _o)[-300:])
        with open(compose_file, 'r+', encoding='utf-8') as fh:
            fh.seek(0); fh.write(new); fh.truncate()
        os.remove(compose_file + '.module-web.tmp')
        # 2. Les anciens conteneurs s'arrêtent et sont mis de côté (renommés), pas supprimés.
        for n in STORE_WEB_CONTAINERS:
            if _store_container_info(n)[0]:
                _px_host('docker stop -t 20 %s >/dev/null && docker rename %s %s' % (shq(n), shq(n), shq(n + '-avant-module')), timeout=60)
                renamed.append(n)
        # 3. Le module prend le relais, sur les mêmes données.
        app = store_app_entry(STORE_WEB_ID) or {'id': STORE_WEB_ID, 'version': '1.0.0', 'name': {'fr': 'Serveur web'}}
        _store_midclt('app.create', {'custom_app': True, 'app_name': 'dt-' + STORE_WEB_ID,
                                     'custom_compose_config': store_webserver_compose(app)}, job=True, timeout=1800)
        if not _store_web_wait_running():
            raise RuntimeError('le serveur web ne redémarre pas')
        # 4. Réussi : on retire les anciens conteneurs.
        for n in renamed:
            _px_host('docker rm %s >/dev/null 2>&1' % shq(n + '-avant-module'), timeout=60)
        reg = store_installed_get()
        reg[STORE_WEB_ID] = {'name': app.get('name'), 'version': app.get('version'), 'kind': 'module', 'image': '',
                             'installed_at': int(_st_time.time()), 'updated_at': int(_st_time.time()), 'migrated': True}
        _store_installed_set(reg)
        state.update(done=True, at=int(_st_time.time()), error='')
        _lic_write_json(STORE_WEB_MIGRATION_FILE, state)
        _store_job_set(STORE_WEB_ID, state='done', step='Installée', error='')
        log.info('Magasin : serveur web migré vers le module dt-webserver')
    except Exception as e:
        log.warning('Magasin : migration du serveur web en échec, retour arrière : %s', e)
        try:
            left = _store_midclt('app.query', [['name', '=', 'dt-' + STORE_WEB_ID]], {'select': ['name']}, timeout=60)
            if left:
                _store_midclt('app.delete', 'dt-' + STORE_WEB_ID, {'remove_images': False}, job=True, timeout=600)
        except Exception:
            pass
        for n in renamed:
            _px_host('docker rename %s %s 2>/dev/null; docker start %s >/dev/null 2>&1' % (shq(n + '-avant-module'), shq(n), shq(n)), timeout=60)
        try:
            if os.path.exists(backup):
                with open(compose_file, 'r+', encoding='utf-8') as fh:
                    fh.seek(0); fh.write(open(backup, encoding='utf-8').read()); fh.truncate()
        except Exception:
            pass
        state.update(error=str(e)[:400])
        _lic_write_json(STORE_WEB_MIGRATION_FILE, state)
        _store_job_set(STORE_WEB_ID, state='error', error='Migration annulée, le serveur web tourne comme avant : ' + str(e)[:200])


# ── MDM-STORE-V1 · module « NPMplus » (dt-npmplus) ─────────────────────────────────────────────────────
# Reverse-proxy HTTPS (certificats Let's Encrypt, domaines publics) installé en un clic depuis Desktroo Apps.
# TrueNAS garde 80/443 pour son interface : NPMplus écoute sur 8090 (http), 8453 (https) et 8181 (admin) ; la box
# redirige 80→8090 et 443→8453. Réseau de l'hôte, comme le recommande NPMplus. Données dans <pool>/desktroo-apps/npmplus.
# Un NPMplus installé à la main avant (commandes guidées de Sites Web) se « reprend » sur demande : mêmes ports,
# mêmes réglages, données copiées dans le dataset, l'original gardé tel quel ; retour arrière si ça échoue.
STORE_NPM_ID = 'npmplus'
STORE_NPM_CONTAINER = 'desktroo-npmplus'
STORE_NPM_FILE = os.path.join(STORE_DIR, 'npmplus.json')
STORE_NPM_DEFAULT_PORTS = {'http': 8090, 'https': 8453, 'admin': 8181}
# Réglages d'un NPMplus existant qu'on garde à la reprise (préfixes des variables d'environnement documentées).
_STORE_NPM_KEEP = ('TZ', 'ACME_', 'TRUST_', 'DISABLE_', 'LOGROTAT', 'GOA', 'NGINX_', 'CUSTOM_OCSP', 'DEFAULT_CERT_ID',
                   'IPV4_BINDING', 'IPV6_BINDING', 'NPM_IPV', 'NPM_LISTEN', 'OIDC_', 'AUTH_REQUEST_', 'CERTBOT_',
                   'ECH_', 'ENABLE_MPTCP', 'LISTEN_PROXY', 'INITIAL_DEFAULT_PAGE', 'COOKIE_SECRET', 'PUID', 'PGID')
STORE_MODULE_IDS = ('webserver', 'npmplus', 'pass')


def store_npm_settings():
    s = _lic_read_json(STORE_NPM_FILE) or {}
    ports = dict(STORE_NPM_DEFAULT_PORTS)
    ports.update({k: int(v) for k, v in (s.get('ports') or {}).items() if k in ports and str(v).isdigit()})
    s['ports'] = ports
    if not s.get('cookie_secret'):
        s['cookie_secret'] = _px_secrets.token_hex(32)
    return s


def _store_npm_settings_save(s):
    os.makedirs(STORE_DIR, exist_ok=True)
    _lic_write_json(STORE_NPM_FILE, s)


def store_npm_compose(app, s):
    images = (app or {}).get('images') or {}
    p = s['ports']
    env = {'TZ': _store_timezone()}
    env.update({k: str(v) for k, v in (s.get('env') or {}).items() if re.match(r'^[A-Z_][A-Z0-9_]*$', str(k))})
    env.update({'HTTP_PORT': str(p['http']), 'HTTPS_PORT': str(p['https']), 'NPM_PORT': str(p['admin']),
                'COOKIE_SECRET': s['cookie_secret']})
    svc = {
        'image': str(images.get('npmplus') or 'docker.io/zoeyvid/npmplus:latest'),
        'container_name': STORE_NPM_CONTAINER, 'restart': 'unless-stopped',
        'network_mode': 'host',
        'cap_drop': ['ALL'], 'cap_add': ['NET_BIND_SERVICE', 'SETGID'],
        'security_opt': ['no-new-privileges:true'],
        'volumes': [(s.get('data') or '/mnt/' + _store_dataset(STORE_NPM_ID)).rstrip('/') + ':/data'],
        'environment': env,
        'labels': {'fr.desktroo.store': STORE_NPM_ID, 'fr.desktroo.version': str((app or {}).get('version', ''))},
    }
    return {'services': {'npmplus': svc}}


def _store_npm_inspect_all():
    """Conteneurs NPMplus / Nginx Proxy Manager présents sur le NAS (docker inspect décodé)."""
    out, _e, code = _px_host("docker ps -a --format '{{.Names}}|{{.Image}}' 2>/dev/null", timeout=30)
    if code != 0:
        return []
    names = []
    for line in (out or '').splitlines():
        name, _s, image = line.strip().partition('|')
        low = image.lower()
        if name and name != STORE_NPM_CONTAINER and ('npmplus' in low or 'nginx-proxy-manager' in low):
            names.append(name)
    if not names:
        return []
    out, _e, code = _px_host('docker inspect ' + ' '.join(shq(n) for n in names) + ' 2>/dev/null', timeout=30)
    try:
        return json.loads(out) if code == 0 else []
    except Exception:
        return []


def store_npm_foreign():
    """Un reverse-proxy installé hors de Desktroo Apps ? Résumé utile à la fiche (ou None)."""
    for c in _store_npm_inspect_all():
        cfg, hc, st = c.get('Config') or {}, c.get('HostConfig') or {}, c.get('State') or {}
        labels = cfg.get('Labels') or {}
        image = str(cfg.get('Image') or '')
        project = str(labels.get('com.docker.compose.project') or '')
        env = {}
        for kv in cfg.get('Env') or []:
            k, _s, v = str(kv).partition('=')
            env[k] = v
        host_net = str(hc.get('NetworkMode') or '') == 'host'
        if host_net:
            ports = {'http': int(env.get('HTTP_PORT') or 80), 'https': int(env.get('HTTPS_PORT') or 443),
                     'admin': int(env.get('NPM_PORT') or 81)}
        else:
            pb = hc.get('PortBindings') or {}
            def hp(key, d):
                try:
                    return int(((pb.get(key) or [{}])[0] or {}).get('HostPort') or d)
                except Exception:
                    return d
            # Réseau ponté : NPMplus écoute dans son conteneur sur ses ports (variables, sinon 80/443/81),
            # publiés sur le NAS sous le même numéro ou un autre (app TrueNAS : 30360-30362, par exemple).
            inner = {'http': int(env.get('HTTP_PORT') or 80), 'https': int(env.get('HTTPS_PORT') or 443),
                     'admin': int(env.get('NPM_PORT') or 81)}
            ports = {k: hp('%d/tcp' % v, v) for k, v in inner.items()}
        data = ''
        for m in c.get('Mounts') or []:
            if m.get('Destination') == '/data':
                data = str(m.get('Source') or '')
        name = str(c.get('Name') or '').lstrip('/')
        why = ''
        if 'npmplus' not in image.lower():
            why = 'Nginx Proxy Manager classique : sa reprise n’est pas prise en charge. Gardez-le, ou remplacez-le à la main par NPMplus.'
        elif project.startswith('ix-'):
            why = ('Installé comme app TrueNAS (%s) : Desktroo Apps ne le reprend pas, ses mises à jour restent dans Applications. '
                   'Vous pouvez le relier à Desktroo pour gérer vos domaines depuis la fenêtre Domaines.' % project[3:])
        elif not data:
            why = 'Dossier de données introuvable sur ce conteneur.'
        return {'container': name, 'image': image, 'running': bool(st.get('Running')), 'project': project,
                'data': data, 'ports': ports, 'host_network': host_net, 'adoptable': not why, 'why': why,
                'env': {k: v for k, v in env.items() if k.startswith(_STORE_NPM_KEEP)}}
    return None


def _store_npm_ports_busy(ports, ignore=''):
    """Ports déjà pris sur le NAS (liste)."""
    out, _e, _c = _px_host("ss -Hltn 2>/dev/null | awk '{print $4}'", timeout=30)
    used = set()
    for line in (out or '').splitlines():
        m = re.search(r':(\d+)$', line.strip())
        if m:
            used.add(int(m.group(1)))
    return [p for p in ports.values() if p in used]


def _store_npm_prepare(app):
    """Configuration du module NPMplus pour une installation neuve ou une mise à jour."""
    s = store_npm_settings()
    if not s.get('data'):
        ds = _store_dataset(STORE_NPM_ID)
        parent = ds.rsplit('/', 1)[0]
        for d in (parent, ds):
            out, _e, code = _px_host('zfs list -H -o name ' + shq(d) + ' 2>/dev/null', timeout=30)
            if code != 0 or not out.strip():
                _store_ds_create(d)
        s['data'] = '/mnt/' + ds
    _store_npm_settings_save(s)
    return s


def _store_npm_wait(s, timeout=180):
    """En marche, puis l'admin répond (le premier démarrage prépare ses certificats : parfois une minute)."""
    end = _st_time.time() + timeout
    while _st_time.time() < end:
        if _store_container_info(STORE_NPM_CONTAINER)[1]:
            _o, _e, code = _px_host('curl -sk -m 4 -o /dev/null https://127.0.0.1:%d/ || wget -q -T 4 --no-check-certificate -O /dev/null https://127.0.0.1:%d/'
                                    % (s['ports']['admin'], s['ports']['admin']), timeout=20)
            if code == 0:
                return True
        _st_time.sleep(4)
    return False


def store_npm_check_install():
    """Avant une installation neuve : pas d'autre proxy, ports libres. Rend un message d'erreur ou ''."""
    f = store_npm_foreign()
    if f:
        if f.get('adoptable'):
            return 'Un NPMplus tourne déjà sur ce NAS (%s) : utilisez « Reprendre » pour le confier à Desktroo Apps.' % f['container']
        return f.get('why') or 'Un reverse-proxy tourne déjà sur ce NAS.'
    busy = _store_npm_ports_busy(store_npm_settings()['ports'])
    if busy:
        return 'Ports déjà utilisés sur le NAS : %s. Libérez-les avant d’installer NPMplus.' % ', '.join(str(p) for p in busy)
    return ''


def store_npm_adopt(body=None):
    gate = store_gate('install')
    if not gate['ok']:
        return 402, {'ok': False, 'code': 'need_key', 'error': 'Clé gratuite requise pour installer ou reprendre une app.', 'gate': gate}
    if STORE_NPM_ID in store_installed_get():
        return 409, {'ok': False, 'error': 'NPMplus est déjà géré par Desktroo Apps.'}
    if _store_running(STORE_NPM_ID):
        return 409, {'ok': False, 'error': 'Une opération est déjà en cours sur NPMplus.'}
    app = store_app_entry(STORE_NPM_ID)
    if not app:
        return 404, {'ok': False, 'error': 'NPMplus absent du catalogue.'}
    f = store_npm_foreign()
    if not f:
        return 404, {'ok': False, 'error': 'Aucun NPMplus à reprendre sur ce NAS.'}
    if not f.get('adoptable'):
        return 409, {'ok': False, 'error': f.get('why')}
    _store_job_set(STORE_NPM_ID, action='adopt', state='running', step='Démarrage', error='')
    _threading.Thread(target=_store_npm_adopt_job, args=(app, f), name='store-npm-adopt', daemon=True).start()
    return 202, {'ok': True, 'job': store_job_get(STORE_NPM_ID)}


def _store_npm_adopt_job(app, f):
    """Reprise : copie des données dans le dataset, même configuration, retour arrière complet si échec."""
    old, src = f['container'], f['data'].rstrip('/')
    stopped = created = False
    try:
        _store_job_set(STORE_NPM_ID, step='Préparation du dossier de données')
        s = store_npm_settings()
        s.pop('data', None)
        s['ports'] = f['ports']
        s['env'] = {k: v for k, v in (f.get('env') or {}).items()
                    if k not in ('HTTP_PORT', 'HTTPS_PORT', 'NPM_PORT')}
        if s['env'].get('COOKIE_SECRET'):
            s['cookie_secret'] = s['env'].pop('COOKIE_SECRET')
        _store_npm_settings_save(s)
        s = _store_npm_prepare(app)
        dst = s['data']
        if os.path.realpath(src) == os.path.realpath(dst):
            raise RuntimeError('Les données sont déjà dans le dossier de Desktroo Apps.')
        _o, _e, code = _px_host('test -d %s' % shq(src), timeout=20)
        if code != 0:
            raise RuntimeError('Dossier de données introuvable : ' + src)
        _store_job_set(STORE_NPM_ID, step='Arrêt de l’ancien NPMplus')
        _o, err, code = _px_host('docker stop -t 30 %s' % shq(old), timeout=90)
        if code != 0:
            raise RuntimeError('Arrêt impossible : ' + (err or '')[-200:])
        stopped = True
        _store_job_set(STORE_NPM_ID, step='Copie des certificats et des réglages')
        _o, err, code = _px_host('cp -a %s/. %s/' % (shq(src), shq(dst)), timeout=900)
        if code != 0:
            raise RuntimeError('Copie des données : ' + (err or '')[-200:])
        _store_job_set(STORE_NPM_ID, step='Démarrage dans Desktroo Apps')
        _store_midclt('app.create', {'custom_app': True, 'app_name': 'dt-' + STORE_NPM_ID,
                                     'custom_compose_config': store_npm_compose(app, s)}, job=True, timeout=1800)
        created = True
        if not _store_npm_wait(s):
            raise RuntimeError("NPMplus ne répond pas après la reprise.")
        # L'ancien conteneur est retiré ; ses données restent à leur place, intactes, en secours.
        _px_host('docker rm %s' % shq(old), timeout=60)
        reg = store_installed_get()
        reg[STORE_NPM_ID] = {'name': app.get('name'), 'version': app.get('version'), 'kind': 'module', 'image': '',
                             'dataset': _store_dataset(STORE_NPM_ID), 'window': {}, 'standalone': False,
                             'adopted_from': {'container': old, 'data': src, 'image': f.get('image')},
                             'installed_at': int(_st_time.time()), 'updated_at': int(_st_time.time())}
        _store_installed_set(reg)
        _store_job_set(STORE_NPM_ID, state='done', step='Repris par Desktroo Apps', error='')
        log.info('Magasin : NPMplus repris (%s, données copiées depuis %s)', old, src)
    except Exception as e:
        log.warning('Magasin : reprise de NPMplus en échec : %s', e)
        if created:
            try:
                _store_midclt('app.delete', 'dt-' + STORE_NPM_ID, {'remove_images': False}, job=True, timeout=600)
            except Exception:
                pass
        else:
            try:
                left = _store_midclt('app.query', [['name', '=', 'dt-' + STORE_NPM_ID]], {'select': ['name']}, timeout=60)
                if left:
                    _store_midclt('app.delete', 'dt-' + STORE_NPM_ID, {'remove_images': False}, job=True, timeout=600)
            except Exception:
                pass
        if stopped:
            _px_host('docker start %s' % shq(old), timeout=90)
        _store_job_set(STORE_NPM_ID, state='error',
                       error='Reprise annulée, votre NPMplus tourne comme avant : ' + _store_explain(e)[:200])


def store_npm_info():
    """Pour la fiche : ports, adresse de l'admin, et un éventuel NPMplus à reprendre."""
    s = _lic_read_json(STORE_NPM_FILE) or {}
    ports = dict(STORE_NPM_DEFAULT_PORTS)
    ports.update(s.get('ports') or {})
    info = {'ports': ports, 'foreign': None, 'linked': s.get('linked') or None}
    if STORE_NPM_ID not in store_installed_get():
        try:
            info['foreign'] = store_npm_foreign()
        except Exception:
            pass
    return info


# ── MDM-STORE-V1 · fenêtre « Domaines » : NPMplus piloté par son API, depuis le NAS ─────────────────────
# L'administration de NPMplus refuse d'être affichée dans un cadre (X-Frame-Options DENY) et sa session ne vit
# qu'en https : Desktroo ne l'affiche donc pas, il lui parle. fileops ouvre une session NPMplus avec un compte
# administrateur gardé dans data/store/npmplus.json : créé par Desktroo à l'installation, ou donné une fois
# par l'administrateur après une reprise. Le navigateur ne voit jamais ce mot de passe, sauf sur demande.
_NPM_API = {'cookie': '', 'until': 0, 'fails': 0, 'job': {}}
_NPM_API_LOCK = _threading.Lock()


def store_npm_remote():
    """Adresse de l'API d'un NPMplus installé sur une autre machine (box, VM, Raspberry…), ou ''."""
    lk = (_lic_read_json(STORE_NPM_FILE) or {}).get('linked') or {}
    return str(lk.get('url') or '') if lk.get('remote') else ''


def _npm_base():
    return store_npm_remote() or 'https://%s:%d' % (SSH_HOST, store_npm_settings()['ports']['admin'])


def _npm_http(method, path, body=None, cookie='', timeout=30, base=None):
    import ssl, urllib.request, urllib.error
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request((base or _npm_base()) + '/api' + path, data=data, method=method)
    req.add_header('Accept', 'application/json')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    if cookie:
        req.add_header('Cookie', cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw, code, hdrs = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, code, hdrs = e.read(), e.code, e.headers
    try:
        out = json.loads(raw.decode('utf-8') or 'null')
    except Exception:
        out = None
    return code, out, hdrs


def _npm_err(out, code):
    msg = ((out or {}).get('error') or {}).get('message') if isinstance(out, dict) else ''
    return str(msg or 'NPMplus a répondu %s' % code)


def _npm_login(email, password, base=None):
    code, out, hdrs = _npm_http('POST', '/tokens', {'identity': email, 'secret': password}, timeout=20, base=base)
    if code != 200:
        raise PermissionError(_npm_err(out, code))
    if isinstance(out, dict) and out.get('requires_2fa'):
        raise PermissionError('Ce compte NPMplus a la double authentification : donnez un compte administrateur sans code, réservé à Desktroo.')
    for sc in hdrs.get_all('Set-Cookie') or []:
        if sc.startswith('__Host-Http-token='):
            return sc.split(';', 1)[0]
    raise PermissionError('NPMplus n’a pas ouvert de session.')


def _npm_session(force=False):
    with _NPM_API_LOCK:
        if not force and _NPM_API['cookie'] and _st_time.time() < _NPM_API['until']:
            return _NPM_API['cookie']
        api = store_npm_settings().get('api') or {}
        if not api.get('email') or not api.get('password'):
            raise LookupError('NPMPLUS_LOGIN')
        if _NPM_API['fails'] >= 3:   # NPMplus bloque après 10 échecs en 5 min : on s'arrête bien avant
            raise PermissionError('Identifiants NPMplus refusés : saisissez-les à nouveau.')
        try:
            ck = _npm_login(api['email'], api['password'])
        except PermissionError:
            _NPM_API['fails'] += 1
            raise
        _NPM_API.update(cookie=ck, until=_st_time.time() + 3000, fails=0)
        return ck


def _npm_call(method, path, body=None, timeout=30):
    code, out, _h = _npm_http(method, path, body, _npm_session(), timeout)
    if code in (401, 403):
        code, out, _h = _npm_http(method, path, body, _npm_session(force=True), timeout)
    if code >= 400:
        raise RuntimeError(_npm_err(out, code))
    return out


def store_npm_setup_admin():
    """Installation neuve : Desktroo crée le compte administrateur de NPMplus (mode « premier démarrage »)."""
    s = store_npm_settings()
    if (s.get('api') or {}).get('password'):
        return True
    for _ in range(20):
        try:
            code, out, _h = _npm_http('GET', '/', timeout=10)
            if code == 200 and isinstance(out, dict):
                break
        except Exception:
            pass
        _st_time.sleep(3)
    else:
        return False
    if out.get('setup'):
        return False      # déjà configuré par quelqu'un : identifiants demandés dans la fenêtre Domaines
    pw = _px_secrets.token_urlsafe(24)
    email = 'desktroo@desktroo.local'
    code, out, _h = _npm_http('POST', '/users', {'name': 'Desktroo', 'nickname': 'Desktroo', 'email': email,
                                                 'roles': ['admin'], 'is_disabled': False,
                                                 'auth': {'type': 'password', 'secret': pw}}, timeout=30)
    if code not in (200, 201):
        log.warning('NPMplus : création du compte administrateur refusée : %s', _npm_err(out, code))
        return False
    s['api'] = {'email': email, 'password': pw, 'created_by': 'desktroo'}
    _store_npm_settings_save(s)
    return True


def _npm_host_view(h):
    cert = h.get('certificate') or None
    return {'id': h.get('id'), 'domains': h.get('domain_names') or [], 'scheme': h.get('forward_scheme'),
            'host': h.get('forward_host'), 'port': h.get('forward_port'), 'enabled': bool(h.get('enabled')),
            'ssl_forced': bool(h.get('ssl_forced')),
            'cert': ({'id': cert.get('id'), 'provider': cert.get('provider'), 'expires_on': cert.get('expires_on'),
                      'name': cert.get('nice_name') or ', '.join(cert.get('domain_names') or [])} if isinstance(cert, dict) else None),
            'error': ((h.get('meta') or {}).get('nginx_err') or '') if isinstance(h.get('meta'), dict) else ''}


def store_npm_state():
    s = _lic_read_json(STORE_NPM_FILE) or {}
    remote = store_npm_remote()
    if s.get('linked') and not remote and STORE_NPM_ID not in store_installed_get():
        try:   # NPMplus relié : ses ports peuvent changer de son côté (réglages de l'app TrueNAS)
            f = store_npm_foreign()
            if f and f.get('ports') and f['ports'] != s.get('ports'):
                s['ports'] = f['ports']; _store_npm_settings_save(store_npm_settings() | {'ports': f['ports']})
        except Exception:
            pass
    inst = STORE_NPM_ID in store_installed_get() or bool(s.get('linked'))
    ports = dict(STORE_NPM_DEFAULT_PORTS); ports.update(s.get('ports') or {})
    st = {'installed': inst, 'linked': bool(s.get('linked')), 'ports': ports, 'job': dict(_NPM_API['job']) or None, 'login': False, 'hosts': [],
          'account': ((s.get('api') or {}).get('email') or ''), 'remote': remote,
          'nas_host': (_truenas_ip() or SSH_HOST) if remote else ''}
    if not inst:
        return st
    try:
        rows = _npm_call('GET', '/nginx/proxy-hosts?expand=certificate') or []
        st['hosts'] = [_npm_host_view(h) for h in rows if isinstance(h, dict)]
    except LookupError:
        st['login'] = True
    except PermissionError as e:
        st['login'] = True; st['error'] = str(e)
    except Exception as e:
        st['error'] = 'NPMplus ne répond pas : ' + str(e)[:200]
    return st


def store_npm_credentials(body):
    email, pw = str(body.get('email') or '').strip(), str(body.get('password') or '')
    if not email or not pw:
        return 400, {'ok': False, 'error': 'Adresse e-mail et mot de passe NPMplus requis.'}
    try:
        ck = _npm_login(email, pw)
    except PermissionError as e:
        return 403, {'ok': False, 'error': str(e)}
    except Exception as e:
        return 502, {'ok': False, 'error': 'NPMplus ne répond pas : ' + str(e)[:200]}
    s = store_npm_settings(); s['api'] = {'email': email, 'password': pw, 'created_by': 'admin'}
    _store_npm_settings_save(s)
    with _NPM_API_LOCK:
        _NPM_API.update(cookie=ck, until=_st_time.time() + 3000, fails=0)
    return 200, {'ok': True}


def store_npm_secret(_body=None):
    api = store_npm_settings().get('api') or {}
    return 200, {'ok': True, 'email': api.get('email') or '', 'password': api.get('password') or ''}


_NPM_DOMAIN_RE = re.compile(r'^(?=.{1,253}$)(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')


def store_npm_host_add(body, advanced='', then=None, adopt=False, replace=False):
    """Publie un domaine. « advanced » et « then » ne servent qu'aux modules (Desktroo Pass), jamais à la page."""
    doms = [str(d).strip().lower().rstrip('.') for d in (body.get('domains') or []) if str(d).strip()][:20]
    bad = [d for d in doms if not _NPM_DOMAIN_RE.match(d)]
    if not doms or bad:
        return 400, {'ok': False, 'error': 'Nom de domaine invalide : ' + ', '.join(bad or ['(vide)'])}
    try:
        port = int(body.get('port'))
        assert 1 <= port <= 65535
    except Exception:
        return 400, {'ok': False, 'error': 'Port invalide.'}
    host = str(body.get('host') or '127.0.0.1').strip()
    if store_npm_remote() and host in ('127.0.0.1', 'localhost', '::1'):
        host = _truenas_ip() or SSH_HOST    # NPMplus sur une autre machine : l'adresse du NAS sur le réseau
    if not re.match(r'^[A-Za-z0-9.\-:\[\]]{1,253}$', host):
        return 400, {'ok': False, 'error': 'Adresse de destination invalide.'}
    scheme = 'https' if body.get('scheme') == 'https' else 'http'
    https = bool(body.get('https', True))
    if _NPM_API['job'].get('state') == 'running':
        return 409, {'ok': False, 'error': 'Une demande de certificat est déjà en cours.'}
    payload = {'domain_names': doms, 'forward_scheme': scheme, 'forward_host': host, 'forward_port': port,
               'block_exploits': True, 'allow_websocket_upgrade': True, 'http2_support': True}
    if https:
        payload.update(certificate_id='new', ssl_forced=True)
    if advanced:
        payload['advanced_config'] = advanced
    existing = None
    if adopt:   # domaine déjà configuré dans NPMplus (à la main, ou par un essai précédent) : on le reprend au lieu d'échouer
        try:
            for hh in _npm_call('GET', '/nginx/proxy-hosts', None, timeout=30) or []:
                if doms[0] in [str(x).lower() for x in (hh.get('domain_names') or [])]:
                    existing = hh
                    break
        except Exception as e:
            log.warning('NPMplus : liste des domaines illisible : %s', e)
        if existing:
            same = (int(existing.get('forward_port') or 0) == port
                    and str(existing.get('forward_host') or '') in {host, '127.0.0.1', _truenas_ip() or host})
            if not same and not replace:
                return 409, {'ok': False, 'busy': True, 'error': 'DOMAIN_BUSY|%s:%s' % (existing.get('forward_host'), existing.get('forward_port'))}
            if existing.get('certificate_id'):
                payload.pop('certificate_id', None)   # certificat déjà en place : on le garde
            log.info('NPMplus : %s déjà configuré, repris pour %s:%s', doms[0], host, port)
    _NPM_API['job'] = {'state': 'running', 'step': ('Demande du certificat pour ' if https else 'Publication de ') + doms[0], 'error': '',
                       'domain': doms[0], 'at': int(_st_time.time())}

    def run():
        try:
            if existing:
                _npm_call('PUT', '/nginx/proxy-hosts/%s' % existing['id'], payload, timeout=240)
            else:
                _npm_call('POST', '/nginx/proxy-hosts', payload, timeout=240)
            if then:
                then(doms[0])
            _NPM_API['job'].update(state='done', step='Publié', error='')
            log.info('NPMplus : %s publié vers %s:%s', ', '.join(doms), host, port)
        except Exception as e:
            _NPM_API['job'].update(state='error', error=_npm_explain(str(e)))
    _threading.Thread(target=run, name='npm-host', daemon=True).start()
    return 202, {'ok': True}


def _npm_explain(t):
    if re.search(r'already in use', t, re.I):
        return 'Ce domaine est déjà publié.'
    if re.search(r'letsencrypt|certbot|challenge|acme|unauthorized|connection|timeout|dns', t, re.I) and store_npm_remote():
        return ('Le certificat n’a pas pu être obtenu. Vérifiez que le domaine pointe vers votre adresse publique et que la box '
                'redirige le port 80 vers la machine qui héberge NPMplus. Détail : %s' % t[:200])
    if re.search(r'letsencrypt|certbot|challenge|acme|unauthorized|connection|timeout|dns', t, re.I):
        return ('Le certificat n’a pas pu être obtenu. Vérifiez que le domaine pointe vers votre adresse publique et que la box '
                'redirige le port 80 vers ce NAS (port %d). Détail : %s' % (store_npm_settings()['ports']['http'], t[:200]))
    return t[:300]


def store_npm_host_delete(body):
    try:
        _npm_call('DELETE', '/nginx/proxy-hosts/%d' % int(body.get('id')))
        return 200, {'ok': True}
    except Exception as e:
        return 502, {'ok': False, 'error': str(e)[:300]}


def store_npm_host_toggle(body):
    try:
        _npm_call('POST', '/nginx/proxy-hosts/%d/%s' % (int(body.get('id')), 'enable' if body.get('enabled') else 'disable'))
        return 200, {'ok': True}
    except Exception as e:
        return 502, {'ok': False, 'error': str(e)[:300]}


def store_npm_cert_renew(body):
    try:
        _npm_call('POST', '/nginx/certificates/%d/renew' % int(body.get('id')), timeout=240)
        return 200, {'ok': True}
    except Exception as e:
        return 502, {'ok': False, 'error': _npm_explain(str(e))}


def store_npm_job_clear(_body=None):
    if _NPM_API['job'].get('state') != 'running':
        _NPM_API['job'] = {}
    return 200, {'ok': True}


def store_npm_linked():
    """NPMplus installé hors de Desktroo Apps (app TrueNAS, par exemple) mais relié à la fenêtre Domaines."""
    return (_lic_read_json(STORE_NPM_FILE) or {}).get('linked') or None


def _npm_remote_url(raw):
    """Adresse saisie → https://hôte:port, ou ValueError lisible. Admin de NPMplus : https, port 81 par défaut."""
    import urllib.parse as _up, ipaddress as _ip
    t = str(raw or '').strip()
    if not t:
        raise ValueError('Indiquez l’adresse de NPMplus (exemple : https://192.168.0.50:81).')
    if '://' not in t:
        t = 'https://' + t
    u = _up.urlsplit(t)
    host = (u.hostname or '').lower()
    if u.scheme not in ('http', 'https') or not host or u.username or u.password:
        raise ValueError('Adresse invalide. Exemple : https://192.168.0.50:81')
    if host in ('localhost', '0.0.0.0') or host.endswith('.localhost'):
        raise ValueError('Cette adresse désigne ce NAS. Pour un NPMplus installé ici, utilisez « Relier à Desktroo ».')
    try:
        ip = _ip.ip_address(host)
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
            raise ValueError('Adresse non autorisée : ' + host)
    except ValueError as e:
        if 'non autorisée' in str(e):
            raise
    try:
        port = u.port or 81
    except ValueError:
        raise ValueError('Port invalide.')
    hostpart = '[%s]' % host if ':' in host else host
    return '%s://%s:%d' % (u.scheme, hostpart, port)


def store_npm_link_remote(body):
    """Relie un NPMplus installé sur une AUTRE machine (box, VM, Raspberry…) : Desktroo ne touche qu'à son API."""
    gate = store_gate('install')
    if not gate['ok']:
        return 402, {'ok': False, 'code': 'need_key', 'error': 'Clé gratuite requise pour relier NPMplus.', 'gate': gate}
    if STORE_NPM_ID in store_installed_get():
        return 409, {'ok': False, 'error': 'NPMplus est déjà géré par Desktroo Apps.'}
    try:
        url = _npm_remote_url(body.get('url'))
    except ValueError as e:
        return 400, {'ok': False, 'error': str(e)}
    email, pw = str(body.get('email') or '').strip(), str(body.get('password') or '')
    if not email or not pw:
        return 400, {'ok': False, 'error': 'Adresse e-mail et mot de passe d’un administrateur NPMplus requis.'}
    try:
        code, out, _h = _npm_http('GET', '/', timeout=10, base=url)
    except Exception as e:
        return 502, {'ok': False, 'error': 'Aucune réponse de %s : %s. Vérifiez l’adresse, le port d’administration et que cette machine est joignable depuis le NAS.' % (url, str(e)[:120])}
    if code != 200 or not isinstance(out, dict) or 'setup' not in out:
        return 502, {'ok': False, 'error': 'Ce n’est pas l’administration d’un NPMplus (adresse ou port à vérifier).'}
    if not out.get('setup'):
        return 409, {'ok': False, 'error': 'Ce NPMplus n’a pas encore de compte administrateur : créez-le d’abord sur son interface, puis revenez ici.'}
    try:
        ck = _npm_login(email, pw, base=url)
    except PermissionError as e:
        return 403, {'ok': False, 'error': str(e)}
    except Exception as e:
        return 502, {'ok': False, 'error': 'NPMplus ne répond pas : ' + str(e)[:200]}
    s = store_npm_settings()
    s['ports'] = {'http': 80, 'https': 443, 'admin': int(url.rsplit(':', 1)[-1])}
    s['linked'] = {'remote': True, 'url': url, 'container': '', 'project': '', 'image': '', 'at': int(_st_time.time())}
    s['api'] = {'email': email, 'password': pw, 'created_by': 'admin'}
    s.pop('data', None)
    _store_npm_settings_save(s)
    with _NPM_API_LOCK:
        _NPM_API.update(cookie=ck, until=_st_time.time() + 3000, fails=0)
    log.info('NPMplus distant relié : %s', url)
    return 200, {'ok': True}


def store_npm_link(body=None):
    """Relie un NPMplus existant à la fenêtre Domaines, sans toucher à son installation."""
    if isinstance(body, dict) and body.get('url'):
        return store_npm_link_remote(body)
    gate = store_gate('install')
    if not gate['ok']:
        return 402, {'ok': False, 'code': 'need_key', 'error': 'Clé gratuite requise pour relier NPMplus.', 'gate': gate}
    if STORE_NPM_ID in store_installed_get():
        return 409, {'ok': False, 'error': 'NPMplus est déjà géré par Desktroo Apps.'}
    f = store_npm_foreign()
    if not f or 'npmplus' not in str(f.get('image', '')).lower():
        return 404, {'ok': False, 'error': 'Aucun NPMplus à relier sur ce NAS.'}
    s = store_npm_settings()
    if (s.get('linked') or {}).get('remote'):
        s.pop('api', None)   # identifiants de l'ancien NPMplus distant : sans objet pour celui-ci
    s['ports'] = f['ports']
    s['linked'] = {'container': f['container'], 'project': f.get('project') or '', 'image': f.get('image') or '',
                   'at': int(_st_time.time())}
    s.pop('data', None)
    _store_npm_settings_save(s)
    with _NPM_API_LOCK:
        _NPM_API.update(cookie='', until=0, fails=0)
    log.info('NPMplus : %s relié à la fenêtre Domaines', f['container'])
    return 200, {'ok': True}


def store_npm_unlink(body=None):
    s = _lic_read_json(STORE_NPM_FILE) or {}
    if s.pop('linked', None) is not None:
        s.pop('api', None)   # identifiants de l'instance reliée : ils ne servent plus
        s['ports'] = dict(STORE_NPM_DEFAULT_PORTS)
        _store_npm_settings_save(s)
    with _NPM_API_LOCK:
        _NPM_API.update(cookie='', until=0, fails=0)
    return 200, {'ok': True}


# ── MDM-STORE-V1 · AdGuard Home sur une autre machine (box, VM…) : Desktroo ne parle qu'à son API ───────────
# Les identifiants restent sur le NAS (data/store/adguard.json). Le navigateur ne les voit jamais : la fenêtre AdGuard
# passe par /store/adguard/api, qui relaie la requête (chemins /control/... seulement) avec ces identifiants.
STORE_AG_ID = 'adguard'
STORE_AG_FILE = os.path.join(STORE_DIR, 'adguard.json')
_AG_PATH_RE = re.compile(r'^/control/[a-z0-9_/]{1,80}(\?[A-Za-z0-9=&%._~:+-]{0,300})?$')


def store_ag_linked():
    l = (_lic_read_json(STORE_AG_FILE) or {}).get('linked')
    return l if isinstance(l, dict) and l.get('url') else None


def _ag_remote_url(raw):
    """Adresse saisie → http(s)://hôte[:port], ou ValueError lisible."""
    import urllib.parse as _up, ipaddress as _ip
    t = str(raw or '').strip()
    if not t:
        raise ValueError('Indiquez l’adresse d’AdGuard Home (exemple : http://192.168.0.50:3000).')
    if '://' not in t:
        t = 'http://' + t
    u = _up.urlsplit(t)
    host = (u.hostname or '').lower()
    if u.scheme not in ('http', 'https') or not host or u.username or u.password:
        raise ValueError('Adresse invalide. Exemple : http://192.168.0.50:3000')
    if host in ('localhost', '0.0.0.0') or host.endswith('.localhost'):
        raise ValueError('Cette adresse désigne ce NAS. Pour un AdGuard installé ici, utilisez « Installer ».')
    try:
        ip = _ip.ip_address(host)
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast:
            raise ValueError('Adresse non autorisée : ' + host)
    except ValueError as e:
        if 'non autorisée' in str(e):
            raise
    try:
        port = u.port
    except ValueError:
        raise ValueError('Port invalide.')
    return '%s://%s%s' % (u.scheme, '[%s]' % host if ':' in host else host, ':%d' % port if port else '')


def _ag_http(method, path, body=None, timeout=15, base=None, user='', password=''):
    """Appel à l'API d'AdGuard (identifiants de la liaison par défaut). Rend (code, texte). Aucune redirection suivie."""
    import ssl, urllib.request, urllib.error, base64
    link = None if base else store_ag_linked()
    if not base and not link:
        raise RuntimeError('AdGuard Home n’est pas relié.')
    base = base or link['url']
    if link:
        user, password = link.get('user') or '', link.get('password') or ''
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('Accept', 'application/json')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    if user or password:
        req.add_header('Authorization', 'Basic ' + base64.b64encode(('%s:%s' % (user, password)).encode('utf-8')).decode('ascii'))
    try:
        with opener.open(req, timeout=timeout) as r:
            raw, code = r.read(4 * 1024 * 1024), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(4 * 1024 * 1024), e.code
    return code, raw.decode('utf-8', 'replace')


def store_adguard_link(body):
    gate = store_gate('install')
    if not gate['ok']:
        return 402, {'ok': False, 'code': 'need_key', 'error': 'Clé gratuite requise pour relier AdGuard Home.', 'gate': gate}
    if STORE_AG_ID in store_installed_get():
        return 409, {'ok': False, 'error': 'AdGuard Home est déjà installé sur ce NAS.'}
    try:
        url = _ag_remote_url((body or {}).get('url'))
    except ValueError as e:
        return 400, {'ok': False, 'error': str(e)}
    user, pw = str((body or {}).get('user') or '').strip()[:128], str((body or {}).get('password') or '')[:256]
    try:
        code, txt = _ag_http('GET', '/control/status', timeout=10, base=url, user=user, password=pw)
    except Exception as e:
        return 502, {'ok': False, 'error': 'Aucune réponse de %s : %s. Vérifiez l’adresse, le port et que cette machine est joignable depuis le NAS.' % (url, str(e)[:120])}
    if code == 401:
        return 403, {'ok': False, 'error': 'Identifiant ou mot de passe refusé par AdGuard Home.'}
    try:
        st = json.loads(txt)
    except Exception:
        st = None
    if code in (301, 302, 303, 307, 308) or code == 404 or not isinstance(st, dict) or 'version' not in st:
        return 502, {'ok': False, 'error': 'Ce n’est pas AdGuard Home, ou il n’est pas encore configuré (adresse, port, ou assistant de démarrage à terminer).'}
    if code != 200:
        return 502, {'ok': False, 'error': 'AdGuard Home a répondu %s.' % code}
    os.makedirs(STORE_DIR, exist_ok=True)
    try:
        fd = os.open(STORE_AG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump({'linked': {'url': url, 'user': user, 'password': pw, 'version': str(st.get('version') or ''), 'at': int(_st_time.time())}}, fh)
        try:
            os.chmod(STORE_AG_FILE, 0o600)
        except OSError:
            pass   # dossier à ACL : le mode ne se change pas ; le fichier est déjà créé en 0600
    except OSError as e:
        return 500, {'ok': False, 'error': 'Impossible d’enregistrer la liaison : %s' % e}
    log.info('AdGuard Home distant relié : %s', url)
    return 200, {'ok': True}


def store_adguard_unlink(body=None):
    try:
        if os.path.exists(STORE_AG_FILE):
            os.remove(STORE_AG_FILE)   # les identifiants disparaissent avec la liaison
    except OSError as e:
        return 500, {'ok': False, 'error': str(e)}
    return 200, {'ok': True}


def store_adguard_api(body):
    """Relaie une requête de la fenêtre AdGuard vers l'AdGuard relié : { method, path, body } → { status, text }."""
    if not store_ag_linked():
        return 409, {'ok': False, 'error': 'AdGuard Home n’est pas relié.'}
    method, path = str((body or {}).get('method') or 'GET').upper(), str((body or {}).get('path') or '')
    path = '/control/' + path.lstrip('/') if not path.startswith('/control/') else path
    if method not in ('GET', 'POST', 'PUT') or not _AG_PATH_RE.match(path):
        return 400, {'ok': False, 'error': 'Requête non autorisée.'}
    payload = (body or {}).get('body')
    if payload is not None and not isinstance(payload, (dict, list)):
        return 400, {'ok': False, 'error': 'Requête invalide.'}
    try:
        code, txt = _ag_http(method, path, payload, timeout=25)
    except Exception as e:
        return 502, {'ok': False, 'error': 'AdGuard Home ne répond pas : ' + str(e)[:160]}
    return 200, {'ok': True, 'status': code, 'text': txt}


# ── MDM-STORE-V1 · Pi-hole : sur ce NAS (conteneur dt-pihole) ou sur une autre machine (Raspberry Pi, VM…) ────────
# Dans les deux cas le navigateur ne parle jamais à Pi-hole : la fenêtre passe par /store/pihole/api, le NAS ouvre la
# session Pi-hole (API v6, identifiant de session « sid ») avec le mot de passe qu'il garde, et relaie la requête.
# Le mot de passe d'un Pi-hole relié reste sur le NAS (data/store/pihole.json) ; celui d'un Pi-hole local est dérivé.
STORE_PH_ID = 'pihole'
STORE_PH_FILE = os.path.join(STORE_DIR, 'pihole.json')
_PH_LOCAL_BASE = 'http://dt-pihole:8080'
_PH_ALLOWED = (   # (méthode, motif) : seules ces requêtes passent
    ('GET', re.compile(r'^/api/(info/version|dns/blocking|config/dns|config/webserver|lists|domains(/(allow|deny)(/(exact|regex))?)?|stats/(summary|history|top_domains|top_clients|upstreams)|history(/clients)?)(\?[A-Za-z0-9=&%._~:+,-]{0,200})?$')),
    ('GET', re.compile(r'^/api/queries(\?[A-Za-z0-9=&%._~:+,*.-]{0,300})?$')),
    ('POST', re.compile(r'^/api/dns/blocking$')),
    ('POST', re.compile(r'^/api/lists(\?[A-Za-z0-9=&%._~:+-]{0,100})?$')),
    ('PUT', re.compile(r'^/api/lists/[A-Za-z0-9%._~:/?=&+@-]{1,400}$')),
    ('DELETE', re.compile(r'^/api/lists/[A-Za-z0-9%._~:/?=&+@-]{1,400}$')),
    ('POST', re.compile(r'^/api/domains/(allow|deny)/(exact|regex)$')),
    ('DELETE', re.compile(r'^/api/domains/(allow|deny)/(exact|regex)/[A-Za-z0-9%._~:/?=&+@^|$()\\*-]{1,300}$')),
    ('PATCH', re.compile(r'^/api/config$')),
    ('POST', re.compile(r'^/api/action/(gravity|restartdns|flush/logs|flush/network)$')),
)
_PH_SIDS = {}   # base → (sid, échéance) ; '' = Pi-hole sans mot de passe
_PH_LOCK = _threading.Lock()


def store_ph_linked():
    l = (_lic_read_json(STORE_PH_FILE) or {}).get('linked')
    return l if isinstance(l, dict) and l.get('url') else None


def _ph_target():
    """(adresse, mot de passe) du Pi-hole piloté : relié d'abord, sinon celui installé ici ; (None, None) si aucun."""
    lk = store_ph_linked()
    if lk:
        return lk['url'], lk.get('password') or ''
    if STORE_PH_ID in store_installed_get():
        return _PH_LOCAL_BASE, _store_derive(STORE_PH_ID, 'ph-admin')
    return None, None


def _ph_raw(base, method, path, body=None, sid='', timeout=15):
    import ssl, urllib.request, urllib.error
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header('Accept', 'application/json')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    if sid:
        req.add_header('sid', sid)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw, code = r.read(4 * 1024 * 1024), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(4 * 1024 * 1024), e.code
    return code, raw.decode('utf-8', 'replace')


def _ph_login(base, password, timeout=15):
    """Ouvre une session Pi-hole : rend (sid, None) — sid vide si Pi-hole n'a pas de mot de passe — ou ('', message)."""
    code, txt = _ph_raw(base, 'POST', '/api/auth', {'password': password or ''}, timeout=timeout)
    try:
        j = json.loads(txt)
    except Exception:
        j = None
    if code in (401, 403) or (isinstance(j, dict) and isinstance(j.get('session'), dict) and j['session'].get('valid') is False):
        return '', 'Mot de passe refusé par Pi-hole.'
    if code != 200 or not isinstance(j, dict) or not isinstance(j.get('session'), dict):
        return '', 'Ce n’est pas Pi-hole version 6 (ou plus récent).'
    return str(j['session'].get('sid') or ''), None


def _ph_http(method, path, body=None, timeout=25):
    """Appel à l'API du Pi-hole piloté, avec session (renouvelée si elle a expiré). Rend (code, texte)."""
    base, pw = _ph_target()
    if not base:
        raise RuntimeError('Pi-hole n’est ni installé ni relié.')
    for attempt in (0, 1):
        with _PH_LOCK:
            ent = _PH_SIDS.get(base)
        if not ent or ent[1] < _st_time.time() or attempt:
            sid, err = _ph_login(base, pw, timeout=timeout)
            if err:
                raise RuntimeError(err)
            ent = (sid, _st_time.time() + 240)
            with _PH_LOCK:
                _PH_SIDS[base] = ent
        code, txt = _ph_raw(base, method, path, body, ent[0], timeout)
        if code != 401 or attempt:
            return code, txt
    return code, txt


def store_pihole_api(body):
    """Relaie une requête de la fenêtre Pi-hole : { method, path, body } → { status, text }."""
    base, _pw = _ph_target()
    if not base:
        return 409, {'ok': False, 'error': 'Pi-hole n’est ni installé ni relié.'}
    method, path = str((body or {}).get('method') or 'GET').upper(), str((body or {}).get('path') or '')
    if not path.startswith('/api/'):
        path = '/api/' + path.lstrip('/')
    if not any(m == method and rx.match(path) for m, rx in _PH_ALLOWED):
        return 400, {'ok': False, 'error': 'Requête non autorisée.'}
    payload = (body or {}).get('body')
    if payload is not None and not isinstance(payload, (dict, list)):
        return 400, {'ok': False, 'error': 'Requête invalide.'}
    if method == 'PATCH':   # seule la liste des serveurs DNS en amont se modifie d'ici
        up = (((payload or {}).get('config') or {}).get('dns') or {}).get('upstreams') if isinstance(payload, dict) else None
        if not (isinstance(payload, dict) and list(payload) == ['config'] and list(payload['config']) == ['dns'] and list(payload['config']['dns']) == ['upstreams']
                and isinstance(up, list) and 0 < len(up) <= 20 and all(isinstance(u, str) and re.match(r'^[A-Za-z0-9.:\[\]#_-]{1,200}$', u) for u in up)):
            return 400, {'ok': False, 'error': 'Réglage non autorisé.'}
    try:
        code, txt = _ph_http(method, path, payload, timeout=180 if path == '/api/action/gravity' else 25)
    except Exception as e:
        return 502, {'ok': False, 'error': 'Pi-hole ne répond pas : ' + str(e)[:160]}
    return 200, {'ok': True, 'status': code, 'text': txt}


def store_pihole_link(body):
    gate = store_gate('install')
    if not gate['ok']:
        return 402, {'ok': False, 'code': 'need_key', 'error': 'Clé gratuite requise pour relier Pi-hole.', 'gate': gate}
    if STORE_PH_ID in store_installed_get():
        return 409, {'ok': False, 'error': 'Pi-hole est déjà installé sur ce NAS.'}
    try:
        url = _ag_remote_url((body or {}).get('url'))
    except ValueError as e:
        return 400, {'ok': False, 'error': str(e).replace('AdGuard Home', 'Pi-hole')}
    pw = str((body or {}).get('password') or '')[:256]
    try:
        sid, err = _ph_login(url, pw, timeout=10)
    except Exception as e:
        return 502, {'ok': False, 'error': 'Aucune réponse de %s : %s. Vérifiez l’adresse, le port et que cette machine est joignable depuis le NAS.' % (url, str(e)[:120])}
    if err:
        return (403 if 'refusé' in err else 502), {'ok': False, 'error': err}
    try:
        code, txt = _ph_raw(url, 'GET', '/api/info/version', sid=sid, timeout=10)
        j = json.loads(txt) if code == 200 else None
    except Exception:
        j = None
    if not isinstance(j, dict) or not isinstance(j.get('version'), dict):
        return 502, {'ok': False, 'error': 'Ce n’est pas Pi-hole version 6 (ou plus récent), ou le mot de passe est requis pour lire ses informations.'}
    core = ((j['version'].get('core') or {}).get('local') or {}).get('version') or ''
    os.makedirs(STORE_DIR, exist_ok=True)
    try:
        fd = os.open(STORE_PH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump({'linked': {'url': url, 'password': pw, 'version': str(core), 'at': int(_st_time.time())}}, fh)
        try:
            os.chmod(STORE_PH_FILE, 0o600)
        except OSError:
            pass   # dossier à ACL : le mode ne se change pas ; le fichier est déjà créé en 0600
    except OSError as e:
        return 500, {'ok': False, 'error': 'Impossible d’enregistrer la liaison : %s' % e}
    with _PH_LOCK:
        _PH_SIDS.clear()
    log.info('Pi-hole distant relié : %s', url)
    return 200, {'ok': True}


def store_pihole_unlink(body=None):
    try:
        if os.path.exists(STORE_PH_FILE):
            os.remove(STORE_PH_FILE)   # le mot de passe disparaît avec la liaison
    except OSError as e:
        return 500, {'ok': False, 'error': str(e)}
    with _PH_LOCK:
        _PH_SIDS.clear()
    return 200, {'ok': True}


def _store_dns_conflict(app_id, busy):
    """Message clair quand Pi-hole et AdGuard Home se disputent le port 53 de ce NAS (sinon None)."""
    other = {STORE_PH_ID: STORE_AG_ID, STORE_AG_ID: STORE_PH_ID}.get(app_id)
    if not other or 53 not in busy or other not in store_installed_get():
        return None
    names = {STORE_PH_ID: 'Pi-hole', STORE_AG_ID: 'AdGuard Home'}
    return ('%s est déjà installé sur ce NAS et utilise le port 53 (DNS) : deux serveurs DNS ne peuvent pas tourner ensemble. '
            'Désinstallez d’abord %s, ou gardez-le et reliez plutôt un %s qui tourne sur une autre machine.'
            % (names[other], names[other], names[app_id]))


# ── MDM-STORE-V1 · fenêtre « WireGuard » : le NAS relaie les requêtes vers l'API de wg-easy ─────────────────────
# wg-easy tourne sur le réseau Docker des apps (dt-wireguard:8080). Son compte administrateur est celui de la fiche
# (mot de passe dérivé) : le navigateur ne le voit jamais, la fenêtre passe par /store/wireguard/api.
STORE_WG_ID = 'wireguard'
_WG_BASE = 'http://dt-wireguard:8080'
_WG_ALLOWED = (   # (méthode, motif) : seules ces requêtes passent
    ('GET', re.compile(r'^/api/client$')),
    ('POST', re.compile(r'^/api/client$')),
    ('GET', re.compile(r'^/api/client/[0-9]{1,6}/(configuration|qrcode\.svg)$')),
    ('POST', re.compile(r'^/api/client/[0-9]{1,6}/(enable|disable)$')),
    ('DELETE', re.compile(r'^/api/client/[0-9]{1,6}$')),
)


def _wg_http(method, path, body=None, timeout=20):
    import urllib.request, urllib.error, base64
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(_WG_BASE + path, data=data, method=method)
    req.add_header('Accept', 'application/json')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    cred = 'admin:' + _store_derive(STORE_WG_ID, 'wg-admin')
    req.add_header('Authorization', 'Basic ' + base64.b64encode(cred.encode('utf-8')).decode('ascii'))
    try:
        with opener.open(req, timeout=timeout) as r:
            raw, code = r.read(2 * 1024 * 1024), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(2 * 1024 * 1024), e.code
    return code, raw.decode('utf-8', 'replace')


def store_wireguard_api(body):
    """Relaie une requête de la fenêtre WireGuard : { method, path, body } → { status, text }."""
    if STORE_WG_ID not in store_installed_get():
        return 409, {'ok': False, 'error': 'WireGuard n’est pas installé.'}
    method, path = str((body or {}).get('method') or 'GET').upper(), str((body or {}).get('path') or '')
    if not path.startswith('/api/'):
        path = '/api/' + path.lstrip('/')
    if not any(m == method and rx.match(path) for m, rx in _WG_ALLOWED):
        return 400, {'ok': False, 'error': 'Requête non autorisée.'}
    payload = (body or {}).get('body')
    if method == 'POST' and path == '/api/client':
        name = str((payload or {}).get('name') or '').strip() if isinstance(payload, dict) else ''
        if not name or len(name) > 64:
            return 400, {'ok': False, 'error': 'Nom d’appareil invalide.'}
        payload = {'name': name, 'expiresAt': None}
    elif method == 'POST':
        payload = {}
    else:
        payload = None
    try:
        code, txt = _wg_http(method, path, payload)
    except Exception as e:
        return 502, {'ok': False, 'error': 'WireGuard ne répond pas : ' + str(e)[:160]}
    return 200, {'ok': True, 'status': code, 'text': txt}


# ── MDM-STORE-V1 · fenêtre « Uptime Kuma » en lecture seule : le NAS lit l'état des services et le met en forme ──────
# Uptime Kuma publie l'état de tous ses services au format Prometheus (/metrics), protégé par une clé d'accès que
# l'utilisateur crée dans Kuma (lecture seule). La clé reste sur le NAS ; le navigateur ne la revoit jamais.
STORE_UK_ID = 'uptime-kuma'
_UK_BASE = 'http://dt-uptime-kuma:8080'
_UK_KEY_RE = re.compile(r'^[A-Za-z0-9_.-]{8,128}$')
_UK_LINE = re.compile(r'^(monitor_[a-z_0-9]+)\{(.*)\}\s+(-?[0-9.]+(?:[eE][-+]?[0-9]+)?|NaN)\s*$')
_UK_LBL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _uk_unesc(v):
    return v.replace('\\n', ' ').replace('\\"', '"').replace('\\\\', '\\')[:200]


def _uk_fetch(key):
    """Le texte /metrics ; ValueError('auth') si la clé est refusée, RuntimeError si Kuma ne répond pas."""
    import urllib.request, urllib.error, base64
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    req = urllib.request.Request(_UK_BASE + '/metrics', headers={
        'Authorization': 'Basic ' + base64.b64encode((':' + key).encode()).decode()})
    try:
        with opener.open(req, timeout=15) as r:
            return r.read(8 * 1024 * 1024).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError('auth')
        raise RuntimeError('HTTP %d' % e.code)
    except Exception as e:
        raise RuntimeError(str(e)[:120])


def _uk_parse(text):
    mons = {}
    for ln in text.splitlines():
        m = _UK_LINE.match(ln)
        if not m:
            continue
        name, lbls, val = m.group(1), dict((k, _uk_unesc(v)) for k, v in _UK_LBL.findall(m.group(2))), m.group(3)
        if name not in ('monitor_status', 'monitor_response_time', 'monitor_cert_days_remaining', 'monitor_uptime_ratio'):
            continue
        if not lbls.get('monitor_name'):
            continue
        key = (lbls.get('monitor_name'), lbls.get('monitor_type', ''), lbls.get('monitor_url', ''),
               lbls.get('monitor_hostname', ''), lbls.get('monitor_port', ''))
        if len(mons) >= 500 and key not in mons:
            continue
        d = mons.setdefault(key, {'name': key[0], 'type': key[1], 'url': key[2], 'host': key[3], 'port': key[4],
                                  'status': None, 'ms': None, 'cert_days': None, 'up1d': None, 'up30d': None})
        try:
            v = float(val)
        except ValueError:
            continue
        if v != v:
            continue
        if name == 'monitor_status':
            d['status'] = int(v)
        elif name == 'monitor_response_time':
            d['ms'] = int(v) if v >= 0 else None
        elif name == 'monitor_cert_days_remaining':
            d['cert_days'] = int(v)
        elif name == 'monitor_uptime_ratio':
            w = lbls.get('window', '')
            if w in ('1d', '24h'):
                d['up1d'] = round(v * 100, 2)
            elif w == '30d':
                d['up30d'] = round(v * 100, 2)
    for d in mons.values():
        for k in ('url', 'host', 'port'):
            if d[k] in ('null', 'undefined'):
                d[k] = ''
    return [d for d in mons.values() if d['status'] is not None]


def store_uptimekuma_api(body):
    """{ action: state | save_key | clear_key | monitors } → { ok, ... }. La clé n'est jamais renvoyée."""
    if STORE_UK_ID not in store_installed_get():
        return 409, {'ok': False, 'error': 'Uptime Kuma n’est pas installé.'}
    action = str((body or {}).get('action') or '')
    fields = _store_fields_get(STORE_UK_ID)
    key = str(fields.get('api_key') or '')
    if action == 'state':
        return 200, {'ok': True, 'configured': bool(key)}
    if action == 'clear_key':
        fields.pop('api_key', None)
        _store_fields_set(STORE_UK_ID, fields or None)
        return 200, {'ok': True, 'configured': False}
    if action == 'save_key':
        k = str((body or {}).get('key') or '').strip()
        if not _UK_KEY_RE.match(k):
            return 400, {'ok': False, 'error': 'Clé invalide : copiez-la telle quelle depuis Uptime Kuma.'}
        try:
            _uk_fetch(k)
        except ValueError:
            return 400, {'ok': False, 'error': 'Uptime Kuma refuse cette clé : vérifiez qu’elle est active et complète.'}
        except RuntimeError as e:
            return 502, {'ok': False, 'error': 'Uptime Kuma ne répond pas : ' + str(e)}
        fields['api_key'] = k
        _store_fields_set(STORE_UK_ID, fields)
        return 200, {'ok': True, 'configured': True}
    if action == 'monitors':
        if not key:
            return 200, {'ok': True, 'configured': False, 'monitors': []}
        try:
            mons = _uk_parse(_uk_fetch(key))
        except ValueError:
            return 200, {'ok': True, 'configured': True, 'denied': True, 'monitors': []}
        except RuntimeError as e:
            return 502, {'ok': False, 'error': 'Uptime Kuma ne répond pas : ' + str(e)}
        return 200, {'ok': True, 'configured': True, 'monitors': mons}
    return 400, {'ok': False, 'error': 'Requête non autorisée.'}


# ── MDM-STORE-V1 · fenêtre « PhotoPrism » : le NAS parle à l'API de PhotoPrism et sert photos, albums et miniatures ──────
# Le NAS se connecte à PhotoPrism avec le compte « admin » créé à l'installation (mot de passe propre à l'installation, ou
# celui que l'utilisateur a choisi depuis). Le mot de passe et la session restent sur le NAS : le navigateur ne voit
# que des photos. Tout est en lecture seule.
STORE_PP_ID = 'photoprism'
_PP_BASE = 'http://dt-photoprism:8080'
_PP_LOCK = _threading.Lock()
_pp_sess = {}
_PP_SIZES = ('tile_224', 'tile_500', 'fit_720', 'fit_1280', 'fit_2048')
_PP_HASH = re.compile(r'^[a-f0-9]{8,64}$')
_PP_UID = re.compile(r'^[a-z0-9]{8,32}$')


def _pp_http(method, path, data=None, headers=None, timeout=30, limit=8 * 1024 * 1024, base=None):
    """(code, type de contenu, octets) ; RuntimeError si PhotoPrism ne répond pas. Ni proxy ni redirection."""
    import urllib.request, urllib.error
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    hd = {'Accept': 'application/json'}
    hd.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        hd['Content-Type'] = 'application/json'
    req = urllib.request.Request((base or _PP_BASE) + path, data=body, headers=hd, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.headers.get('Content-Type', ''), r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.headers.get('Content-Type', ''), e.read(65536)
        except Exception:
            return e.code, '', b''
    except Exception as e:
        raise RuntimeError(str(e)[:120])


def _pp_password():
    return str(_store_fields_get(STORE_PP_ID).get('password') or '') or _store_derive(STORE_PP_ID, 'photoprism')


def _pp_login(pw=None):
    """Ouvre une session ; ValueError('auth') si le mot de passe est refusé, RuntimeError sinon."""
    st, _ct, raw = _pp_http('POST', '/api/v1/session', {'username': 'admin', 'password': _pp_password() if pw is None else pw})
    if st in (401, 403):
        raise ValueError('auth')
    if st != 200:
        raise RuntimeError('HTTP %d' % st)
    try:
        d = json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        raise RuntimeError('réponse illisible')
    cfg = d.get('config') if isinstance(d.get('config'), dict) else {}
    sid = str(d.get('id') or d.get('session_id') or d.get('access_token') or '')
    prev = str(cfg.get('previewToken') or d.get('previewToken') or '')
    if not sid or not prev:
        raise RuntimeError('session incomplète')
    with _PP_LOCK:
        _pp_sess.clear()
        _pp_sess.update({'id': sid, 'prev': prev, 'at': _st_time.time()})


def _pp_auth(force=False):
    with _PP_LOCK:
        ok = _pp_sess.get('id') and _st_time.time() - _pp_sess.get('at', 0) < 3000
    if force or not ok:
        _pp_login()
    with _PP_LOCK:
        return {'X-Auth-Token': _pp_sess['id'], 'X-Session-ID': _pp_sess['id']}, _pp_sess['prev']


def _pp_get(path, params=None, limit=8 * 1024 * 1024):
    import urllib.parse
    url = path + (('?' + urllib.parse.urlencode(params)) if params else '')
    for attempt in (0, 1):
        hd, _prev = _pp_auth(force=bool(attempt))
        st, ct, raw = _pp_http('GET', url, headers=hd, limit=limit)
        if st in (401, 403) and not attempt:
            continue
        if st in (401, 403):
            raise ValueError('auth')
        if st != 200:
            raise RuntimeError('HTTP %d' % st)
        return ct, raw
    raise RuntimeError('session refusée')


def _pp_write(method, path, data=None):
    """Écriture dans PhotoPrism (albums, favoris) → JSON ; une reconnexion si la session est refusée."""
    for attempt in (0, 1):
        hd, _prev = _pp_auth(force=bool(attempt))
        st, _ct, raw = _pp_http(method, path, data if data is not None else ({} if method in ('POST', 'PUT') else None), hd, limit=1024 * 1024)
        if st in (401, 403) and not attempt:
            continue
        if st in (401, 403):
            raise ValueError('auth')
        if st == 404:
            raise ValueError('Élément introuvable dans PhotoPrism.')
        if st not in (200, 201):
            raise RuntimeError('HTTP %d' % st)
        try:
            return json.loads(raw.decode('utf-8', 'replace')) if raw else {}
        except ValueError:
            return {}
    raise RuntimeError('session refusée')


def _pp_original(uid, video_only=False):
    """Chemin, sur le NAS, du fichier original d'une photo ou vidéo ; ValueError (message lisible) sinon."""
    _ct, raw = _pp_get('/api/v1/photos/%s' % uid)
    d = _pp_json(raw)
    files = [x for x in ((d.get('Files') or d.get('files') or []) if isinstance(d, dict) else []) if isinstance(x, dict)]
    def kind(x):
        return str(_px_media_type(str(_pp_first(x, 'Name', 'name'))) or '')
    pick = [x for x in files if kind(x).startswith('video/')] if video_only else \
        ([x for x in files if _pp_first(x, 'Primary', 'primary')] or files)
    if not pick:
        raise ValueError('Fichier introuvable : il n’est pas lisible depuis Desktroo.')
    name = str(_pp_first(pick[0], 'Name', 'name')).strip('/')
    if any(seg in ('', '.', '..') for seg in name.split('/')):
        raise ValueError('Fichier introuvable : il n’est pas lisible depuis Desktroo.')
    for m in (_store_nas_get(STORE_PP_ID) or {}).get('mounts') or []:
        lab = str(m.get('label') or '')
        if lab and name.startswith(lab + '/'):
            root = os.path.realpath(str(m.get('path') or ''))
            cand = os.path.realpath(os.path.join(root, name[len(lab) + 1:]))
            if cand.startswith(root + os.sep) and os.path.isfile(cand):
                return cand
            break
    raise ValueError('Ce fichier n’est pas accessible depuis Desktroo.')


def _pp_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _pp_first(d, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return ''


def _pp_photo(p):
    h = str(_pp_first(p, 'Hash', 'hash'))
    if not _PP_HASH.match(h):
        return None
    return {'uid': str(_pp_first(p, 'UID', 'uid')), 'hash': h, 'title': str(_pp_first(p, 'Title', 'title'))[:160],
            'taken': str(_pp_first(p, 'TakenAtLocal', 'TakenAt', 'taken_at'))[:25], 'type': str(_pp_first(p, 'Type', 'type')),
            'w': _pp_int(_pp_first(p, 'Width', 'width')), 'h': _pp_int(_pp_first(p, 'Height', 'height')),
            'fav': bool(_pp_first(p, 'Favorite', 'favorite'))}


def _pp_json(raw):
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        raise RuntimeError('réponse illisible')


def _pp_clean_path(p):
    p = str(p or '').strip().strip('/')
    if len(p) > 400 or '\x00' in p or any(seg in ('..', '.') for seg in p.split('/')):
        raise ValueError('Dossier invalide.')
    return p


_pp_job = {}
_PP_HIDE = re.compile(r'^(appdata_.*|files_trashbin|files_versions|files_encryption|cache|@eaDir|#recycle|\..*)$', re.I)


def _pp_index_run(path):
    """Fil d'arrière-plan : PhotoPrism ne répond à /api/v1/index qu'une fois l'indexation terminée."""
    try:
        st, _ct, raw = _pp_http('POST', '/api/v1/index', {'path': '/' + path, 'rescan': False, 'cleanup': False},
                                headers=_pp_auth()[0], timeout=8 * 3600, limit=65536)
        err = ''
        if st in (401, 403):
            err = 'auth'
        elif st != 200:
            try:
                err = str(json.loads(raw.decode('utf-8', 'replace')).get('error') or '')[:200]
            except ValueError:
                err = ''
            err = err or ('HTTP %d' % st)
    except ValueError:
        err = 'auth'
    except RuntimeError as e:
        err = str(e)[:200]
    with _PP_LOCK:
        if _pp_job.get('path') == path and _pp_job.get('state') == 'running':
            _pp_job.update({'state': 'error' if err else 'done', 'error': err, 'ended': int(_st_time.time())})


def _pp_job_view():
    with _PP_LOCK:
        j = dict(_pp_job)
    if j.get('state') == 'running':
        j['elapsed'] = max(0, int(_st_time.time()) - int(j.get('started') or 0))
    return j


def store_photoprism_img(h, s):
    """(code, type, octets) d'une miniature ; l'adresse de PhotoPrism et son jeton ne sortent pas du NAS."""
    h, s = str(h or ''), str(s or '')
    if not _PP_HASH.match(h) or s not in _PP_SIZES:
        return 400, 'text/plain', b''
    try:
        for attempt in (0, 1):
            hd, prev = _pp_auth(force=bool(attempt))
            hd = dict(hd)
            hd['Accept'] = 'image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5'
            st, ct, raw = _pp_http('GET', '/api/v1/t/%s/%s/%s' % (h, prev, s), headers=hd, timeout=60, limit=24 * 1024 * 1024)
            if st == 200 and ct.lower().startswith('image/'):
                return 200, ct.split(';')[0], raw
            why = 'PhotoPrism : HTTP %s (%s) %s' % (st, ct.split(';')[0] or '?', raw[:120].decode('utf-8', 'replace') if st != 200 else '')
            if not attempt:
                continue   # PhotoPrism change de jeton d'aperçu à chaque redémarrage : une seule reconnexion, puis on réessaie
            log.warning('Miniature PhotoPrism refusée : %s', why)
            return (404 if st == 404 else 502), 'text/plain; charset=utf-8', why.encode('utf-8')
    except ValueError:
        return 502, 'text/plain; charset=utf-8', 'PhotoPrism : session refusée'.encode('utf-8')
    except RuntimeError as e:
        return 502, 'text/plain; charset=utf-8', ('PhotoPrism : ' + str(e)[:140]).encode('utf-8')
    return 502, 'text/plain', b''


def store_photoprism_api(body):
    """{ action: state | save_password | clear_password | photos | albums | folders } → { ok, ... }."""
    if STORE_PP_ID not in store_installed_get():
        return 409, {'ok': False, 'error': 'PhotoPrism n’est pas installé.'}
    b = body if isinstance(body, dict) else {}
    action = str(b.get('action') or '')
    fields = _store_fields_get(STORE_PP_ID)
    try:
        if action == 'state':
            try:
                _pp_auth(force=True)
            except ValueError:
                return 200, {'ok': True, 'denied': True, 'custom': bool(fields.get('password'))}
            return 200, {'ok': True, 'denied': False, 'custom': bool(fields.get('password'))}
        if action == 'clear_password':
            fields.pop('password', None)
            _store_fields_set(STORE_PP_ID, fields or None)
            with _PP_LOCK:
                _pp_sess.clear()
            return 200, {'ok': True}
        if action == 'save_password':
            pw = str(b.get('password') or '')
            if not (8 <= len(pw) <= 72):
                return 400, {'ok': False, 'error': 'Le mot de passe doit faire de 8 à 72 caractères.'}
            try:
                _pp_login(pw)
            except ValueError:
                return 400, {'ok': False, 'error': 'PhotoPrism refuse ce mot de passe pour le compte « admin ».'}
            fields['password'] = pw
            _store_fields_set(STORE_PP_ID, fields)
            return 200, {'ok': True}
        if action == 'index_status':
            return 200, {'ok': True, 'job': _pp_job_view()}
        if action == 'index_cancel':
            try:
                _pp_http('DELETE', '/api/v1/index', headers=_pp_auth()[0], limit=65536)
            except (ValueError, RuntimeError):
                pass
            with _PP_LOCK:
                if _pp_job.get('state') == 'running':
                    _pp_job.update({'state': 'cancelled', 'ended': int(_st_time.time())})
            return 200, {'ok': True, 'job': _pp_job_view()}
        if action == 'index_start':
            path = _pp_clean_path(b.get('path'))
            if not path:
                return 400, {'ok': False, 'error': 'Ouvrez d’abord un dossier précis : l’indexation de tout le NAS n’est pas proposée.'}
            if any(_PP_HIDE.match(seg) for seg in path.split('/')):
                return 400, {'ok': False, 'error': 'Ce dossier est un dossier technique : il ne contient pas vos photos.'}
            _pp_auth()
            with _PP_LOCK:
                if _pp_job.get('state') == 'running':
                    return 409, {'ok': False, 'error': 'Une indexation est déjà en cours.'}
                _pp_job.clear()
                _pp_job.update({'path': path, 'state': 'running', 'started': int(_st_time.time()), 'error': ''})
            t = _threading.Thread(target=_pp_index_run, args=(path,), daemon=True)
            t.start()
            return 200, {'ok': True, 'job': _pp_job_view()}
        if action == 'fav':
            uid = str(b.get('uid') or '')
            if not _PP_UID.match(uid):
                return 400, {'ok': False, 'error': 'Requête non autorisée.'}
            on = bool(b.get('on'))
            for attempt in (0, 1):
                hd, _prev = _pp_auth(force=bool(attempt))
                st, _ct, _raw = _pp_http('POST' if on else 'DELETE', '/api/v1/photos/%s/like' % uid, headers=hd, limit=65536)
                if st in (401, 403) and not attempt:
                    continue
                break
            if st in (401, 403):
                raise ValueError('auth')
            if st != 200:
                raise RuntimeError('HTTP %d' % st)
            return 200, {'ok': True, 'fav': on}
        if action in ('video', 'original'):
            uid = str(b.get('uid') or '')
            if not _PP_UID.match(uid):
                return 400, {'ok': False, 'error': 'Requête non autorisée.'}
            host = _pp_original(uid, video_only=(action == 'video'))
            try:
                t = _px_media_ticket(host)
            except ValueError as e:
                return 400, {'ok': False, 'error': str(e)}
            return 200, {'ok': True, 'url': '/media/' + t + '/' + _px_quote(os.path.basename(host)), 'type': _px_media_type(host),
                         'name': os.path.basename(host)}
        if action == 'info':
            uid = str(b.get('uid') or '')
            if not _PP_UID.match(uid):
                return 400, {'ok': False, 'error': 'Requête non autorisée.'}
            _ct, raw = _pp_get('/api/v1/photos/%s' % uid)
            d = _pp_json(raw) if raw else {}
            d = d if isinstance(d, dict) else {}
            files = [x for x in (d.get('Files') or d.get('files') or []) if isinstance(x, dict)]
            fl = files[0] if files else {}
            cam = ' '.join(x for x in (str(_pp_first(d, 'CameraMake', 'camera_make') or ''), str(_pp_first(d, 'CameraModel', 'camera_model') or '')) if x)
            place = ', '.join(x for x in (str(_pp_first(d, 'PlaceCity', 'place_city') or ''), str(_pp_first(d, 'PlaceCountry', 'place_country') or '')) if x)
            return 200, {'ok': True, 'info': {
                'title': str(_pp_first(d, 'Title', 'title'))[:200], 'description': str(_pp_first(d, 'Description', 'description'))[:600],
                'taken': str(_pp_first(d, 'TakenAtLocal', 'TakenAt', 'taken_at'))[:25], 'camera': cam[:80],
                'lens': str(_pp_first(d, 'LensModel', 'lens_model') or '')[:80], 'iso': _pp_int(_pp_first(d, 'Iso', 'iso')),
                'exposure': str(_pp_first(d, 'Exposure', 'exposure') or '')[:20], 'fnumber': _pp_first(d, 'FNumber', 'f_number') or 0,
                'focal': _pp_int(_pp_first(d, 'FocalLength', 'focal_length')), 'place': place[:120],
                'width': _pp_int(_pp_first(fl, 'Width', 'width')), 'height': _pp_int(_pp_first(fl, 'Height', 'height')),
                'size': _pp_int(_pp_first(fl, 'Size', 'size')), 'file': str(_pp_first(fl, 'Name', 'name') or '')[:300],
                'mime': str(_pp_first(fl, 'Mime', 'mime') or '')[:40]}}
        if action == 'album_create':
            title = str(b.get('title') or '').strip()[:120]
            if not title:
                return 400, {'ok': False, 'error': 'Donnez un nom à l’album.'}
            d = _pp_write('POST', '/api/v1/albums', {'Title': title, 'Favorite': False})
            auid = str(_pp_first(d, 'UID', 'uid'))
            if not _PP_UID.match(auid):
                raise RuntimeError('album non créé')
            return 200, {'ok': True, 'uid': auid, 'title': title}
        if action in ('album_rename', 'album_delete', 'album_add', 'album_remove'):
            auid = str(b.get('uid') or '')
            if not _PP_UID.match(auid):
                return 400, {'ok': False, 'error': 'Requête non autorisée.'}
            if action == 'album_delete':
                _pp_write('DELETE', '/api/v1/albums/%s' % auid)
            elif action == 'album_rename':
                title = str(b.get('title') or '').strip()[:120]
                if not title:
                    return 400, {'ok': False, 'error': 'Donnez un nom à l’album.'}
                _pp_write('PUT', '/api/v1/albums/%s' % auid, {'Title': title})
            else:
                ph = [str(x) for x in (b.get('photos') or [])][:50]
                if not ph or not all(_PP_UID.match(x) for x in ph):
                    return 400, {'ok': False, 'error': 'Requête non autorisée.'}
                _pp_write('POST' if action == 'album_add' else 'DELETE', '/api/v1/albums/%s/photos' % auid, {'photos': ph})
            return 200, {'ok': True}
        count = max(1, min(int(b.get('count') or 60), 120))
        offset = max(0, int(b.get('offset') or 0))
        if action == 'photos':
            prm = {'count': count, 'offset': offset, 'merged': 'true', 'order': 'newest'}
            q = str(b.get('q') or '').strip()[:200]
            if q:
                prm['q'] = q
            album = str(b.get('album') or '')
            if album:
                if not _PP_UID.match(album):
                    return 400, {'ok': False, 'error': 'Requête non autorisée.'}
                prm['album'] = album
            if b.get('path'):
                prm['path'] = _pp_clean_path(b.get('path'))
            kind = str(b.get('kind') or '')
            if kind == 'video':
                prm['type'] = 'video'
            elif kind == 'fav':
                prm['favorite'] = 'true'
            _ct, raw = _pp_get('/api/v1/photos', prm)
            data = _pp_json(raw)
            rows = [x for x in (_pp_photo(p) for p in (data if isinstance(data, list) else [])) if x]
            return 200, {'ok': True, 'photos': rows, 'more': isinstance(data, list) and len(data) >= count}
        if action == 'albums':
            kind = str(b.get('kind') or 'album')
            if kind not in ('album', 'moment', 'month', 'state'):
                kind = 'album'
            _ct, raw = _pp_get('/api/v1/albums', {'count': 300, 'offset': 0, 'type': kind, 'order': 'newest'})
            data = _pp_json(raw)
            rows = []
            for a in (data if isinstance(data, list) else []):
                th = str(_pp_first(a, 'Thumb', 'thumb'))
                rows.append({'uid': str(_pp_first(a, 'UID', 'uid')), 'title': str(_pp_first(a, 'Title', 'title'))[:120],
                              'count': _pp_int(_pp_first(a, 'PhotoCount', 'photo_count')),
                              'thumb': th if _PP_HASH.match(th) else ''})
            rows = [r for r in rows if _PP_UID.match(r['uid'])]
            if kind != 'album':
                # Mois / moments : PhotoPrism tient mal leur décompte et leur vignette ; on vérifie avec la première photo,
                # et on retire ceux qui ne contiennent rien (restes d'anciennes indexations).
                keep, probes = [], 0
                for r in rows:
                    if r['count'] > 0 and r['thumb']:
                        keep.append(r); continue
                    if probes >= 60:
                        continue
                    probes += 1
                    try:
                        _c2, raw2 = _pp_get('/api/v1/photos', {'count': 1, 'offset': 0, 'merged': 'true', 'album': r['uid']})
                        first = [x for x in (_pp_photo(p) for p in (_pp_json(raw2) if isinstance(_pp_json(raw2), list) else [])) if x]
                    except (ValueError, RuntimeError):
                        first = []
                    if first:
                        r['thumb'] = r['thumb'] or str(first[0].get('hash') or '')
                        keep.append(r)
                rows = keep
            return 200, {'ok': True, 'albums': rows}
        if action == 'folders':
            import urllib.parse
            path = _pp_clean_path(b.get('path'))
            ep = '/api/v1/folders/originals' + (('/' + urllib.parse.quote(path)) if path else '')
            _ct, raw = _pp_get(ep, {'recursive': 'false', 'uncached': 'true', 'count': 1000, 'offset': 0})
            data = _pp_json(raw)
            lst = data if isinstance(data, list) else (data.get('folders') or data.get('Folders') or []) if isinstance(data, dict) else []
            rows = []
            for f in lst:
                p = str(_pp_first(f, 'Path', 'path')).strip('/')
                if not p or p == path or _PP_HIDE.match(p.rsplit('/', 1)[-1]):
                    continue
                th = str(_pp_first(f, 'Thumb', 'thumb'))
                rows.append({'path': p, 'name': str(_pp_first(f, 'Name', 'name') or p.rsplit('/', 1)[-1])[:120],
                              'thumb': th if _PP_HASH.match(th) else ''})
            rows.sort(key=lambda r: r['name'].lower())
            return 200, {'ok': True, 'folders': rows}
    except ValueError as e:
        if str(e) == 'auth':
            return 200, {'ok': True, 'denied': True}
        return 400, {'ok': False, 'error': str(e)}
    except RuntimeError as e:
        return 502, {'ok': False, 'error': 'PhotoPrism ne répond pas : ' + str(e)}
    return 400, {'ok': False, 'error': 'Requête non autorisée.'}


# ── MDM-STORE-V1 · fenêtre « Jellyfin » : le NAS parle à l'API de Jellyfin (bibliothèques, comptes, état) ──────────────
# Le NAS crée lui-même le compte administrateur « admin » au premier démarrage (assistant de Jellyfin sauté), puis s'y
# connecte avec son mot de passe propre à l'installation (ou celui que l'utilisateur a choisi depuis). Mot de passe et
# jeton restent sur le NAS : le navigateur ne voit que des listes et des affiches.
STORE_JF_ID = 'jellyfin'
_JF_BASE = 'http://dt-jellyfin:8080'
_JF_LOCK = _threading.Lock()
_JF_SETUP_LOCK = _threading.Lock()
_jf_sess = {}
_JF_ID = re.compile(r'^[a-f0-9]{32}$')
_JF_TYPES = ('movies', 'tvshows', 'music', 'musicvideos', 'homevideos', 'books', 'mixed')
_JF_AUTH = 'MediaBrowser Client="Desktroo", Device="Desktroo NAS", DeviceId="desktroo-nas", Version="1.0"'


def _jf_http(method, path, data=None, token=None, timeout=30, limit=4 * 1024 * 1024):
    """(code, octets) ; RuntimeError si Jellyfin ne répond pas."""
    hd = {'Authorization': _JF_AUTH + (', Token="%s"' % token if token else '')}
    try:
        st, _ct, raw = _pp_http(method, path, data, hd, timeout=timeout, limit=limit, base=_JF_BASE)
    except RuntimeError:
        raise
    return st, raw


def _jf_json(raw):
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        return None


def _jf_password():
    return str(_store_fields_get(STORE_JF_ID).get('password') or '') or _store_derive(STORE_JF_ID, 'jellyfin')


def _jf_login(pw=None, user=None):
    """Ouvre une session ; ValueError('auth') si le mot de passe est refusé, RuntimeError sinon."""
    user = user or str(_store_fields_get(STORE_JF_ID).get('user') or '') or 'admin'
    st, raw = _jf_http('POST', '/Users/AuthenticateByName', {'Username': user, 'Pw': _jf_password() if pw is None else pw})
    if st in (400, 401, 403):
        raise ValueError('auth')
    d = _jf_json(raw) if st == 200 else None
    if not isinstance(d, dict) or not d.get('AccessToken'):
        raise RuntimeError('HTTP %d' % st)
    uid = str((d.get('User') or {}).get('Id') or '').replace('-', '')
    with _JF_LOCK:
        _jf_sess.clear()
        _jf_sess.update({'tok': str(d['AccessToken']), 'uid': uid, 'at': _st_time.time()})


def _jf_token(force=False):
    with _JF_LOCK:
        ok = _jf_sess.get('tok') and _st_time.time() - _jf_sess.get('at', 0) < 1800
    if force or not ok:
        _jf_login()
    with _JF_LOCK:
        return _jf_sess['tok']


def _jf_call(method, path, params=None, data=None, limit=4 * 1024 * 1024, expect=(200, 204)):
    """Appel authentifié (une reconnexion si le jeton est refusé) → JSON ou None ; ValueError('auth'), RuntimeError."""
    import urllib.parse
    url = path + (('?' + urllib.parse.urlencode(params)) if params else '')
    for attempt in (0, 1):
        st, raw = _jf_http(method, url, data, token=_jf_token(force=bool(attempt)), limit=limit)
        if st == 401 and not attempt:
            continue
        if st == 401:
            raise ValueError('auth')
        if st == 400 and raw:
            d = _jf_json(raw)
            raise ValueError(str(d if isinstance(d, str) else (d or {}).get('title') or raw[:160].decode('utf-8', 'replace'))[:160])
        if st not in expect:
            raise RuntimeError('HTTP %d' % st)
        return _jf_json(raw) if raw else None
    raise RuntimeError('session refusée')


def _jf_setup():
    """Premier démarrage : langue, pays et compte « admin » (assistant sauté). Sans effet si déjà fait."""
    with _JF_SETUP_LOCK:
        st, raw = _jf_http('GET', '/System/Info/Public')
        d = _jf_json(raw) if st == 200 else None
        if not isinstance(d, dict):
            raise RuntimeError('HTTP %d' % st)
        if d.get('StartupWizardCompleted'):
            return False
        pw = _jf_password()
        steps = [('POST', '/Startup/Configuration', {'UICulture': 'fr', 'MetadataCountryCode': 'FR', 'PreferredMetadataLanguage': 'fr'}),
                 ('GET', '/Startup/User', None),
                 ('POST', '/Startup/User', {'Name': 'admin', 'Password': pw}),
                 ('POST', '/Startup/RemoteAccess', {'EnableRemoteAccess': True, 'EnableAutomaticPortMapping': False}),
                 ('POST', '/Startup/Complete', None)]
        for m, p, body in steps:
            s, _r = _jf_http(m, p, body)
            if s not in (200, 204):
                raise RuntimeError('assistant : %s HTTP %d' % (p, s))
        log.info('Magasin : Jellyfin configuré (compte administrateur créé)')
        return True


def _jf_hexid(v):
    return str(v or '').replace('-', '').lower()


def _jf_clean_path(p, allow_root=False):
    """Chemin sous /nas (les dossiers cochés pour l'app), sans « .. » ni dossier technique ; '' si refusé."""
    p = '/' + str(p or '').strip().strip('/')
    if p == '/' and allow_root:
        return '/nas'
    if not re.match(r'^/nas(/[^/\x00-\x1f]{1,255}){0,12}$', p) or '..' in p.split('/'):
        return ''
    if any(_PP_HIDE.match(seg) for seg in p.split('/')[2:]):
        return ''
    return p


def _jf_item(x):
    iid = _jf_hexid(x.get('Id'))
    if not _JF_ID.match(iid):
        return None
    tags = x.get('ImageTags') if isinstance(x.get('ImageTags'), dict) else {}
    return {'id': iid, 'name': str(x.get('Name') or '')[:120], 'type': str(x.get('Type') or '')[:20],
            'series': str(x.get('SeriesName') or x.get('AlbumArtist') or '')[:120], 'year': _pp_int(x.get('ProductionYear')),
            'img': bool(tags.get('Primary'))}


_JF_CNT = {}
_JF_IDENT = {}      # derniers résultats de recherche d'identification, par élément (le navigateur ne renvoie jamais leur contenu)


def _jf_libraries(busy=False):
    """Bibliothèques et nombre de titres. Pendant une analyse Jellyfin est très sollicité : on réutilise le dernier
    décompte (moins d'une minute) au lieu de l'interroger à chaque rafraîchissement."""
    rows = []
    for f in (_jf_call('GET', '/Library/VirtualFolders') or []):
        iid = _jf_hexid(f.get('ItemId'))
        typ = str(f.get('CollectionType') or '')
        count = 0
        incl = {'movies': 'Movie', 'tvshows': 'Series', 'music': 'MusicAlbum', 'books': 'Book', 'homevideos': 'Video,Photo', 'musicvideos': 'MusicVideo'}.get(typ)
        if incl and _JF_ID.match(iid):
            old = _JF_CNT.get(iid)
            if busy and old and _st_time.time() - old[1] < 60:
                count = old[0]
            else:
                try:
                    r = _jf_call('GET', '/Items', {'ParentId': iid, 'Recursive': 'true', 'Limit': 0, 'IncludeItemTypes': incl})
                    count = _pp_int((r or {}).get('TotalRecordCount'))
                    _JF_CNT[iid] = (count, _st_time.time())
                except (ValueError, RuntimeError):
                    count = old[0] if old else 0
        rows.append({'id': iid, 'name': str(f.get('Name') or '')[:80], 'type': typ,
                     'paths': [str(x)[:300] for x in (f.get('Locations') or [])][:10], 'count': count})
    return rows

def _jf_dir_exists(path):
    try:
        _jf_call('GET', '/Environment/DirectoryContents', {'path': path, 'includeFiles': 'false', 'includeDirectories': 'true'})
        return True
    except ValueError:
        return False
    except RuntimeError as e:
        if str(e).startswith('HTTP '):    # Jellyfin répond par une erreur (404, 500…) pour un dossier qui n'existe pas
            return False
        raise


def _jf_repair_plan():
    """Chemins de bibliothèque devenus introuvables (ex. /media/series d'un ancien serveur) et dossier /nas proposé."""
    folders = []
    try:
        for f in (_jf_call('GET', '/Environment/DirectoryContents', {'path': '/nas', 'includeFiles': 'false', 'includeDirectories': 'true'}) or []):
            p = str(f.get('Path') or '').rstrip('/')
            if p.startswith('/nas/') and p.count('/') == 2 and _jf_clean_path(p):
                folders.append(p)
    except ValueError:
        pass
    except RuntimeError as e:
        if not str(e).startswith('HTTP '):
            raise
    norm = lambda s: re.sub(r'[^a-z0-9]', '', str(s).lower())
    plan = []
    for lib in _jf_libraries(True):
        for old in lib['paths']:
            if old.startswith('/nas/') or old.startswith('/config/') or _jf_dir_exists(old):
                continue
            n = norm(old.rstrip('/').rsplit('/', 1)[-1])
            cand = [f for f in folders if norm(f.rsplit('/', 1)[-1]) == n]
            if not cand:
                pre = [f for f in folders if n and norm(f.rsplit('/', 1)[-1]).startswith(n)]
                cand = pre if len(pre) == 1 else []
            plan.append({'library': lib['name'], 'old': old, 'new': cand[0] if len(cand) == 1 else ''})
    return plan



def _jf_scan_state():
    for t in (_jf_call('GET', '/ScheduledTasks') or []):
        if t.get('Key') == 'RefreshLibrary':
            run = str(t.get('State') or '') == 'Running'
            return {'running': run, 'pct': int(round(float(t.get('CurrentProgressPercentage') or 0))) if run else 0}
    return {'running': False, 'pct': 0}


def _jf_user_row(u):
    pol = u.get('Policy') if isinstance(u.get('Policy'), dict) else {}
    return {'id': _jf_hexid(u.get('Id')), 'name': str(u.get('Name') or '')[:60], 'admin': bool(pol.get('IsAdministrator')),
            'disabled': bool(pol.get('IsDisabled')), 'rating': pol.get('MaxParentalRating') if isinstance(pol.get('MaxParentalRating'), int) else None,
            'last': str(u.get('LastActivityDate') or '')[:25],
            'all_folders': bool(pol.get('EnableAllFolders', True)),
            'folders': [_jf_hexid(x) for x in (pol.get('EnabledFolders') or []) if _JF_ID.match(_jf_hexid(x))]}


def _jf_guid(h):
    import uuid
    return str(uuid.UUID(str(h)))


def _jf_set_policy(uid, disabled=None, rating='keep', folders='keep'):
    u = _jf_call('GET', '/Users/' + uid)
    pol = u.get('Policy') if isinstance(u, dict) and isinstance(u.get('Policy'), dict) else None
    if pol is None:
        raise RuntimeError('compte illisible')
    if disabled is not None:
        pol['IsDisabled'] = bool(disabled)
    if rating != 'keep':
        pol['MaxParentalRating'] = rating
    if folders != 'keep':          # None = toutes les bibliothèques ; liste = seulement celles-ci
        if folders is None:
            pol['EnableAllFolders'] = True; pol['EnabledFolders'] = []
        else:
            pol['EnableAllFolders'] = False; pol['EnabledFolders'] = [_jf_guid(x) for x in folders]
    _jf_call('POST', '/Users/%s/Policy' % uid, data=pol)


def _jf_folders_arg(v):
    """'keep' si absent ; None (toutes) ou liste d'identifiants de bibliothèques existantes ; ValueError sinon."""
    if v == 'keep':
        return 'keep'
    if v is None:
        return None
    if not isinstance(v, list):
        raise ValueError('Bibliothèques invalides.')
    known = {l['id'] for l in _jf_libraries(True)}
    ids = [_jf_hexid(x) for x in v][:50]
    if not all(i in known for i in ids):
        raise ValueError('Bibliothèque inconnue.')
    return ids


def store_jellyfin_img(item_id, size='tile'):
    """Affiche d'un élément : (code, type de contenu, octets). Jamais d'identifiant libre vers Jellyfin."""
    iid = _jf_hexid(item_id)
    if STORE_JF_ID not in store_installed_get() or not _JF_ID.match(iid):
        return 404, 'text/plain', b''
    h = 360 if size != 'big' else 900
    try:
        for attempt in (0, 1):
            st, ct, raw = _pp_http('GET', '/Items/%s/Images/Primary?fillHeight=%d&quality=85' % (iid, h),
                                   headers={'Authorization': _JF_AUTH + ', Token="%s"' % _jf_token(force=bool(attempt)),
                                            'Accept': 'image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.5'},
                                   base=_JF_BASE, limit=4 * 1024 * 1024)
            if st == 401 and not attempt:
                continue
            break
    except (ValueError, RuntimeError):
        return 502, 'text/plain', b''
    if st != 200 or not str(ct).lower().startswith('image/'):
        return 404, 'text/plain', b''
    return 200, str(ct).split(';')[0], raw


# ── Lecture dans la fenêtre : Jellyfin relayé par une adresse signée (le navigateur n'a ni jeton ni accès au réseau local) ──
_JF_DIRECT_CONT = ('mp4', 'm4v', 'mov', 'webm')
_JF_DIRECT_VID = ('h264', 'vp8', 'vp9', 'av1')
_JF_DIRECT_AUD = ('aac', 'mp3', 'opus', 'vorbis', 'flac')


def _jf_ticket(iid, mode, start=0, ttl=8 * 3600, audio=None, sub=None, burn=False):
    raw = json.dumps({'i': iid, 'm': mode, 's': int(start), 'e': int(_px_time.time()) + int(ttl),
                      'a': audio, 'u': sub, 'b': bool(burn)}).encode('utf-8')
    sig = _px_hmac.new(_PX_MEDIA_KEY, b'jf' + raw, _px_hash.sha256).digest()[:16]
    return _px_b64.urlsafe_b64encode(sig + raw).decode('ascii').rstrip('=')


def _jf_ticket_parse(t):
    """→ (id, mode, début en s, index audio ou None, index sous-titre ou None, incrustation) ou None."""
    try:
        blob = _px_b64.urlsafe_b64decode(str(t) + '=' * (-len(str(t)) % 4))
        sig, raw = blob[:16], blob[16:]
        if not _px_hmac.compare_digest(sig, _px_hmac.new(_PX_MEDIA_KEY, b'jf' + raw, _px_hash.sha256).digest()[:16]):
            return None
        d = json.loads(raw.decode('utf-8'))
        iid, mode = str(d['i']), str(d['m'])
        if int(d['e']) < _px_time.time() or not _JF_ID.match(iid) or mode not in ('direct', 'trans', 'audio', 'vtt'):
            return None
        ai = d.get('a') if isinstance(d.get('a'), int) and 0 <= d.get('a') < 1000 else None
        si = d.get('u') if isinstance(d.get('u'), int) and 0 <= d.get('u') < 1000 else None
        return iid, mode, max(0, int(d['s'])), ai, si, bool(d.get('b'))
    except Exception:
        return None


def _jf_stream_path(iid, mode, start, audio=None, sub=None, burn=False):
    if mode == 'audio':
        return '/Audio/%s/stream?static=true&MediaSourceId=%s' % (iid, iid)
    if mode == 'direct':
        return '/Videos/%s/stream?static=true&MediaSourceId=%s' % (iid, iid)
    if mode == 'vtt':
        return '/Videos/%s/%s/Subtitles/%d/%d/Stream.vtt' % (iid, iid, sub or 0, int(start) * 10000000)
    extra = ('&AudioStreamIndex=%d' % audio) if audio is not None else ''
    if burn and sub is not None:
        extra += '&SubtitleStreamIndex=%d&SubtitleMethod=Encode' % sub
    else:
        extra += '&SubtitleMethod=Hide'
    return ('/Videos/%s/stream.mp4?MediaSourceId=%s&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2&TranscodingMaxAudioChannels=2'
            '&VideoBitRate=8000000&AudioBitRate=192000&DeviceId=desktroo-nas&StartTimeTicks=%d&PlaySessionId=dk%s%s'
            % (iid, iid, int(start) * 10000000, _px_time.strftime('%H%M%S'), extra))


def _jf_stream_serve(handler, ticket):
    """Relaie le flux Jellyfin (Range compris) vers le navigateur."""
    import http.client
    t = _jf_ticket_parse(ticket)
    if not t or STORE_JF_ID not in store_installed_get():
        handler.send_response(404); handler.send_header('Content-Length', '0'); handler.end_headers(); return
    iid, mode, start, aud, sub, burn = t
    conn = None
    try:
        host, port = _JF_BASE.split('//', 1)[1].split(':')
        r = None
        for attempt in (0, 1):
            hd = {'Authorization': _JF_AUTH + ', Token="%s"' % _jf_token(force=bool(attempt))}
            if handler.headers.get('Range') and mode != 'trans':
                hd['Range'] = handler.headers.get('Range')
            conn = http.client.HTTPConnection(host, int(port), timeout=60)
            conn.request('GET', _jf_stream_path(iid, mode, start, aud, sub, burn), headers=hd)
            r = conn.getresponse()
            if r.status in (401, 403) and not attempt:
                conn.close()
                continue
            break
        if r.status not in (200, 206):
            handler.send_response(502 if r.status >= 500 else 404); handler.send_header('Content-Length', '0'); handler.end_headers(); return
        if mode == 'vtt':        # sous-titres : petit fichier texte, envoyé d'un bloc
            body = r.read(4 * 1024 * 1024)
            handler.send_response(200); handler.send_header('Content-Type', 'text/vtt; charset=utf-8')
            handler.send_header('Content-Length', str(len(body))); handler.send_header('Cache-Control', 'private, max-age=600')
            handler.send_header('X-Content-Type-Options', 'nosniff'); handler.end_headers(); handler.wfile.write(body); return
        handler.send_response(r.status)
        for k in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
            if r.getheader(k):
                handler.send_header(k, r.getheader(k))
        if not r.getheader('Content-Length'):
            handler.send_header('Connection', 'close')
            handler.close_connection = True
        handler.send_header('Cache-Control', 'private, no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.end_headers()
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:
        log.warning('lecture Jellyfin : %s', e)
        try:
            handler.send_response(502); handler.send_header('Content-Length', '0'); handler.end_headers()
        except Exception:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def _jf_uid():
    with _JF_LOCK:
        uid = _jf_sess.get('uid') or ''
    if not _JF_ID.match(uid):
        _jf_token(force=True)
        with _JF_LOCK:
            uid = _jf_sess.get('uid') or ''
    return uid


def _jf_try(variants):
    """Essaie plusieurs chemins (selon la version de Jellyfin) ; garde la première réponse acceptée."""
    last = None
    for m, p, prm, data in variants:
        try:
            return _jf_call(m, p, prm, data)
        except RuntimeError as e:
            last = e
    raise last or RuntimeError('Jellyfin')


def _jf_sec(ticks):
    return int(_pp_int(ticks) / 10000000)


def _jf_detail(iid):
    uid = _jf_uid()
    x = _jf_call('GET', '/Users/%s/Items/%s' % (uid, iid)) or {}
    ud = x.get('UserData') if isinstance(x.get('UserData'), dict) else {}
    typ = str(x.get('Type') or '')
    srcs = [m for m in (x.get('MediaSources') or []) if isinstance(m, dict)]
    ppl = [{'name': str(p.get('Name') or '')[:60], 'role': str(p.get('Role') or p.get('Type') or '')[:60]}
           for p in (x.get('People') or []) if isinstance(p, dict) and p.get('Name')][:8]
    out = {'id': iid, 'name': str(x.get('Name') or '')[:160], 'type': typ, 'year': _pp_int(x.get('ProductionYear')),
           'rating': str(x.get('OfficialRating') or '')[:12], 'score': round(float(x.get('CommunityRating') or 0), 1),
           'minutes': int(_jf_sec(x.get('RunTimeTicks')) / 60), 'overview': str(x.get('Overview') or '')[:1500],
           'genres': [str(g)[:30] for g in (x.get('Genres') or [])][:6], 'people': ppl,
           'series': str(x.get('SeriesName') or '')[:120], 'season': _pp_int(x.get('ParentIndexNumber')), 'ep': _pp_int(x.get('IndexNumber')),
           'pos': _jf_sec(ud.get('PlaybackPositionTicks')), 'played': bool(ud.get('Played')), 'fav': bool(ud.get('IsFavorite')),
           'img': bool((x.get('ImageTags') or {}).get('Primary')), 'playable': typ in ('Movie', 'Episode', 'Video', 'MusicVideo', 'Audio'),
           'seriesId': _jf_hexid(x.get('SeriesId')), 'albumId': _jf_hexid(x.get('AlbumId')), 'children': [], 'next': '',
           'artist': str(x.get('AlbumArtist') or '')[:120]}
    if typ == 'Series':
        eps = (_jf_call('GET', '/Shows/%s/Episodes' % iid, {'userId': uid, 'Fields': 'Overview', 'Limit': 400}) or {}).get('Items') or []
        seasons = {}
        for e in eps:
            if not isinstance(e, dict):
                continue
            eid = _jf_hexid(e.get('Id'))
            if not _JF_ID.match(eid):
                continue
            eu = e.get('UserData') if isinstance(e.get('UserData'), dict) else {}
            row = {'id': eid, 'name': str(e.get('Name') or '')[:120], 'n': _pp_int(e.get('IndexNumber')), 'played': bool(eu.get('Played')),
                   'pos': _jf_sec(eu.get('PlaybackPositionTicks')), 'minutes': int(_jf_sec(e.get('RunTimeTicks')) / 60),
                   'overview': str(e.get('Overview') or '')[:300]}
            seasons.setdefault(_pp_int(e.get('ParentIndexNumber')), []).append(row)
            if not out['next'] and (row['pos'] or not row['played']):
                out['next'] = eid
        out['children'] = [{'season': k, 'eps': v} for k, v in sorted(seasons.items())]
    elif typ in ('MusicAlbum', 'Playlist'):
        tr = (_jf_call('GET', '/Items', {'ParentId': iid, 'IncludeItemTypes': 'Audio', 'SortBy': 'ParentIndexNumber,IndexNumber', 'Limit': 200}) or {}).get('Items') or []
        out['children'] = [{'season': 0, 'eps': [{'id': _jf_hexid(e.get('Id')), 'name': str(e.get('Name') or '')[:120], 'n': _pp_int(e.get('IndexNumber')),
                                                  'played': False, 'pos': 0, 'minutes': int(_jf_sec(e.get('RunTimeTicks')) / 60), 'overview': '',
                                                  'sec': int(_jf_sec(e.get('RunTimeTicks'))),
                                                  'artist': str((e.get('Artists') or [e.get('AlbumArtist') or ''])[0] if isinstance(e.get('Artists'), list) else (e.get('AlbumArtist') or ''))[:120]}
                                                 for e in tr if isinstance(e, dict) and _JF_ID.match(_jf_hexid(e.get('Id')))]}]
    if typ in ('MusicAlbum', 'Playlist') and out['children'] and out['children'][0]['eps']:
        out['next'] = out['children'][0]['eps'][0]['id']
        out['playable'] = False
    out['_src'] = srcs[0] if srcs else {}
    return out


def _jf_tracks(src):
    """Pistes audio et sous-titres d'une source : [{i, label, lang, ...}]."""
    au, sb = [], []
    for st in (src.get('MediaStreams') or []):
        if not isinstance(st, dict) or not isinstance(st.get('Index'), int):
            continue
        lang = str(st.get('Language') or '')[:8].lower()
        label = str(st.get('DisplayTitle') or st.get('Title') or lang or ('Piste %d' % st['Index']))[:80]
        if st.get('Type') == 'Audio':
            au.append({'i': st['Index'], 'label': label, 'lang': lang, 'default': bool(st.get('IsDefault')), 'codec': str(st.get('Codec') or '')[:12].lower()})
        elif st.get('Type') == 'Subtitle':
            sb.append({'i': st['Index'], 'label': label, 'lang': lang, 'default': bool(st.get('IsDefault')), 'forced': bool(st.get('IsForced')),
                       'text': bool(st.get('IsTextSubtitleStream'))})
    return au, sb


def _jf_play(iid, force_trans=False, start=None, audio=None, sub=None, alang='', slang=''):
    d = _jf_detail(iid)
    src = d.pop('_src', {})
    if not d['playable']:
        raise ValueError('Cet élément ne se lit pas directement.')
    streams = [x for x in (src.get('MediaStreams') or []) if isinstance(x, dict)]
    vid = next((str(x.get('Codec') or '').lower() for x in streams if x.get('Type') == 'Video'), '')
    tracks_a, tracks_s = _jf_tracks(src)
    aud = tracks_a[0]['codec'] if tracks_a else ''
    # piste audio : choix explicite, sinon langue souhaitée, sinon celle par défaut de Jellyfin
    a_idx = None
    if isinstance(audio, int) and any(t['i'] == audio for t in tracks_a):
        a_idx = audio
    elif alang:
        a_idx = next((t['i'] for t in tracks_a if t['lang'] == alang), None)
    if a_idx is None and tracks_a:
        a_idx = next((t['i'] for t in tracks_a if t['default']), tracks_a[0]['i'])
    a_first = (not tracks_a) or a_idx == tracks_a[0]['i']
    a_codec = next((t['codec'] for t in tracks_a if t['i'] == a_idx), aud)
    # sous-titres : choix explicite (-1 = aucun), sinon langue souhaitée, sinon aucun
    s_idx = None
    if isinstance(sub, int):
        s_idx = sub if any(t['i'] == sub for t in tracks_s) else None
    elif slang and slang != 'off':
        s_idx = next((t['i'] for t in tracks_s if t['lang'] == slang and not t['forced']), None)
        if s_idx is None:
            s_idx = next((t['i'] for t in tracks_s if t['lang'] == slang), None)
    s_trk = next((t for t in tracks_s if t['i'] == s_idx), None)
    burn = bool(s_trk and not s_trk['text'])     # image (PGS, VobSub…) : incrustée dans la vidéo
    cont = [c.strip().lower() for c in str(src.get('Container') or '').split(',')]
    pos = d['pos'] if start is None else max(0, int(start))
    if d['type'] == 'Audio':
        mode = 'audio'
    elif (not force_trans and not burn and a_first and any(c in _JF_DIRECT_CONT for c in cont) and vid in _JF_DIRECT_VID
          and (not a_codec or a_codec in _JF_DIRECT_AUD)):
        mode = 'direct'
    else:
        mode = 'trans'
    off = pos if mode == 'trans' else 0
    dur = _jf_sec(src.get('RunTimeTicks')) or d['minutes'] * 60
    subs_out = []
    for t in tracks_s:
        row = {'i': t['i'], 'label': t['label'], 'lang': t['lang'], 'text': t['text'], 'forced': t['forced']}
        if t['text']:
            row['url'] = '/store/jellyfin/sub/' + _jf_ticket(iid, 'vtt', off, 8 * 3600, None, t['i'])
        subs_out.append(row)
    # générique / récap (segments Jellyfin 10.10+) et épisode suivant : facultatifs, jamais bloquants
    segs, next_id = [], ''
    if d['type'] == 'Episode':
        try:
            sg = _jf_call('GET', '/MediaSegments/%s' % iid) or {}
            for z in (sg.get('Items') if isinstance(sg, dict) else []) or []:
                if isinstance(z, dict) and str(z.get('Type') or '') in ('Intro', 'Outro', 'Recap', 'Preview'):
                    segs.append({'type': str(z['Type']), 'start': _jf_sec(z.get('StartTicks')), 'end': _jf_sec(z.get('EndTicks'))})
        except (ValueError, RuntimeError):
            pass
        if _JF_ID.match(d.get('seriesId') or ''):
            try:
                eps = (_jf_call('GET', '/Shows/%s/Episodes' % d['seriesId'], {'userId': _jf_uid(), 'Limit': 600}) or {}).get('Items') or []
                ids = [_jf_hexid(e.get('Id')) for e in eps if isinstance(e, dict)]
                if iid in ids and ids.index(iid) + 1 < len(ids) and _JF_ID.match(ids[ids.index(iid) + 1]):
                    next_id = ids[ids.index(iid) + 1]
            except (ValueError, RuntimeError):
                pass
    return {'id': iid, 'mode': mode, 'url': '/store/jellyfin/stream/' + _jf_ticket(iid, mode, off, 8 * 3600, a_idx if mode == 'trans' else None, s_idx if burn else None, burn),
            'name': d['name'], 'series': d['series'], 'season': d['season'], 'ep': d['ep'], 'duration': dur, 'pos': pos,
            'offset': off, 'audio': mode == 'audio', 'vcodec': vid, 'acodec': a_codec, 'container': ','.join(cont)[:30],
            'tracks_a': [{'i': t['i'], 'label': t['label'], 'lang': t['lang']} for t in tracks_a], 'tracks_s': subs_out,
            'aidx': a_idx, 'sidx': s_idx if s_idx is not None else -1, 'segments': segs[:12], 'next_id': next_id, 'album_id': d.get('albumId', '')}


# MDM-JF-ADM-V1 : le reste de l'API Jellyfin (tâches, plugins, transcodage, réseau, clés d'API, Quick Connect, sessions,
# télévision en direct, sous-titres, métadonnées, collections, options de bibliothèque, droits fins par compte).
# Une liste blanche d'opérations : le navigateur ne choisit jamais un chemin libre.
_JF_GUID = re.compile(r'^[A-Fa-f0-9\-]{32,36}$')
_JF_TOK = re.compile(r'^[A-Za-z0-9._\-]{1,128}$')
_JF_LANG = re.compile(r'^[a-z]{2,3}$')
_JF_CFG = {
    'conf': ('/System/Configuration', ('ServerName', 'UICulture', 'PreferredMetadataLanguage', 'MetadataCountryCode', 'QuickConnectAvailable',
                                       'LibraryMonitorDelay', 'LogFileRetentionDays', 'MinResumePct', 'MaxResumePct', 'MinResumeDurationSeconds',
                                       'EnableGroupingMoviesIntoCollections', 'EnableGroupingShowsIntoCollections')),
    'encoding': ('/System/Configuration/encoding', ('EnableHardwareEncoding', 'HardwareAccelerationType', 'VaapiDevice', 'QsvDevice', 'EnableTonemapping',
                                                    'TonemappingAlgorithm', 'EnableDecodingColorDepth10Hevc', 'EnableDecodingColorDepth10Vp9',
                                                    'EnableDecodingColorDepth10HevcRext', 'EnableDecodingColorDepth12HevcRext', 'EncoderPreset',
                                                    'DeinterlaceMethod', 'DeinterlaceDoubleRate', 'H264Crf', 'H265Crf', 'EnableSubtitleExtraction',
                                                    'AllowHevcEncoding', 'AllowAv1Encoding', 'EnableEnhancedNvdecDecoder', 'PreferSystemNativeHwDecoder',
                                                    'EnableIntelLowPowerH264HwEncoder', 'EnableIntelLowPowerHevcHwEncoder', 'HardwareDecodingCodecs',
                                                    'EncodingThreadCount', 'DownMixAudioBoost', 'EnableThrottling', 'ThrottleDelaySeconds',
                                                    'EnableSegmentDeletion', 'SegmentKeepSeconds', 'MaxMuxingQueueSize')),
    'network': ('/System/Configuration/network', ('EnableRemoteAccess', 'EnableIPv4', 'EnableIPv6', 'EnableUPnP', 'KnownProxies', 'LocalNetworkSubnets',
                                                  'IgnoreVirtualInterfaces', 'RequireHttps')),
}
_JF_LIBOPT_KEYS = ('EnableRealtimeMonitor', 'EnableChapterImageExtraction', 'ExtractChapterImagesDuringLibraryScan', 'EnableTrickplayImageExtraction',
                   'ExtractTrickplayImagesDuringLibraryScan', 'SaveLocalMetadata', 'EnableEmbeddedTitles', 'EnableEmbeddedEpisodeInfos',
                   'AutomaticRefreshIntervalDays', 'PreferredMetadataLanguage', 'MetadataCountryCode', 'SubtitleDownloadLanguages',
                   'RequirePerfectSubtitleMatch', 'SaveSubtitlesWithMedia', 'SkipSubtitlesIfEmbeddedSubtitlesPresent',
                   'SkipSubtitlesIfAudioTrackMatches', 'EnableInternetProviders', 'EnablePhotos', 'EnableLUFSScan')
_JF_POLICY_KEYS = ('RemoteClientBitrateLimit', 'MaxActiveSessions', 'LoginAttemptsBeforeLockout', 'EnableMediaPlayback', 'EnableAudioPlaybackTranscoding',
                   'EnableVideoPlaybackTranscoding', 'EnablePlaybackRemuxing', 'EnableContentDownloading', 'EnableLiveTvAccess', 'EnableLiveTvManagement',
                   'EnableRemoteAccess', 'EnableSyncTranscoding', 'EnablePublicSharing')
_JF_ITEM_KEYS = ('Name', 'OriginalTitle', 'ForcedSortName', 'Overview', 'Taglines', 'Genres', 'Tags', 'OfficialRating', 'ProductionYear', 'CommunityRating',
                 'CriticRating', 'PremiereDate', 'DateCreated', 'Studios', 'LockData', 'LockedFields')


def _jf_coerce(cur, v):
    if isinstance(cur, bool):
        return v if isinstance(v, bool) else str(v).lower() in ('1', 'true', 'yes', 'on')
    if isinstance(cur, int):
        return int(v)
    if isinstance(cur, float):
        return float(v)
    if isinstance(cur, list):
        if not isinstance(v, list):
            v = [x.strip() for x in str(v).split(',') if x.strip()]
        return [x if isinstance(x, (dict, int, float, bool)) else str(x)[:200] for x in v][:200]
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    return str(v)[:300]


def _jf_merge(cur, vals, keys):
    cur = dict(cur or {})
    for k in keys:
        if k in vals:
            cur[k] = _jf_coerce(cur.get(k), vals[k])
    return cur


def _jf_arg(x, rx):
    x = str(x or '')
    if not rx.match(x):
        raise ValueError('Requête non autorisée.')
    return x


def _jf_qp(p, allowed):
    out = {}
    for k in allowed:
        if k in p and p[k] is not None and p[k] != '':
            v = p[k]
            out[k] = ('true' if v else 'false') if isinstance(v, bool) else str(v)[:300]
    return out


def _jf_uid():
    with _JF_LOCK:
        return _jf_sess.get('uid') or ''


def _jf_adm(b):
    import urllib.parse as _up
    op = str(b.get('op') or '')
    i = b.get('id')
    p = b.get('params') if isinstance(b.get('params'), dict) else {}
    body = b.get('body') if isinstance(b.get('body'), (dict, list)) else None
    vals = body if isinstance(body, dict) else {}

    def ok(d=None):
        return 200, {'ok': True, 'data': d}

    def timers():
        return ok({'timers': _jf_call('GET', '/LiveTv/Timers'), 'series': _jf_call('GET', '/LiveTv/SeriesTimers')})

    def livecfg():
        return ok(_jf_call('GET', '/System/Configuration/livetv'))

    # ── Serveur : réglages, transcodage, réseau, redémarrage ──
    for name, (path, keys) in _JF_CFG.items():
        if op == name + '_get':
            return ok(_jf_call('GET', path))
        if op == name + '_set':
            _jf_call('POST', path, None, _jf_merge(_jf_call('GET', path), vals, keys))
            return ok(_jf_call('GET', path))
    if op == 'restart':
        _jf_call('POST', '/System/Restart')
        return ok()
    # ── Tâches planifiées ──
    if op == 'tasks':
        return ok(_jf_call('GET', '/ScheduledTasks', {'isHidden': 'false'}))
    if op == 'task_run':
        _jf_call('POST', '/ScheduledTasks/Running/' + _jf_arg(i, _JF_TOK))
        return ok()
    if op == 'task_stop':
        _jf_call('DELETE', '/ScheduledTasks/Running/' + _jf_arg(i, _JF_TOK))
        return ok()
    if op == 'task_triggers':
        trg = []
        for t in (body if isinstance(body, list) else [])[:12]:
            t = t if isinstance(t, dict) else {}
            ty = str(t.get('Type') or '')
            if ty not in ('DailyTrigger', 'WeeklyTrigger', 'IntervalTrigger', 'StartupTrigger'):
                raise ValueError('Déclencheur invalide.')
            d = {'Type': ty}
            for k in ('TimeOfDayTicks', 'IntervalTicks', 'MaxRuntimeTicks'):
                if t.get(k) is not None:
                    d[k] = int(t[k])
            if t.get('DayOfWeek') is not None:
                d['DayOfWeek'] = str(t['DayOfWeek'])[:12]
            trg.append(d)
        _jf_call('POST', '/ScheduledTasks/%s/Triggers' % _jf_arg(i, _JF_TOK), None, trg)
        return ok()
    # ── Plugins ──
    if op == 'plugins':
        return ok(_jf_call('GET', '/Plugins'))
    if op in ('plugin_on', 'plugin_off', 'plugin_del'):
        pid, ver = _jf_arg(i, _JF_GUID), _jf_arg(b.get('version'), re.compile(r'^[0-9.]{1,32}$'))
        if op == 'plugin_del':
            _jf_call('DELETE', '/Plugins/%s/%s' % (pid, ver))
        else:
            _jf_call('POST', '/Plugins/%s/%s/%s' % (pid, ver, 'Enable' if op == 'plugin_on' else 'Disable'))
        return ok()
    if op == 'packages':
        return ok(_jf_call('GET', '/Packages', None, None, 8 * 1024 * 1024))
    if op == 'pkg_install':
        q = _jf_qp(p, ('assemblyGuid', 'version', 'repositoryUrl'))
        _jf_call('POST', '/Packages/Installed/' + _up.quote(_jf_arg(i, re.compile(r'^[A-Za-z0-9._\- ]{1,120}$')), safe=''), q)
        return ok()
    if op == 'repos':
        return ok(_jf_call('GET', '/Repositories'))
    if op == 'repos_set':
        reps = []
        for r in (body if isinstance(body, list) else [])[:20]:
            r = r if isinstance(r, dict) else {}
            u = str(r.get('Url') or '')
            if not re.match(r'^https://[^\s]{4,300}$', u):
                raise ValueError('Adresse de dépôt invalide (https:// attendu).')
            reps.append({'Name': str(r.get('Name') or u)[:100], 'Url': u, 'Enabled': bool(r.get('Enabled', True))})
        _jf_call('POST', '/Repositories', None, reps)
        return ok()
    # ── Clés d'API et Quick Connect ──
    if op == 'keys':
        return ok(_jf_call('GET', '/Auth/Keys'))
    if op == 'key_add':
        _jf_call('POST', '/Auth/Keys', {'app': _jf_arg(i, re.compile(r'^[^\x00-\x1f]{1,60}$'))})
        return ok(_jf_call('GET', '/Auth/Keys'))
    if op == 'key_del':
        _jf_call('DELETE', '/Auth/Keys/' + _jf_arg(i, _JF_TOK))
        return ok(_jf_call('GET', '/Auth/Keys'))
    if op == 'qc_state':
        return ok(_jf_call('GET', '/QuickConnect/Enabled'))
    if op == 'qc_auth':
        q = {'code': _jf_arg(i, re.compile(r'^[0-9]{4,10}$'))}
        uid = _jf_uid()
        if uid:
            q['userId'] = uid
        return ok(_jf_call('POST', '/QuickConnect/Authorize', q))
    # ── Sessions : message et télécommande ──
    if op == 'sess_msg':
        _jf_call('POST', '/Sessions/%s/Message' % _jf_hexid(i), None,
                 {'Header': str(vals.get('Header') or 'Desktroo')[:60], 'Text': str(vals.get('Text') or '')[:300], 'TimeoutMs': 8000})
        return ok()
    if op == 'sess_play':
        cmd = str(b.get('command') or '')
        if cmd not in ('Pause', 'Unpause', 'PlayPause', 'NextTrack', 'PreviousTrack'):
            raise ValueError('Commande invalide.')
        _jf_call('POST', '/Sessions/%s/Playing/%s' % (_jf_hexid(i), cmd))
        return ok()
    # ── Télévision en direct ──
    if op == 'lt_info':
        return ok(_jf_call('GET', '/LiveTv/Info'))
    if op == 'lt_cfg':
        return livecfg()
    if op == 'lt_discover':
        return ok(_jf_call('GET', '/LiveTv/Tuners/Discover', {'newDevicesOnly': 'false'}))
    if op == 'lt_host_add':
        ty = str(vals.get('Type') or '')
        if ty not in ('hdhomerun', 'm3u'):
            raise ValueError('Type de tuner inconnu.')
        u = str(vals.get('Url') or '').strip()
        if not re.match(r'^(https?://|/)[^\s]{2,400}$|^[A-Za-z0-9.\-]{3,120}(:\d+)?$', u):
            raise ValueError('Adresse du tuner invalide.')
        h = {'Type': ty, 'Url': u, 'FriendlyName': str(vals.get('FriendlyName') or '')[:80], 'ImportFavoritesOnly': bool(vals.get('ImportFavoritesOnly')),
             'AllowHWTranscoding': bool(vals.get('AllowHWTranscoding')), 'TunerCount': int(vals.get('TunerCount') or 0), 'IgnoreDts': True}
        if vals.get('Id'):
            h['Id'] = _jf_arg(vals.get('Id'), _JF_TOK)
        _jf_call('POST', '/LiveTv/TunerHosts', None, h)
        return livecfg()
    if op == 'lt_host_del':
        _jf_call('DELETE', '/LiveTv/TunerHosts', {'id': _jf_arg(i, _JF_TOK)})
        return livecfg()
    if op == 'lt_prov_add':
        ty = str(vals.get('Type') or 'xmltv')
        if ty not in ('xmltv', 'SchedulesDirect'):
            raise ValueError('Type de guide inconnu.')
        d = {'Type': ty, 'EnableAllTuners': True, 'EnabledTuners': [], 'NewsCategories': ['news'], 'SportsCategories': ['sports'], 'KidsCategories': ['kids'],
             'MovieCategories': ['movie']}
        if ty == 'xmltv':
            u = str(vals.get('Path') or '').strip()
            if not re.match(r'^(https?://|/)[^\s]{2,400}$', u):
                raise ValueError('Adresse du guide invalide (http:// ou chemin).')
            d['Path'] = u
        else:
            d.update({'Username': str(vals.get('Username') or '')[:80], 'Password': str(vals.get('Password') or '')[:80],
                      'ListingsId': str(vals.get('ListingsId') or '')[:80], 'Country': str(vals.get('Country') or '')[:3], 'ZipCode': str(vals.get('ZipCode') or '')[:12]})
        _jf_call('POST', '/LiveTv/ListingProviders', {'validateListings': 'false', 'validateLogin': 'false'}, d)
        return livecfg()
    if op == 'lt_prov_del':
        _jf_call('DELETE', '/LiveTv/ListingProviders', {'id': _jf_arg(i, _JF_TOK)})
        return livecfg()
    if op == 'lt_channels':
        q = _jf_qp(p, ('startIndex', 'limit', 'searchTerm', 'isFavorite'))
        q.setdefault('limit', '100')
        q.setdefault('addCurrentProgram', 'true')
        return ok(_jf_call('GET', '/LiveTv/Channels', q))
    if op == 'lt_programs':
        q = _jf_qp(p, ('startIndex', 'limit', 'isMovie', 'isSeries', 'isNews', 'isKids', 'isSports', 'searchTerm', 'hasAired'))
        q.setdefault('limit', '60')
        q['enableTotalRecordCount'] = 'true'
        q['sortBy'] = 'StartDate'
        return ok(_jf_call('GET', '/LiveTv/Programs', q))
    if op == 'lt_recs':
        return ok(_jf_call('GET', '/LiveTv/Recordings', {'limit': '100'}))
    if op == 'lt_timers':
        return timers()
    if op == 'lt_timer_new':
        d = _jf_call('GET', '/LiveTv/Timers/Defaults', {'programId': _jf_arg(i, _JF_TOK)})
        _jf_call('POST', '/LiveTv/SeriesTimers' if b.get('series') else '/LiveTv/Timers', None, d)
        return timers()
    if op in ('lt_timer_del', 'lt_stimer_del'):
        _jf_call('DELETE', ('/LiveTv/Timers/' if op == 'lt_timer_del' else '/LiveTv/SeriesTimers/') + _jf_arg(i, _JF_TOK))
        return timers()
    if op == 'lt_rec_del':
        _jf_call('DELETE', '/LiveTv/Recordings/' + _jf_arg(i, _JF_TOK))
        return ok()
    # ── Fiche d'un élément : métadonnées, images, sous-titres, suppression ──
    if op in ('item_get', 'item_save', 'item_delete', 'sub_search', 'sub_get', 'sub_del', 'img_search', 'img_set', 'coll_new', 'coll_add'):
        iid = _jf_hexid(i)
        if not _JF_ID.match(iid):
            raise ValueError('Élément inconnu.')
        if op == 'item_get':
            d = _jf_call('GET', '/Items/%s/MetadataEditor' % iid) or {}
            it = _jf_call('GET', '/Users/%s/Items/%s' % (_jf_uid(), iid)) or {}
            return ok({'item': {k: it.get(k) for k in _JF_ITEM_KEYS + ('Id', 'Type', 'Path')}, 'ratings': d.get('ParentalRatingOptions') or []})
        if op == 'item_save':
            it = _jf_call('GET', '/Users/%s/Items/%s' % (_jf_uid(), iid)) or {}
            for k in _JF_ITEM_KEYS:
                if k not in vals:
                    continue
                v = vals[k]
                if k in ('Genres', 'Tags', 'Taglines', 'LockedFields'):
                    v = [str(x).strip()[:100] for x in (v if isinstance(v, list) else str(v).split(','))]
                    v = [x for x in v if x][:50]
                elif k == 'ProductionYear':
                    v = int(v) if v not in (None, '') else None
                elif k in ('CommunityRating', 'CriticRating'):
                    v = float(v) if v not in (None, '') else None
                elif k == 'LockData':
                    v = bool(v)
                elif k == 'Studios':
                    v = [{'Name': str(x).strip()[:100]} for x in (v if isinstance(v, list) else str(v).split(',')) if str(x).strip()][:30]
                else:
                    v = str(v)[:8000] if v is not None else None
                it[k] = v
            if 'Genres' in vals:
                it['GenreItems'] = [{'Name': g} for g in it.get('Genres') or []]
            if 'Tags' in vals:
                it['TagItems'] = [{'Name': g} for g in it.get('Tags') or []]
            _jf_call('POST', '/Items/' + iid, None, it)
            return ok()
        if op == 'item_delete':
            if b.get('confirm') is not True:
                raise ValueError('Suppression non confirmée.')
            _jf_call('DELETE', '/Items/' + iid)
            return ok()
        if op == 'sub_search':
            return ok(_jf_call('GET', '/Items/%s/RemoteSearch/Subtitles/%s' % (iid, _jf_arg(b.get('lang'), _JF_LANG)), {'isPerfectMatch': 'false'}))
        if op == 'sub_get':
            sid = _jf_arg(b.get('sub'), re.compile(r'^[A-Za-z0-9_\-=.%~]{1,400}$'))
            _jf_call('POST', '/Items/%s/RemoteSearch/Subtitles/%s' % (iid, _up.quote(sid, safe='')))
            return ok()
        if op == 'sub_del':
            _jf_call('DELETE', '/Videos/%s/Subtitles/%d' % (iid, int(b.get('index'))))
            return ok()
        if op == 'img_search':
            return ok(_jf_call('GET', '/Items/%s/RemoteImages' % iid, {'type': 'Primary', 'limit': '12', 'includeAllLanguages': 'false'}))
        if op == 'img_set':
            u = str(b.get('url') or '')
            if not re.match(r'^https://[^\s]{4,500}$', u):
                raise ValueError('Adresse d’image invalide.')
            _jf_call('POST', '/Items/%s/RemoteImages/Download' % iid, {'type': 'Primary', 'imageUrl': u})
            return ok()
        if op == 'coll_new':
            return ok(_jf_call('POST', '/Collections', {'name': _jf_arg(b.get('name'), re.compile(r'^[^\x00-\x1f]{1,80}$')), 'ids': iid}))
        if op == 'coll_add':
            coll = _jf_hexid(b.get('coll'))
            if not _JF_ID.match(coll):
                raise ValueError('Collection inconnue.')
            _jf_call('POST', '/Collections/%s/Items' % coll, {'ids': iid})
            return ok()
    # ── Options d'une bibliothèque ──
    if op in ('lib_opts_get', 'lib_opts_set'):
        for f in (_jf_call('GET', '/Library/VirtualFolders') or []):
            if _jf_hexid(f.get('ItemId')) == _jf_hexid(i):
                o = f.get('LibraryOptions') or {}
                if op == 'lib_opts_get':
                    return ok({k: o.get(k) for k in _JF_LIBOPT_KEYS if k in o})
                _jf_call('POST', '/Library/VirtualFolders/LibraryOptions', None, {'Id': f.get('ItemId'), 'LibraryOptions': _jf_merge(o, vals, _JF_LIBOPT_KEYS)})
                return ok()
        raise ValueError('Bibliothèque introuvable.')
    # ── Droits fins d'un compte (hors administrateurs) ──
    if op in ('user_policy_get', 'user_policy_set'):
        uid = _jf_hexid(i)
        if not _JF_ID.match(uid):
            raise ValueError('Requête non autorisée.')
        pol = (_jf_call('GET', '/Users/' + uid) or {}).get('Policy') or {}
        if pol.get('IsAdministrator'):
            raise ValueError('Les comptes administrateurs se gèrent dans Jellyfin.')
        if op == 'user_policy_get':
            return ok({k: pol.get(k) for k in _JF_POLICY_KEYS})
        _jf_call('POST', '/Users/%s/Policy' % uid, None, _jf_merge(pol, vals, _JF_POLICY_KEYS))
        return ok()
    raise ValueError('Opération inconnue.')


# ── MDM-CAM-V1 : app Caméras (moteur Frigate) ──
# Desktroo garde la liste des caméras, en écrit la configuration Frigate et relaie images, événements et enregistrements :
# le navigateur ne parle jamais à Frigate (aucun port ouvert, tout passe par la session Desktroo).
STORE_CAM_ID = 'cameras'
_CAM_HOST, _CAM_PORT = 'dt-cameras', 5000
_CAM_BASE = 'http://%s:%d' % (_CAM_HOST, _CAM_PORT)
_CAM_NAME = re.compile(r'^[a-z0-9][a-z0-9_]{0,29}$')
_CAM_EVT = re.compile(r'^[0-9]{6,12}\.[0-9]{1,8}-[a-z0-9]{3,12}$')
_CAM_URL = re.compile(r'^(rtsp|rtsps|rtmp|http|https|tapo)://[^\s"\'\\]{4,400}$')
_CAM_LOCK = _threading.Lock()
_CAM_OBJ_ALLOWED = ('person', 'car', 'dog', 'cat', 'truck', 'bicycle', 'motorcycle', 'bird', 'package')
_CAM_OBJ_DEFAULT = ('person', 'car', 'dog', 'cat')
# sensibilité → (seuil de mouvement, surface minimale, score minimal, score de confirmation) ; seuil bas = plus sensible
_CAM_SENS = {'low': (45, 30, 0.6, 0.8), 'normal': (30, 10, 0.5, 0.7), 'high': (20, 8, 0.4, 0.6)}


def _cam_list():
    try:
        l = json.loads(str(_store_fields_get(STORE_CAM_ID).get('cams') or '[]'))
    except ValueError:
        l = []
    return [c for c in l if isinstance(c, dict) and _CAM_NAME.match(str(c.get('id') or ''))]


def _cam_save_list(l):
    f = _store_fields_get(STORE_CAM_ID)
    f['cams'] = json.dumps(l, ensure_ascii=False)
    import hashlib as _hl
    f['cfg'] = _hl.sha256(_cam_config(l).encode('utf-8')).hexdigest()[:16]   # configuration envoyée à Frigate
    _store_fields_set(STORE_CAM_ID, f)


def _cam_yaml_str(s):
    return json.dumps(str(s), ensure_ascii=False)   # JSON est du YAML valide : aucune injection possible


def _cam_sources(url):
    """Une caméra Tapo accepte, selon son micrologiciel, le mot de passe tel quel ou son empreinte (MD5, SHA-256) :
    go2rtc essaie les sources dans l'ordre jusqu'à ce que l'une marche."""
    import hashlib, urllib.parse as _up
    m = re.match(r'^tapo://([^@/]*)@([^/?]+)(.*)$', str(url))
    if not m:
        return [url]
    pw = _up.unquote(m.group(1))
    tail = '@%s%s' % (m.group(2), m.group(3))
    return [url] + ['tapo://admin:%s%s' % (h(pw.encode('utf-8')).hexdigest().upper(), tail) for h in (hashlib.md5, hashlib.sha256)]


def _cam_onvif(c):
    """(hôte, port, identifiant, mot de passe) ONVIF d'une caméra motorisée, ou None. Par défaut : hôte et identifiants de l'adresse RTSP."""
    if not c.get('ptz') or c.get('battery'):
        return None
    m = re.match(r'^[a-z0-9]+://(?:([^:/@]*)(?::([^/@]*))?@)?([^:/?#]+)', str(c.get('url') or ''))
    if not m:
        return None
    from urllib.parse import unquote
    user = c.get('onvif_user') or unquote(m.group(1) or '')
    pw = c.get('onvif_pass') or unquote(m.group(2) or '')
    try:
        port = int(c.get('onvif_port') or 80)
    except (TypeError, ValueError):
        port = 80
    return m.group(3), max(1, min(65535, port)), user, pw


_CAM_PTZ_CMD = re.compile(r'^(MOVE_LEFT|MOVE_RIGHT|MOVE_UP|MOVE_DOWN|ZOOM_IN|ZOOM_OUT|STOP|preset_[A-Za-z0-9 _.\-]{1,60})$')


def _cam_ws_send(topic, payload):
    """Envoie un message au moteur par son WebSocket interne (sans bibliothèque : poignée de main + une trame texte)."""
    import socket, os as _os, base64 as _b64
    s = socket.create_connection((_CAM_HOST, _CAM_PORT), 6)
    try:
        s.settimeout(6)
        key = _b64.b64encode(_os.urandom(16)).decode()
        s.sendall(('GET /ws HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n' % (_CAM_HOST, _CAM_PORT, key)).encode())
        buf = b''
        while b'\r\n\r\n' not in buf and len(buf) < 8192:
            ch = s.recv(1024)
            if not ch:
                break
            buf += ch
        if b' 101 ' not in buf.split(b'\r\n')[0] + b' ':
            raise RuntimeError('le moteur a refusé la connexion')
        data = json.dumps({'topic': topic, 'payload': payload}).encode('utf-8')
        mask = _os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, 'big'))
        s.sendall(head + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(data)))
        _login_time.sleep(0.15)
        try:
            s.sendall(bytes([0x88, 0x80]) + _os.urandom(4))
        except OSError:
            pass
    finally:
        s.close()


def _cam_config(cams):
    """Configuration Frigate complète d'après la liste des caméras de Desktroo."""
    o = ['mqtt:', '  enabled: false', 'detect:', '  enabled: false', 'snapshots:', '  enabled: true', '  bounding_box: true', 'go2rtc:']
    o.append('  streams:' if cams else '  streams: {}')
    for c in cams:
        o.append('    %s:' % c['id'])
        for src in _cam_sources(c['url']):
            o.append('      - %s' % _cam_yaml_str(src))
        o.append('    %s_mjpeg:' % c['id'])   # vidéo vue dans Desktroo : images JPEG tirées du flux de la caméra, à la demande
        o.append('      - %s' % _cam_yaml_str('ffmpeg:%s#video=mjpeg' % c['id']))
        if c.get('sub'):
            o.append('    %s_sub:' % c['id'])
            o.append('      - %s' % _cam_yaml_str(c['sub']))
    live = [c for c in cams if not c.get('battery')]
    o.append('cameras:' if live else 'cameras: {}')
    for c in cams:
        if c.get('battery'):   # caméra sur batterie : pas de lecture permanente (elle ne dormirait plus) ; image à la demande via go2rtc
            continue
        rec, days = c.get('record', 'motion'), max(1, min(365, int(c.get('days') or 7)))
        o += ['  %s:' % c['id'], '    enabled: %s' % ('true' if c.get('on', True) else 'false'), '    ffmpeg:', '      inputs:']
        main = '%s' % c['id']
        o += ['        - path: rtsp://127.0.0.1:8554/%s' % main, '          input_args: preset-rtsp-restream', '          roles:']
        if c.get('sub'):
            o += ['            - record', '        - path: rtsp://127.0.0.1:8554/%s_sub' % c['id'], '          input_args: preset-rtsp-restream', '          roles:', '            - detect']
        else:
            o += ['            - record', '            - detect']
        ov = _cam_onvif(c)
        if ov:
            o += ['    onvif:', '      host: %s' % _cam_yaml_str(ov[0]), '      port: %d' % ov[1], '      user: %s' % _cam_yaml_str(ov[2]), '      password: %s' % _cam_yaml_str(ov[3])]
        o += ['    detect:', '      enabled: %s' % ('true' if c.get('detect') else 'false')]
        o += ['    record:', '      enabled: %s' % ('true' if rec != 'off' else 'false')]
        if rec != 'off':
            o += ['      continuous:', '        days: %d' % (days if rec == 'continuous' else 0), '      motion:', '        days: %d' % days]
        objs = [x for x in (c.get('objects') or _CAM_OBJ_DEFAULT) if x in _CAM_OBJ_ALLOWED] or list(_CAM_OBJ_DEFAULT)
        sv = _CAM_SENS.get(c.get('sens'), _CAM_SENS['normal'])
        o += ['    motion:', '      threshold: %d' % sv[0], '      contour_area: %d' % sv[1],
              '    objects:', '      track:'] + ['        - %s' % x for x in objs] + ['      filters:']
        for x in objs:
            o += ['        %s:' % x, '          min_score: %s' % sv[2], '          threshold: %s' % sv[3]]
    return '\n'.join(o) + '\n'


def _cam_http(method, path, data=None, ctype=None, timeout=20, limit=8 * 1024 * 1024):
    import urllib.request, urllib.error
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(_CAM_BASE + path, data=data, method=method, headers={'Content-Type': ctype} if ctype else {})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.headers.get('Content-Type', ''), r.read(limit)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.headers.get('Content-Type', ''), e.read(65536)
        except Exception:
            return e.code, '', b''
    except Exception as e:
        raise RuntimeError(str(e)[:120])


def _cam_json(path, **kw):
    st, _ct, raw = _cam_http('GET', path, **kw)
    if st != 200:
        raise RuntimeError('HTTP %d' % st)
    return json.loads(raw.decode('utf-8', 'replace'))


def _cam_push(cams, restart=True):
    """Écrit la configuration dans Frigate (qui la valide) ; ValueError avec son message si elle est refusée."""
    txt = _cam_config(cams)
    st, _ct, raw = _cam_http('POST', '/api/config/save?save_option=' + ('restart' if restart else 'saveonly'), txt.encode('utf-8'), 'text/plain', timeout=60)
    if st != 200:
        msg = raw.decode('utf-8', 'replace')
        try:
            d = json.loads(msg)
            msg = str(d.get('message') or d.get('detail') or msg)
        except ValueError:
            pass
        raise ValueError('Frigate refuse cette configuration : ' + msg[:300])


def _cam_mask(u):
    return re.sub(r'(://)[^/@]*@', r'\1***@', str(u))


def _cam_public(c):
    return {'id': c['id'], 'name': c.get('name') or c['id'], 'url': _cam_mask(c['url']), 'sub': _cam_mask(c.get('sub') or ''), 'record': c.get('record', 'motion'),
            'detect': bool(c.get('detect')), 'days': int(c.get('days') or 7), 'on': c.get('on', True) is not False, 'battery': bool(c.get('battery')),
            'objects': [x for x in (c.get('objects') or _CAM_OBJ_DEFAULT) if x in _CAM_OBJ_ALLOWED], 'sens': c.get('sens') if c.get('sens') in _CAM_SENS else 'normal', 'sms': bool(c.get('sms')),
            'ptz': bool(c.get('ptz')), 'onvif_port': int(c.get('onvif_port') or 80), 'onvif_user': c.get('onvif_user') or '', 'onvif_pw': bool(c.get('onvif_pass'))}


_CAM_SCAN_PY = r"""
import socket, json, sys, concurrent.futures as cf
base = sys.argv[1]
def chk(a):
    ip, p = a
    try:
        s = socket.create_connection((ip, p), 0.7)
    except Exception:
        return None
    ban = ''
    if p in (554, 8554):
        try:
            s.settimeout(1.2)
            s.sendall(('OPTIONS rtsp://%s:%d/ RTSP/1.0\r\nCSeq: 1\r\n\r\n' % (ip, p)).encode())
            d = s.recv(600).decode('latin1')
            ban = ' '.join(l.strip() for l in d.split('\n') if l.lower().startswith('server'))[:60]
        except Exception:
            pass
    s.close()
    return (ip, p, ban)
jobs = [('%s.%d' % (base, i), p) for i in range(1, 255) for p in (554, 8554, 8800, 2020)]
out = {}
with cf.ThreadPoolExecutor(120) as ex:
    for r in ex.map(chk, jobs):
        if r:
            d = out.setdefault(r[0], {'ip': r[0], 'ports': [], 'banner': ''})
            d['ports'].append(r[1])
            d['banner'] = d['banner'] or r[2]
print(json.dumps(list(out.values())))
"""


def _cam_scan():
    """Caméras du réseau local : machines qui répondent sur les ports RTSP (554, 8554) ou Tapo (8800, 2020)."""
    ip = _store_bind_ip()
    m = re.match(r'^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$', ip)
    if not m:
        raise ValueError('Adresse du NAS sur le réseau local introuvable.')
    out, err, code = _px_host('python3 -c ' + shq(_CAM_SCAN_PY) + ' ' + shq(m.group(1)), timeout=90)
    if code != 0:
        raise ValueError('Recherche impossible sur le NAS : ' + str(err or out)[:120])
    try:
        found = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ValueError('Résultat de la recherche illisible.')
    known = ' '.join(str(c.get('url') or '') for c in _cam_list())
    res = []
    for d in found:
        if d['ip'] == ip or not re.match(r'^[0-9.]{7,15}$', str(d['ip'])):
            continue
        ports = sorted(int(p) for p in d.get('ports') or [])
        kind = 'tapo' if (8800 in ports or 2020 in ports) else 'rtsp'
        res.append({'ip': d['ip'], 'ports': ports, 'kind': kind, 'banner': str(d.get('banner') or '')[:60],
                    'known': ('@%s' % d['ip']) in known or ('//%s' % d['ip']) in known})
    res.sort(key=lambda x: tuple(int(n) for n in x['ip'].split('.')))
    return res


_CAM_SEEN = []


def _cam_alert_tick(state):
    cams = [c for c in _cam_list() if c.get('sms') and c.get('on', True) is not False and not c.get('battery')]
    if not cams or STORE_CAM_ID not in store_installed_get():
        state['ts'] = _login_time.time()
        return
    hook = store_sms_hook()
    if not hook.get('ok'):
        return
    import urllib.request as _ur
    q = 'limit=30&include_thumbnails=0&labels=person&cameras=%s&after=%.1f' % (','.join(c['id'] for c in cams), state['ts'] - 5)
    ev = _cam_json('/api/events?' + q)
    names = {c['id']: c.get('name') or c['id'] for c in cams}
    now = _login_time.time()
    for e in sorted([e for e in (ev if isinstance(ev, list) else []) if isinstance(e, dict)], key=lambda e: e.get('start_time') or 0):
        eid = str(e.get('id') or '')
        if not _CAM_EVT.match(eid) or eid in _CAM_SEEN:
            continue
        _CAM_SEEN.append(eid)
        del _CAM_SEEN[:-300]
        cam = str(e.get('camera') or '')
        if now - state['sent'].get(cam, 0) < 300:   # une alerte par caméra et par 5 minutes
            continue
        state['sent'][cam] = now
        hm = _login_time.strftime('%H:%M', _login_time.localtime(float(e.get('start_time') or now)))
        msg = 'Caméra %s : personne détectée à %s' % (names.get(cam, cam), hm)
        req = _ur.Request(hook['url'], data=json.dumps({'text': msg}).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
        _ur.urlopen(req, timeout=20).read()
    state['ts'] = now


def _cam_alert_loop():
    _login_time.sleep(120)
    state = {'ts': _login_time.time(), 'sent': {}}
    while True:
        try:
            _cam_alert_tick(state)
        except Exception:
            pass
        _login_time.sleep(20)


def store_cameras_api(body):
    """{ action: state | cam_save | cam_del | cam_test | events | event_del } → { ok, ... }."""
    if STORE_CAM_ID not in store_installed_get():
        return 409, {'ok': False, 'error': 'L’app Caméras n’est pas installée.'}
    b = body if isinstance(body, dict) else {}
    action = str(b.get('action') or '')
    try:
        if action == 'state':
            cams = _cam_list()
            out = {'ok': True, 'cams': [_cam_public(c) for c in cams], 'up': False}
            try:
                out['version'] = _cam_http('GET', '/api/version', timeout=4)[2].decode('utf-8', 'replace').strip()[:30]
                out['up'] = True
                st = _cam_json('/api/stats', timeout=6)
                out['stats'] = {k: {'fps': v.get('camera_fps'), 'det': v.get('detection_fps'), 'pid': v.get('pid')} for k, v in (st.get('cameras') or {}).items()}
                out['cpu_detect'] = bool(st.get('detectors'))
                try:   # la configuration est refaite quand une mise à jour de Desktroo change sa forme
                    import hashlib as _hl
                    h = _hl.sha256(_cam_config(cams).encode('utf-8')).hexdigest()[:16]
                    f = _store_fields_get(STORE_CAM_ID)
                    if cams and f.get('cfg') != h and _CAM_LOCK.acquire(False):
                        try:
                            _cam_push(cams)
                            f['cfg'] = h
                            _store_fields_set(STORE_CAM_ID, f)
                            out['up'] = False
                        finally:
                            _CAM_LOCK.release()
                except (ValueError, RuntimeError):
                    pass
            except RuntimeError:
                pass
            return 200, out
        if action == 'cam_scan':
            return 200, {'ok': True, 'found': _cam_scan()}
        if action in ('ptz', 'ptz_info'):
            cid = str(b.get('id') or '')
            cam = [c for c in _cam_list() if c['id'] == cid and c.get('ptz') and not c.get('battery')]
            if not cam:
                return 404, {'ok': False, 'error': 'Cette caméra n’est pas réglée comme motorisée.'}
            if action == 'ptz_info':
                try:
                    return 200, {'ok': True, 'info': _cam_json('/api/%s/ptz/info' % cid, timeout=15)}
                except RuntimeError as e:
                    return 200, {'ok': False, 'error': 'Le moteur n’a pas pu interroger la caméra en ONVIF (%s). Vérifiez le port ONVIF, l’identifiant et le mot de passe.' % e}
            cmd = str(b.get('cmd') or '')
            if not _CAM_PTZ_CMD.match(cmd):
                return 400, {'ok': False, 'error': 'Commande inconnue.'}
            _cam_ws_send('%s/ptz' % cid, cmd)
            return 200, {'ok': True}
        if action == 'cam_save':
            name = str(b.get('name') or '').strip()[:40]
            url = str(b.get('url') or '').strip()
            sub = str(b.get('sub') or '').strip()
            if not name:
                return 400, {'ok': False, 'error': 'Donnez un nom à la caméra.'}
            if not _CAM_URL.match(url) or (sub and not _CAM_URL.match(sub)):
                return 400, {'ok': False, 'error': 'Adresse invalide : rtsp://identifiant:mot-de-passe@adresse:554/flux'}
            rec = str(b.get('record') or 'motion')
            if rec not in ('off', 'motion', 'continuous'):
                rec = 'motion'
            with _CAM_LOCK:
                cams = _cam_list()
                cid = str(b.get('id') or '')
                if cid:
                    cur = [c for c in cams if c['id'] == cid]
                    if not cur:
                        return 404, {'ok': False, 'error': 'Caméra inconnue.'}
                    cur = cur[0]
                    mm = re.match(r'^[a-z0-9]+://([^/@]*)@', cur['url'])
                    if '***@' in url and mm:   # adresse masquée renvoyée telle quelle : on garde l'identifiant et le mot de passe enregistrés
                        url = url.replace('***@', mm.group(1) + '@', 1)
                    if '***@' in sub:
                        sub = cur.get('sub') or ''
                else:
                    base = re.sub(r'[^a-z0-9]+', '_', name.lower().encode('ascii', 'ignore').decode()).strip('_')[:24] or 'camera'
                    cid, n = base, 2
                    while any(c['id'] == cid for c in cams):
                        cid = '%s_%d' % (base, n)
                        n += 1
                    cur = None
                new = {'id': cid, 'name': name, 'url': url, 'sub': sub, 'record': rec, 'detect': bool(b.get('detect')),
                       'days': max(1, min(365, int(b.get('days') or 7))), 'on': b.get('on', True) is not False,
                       'battery': bool(b.get('battery')) and url.startswith('tapo://'),
                       'objects': [x for x in (b.get('objects') if isinstance(b.get('objects'), list) else _CAM_OBJ_DEFAULT) if x in _CAM_OBJ_ALLOWED] or list(_CAM_OBJ_DEFAULT),
                       'sens': b.get('sens') if b.get('sens') in _CAM_SENS else 'normal', 'sms': bool(b.get('sms')) and not url.startswith('tapo://'),
                       'ptz': bool(b.get('ptz')) and not url.startswith('tapo://'), 'onvif_port': max(1, min(65535, int(b.get('onvif_port') or 80))),
                       'onvif_user': str(b.get('onvif_user') or '').strip()[:60],
                       'onvif_pass': str(b.get('onvif_pass') or '')[:100] or ((cur or {}).get('onvif_pass') or '')}
                nl = [new if c['id'] == cid else c for c in cams] + ([] if cur else [new])
                _cam_push(nl)
                _cam_save_list(nl)
            return 200, {'ok': True, 'cams': [_cam_public(c) for c in nl], 'id': cid}
        if action == 'cam_del':
            cid = str(b.get('id') or '')
            with _CAM_LOCK:
                nl = [c for c in _cam_list() if c['id'] != cid]
                _cam_push(nl)
                _cam_save_list(nl)
            return 200, {'ok': True, 'cams': [_cam_public(c) for c in nl]}
        if action == 'cam_test':
            url = str(b.get('url') or '').strip()
            if '***@' in url:
                c = [c for c in _cam_list() if c['id'] == str(b.get('id') or '')]
                url = c[0]['url'] if c else ''
            if not _CAM_URL.match(url):
                return 400, {'ok': False, 'error': 'Adresse invalide.'}
            if url.startswith('tapo://'):
                return 200, {'ok': True, 'works': None, 'detail': 'Les caméras Tapo se testent après l’enregistrement, avec « Voir ».'}
            import urllib.parse as _up
            r = _cam_json('/api/ffprobe?paths=' + _up.quote(url, safe=''), timeout=25)
            r = r[0] if isinstance(r, list) and r else r
            if not isinstance(r, dict) or r.get('return_code') not in (0, '0'):
                err = str((r or {}).get('stderr') or '')[-160:] if isinstance(r, dict) else ''
                return 200, {'ok': True, 'works': False, 'detail': err or 'La caméra ne répond pas.'}
            vid = ''
            try:
                st = json.loads(r.get('stdout') or '{}').get('streams') or []
                v = [s for s in st if s.get('codec_type') == 'video']
                if v:
                    vid = '%s %sx%s' % (v[0].get('codec_name', ''), v[0].get('width', '?'), v[0].get('height', '?'))
            except ValueError:
                pass
            return 200, {'ok': True, 'works': True, 'detail': vid}
        if action == 'events':
            q = ['limit=%d' % max(1, min(100, int(b.get('limit') or 40))), 'include_thumbnails=0']
            cam = str(b.get('camera') or '')
            if cam:
                if not _CAM_NAME.match(cam):
                    return 400, {'ok': False, 'error': 'Caméra invalide.'}
                q.append('cameras=' + cam)
            lab = str(b.get('label') or '')
            if lab:
                if not re.match(r'^[a-z_]{2,20}$', lab):
                    return 400, {'ok': False, 'error': 'Objet invalide.'}
                q.append('labels=' + lab)
            names = {c['id']: c.get('name') or c['id'] for c in _cam_list()}
            ev = _cam_json('/api/events?' + '&'.join(q), timeout=15)
            return 200, {'ok': True, 'events': [{'id': e.get('id'), 'cam': e.get('camera'), 'camName': names.get(e.get('camera'), e.get('camera')),
                                                 'label': e.get('label'), 'start': e.get('start_time'), 'end': e.get('end_time'),
                                                 'clip': bool(e.get('has_clip')), 'snap': bool(e.get('has_snapshot')),
                                                 'score': (e.get('data') or {}).get('top_score')} for e in (ev if isinstance(ev, list) else []) if _CAM_EVT.match(str(e.get('id') or ''))]}
        if action == 'event_del':
            eid = str(b.get('id') or '')
            if not _CAM_EVT.match(eid):
                return 400, {'ok': False, 'error': 'Événement invalide.'}
            st, _ct, _raw = _cam_http('DELETE', '/api/events/' + eid)
            return (200, {'ok': True}) if st == 200 else (502, {'ok': False, 'error': 'HTTP %d' % st})
        return 400, {'ok': False, 'error': 'Action inconnue.'}
    except ValueError as e:
        return 400, {'ok': False, 'error': str(e)[:300]}
    except RuntimeError as e:
        return 502, {'ok': False, 'error': 'Frigate ne répond pas (%s). S’il vient de redémarrer, patientez une minute.' % str(e)[:80]}


def store_cameras_media(handler, qs):
    """Image d’une caméra, vignette, instantané ou enregistrement d’un événement (avec lecture par morceaux, pour avancer dans la vidéo)."""
    import http.client
    if STORE_CAM_ID not in store_installed_get():
        handler.send_error(404)
        return
    k, c, i = qs.get('k', ''), qs.get('c', ''), qs.get('i', '')
    port = _CAM_PORT
    if k == 'live' and _CAM_NAME.match(c):
        if [x for x in _cam_list() if x['id'] == c and x.get('battery')]:   # sans Frigate : go2rtc réveille la caméra le temps d'une image
            path, port = '/api/frame.jpeg?src=%s' % c, 1984
        else:
            path = '/api/%s/latest.jpg?h=%d' % (c, 720 if qs.get('s') == 'big' else 360)
    elif k == 'mjpeg' and _CAM_NAME.match(c):   # vidéo : flux d'images JPEG fabriqué par go2rtc (la caméra n'est réveillée que pendant la lecture)
        path, port = '/api/stream.mjpeg?src=%s_mjpeg' % c, 1984
    elif k in ('thumb', 'snap', 'clip') and _CAM_EVT.match(i):
        path = '/api/events/%s/%s' % (i, {'thumb': 'thumbnail.jpg', 'snap': 'snapshot.jpg', 'clip': 'clip.mp4'}[k])
    else:
        handler.send_error(400)
        return
    hd = {}
    if handler.headers.get('Range') and re.match(r'^bytes=\d*-\d*$', handler.headers.get('Range')):
        hd['Range'] = handler.headers.get('Range')
    try:
        cn = http.client.HTTPConnection(_CAM_HOST, port, timeout=45)
        cn.request('GET', path, headers=hd)
        r = cn.getresponse()
    except Exception:
        handler.send_error(502)
        return
    try:
        ct = r.getheader('Content-Type', 'application/octet-stream')
        if r.status not in (200, 206) or not (ct.startswith('image/') or ct.startswith('video/') or ct.startswith('multipart/')):
            msg = re.sub(r'\s+', ' ', r.read(500).decode('utf-8', 'replace')).strip()[:240] or ('HTTP %d' % r.status)
            data = ('Le moteur répond : ' + msg + (' [' + ct.split(';')[0] + ']' if ct else '')).encode('utf-8')
            handler.send_response(502)
            handler.send_header('Content-Type', 'text/plain; charset=utf-8')
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return
        handler.send_response(r.status)
        handler.send_header('Content-Type', ct.split(';')[0])
        for h in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
            if r.getheader(h):
                handler.send_header(h, r.getheader(h))
        if k == 'mjpeg':
            handler.send_header('Connection', 'close')
            handler.close_connection = True
        handler.send_header('Cache-Control', 'no-store' if k in ('live', 'mjpeg') else 'private, max-age=3600')
        handler.end_headers()
        while True:
            chunk = r.read1(65536) if k == 'mjpeg' else r.read(65536)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        cn.close()



# ══ Navidrome : musique en streaming (API Subsonic), fenêtre « Musique » ══
STORE_ND_ID = 'navidrome'
_ND_BASE = 'http://dt-navidrome:8080'
_ND_LOCK = _threading.Lock()
_ND_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


def _nd_fields():
    return _store_fields_get(STORE_ND_ID)


def _nd_user():
    return str(_nd_fields().get('user') or '') or 'admin'


def _nd_password():
    return str(_nd_fields().get('password') or '') or _store_derive(STORE_ND_ID, 'navidrome')


def _nd_auth(user=None, pw=None):
    import hashlib as _hl
    salt = os.urandom(6).hex()
    pw = _nd_password() if pw is None else pw
    return {'u': user or _nd_user(), 't': _hl.md5((pw + salt).encode('utf-8')).hexdigest(), 's': salt, 'v': '1.16.1', 'c': 'desktroo', 'f': 'json'}


def _nd_qs(endpoint, params=None, user=None, pw=None):
    import urllib.parse
    q = _nd_auth(user, pw)
    for k, v in (params or {}).items():
        if not isinstance(v, (list, tuple)):
            q[k] = v
    parts = [urllib.parse.urlencode(q)]
    for k, v in (params or {}).items():
        if isinstance(v, (list, tuple)):
            parts += [urllib.parse.urlencode({k: x}) for x in v]
    return '/rest/' + endpoint + '?' + '&'.join(parts)


def _nd_call(endpoint, params=None, user=None, pw=None, timeout=30):
    """Réponse Subsonic (dict) ; ValueError('auth') si le compte est refusé, RuntimeError si Navidrome ne répond pas."""
    st, _ct, raw = _pp_http('GET', _nd_qs(endpoint, params, user, pw), timeout=timeout, base=_ND_BASE, limit=16 * 1024 * 1024)
    if st != 200:
        raise RuntimeError('HTTP %d' % st)
    try:
        d = (json.loads(raw.decode('utf-8', 'replace')) or {}).get('subsonic-response') or {}
    except ValueError:
        raise RuntimeError('réponse illisible')
    if d.get('status') != 'ok':
        e = d.get('error') or {}
        if e.get('code') in (40, 41, 44):
            raise ValueError('auth')
        raise RuntimeError(str(e.get('message') or 'erreur')[:160])
    return d


def _nd_setup():
    """Premier démarrage : crée le compte « admin » si Navidrome n'a encore aucun compte. Sans effet sinon."""
    with _ND_LOCK:
        try:
            _nd_call('ping', timeout=8)
            return False
        except ValueError:
            pass
        st, _ct, _raw = _pp_http('POST', '/auth/createAdmin', {'username': 'admin', 'password': _store_derive(STORE_ND_ID, 'navidrome')}, base=_ND_BASE, timeout=15)
        if st in (200, 201):
            log.info('Magasin : Navidrome configuré (compte administrateur créé)')
            return True
        return False


def _nd_cover(d):
    c = str(d.get('coverArt') or '')
    return c if _ND_ID.match(c) else ''


def _nd_song(x):
    return {'id': str(x.get('id') or ''), 'title': str(x.get('title') or '')[:200], 'artist': str(x.get('artist') or '')[:120], 'album': str(x.get('album') or '')[:160],
            'albumId': str(x.get('albumId') or ''), 'artistId': str(x.get('artistId') or ''), 'dur': int(x.get('duration') or 0), 'track': int(x.get('track') or 0),
            'disc': int(x.get('discNumber') or 0), 'year': int(x.get('year') or 0), 'cover': _nd_cover(x), 'fmt': str(x.get('suffix') or '')[:8], 'br': int(x.get('bitRate') or 0),
            'star': bool(x.get('starred'))}


def _nd_album(x):
    return {'id': str(x.get('id') or ''), 'name': str(x.get('name') or x.get('title') or '')[:160], 'artist': str(x.get('artist') or '')[:120], 'artistId': str(x.get('artistId') or ''),
            'year': int(x.get('year') or 0), 'cover': _nd_cover(x), 'n': int(x.get('songCount') or 0), 'dur': int(x.get('duration') or 0), 'star': bool(x.get('starred'))}


def _nd_lst(v):
    return v if isinstance(v, list) else ([v] if isinstance(v, dict) else [])


def _nd_ticket(sid, ttl=8 * 3600):
    raw = json.dumps({'i': sid, 'e': int(_px_time.time()) + int(ttl)}).encode('utf-8')
    sig = _px_hmac.new(_PX_MEDIA_KEY, b'nd' + raw, _px_hash.sha256).digest()[:16]
    return _px_b64.urlsafe_b64encode(sig + raw).decode('ascii').rstrip('=')


def _nd_ticket_parse(t):
    try:
        blob = _px_b64.urlsafe_b64decode(str(t) + '=' * (-len(str(t)) % 4))
        sig, raw = blob[:16], blob[16:]
        if not _px_hmac.compare_digest(sig, _px_hmac.new(_PX_MEDIA_KEY, b'nd' + raw, _px_hash.sha256).digest()[:16]):
            return None
        d = json.loads(raw.decode('utf-8'))
        sid = str(d['i'])
        if int(d['e']) < _px_time.time() or not _ND_ID.match(sid):
            return None
        return sid
    except Exception:
        return None


def store_navidrome_img(cid, size=''):
    cid = str(cid or '')
    if STORE_ND_ID not in store_installed_get() or not _ND_ID.match(cid):
        return 404, 'text/plain', b''
    try:
        st, ct, raw = _pp_http('GET', _nd_qs('getCoverArt', {'id': cid, 'size': 600 if size == 'big' else 300}), base=_ND_BASE, limit=6 * 1024 * 1024,
                               headers={'Accept': 'image/*'})
    except RuntimeError:
        return 502, 'text/plain', b''
    if st != 200 or not str(ct).lower().startswith('image/'):
        return 404, 'text/plain', b''
    return 200, str(ct).split(';')[0], raw


def _nd_stream_serve(handler, ticket):
    """Relaie le fichier audio (Range compris, pour avancer dans un morceau) vers le navigateur."""
    import http.client
    sid = _nd_ticket_parse(ticket)
    if not sid or STORE_ND_ID not in store_installed_get():
        handler.send_response(404); handler.send_header('Content-Length', '0'); handler.end_headers(); return
    conn = None
    try:
        host, port = _ND_BASE.split('//', 1)[1].split(':')
        hd = {}
        if handler.headers.get('Range'):
            hd['Range'] = handler.headers.get('Range')
        conn = http.client.HTTPConnection(host, int(port), timeout=60)
        conn.request('GET', _nd_qs('stream', {'id': sid, 'format': 'raw'}), headers=hd)
        r = conn.getresponse()
        if r.status not in (200, 206):
            handler.send_response(404); handler.send_header('Content-Length', '0'); handler.end_headers(); return
        handler.send_response(r.status)
        for k in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
            if r.getheader(k):
                handler.send_header(k, r.getheader(k))
        if not r.getheader('Content-Length'):
            handler.send_header('Connection', 'close')
            handler.close_connection = True
        handler.send_header('Cache-Control', 'private, no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.end_headers()
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as e:
        log.warning('lecture Navidrome : %s', e)
        try:
            handler.send_response(502); handler.send_header('Content-Length', '0'); handler.end_headers()
        except Exception:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass


def store_navidrome_api(body):
    """{ action: state | save_password | clear_password | overview | artists | artist | albums | album | search | starred | star | unstar |
    playlists | playlist | playlist_new | playlist_add | playlist_remove | playlist_del | scan | play } → { ok, ... }."""
    if STORE_ND_ID not in store_installed_get():
        return 409, {'ok': False, 'error': 'Navidrome n’est pas installé.'}
    b = body if isinstance(body, dict) else {}
    action = str(b.get('action') or '')
    fields = _nd_fields()

    def ident(k):
        v = str(b.get(k) or '')
        return v if _ND_ID.match(v) else ''

    def idlist(k):
        return [x for x in (b.get(k) or []) if isinstance(x, str) and _ND_ID.match(x)][:500]
    try:
        if action == 'state':
            _nd_setup()
            try:
                _nd_call('ping', timeout=10)
                return 200, {'ok': True, 'denied': False, 'custom': bool(fields.get('password')), 'user': _nd_user()}
            except ValueError:
                return 200, {'ok': True, 'denied': True, 'custom': bool(fields.get('password')), 'user': _nd_user()}
        if action == 'clear_password':
            fields.pop('password', None)
            fields.pop('user', None)
            _store_fields_set(STORE_ND_ID, fields or None)
            return 200, {'ok': True}
        if action == 'save_password':
            pw = str(b.get('password') or '')
            user = str(b.get('user') or '').strip() or 'admin'
            if not re.match(r'^[^\x00-\x1f]{1,40}$', user):
                return 400, {'ok': False, 'error': 'Nom de compte invalide.'}
            if not (8 <= len(pw) <= 72):
                return 400, {'ok': False, 'error': 'Le mot de passe doit faire de 8 à 72 caractères.'}
            try:
                _nd_call('ping', user=user, pw=pw, timeout=10)
            except ValueError:
                return 400, {'ok': False, 'error': 'Navidrome refuse ce compte ou ce mot de passe.'}
            fields['password'] = pw
            if user != 'admin':
                fields['user'] = user
            else:
                fields.pop('user', None)
            _store_fields_set(STORE_ND_ID, fields)
            return 200, {'ok': True}
        _nd_setup()
        if action == 'overview':
            out = {'ok': True}
            for key in ('recent', 'newest', 'frequent', 'random'):
                try:
                    out[key] = [_nd_album(x) for x in _nd_lst((_nd_call('getAlbumList2', {'type': key, 'size': 18}).get('albumList2') or {}).get('album'))]
                except RuntimeError:
                    out[key] = []
            try:
                sc = _nd_call('getScanStatus').get('scanStatus') or {}
                out['scan'] = {'on': bool(sc.get('scanning')), 'count': int(sc.get('count') or 0), 'folders': int(sc.get('folderCount') or 0)}
            except RuntimeError:
                out['scan'] = {'on': False, 'count': 0, 'folders': 0}
            return 200, out
        if action == 'artists':
            rows = []
            for g in _nd_lst((_nd_call('getArtists').get('artists') or {}).get('index')):
                for a in _nd_lst(g.get('artist')):
                    rows.append({'id': str(a.get('id') or ''), 'name': str(a.get('name') or '')[:120], 'n': int(a.get('albumCount') or 0), 'cover': _nd_cover(a)})
            return 200, {'ok': True, 'artists': rows}
        if action == 'artist':
            i = ident('id')
            if not i:
                return 400, {'ok': False, 'error': 'Artiste inconnu.'}
            a = _nd_call('getArtist', {'id': i}).get('artist') or {}
            return 200, {'ok': True, 'name': str(a.get('name') or ''), 'albums': [_nd_album(x) for x in _nd_lst(a.get('album'))]}
        if action == 'albums':
            typ = str(b.get('type') or 'alphabeticalByName')
            if typ not in ('alphabeticalByName', 'alphabeticalByArtist', 'newest', 'recent', 'frequent', 'random', 'starred'):
                typ = 'alphabeticalByName'
            try:
                off = max(0, min(100000, int(b.get('offset') or 0)))
            except (TypeError, ValueError):
                off = 0
            rows = [_nd_album(x) for x in _nd_lst((_nd_call('getAlbumList2', {'type': typ, 'size': 60, 'offset': off}).get('albumList2') or {}).get('album'))]
            return 200, {'ok': True, 'albums': rows, 'more': len(rows) >= 60}
        if action == 'album':
            i = ident('id')
            if not i:
                return 400, {'ok': False, 'error': 'Album inconnu.'}
            a = _nd_call('getAlbum', {'id': i}).get('album') or {}
            return 200, {'ok': True, 'album': _nd_album(a), 'songs': [_nd_song(x) for x in _nd_lst(a.get('song'))]}
        if action == 'search':
            q = str(b.get('q') or '').strip()[:80]
            if not q:
                return 200, {'ok': True, 'artists': [], 'albums': [], 'songs': []}
            r = _nd_call('search3', {'query': q, 'artistCount': 8, 'albumCount': 16, 'songCount': 30}).get('searchResult3') or {}
            return 200, {'ok': True,
                         'artists': [{'id': str(x.get('id') or ''), 'name': str(x.get('name') or '')[:120], 'n': int(x.get('albumCount') or 0), 'cover': _nd_cover(x)} for x in _nd_lst(r.get('artist'))],
                         'albums': [_nd_album(x) for x in _nd_lst(r.get('album'))], 'songs': [_nd_song(x) for x in _nd_lst(r.get('song'))]}
        if action == 'starred':
            r = _nd_call('getStarred2').get('starred2') or {}
            return 200, {'ok': True, 'albums': [_nd_album(x) for x in _nd_lst(r.get('album'))], 'songs': [_nd_song(x) for x in _nd_lst(r.get('song'))]}
        if action in ('star', 'unstar'):
            i = ident('id')
            if not i:
                return 400, {'ok': False, 'error': 'Élément inconnu.'}
            _nd_call(action, {'id': i})
            return 200, {'ok': True}
        if action == 'playlists':
            rows = [{'id': str(x.get('id') or ''), 'name': str(x.get('name') or '')[:120], 'n': int(x.get('songCount') or 0), 'dur': int(x.get('duration') or 0), 'cover': _nd_cover(x)}
                    for x in _nd_lst((_nd_call('getPlaylists').get('playlists') or {}).get('playlist'))]
            return 200, {'ok': True, 'playlists': rows}
        if action == 'playlist':
            i = ident('id')
            if not i:
                return 400, {'ok': False, 'error': 'Liste inconnue.'}
            p = _nd_call('getPlaylist', {'id': i}).get('playlist') or {}
            return 200, {'ok': True, 'name': str(p.get('name') or ''), 'songs': [_nd_song(x) for x in _nd_lst(p.get('entry'))]}
        if action == 'playlist_new':
            name = str(b.get('name') or '').strip()[:80]
            if not name:
                return 400, {'ok': False, 'error': 'Donnez un nom à la liste.'}
            r = _nd_call('createPlaylist', {'name': name, 'songId': idlist('songs')})
            return 200, {'ok': True, 'id': str((r.get('playlist') or {}).get('id') or '')}
        if action == 'playlist_add':
            i, sids = ident('id'), idlist('songs')
            if not i or not sids:
                return 400, {'ok': False, 'error': 'Rien à ajouter.'}
            _nd_call('updatePlaylist', {'playlistId': i, 'songIdToAdd': sids})
            return 200, {'ok': True}
        if action == 'playlist_remove':
            i = ident('id')
            try:
                idx = int(b.get('index'))
            except (TypeError, ValueError):
                idx = -1
            if not i or idx < 0:
                return 400, {'ok': False, 'error': 'Morceau inconnu.'}
            _nd_call('updatePlaylist', {'playlistId': i, 'songIndexToRemove': idx})
            return 200, {'ok': True}
        if action == 'playlist_del':
            i = ident('id')
            if not i:
                return 400, {'ok': False, 'error': 'Liste inconnue.'}
            _nd_call('deletePlaylist', {'id': i})
            return 200, {'ok': True}
        if action == 'scrobble':
            i = ident('id')
            if i:
                _nd_call('scrobble', {'id': i, 'submission': 'true'})
            return 200, {'ok': True}
        if action == 'scan':
            _nd_call('startScan', {'fullScan': 'true' if b.get('full') else 'false'})
            return 200, {'ok': True}
        if action == 'play':
            i = ident('id')
            if not i:
                return 400, {'ok': False, 'error': 'Morceau inconnu.'}
            return 200, {'ok': True, 'url': '/store/navidrome/stream/' + _nd_ticket(i)}
        return 400, {'ok': False, 'error': 'Action inconnue.'}
    except ValueError as e:
        if str(e) == 'auth':
            return 200, {'ok': True, 'denied': True}
        return 400, {'ok': False, 'error': str(e)[:160]}
    except RuntimeError as e:
        return 502, {'ok': False, 'error': 'Navidrome ne répond pas pour le moment (%s).' % str(e)[:80]}


def store_jellyfin_api(body):
    """{ action: state | save_password | clear_password | overview | libraries | folders | library_add | library_remove |
    scan | users | ratings | user_add | user_set | user_delete } → { ok, ... }."""
    if STORE_JF_ID not in store_installed_get():
        return 409, {'ok': False, 'error': 'Jellyfin n’est pas installé.'}
    b = body if isinstance(body, dict) else {}
    action = str(b.get('action') or '')
    fields = _store_fields_get(STORE_JF_ID)
    try:
        if action == 'state':
            _jf_setup()
            try:
                _jf_token(force=True)
            except ValueError:
                return 200, {'ok': True, 'denied': True, 'custom': bool(fields.get('password')), 'user': str(fields.get('user') or 'admin')}
            return 200, {'ok': True, 'denied': False, 'custom': bool(fields.get('password')), 'user': str(fields.get('user') or 'admin')}
        if action == 'clear_password':
            fields.pop('password', None); fields.pop('user', None)
            _store_fields_set(STORE_JF_ID, fields or None)
            with _JF_LOCK:
                _jf_sess.clear()
            return 200, {'ok': True}
        if action == 'save_password':
            pw = str(b.get('password') or '')
            user = str(b.get('user') or '').strip() or 'admin'
            if not re.match(r'^[^\x00-\x1f]{1,40}$', user):
                return 400, {'ok': False, 'error': 'Nom de compte invalide.'}
            if not (8 <= len(pw) <= 72):
                return 400, {'ok': False, 'error': 'Le mot de passe doit faire de 8 à 72 caractères.'}
            try:
                _jf_login(pw, user)
            except ValueError:
                return 400, {'ok': False, 'error': 'Jellyfin refuse ce compte ou ce mot de passe.'}
            fields['password'] = pw
            if user != 'admin':
                fields['user'] = user
            else:
                fields.pop('user', None)
            _store_fields_set(STORE_JF_ID, fields)
            return 200, {'ok': True}
        _jf_setup()
        if action == 'overview':
            def soft(fn, default):
                try:
                    return fn()
                except RuntimeError:
                    return default   # Jellyfin occupé (analyse en cours) : on garde le reste de l'écran
            info = _jf_call('GET', '/System/Info') or {}
            scan = soft(_jf_scan_state, {'running': False, 'pct': 0})
            cnt = soft(lambda: _jf_call('GET', '/Items/Counts') or {}, {})
            # MDM-JF-COUNTS-V1 : /Items/Counts gonfle les totaux (éléments en double, dossiers cachés…). On compte comme
            # les bibliothèques : ce que Jellyfin renvoie réellement pour chaque type (TotalRecordCount).
            def _jf_total(t, fb):
                try:
                    r = _jf_call('GET', '/Items', {'Recursive': 'true', 'Limit': 0, 'IncludeItemTypes': t}) or {}
                    return _pp_int(r.get('TotalRecordCount')) if 'TotalRecordCount' in r else fb
                except RuntimeError:
                    return fb
            _real = {'movies': _jf_total('Movie', _pp_int(cnt.get('MovieCount'))), 'series': _jf_total('Series', _pp_int(cnt.get('SeriesCount'))),
                     'episodes': _jf_total('Episode', _pp_int(cnt.get('EpisodeCount'))), 'songs': _jf_total('Audio', _pp_int(cnt.get('SongCount'))),
                     'albums': _jf_total('MusicAlbum', _pp_int(cnt.get('AlbumCount')))}
            playing = []
            for s in soft(lambda: _jf_call('GET', '/Sessions', {'activeWithinSeconds': 960}) or [], []):
                npi = s.get('NowPlayingItem')
                if isinstance(npi, dict):
                    ps = s.get('PlayState') if isinstance(s.get('PlayState'), dict) else {}
                    playing.append({'user': str(s.get('UserName') or '')[:60], 'device': str(s.get('DeviceName') or '')[:60],
                                    'client': str(s.get('Client') or '')[:40], 'paused': bool(ps.get('IsPaused')),
                                    'item': _jf_item(npi) or {'id': '', 'name': str(npi.get('Name') or '')[:120], 'series': '', 'img': False}})
            latest = []
            with _JF_LOCK:
                uid = _jf_sess.get('uid') or ''
            if _JF_ID.match(uid):
                for x in soft(lambda: _jf_call('GET', '/Users/%s/Items/Latest' % uid, {'Limit': 24, 'Fields': 'PrimaryImageAspectRatio'}) or [], []):
                    it = _jf_item(x) if isinstance(x, dict) else None
                    if it and it['img']:
                        latest.append(it)
            libs = soft(lambda: _jf_libraries(scan.get('running')), [])
            rails = []
            if not scan.get('running') and _JF_ID.match(uid):   # une rangée de nouveautés par bibliothèque (pas pendant une analyse)
                for l in libs[:5]:
                    if not _JF_ID.match(l.get('id') or '') or l.get('type') == 'music':
                        continue
                    rows = soft(lambda l=l: _jf_call('GET', '/Users/%s/Items/Latest' % uid, {'ParentId': l['id'], 'Limit': 16, 'Fields': 'PrimaryImageAspectRatio'}) or [], [])
                    its = [it for it in (_jf_item(x) for x in rows if isinstance(x, dict)) if it and it['img']]
                    if its:
                        rails.append({'id': l['id'], 'name': l['name'], 'type': l['type'], 'items': its[:16]})
            albums_new = []
            if _JF_ID.match(uid):   # derniers albums ajoutés (toutes bibliothèques musicales), même pendant une analyse
                for x in soft(lambda: (_jf_call('GET', '/Items', {'userId': uid, 'Recursive': 'true', 'IncludeItemTypes': 'MusicAlbum',
                                                                  'SortBy': 'DateCreated', 'SortOrder': 'Descending', 'Limit': 24,
                                                                  'Fields': 'PrimaryImageAspectRatio'}) or {}).get('Items') or [], []):
                    it = _jf_item(x) if isinstance(x, dict) else None
                    if it:
                        albums_new.append(it)
            resume, nextup = [], []
            if not scan.get('running') and _JF_ID.match(uid):
                for x in soft(lambda: (_jf_call('GET', '/Users/%s/Items/Resume' % uid, {'Limit': 12, 'MediaTypes': 'Video', 'Fields': 'PrimaryImageAspectRatio'}) or {}).get('Items') or [], []):
                    it = _jf_item(x) if isinstance(x, dict) else None
                    ud = x.get('UserData') if isinstance(x.get('UserData'), dict) else {}
                    rt = _pp_int(x.get('RunTimeTicks'))
                    if it and it['img'] and rt:
                        it['pct'] = max(1, min(99, int(100 * _pp_int(ud.get('PlaybackPositionTicks')) / rt)))
                        resume.append(it)
                for x in soft(lambda: (_jf_call('GET', '/Shows/NextUp', {'userId': uid, 'Limit': 10, 'Fields': 'PrimaryImageAspectRatio'}) or {}).get('Items') or [], []):
                    it = _jf_item(x) if isinstance(x, dict) else None
                    if it and it['img']:
                        nextup.append(it)
            return 200, {'ok': True, 'server': {'name': str(info.get('ServerName') or '')[:60], 'version': str(info.get('Version') or '')[:20]},
                         'counts': _real,
                         'libraries': libs, 'scan': scan, 'playing': playing, 'latest': latest[:20], 'rails': rails, 'albums_new': albums_new[:24],
                         'resume': resume[:12], 'nextup': nextup[:10]}
        if action in ('detail', 'play', 'report', 'mark'):
            iid = _jf_hexid(b.get('id'))
            if not _JF_ID.match(iid):
                return 400, {'ok': False, 'error': 'Élément inconnu.'}
            if action == 'detail':
                d = _jf_detail(iid); d.pop('_src', None)
                return 200, {'ok': True, 'item': d}
            if action == 'play':
                def _ix(v):
                    return v if isinstance(v, int) and not isinstance(v, bool) and -1 <= v < 1000 else None
                return 200, dict(_jf_play(iid, bool(b.get('transcode')), b.get('start'), _ix(b.get('audio')), _ix(b.get('sub')),
                                          re.sub(r'[^a-z]', '', str(b.get('alang') or '').lower())[:8], re.sub(r'[^a-z]', '', str(b.get('slang') or '').lower())[:8]), ok=True)
            if action == 'report':
                st = str(b.get('state') or 'progress')
                if st not in ('start', 'progress', 'stop'):
                    return 400, {'ok': False, 'error': 'État inconnu.'}
                body2 = {'ItemId': iid, 'MediaSourceId': iid, 'PositionTicks': max(0, int(b.get('pos') or 0)) * 10000000,
                         'IsPaused': bool(b.get('paused')), 'CanSeek': True, 'PlayMethod': 'DirectStream' if b.get('mode') == 'direct' else 'Transcode'}
                _jf_call('POST', {'start': '/Sessions/Playing', 'progress': '/Sessions/Playing/Progress', 'stop': '/Sessions/Playing/Stopped'}[st], None, body2)
                return 200, {'ok': True}
            uid = _jf_uid()
            if 'fav' in b:
                on = bool(b.get('fav'))
                _jf_try([('POST' if on else 'DELETE', '/UserFavoriteItems/%s' % iid, {'userId': uid}, None),
                         ('POST' if on else 'DELETE', '/Users/%s/FavoriteItems/%s' % (uid, iid), None, None)])
            if 'played' in b:
                on = bool(b.get('played'))
                _jf_try([('POST' if on else 'DELETE', '/UserPlayedItems/%s' % iid, {'userId': uid}, None),
                         ('POST' if on else 'DELETE', '/Users/%s/PlayedItems/%s' % (uid, iid), None, None)])
            return 200, {'ok': True}
        if action == 'browse':
            view = str(b.get('view') or '')
            lib = _jf_hexid(b.get('library'))
            uid = _jf_uid()
            typ = str(b.get('type') or '')
            prm = {'userId': uid, 'Recursive': 'true', 'Limit': max(1, min(int(b.get('limit') or 48), 72)),
                   'StartIndex': max(0, int(b.get('start') or 0)), 'Fields': 'PrimaryImageAspectRatio', 'EnableImageTypes': 'Primary'}
            sortmap = {'name': ('SortName', 'Ascending'), 'added': ('DateCreated,SortName', 'Descending'),
                       'year': ('PremiereDate,ProductionYear,SortName', 'Descending'), 'rating': ('CommunityRating,SortName', 'Descending'),
                       'played': ('DatePlayed', 'Descending')}
            sk = str(b.get('sort') or 'name')
            prm['SortBy'], prm['SortOrder'] = sortmap.get(sk, sortmap['name'])
            if view == 'fav':
                prm['Filters'] = 'IsFavorite'; prm['IncludeItemTypes'] = 'Movie,Series,Episode,MusicAlbum,Audio'
            elif view == 'history':
                prm['Filters'] = 'IsPlayed'; prm['IncludeItemTypes'] = 'Movie,Episode'; prm['SortBy'], prm['SortOrder'] = sortmap['played']
            elif view == 'playlists':
                prm['IncludeItemTypes'] = 'Playlist'
            elif view == 'search':
                prm['IncludeItemTypes'] = 'Movie,Series,Episode,MusicAlbum,Audio'
                if not str(b.get('q') or '').strip():
                    return 400, {'ok': False, 'error': 'Saisissez un mot à chercher.'}
            else:
                if not _JF_ID.match(lib):
                    return 400, {'ok': False, 'error': 'Bibliothèque inconnue.'}
                prm['ParentId'] = lib
                incl = {'movies': 'Movie', 'tvshows': 'Series', 'music': 'MusicAlbum', 'books': 'Book', 'homevideos': 'Video,Photo', 'musicvideos': 'MusicVideo'}.get(typ)
                if incl:
                    prm['IncludeItemTypes'] = incl
            flt = str(b.get('filter') or '')
            if flt in ('unplayed', 'played', 'fav') and view not in ('fav', 'history'):
                prm['Filters'] = {'unplayed': 'IsUnplayed', 'played': 'IsPlayed', 'fav': 'IsFavorite'}[flt]
            genre = str(b.get('genre') or '').strip()[:60]
            if genre and '|' not in genre:
                prm['Genres'] = genre
            q = str(b.get('q') or '').strip()[:100]
            if q:
                prm['SearchTerm'] = q
            r = _jf_call('GET', '/Items', prm) or {}
            its = [it for it in (_jf_item(x) for x in (r.get('Items') or []) if isinstance(x, dict)) if it]
            return 200, {'ok': True, 'items': its, 'total': _pp_int(r.get('TotalRecordCount'))}
        if action == 'genres':
            lib = _jf_hexid(b.get('library'))
            prm = {'userId': _jf_uid(), 'Recursive': 'true', 'SortBy': 'SortName'}
            if _JF_ID.match(lib):
                prm['ParentId'] = lib
            r = _jf_call('GET', '/Genres', prm) or {}
            return 200, {'ok': True, 'genres': [str(g.get('Name') or '')[:60] for g in (r.get('Items') or []) if isinstance(g, dict) and g.get('Name')][:80]}
        if action in ('backup_list', 'backup_create', 'backup_restore'):
            def _bk_list():
                try:
                    rows = _jf_call('GET', '/Backup') or []
                except RuntimeError as e:
                    if 'HTTP 404' in str(e):
                        raise ValueError('Cette version de Jellyfin n’a pas de sauvegarde intégrée (il faut la 10.11 ou plus récente).')
                    raise
                out = []
                for r in (rows if isinstance(rows, list) else []):
                    if not isinstance(r, dict):
                        continue
                    nm = str(r.get('Path') or '').replace('\\', '/').rsplit('/', 1)[-1]
                    if re.match(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{0,120}\.zip$', nm):
                        opt = r.get('Options') if isinstance(r.get('Options'), dict) else {}
                        out.append({'file': nm, 'date': str(r.get('DateCreated') or '')[:25], 'version': str(r.get('ServerVersion') or '')[:20],
                                    'metadata': bool(opt.get('Metadata')), 'database': bool(opt.get('Database', True))})
                out.sort(key=lambda x: x['date'], reverse=True)
                return out
            if action == 'backup_list':
                return 200, {'ok': True, 'backups': _bk_list()}
            if action == 'backup_create':
                _jf_call('POST', '/Backup/Create', data={'Metadata': bool(b.get('metadata')), 'Trickplay': False, 'Subtitles': True, 'Database': True})
                return 200, {'ok': True, 'backups': _bk_list()}
            name = str(b.get('file') or '')
            if name not in [x['file'] for x in _bk_list()]:
                return 404, {'ok': False, 'error': 'Sauvegarde introuvable.'}
            _jf_call('POST', '/Backup/Restore', data={'ArchiveFileName': name})
            with _JF_LOCK:
                _jf_sess.clear()
            return 200, {'ok': True, 'restarting': True}
        if action == 'activity':
            def soft2(fn, default):
                try:
                    return fn()
                except (ValueError, RuntimeError):
                    return default
            sess = []
            for x in soft2(lambda: _jf_call('GET', '/Sessions', {'activeWithinSeconds': 960}) or [], []):
                if not isinstance(x, dict):
                    continue
                sid = _jf_hexid(x.get('Id'))
                if not _JF_ID.match(sid) or x.get('Client') == 'Desktroo':
                    continue
                npi = x.get('NowPlayingItem') if isinstance(x.get('NowPlayingItem'), dict) else None
                ps = x.get('PlayState') if isinstance(x.get('PlayState'), dict) else {}
                sess.append({'id': sid, 'user': str(x.get('UserName') or '')[:60], 'device': str(x.get('DeviceName') or '')[:60], 'client': str(x.get('Client') or '')[:40],
                             'playing': ((str(npi.get('SeriesName') or '') + ' · ') if npi and npi.get('SeriesName') else '') + str(npi.get('Name') or '')[:120] if npi else '',
                             'paused': bool(ps.get('IsPaused')), 'pos': _jf_sec(ps.get('PositionTicks')) if npi else 0})
            devs = []
            dd = soft2(lambda: _jf_call('GET', '/Devices') or {}, {})
            for x in (dd.get('Items') if isinstance(dd, dict) else []) or []:
                did = str(x.get('Id') or '')
                if isinstance(x, dict) and re.match(r'^[A-Za-z0-9._\-]{1,128}$', did) and did != 'desktroo-nas':
                    devs.append({'id': did, 'name': str(x.get('Name') or '')[:60], 'app': str(x.get('AppName') or '')[:40],
                                 'user': str(x.get('LastUserName') or '')[:60], 'last': str(x.get('DateLastActivity') or '')[:25]})
            devs.sort(key=lambda x: x['last'], reverse=True)
            ev = []
            ae = soft2(lambda: _jf_call('GET', '/System/ActivityLog/Entries', {'limit': 40}) or {}, {})
            for x in (ae.get('Items') if isinstance(ae, dict) else []) or []:
                if isinstance(x, dict):
                    ev.append({'name': str(x.get('Name') or '')[:200], 'detail': str(x.get('ShortOverview') or '')[:200],
                               'date': str(x.get('Date') or '')[:25], 'level': str(x.get('Severity') or '')[:12]})
            return 200, {'ok': True, 'sessions': sess[:30], 'devices': devs[:40], 'events': ev[:40]}
        if action == 'session_stop':
            sid = _jf_hexid(b.get('id'))
            if not _JF_ID.match(sid):
                return 400, {'ok': False, 'error': 'Séance inconnue.'}
            _jf_call('POST', '/Sessions/%s/Playing/Stop' % sid)
            return 200, {'ok': True}
        if action == 'device_delete':
            did = str(b.get('id') or '')
            if not re.match(r'^[A-Za-z0-9._\-]{1,128}$', did) or did == 'desktroo-nas':
                return 400, {'ok': False, 'error': 'Appareil inconnu.'}
            _jf_call('DELETE', '/Devices', {'id': did})
            return 200, {'ok': True}
        if action == 'errors':
            files = _jf_call('GET', '/System/Logs') or []
            names = sorted([str(f.get('Name') or '') for f in files if isinstance(f, dict) and re.match(r'^log_[0-9]{8}[A-Za-z0-9_.\-]*\.log$', str(f.get('Name') or ''))], reverse=True)
            lines = []
            if names:
                import urllib.parse
                st, raw = _jf_http('GET', '/System/Logs/Log?' + urllib.parse.urlencode({'name': names[0]}), token=_jf_token(), limit=3 * 1024 * 1024)
                if st == 200:
                    lines = [ln[:400] for ln in raw.decode('utf-8', 'replace').splitlines() if '[ERR]' in ln or '[WRN]' in ln][-60:]
            return 200, {'ok': True, 'lines': lines, 'file': names[0] if names else ''}
        if action in ('refresh', 'identify', 'identify_apply', 'playlist_add'):
            iid = _jf_hexid(b.get('id'))
            if not _JF_ID.match(iid):
                return 400, {'ok': False, 'error': 'Élément inconnu.'}
            if action == 'refresh':
                _jf_call('POST', '/Items/%s/Refresh' % iid, {'Recursive': 'true', 'ImageRefreshMode': 'FullRefresh', 'MetadataRefreshMode': 'FullRefresh',
                                                              'ReplaceAllImages': 'false', 'ReplaceAllMetadata': 'false'})
                return 200, {'ok': True}
            if action == 'identify':
                it = _jf_call('GET', '/Users/%s/Items/%s' % (_jf_uid(), iid)) or {}
                kind = {'Movie': 'Movie', 'Series': 'Series'}.get(str(it.get('Type') or ''))
                if not kind:
                    return 400, {'ok': False, 'error': 'Seuls les films et les séries peuvent être identifiés.'}
                nm = str(b.get('name') or it.get('Name') or '').strip()[:120]
                yr = _pp_int(b.get('year')) or None
                res = _jf_call('POST', '/Items/RemoteSearch/' + kind, data={'SearchInfo': {'Name': nm, 'Year': yr}, 'ItemId': _jf_guid(iid)}) or []
                res = [r for r in (res if isinstance(res, list) else []) if isinstance(r, dict)][:12]
                _JF_IDENT.clear() if len(_JF_IDENT) > 20 else None
                _JF_IDENT[iid] = res
                return 200, {'ok': True, 'results': [{'n': i, 'name': str(r.get('Name') or '')[:120], 'year': _pp_int(r.get('ProductionYear')),
                                                      'overview': str(r.get('Overview') or '')[:220], 'provider': str(r.get('SearchProviderName') or '')[:30],
                                                      'img': str(r.get('ImageUrl') or '')[:300] if str(r.get('ImageUrl') or '').startswith('https://') else ''}
                                                     for i, r in enumerate(res)], 'name': nm}
            if action == 'identify_apply':
                n = b.get('n')
                res = _JF_IDENT.get(iid) or []
                if not isinstance(n, int) or isinstance(n, bool) or not (0 <= n < len(res)):
                    return 400, {'ok': False, 'error': 'Relancez la recherche.'}
                _jf_call('POST', '/Items/RemoteSearch/Apply/%s' % iid, {'ReplaceAllImages': 'true'}, res[n])
                return 200, {'ok': True}
            # playlist_add : ajoute l'élément à une liste existante (list) ou à une nouvelle (name)
            uid = _jf_uid()
            it = _jf_call('GET', '/Users/%s/Items/%s' % (uid, iid)) or {}
            media = 'Audio' if str(it.get('Type') or '') in ('Audio', 'MusicAlbum') else 'Video'
            pid = _jf_hexid(b.get('list'))
            if _JF_ID.match(pid):
                _jf_call('POST', '/Playlists/%s/Items' % pid, {'Ids': _jf_guid(iid), 'UserId': _jf_guid(uid)})
                return 200, {'ok': True}
            nm = str(b.get('name') or '').strip()[:80]
            if not nm:
                return 400, {'ok': False, 'error': 'Donnez un nom à la liste.'}
            _jf_call('POST', '/Playlists', data={'Name': nm, 'Ids': [_jf_guid(iid)], 'UserId': _jf_guid(uid), 'MediaType': media})
            return 200, {'ok': True}
        if action == 'repair':
            if not b.get('apply'):
                return 200, {'ok': True, 'plan': _jf_repair_plan()}
            libs = {l['name']: l['paths'] for l in _jf_libraries()}
            done = 0
            for it in (b.get('items') if isinstance(b.get('items'), list) else [])[:30]:
                name, old, new = str(it.get('library') or ''), str(it.get('old') or ''), _jf_clean_path(it.get('new'))
                if name not in libs or old not in libs[name] or not new or new == '/nas' or old.startswith('/nas/'):
                    continue
                if new not in libs[name]:
                    _jf_call('POST', '/Library/VirtualFolders/Paths', {'refreshLibrary': 'false'}, {'Name': name, 'PathInfo': {'Path': new}})
                _jf_call('DELETE', '/Library/VirtualFolders/Paths', {'name': name, 'path': old, 'refreshLibrary': 'false'})
                done += 1
            if done:
                _jf_call('POST', '/Library/Refresh')
            return 200, {'ok': True, 'done': done, 'libraries': _jf_libraries(), 'scan': _jf_scan_state()}
        if action == 'libraries':
            scan = _jf_scan_state()
            return 200, {'ok': True, 'libraries': _jf_libraries(scan.get('running')), 'scan': scan}
        if action == 'folders':
            path = _jf_clean_path(b.get('path'), allow_root=True)
            if not path:
                return 400, {'ok': False, 'error': 'Dossier non autorisé.'}
            try:
                data = _jf_call('GET', '/Environment/DirectoryContents', {'path': path, 'includeFiles': 'false', 'includeDirectories': 'true'}) or []
            except ValueError:
                return 200, {'ok': True, 'path': path, 'folders': [], 'empty': 'Aucun dossier du NAS n’est accessible ici.'}
            rows = []
            for f in data:
                p = str(f.get('Path') or '').rstrip('/')
                nm = p.rsplit('/', 1)[-1]
                if p.startswith(path + '/') and p.count('/') == path.count('/') + 1 and _jf_clean_path(p) and not nm.startswith('.'):
                    rows.append({'name': nm[:120], 'path': p})
            rows.sort(key=lambda r: r['name'].lower())
            return 200, {'ok': True, 'path': path, 'folders': rows}
        if action == 'library_add':
            name = str(b.get('name') or '').strip()
            typ = str(b.get('type') or '')
            paths = [_jf_clean_path(p) for p in (b.get('paths') if isinstance(b.get('paths'), list) else [])][:10]
            if not re.match(r'^[^\\/\x00-\x1f]{1,60}$', name):
                return 400, {'ok': False, 'error': 'Nom de bibliothèque invalide (1 à 60 caractères, sans « / »).'}
            if typ not in _JF_TYPES:
                return 400, {'ok': False, 'error': 'Type de bibliothèque inconnu.'}
            if not paths or not all(paths) or any(p == '/nas' for p in paths):
                return 400, {'ok': False, 'error': 'Choisissez au moins un dossier de votre NAS (pas la racine).'}
            if any(l['name'].lower() == name.lower() for l in _jf_libraries()):
                return 400, {'ok': False, 'error': 'Une bibliothèque porte déjà ce nom.'}
            opts = {'PathInfos': [{'Path': p} for p in paths], 'PreferredMetadataLanguage': 'fr', 'MetadataCountryCode': 'FR'}
            _qp = {'name': name, 'refreshLibrary': 'true'}
            if typ != 'mixed':   # « Films et séries mélangés » : bibliothèque sans type
                _qp['collectionType'] = typ
            _jf_call('POST', '/Library/VirtualFolders', _qp, {'LibraryOptions': opts})
            return 200, {'ok': True, 'libraries': _jf_libraries(), 'scan': _jf_scan_state()}
        if action == 'library_remove':
            name = str(b.get('name') or '')
            if not any(l['name'] == name for l in _jf_libraries()):
                return 404, {'ok': False, 'error': 'Bibliothèque introuvable.'}
            _jf_call('DELETE', '/Library/VirtualFolders', {'name': name, 'refreshLibrary': 'true'})
            return 200, {'ok': True, 'libraries': _jf_libraries(), 'scan': _jf_scan_state()}
        if action == 'scan':
            _jf_call('POST', '/Library/Refresh')
            return 200, {'ok': True, 'scan': {'running': True, 'pct': 0}}
        if action == 'ratings':
            rat = _jf_call('GET', '/Localization/ParentalRatings') or []
            fr = [r for r in rat if str(r.get('Name') or '').startswith('FR-')] or [r for r in rat if str(r.get('Name') or '').startswith('US-')] or rat
            seen, out = set(), []
            for r in sorted(fr, key=lambda r: (_pp_int(r.get('Value')), str(r.get('Name')))):
                v = r.get('Value')
                if isinstance(v, int) and v not in seen:
                    seen.add(v); out.append({'name': str(r.get('Name'))[:30], 'value': v})
            return 200, {'ok': True, 'ratings': out[:14]}
        if action == 'users':
            return 200, {'ok': True, 'users': [_jf_user_row(u) for u in (_jf_call('GET', '/Users') or [])]}
        if action == 'user_add':
            name, pw = str(b.get('name') or '').strip(), str(b.get('password') or '')
            if not re.match(r'^[^\x00-\x1f]{1,40}$', name) or name.lower() == 'admin':
                return 400, {'ok': False, 'error': 'Nom invalide (1 à 40 caractères, différent de « admin »).'}
            if not (4 <= len(pw) <= 72):
                return 400, {'ok': False, 'error': 'Le mot de passe doit faire de 4 à 72 caractères.'}
            if any(u['name'].lower() == name.lower() for u in [_jf_user_row(x) for x in (_jf_call('GET', '/Users') or [])]):
                return 400, {'ok': False, 'error': 'Ce nom existe déjà.'}
            u = _jf_call('POST', '/Users/New', data={'Name': name, 'Password': pw})
            uid = _jf_hexid((u or {}).get('Id'))
            rating = b.get('rating')
            fol = _jf_folders_arg(b.get('folders', 'keep'))
            if _JF_ID.match(uid) and (isinstance(rating, int) or fol != 'keep'):
                _jf_set_policy(uid, rating=rating if isinstance(rating, int) else 'keep', folders=fol)
            return 200, {'ok': True, 'users': [_jf_user_row(x) for x in (_jf_call('GET', '/Users') or [])]}
        if action == 'adm':   # MDM-JF-ADM-V1
            return _jf_adm(b)
        if action in ('user_set', 'user_delete'):
            uid = _jf_hexid(b.get('id'))
            if not _JF_ID.match(uid):
                return 400, {'ok': False, 'error': 'Requête non autorisée.'}
            target = _jf_user_row(_jf_call('GET', '/Users/' + uid) or {})
            if target['admin']:
                return 400, {'ok': False, 'error': 'Les comptes administrateurs se gèrent dans Jellyfin.'}
            if action == 'user_delete':
                _jf_call('DELETE', '/Users/' + uid)
            else:
                rating = b.get('rating', 'keep')
                if rating != 'keep' and rating is not None and not isinstance(rating, int):
                    return 400, {'ok': False, 'error': 'Limite d’âge invalide.'}
                fol = _jf_folders_arg(b.get('folders', 'keep'))
                _jf_set_policy(uid, disabled=b.get('disabled') if isinstance(b.get('disabled'), bool) else None, rating=rating, folders=fol)
                if b.get('password') is not None:
                    npw = str(b.get('password'))
                    if not (4 <= len(npw) <= 72):
                        return 400, {'ok': False, 'error': 'Le mot de passe doit faire de 4 à 72 caractères.'}
                    _jf_try([('POST', '/Users/%s/Password' % uid, None, {'NewPw': npw, 'ResetPassword': False}),
                             ('POST', '/Users/Password', {'userId': uid}, {'NewPw': npw, 'ResetPassword': False})])
            return 200, {'ok': True, 'users': [_jf_user_row(x) for x in (_jf_call('GET', '/Users') or [])]}
    except ValueError as e:
        if str(e) == 'auth':
            return 200, {'ok': True, 'denied': True}
        return 400, {'ok': False, 'error': str(e)}
    except RuntimeError as e:
        return 502, {'ok': False, 'error': 'Jellyfin ne répond pas : ' + str(e)}
    return 400, {'ok': False, 'error': 'Requête non autorisée.'}


# ── MDM-STORE-V1 · module « Desktroo Pass » (dt-pass) : coffre de mots de passe compatible Bitwarden ──────
# Serveur Vaultwarden (AGPL-3.0, image officielle non modifiée), utilisable avec les applis Bitwarden officielles
# (extensions de navigateur, téléphone, PC). Desktroo l'installe, le publie en https sous un domaine via NPMplus
# (chemin /admin bloqué côté Internet) et l'administre depuis sa fenêtre : comptes sur invitation seulement.
# Le jeton d'administration reste sur le NAS (data/store/pass.json) ; le navigateur ne le voit jamais.
STORE_PASS_ID = 'pass'
STORE_PASS_CONTAINER = 'desktroo-pass'
STORE_PASS_FILE = os.path.join(STORE_DIR, 'pass.json')
STORE_PASS_PORT = 8282          # port publié sur le NAS, vers lequel NPMplus relaie le domaine
_PASS = {'cookie': '', 'until': 0, 'job': {}}
_PASS_LOCK = _threading.Lock()


def store_pass_settings():
    s = _lic_read_json(STORE_PASS_FILE) or {}
    if not s.get('admin_token'):
        s['admin_token'] = _px_secrets.token_urlsafe(32)
    s['port'] = int(s.get('port') or STORE_PASS_PORT)
    return s


def _store_pass_save(s):
    os.makedirs(STORE_DIR, exist_ok=True)
    _lic_write_json(STORE_PASS_FILE, s)


def _store_pass_smtp():
    """Messagerie de TrueNAS (celle des alertes), reprise pour les invitations du coffre. Rend (env, raison)."""
    try:
        m = _store_midclt('mail.config', timeout=30) or {}
    except Exception as e:
        return {}, 'Messagerie de TrueNAS illisible : ' + str(e)[:120]
    if m.get('oauth'):
        return {}, 'La messagerie du NAS passe par Gmail ou Outlook (connexion OAuth) : le coffre ne peut pas s’en servir. Configurez un serveur SMTP avec mot de passe.'
    host, sender = str(m.get('outgoingserver') or ''), str(m.get('fromemail') or '')
    if not host or not sender:
        return {}, 'La messagerie du NAS n’est pas configurée.'
    sec = {'TLS': 'starttls', 'SSL': 'force_tls'}.get(str(m.get('security') or '').upper(), 'off')
    env = {'SMTP_HOST': host, 'SMTP_PORT': str(m.get('port') or (465 if sec == 'force_tls' else 587)), 'SMTP_SECURITY': sec,
           'SMTP_FROM': sender, 'SMTP_FROM_NAME': 'Desktroo Pass'}
    if m.get('smtp') and m.get('user'):
        env['SMTP_USERNAME'] = str(m.get('user')); env['SMTP_PASSWORD'] = str(m.get('pass') or '')
    return env, ''


def _store_pass_mail_sig(env):
    """Empreinte des réglages d'envoi donnés au coffre : sert à voir que la messagerie du NAS a changé depuis."""
    import hashlib
    return hashlib.sha256(json.dumps(env, sort_keys=True).encode('utf-8')).hexdigest()[:16] if env else ''


_PASS_MAILCHK = {'t': 0, 'env': {}, 'why': ''}


def _store_pass_smtp_now():
    """Messagerie actuelle du NAS, relue au plus une fois par minute (affichage de l'état du coffre)."""
    if _st_time.time() - _PASS_MAILCHK['t'] > 60:
        env, why = _store_pass_smtp()
        _PASS_MAILCHK.update(t=_st_time.time(), env=env, why=why)
    return _PASS_MAILCHK['env'], _PASS_MAILCHK['why']


def store_pass_compose(app, s):
    images = (app or {}).get('images') or {}
    env = {'TZ': _store_timezone(), 'ROCKET_PORT': '8080', 'SIGNUPS_ALLOWED': 'false', 'INVITATIONS_ALLOWED': 'true',
           'SIGNUPS_VERIFY': 'false', 'SHOW_PASSWORD_HINT': 'false', 'ADMIN_TOKEN': s['admin_token'],
           'INVITATION_ORG_NAME': 'Desktroo Pass', 'LOG_LEVEL': 'warn', 'DATA_FOLDER': '/data'}
    if s.get('domain'):
        env['DOMAIN'] = 'https://' + s['domain']
    smtp, _why = _store_pass_smtp()   # invitations par e-mail, avec la messagerie du NAS
    env.update(smtp)
    s['mail'] = {'ok': bool(smtp), 'from': smtp.get('SMTP_FROM', ''), 'why': _why, 'sig': _store_pass_mail_sig(smtp),
                 'server': ('%s:%s' % (smtp.get('SMTP_HOST'), smtp.get('SMTP_PORT'))) if smtp else ''}
    _PASS_MAILCHK['t'] = 0
    _store_pass_save(s)
    svc = {
        'image': str(images.get('vaultwarden') or 'docker.io/vaultwarden/server:latest'),
        'container_name': STORE_PASS_CONTAINER, 'restart': 'unless-stopped',
        'user': '568:568',
        'environment': env,
        'volumes': ['/mnt/' + _store_dataset(STORE_PASS_ID) + '/data:/data'],
        'ports': ['%d:8080' % s['port']],
        'networks': [STORE_NET],
        'security_opt': ['no-new-privileges:true'], 'cap_drop': ['ALL'],
        'labels': {'fr.desktroo.store': STORE_PASS_ID, 'fr.desktroo.version': str((app or {}).get('version', ''))},
    }
    return {'services': {'pass': svc}, 'networks': {STORE_NET: {'external': True}}}


def _store_pass_prepare(app):
    s = store_pass_settings()
    ds = _store_dataset(STORE_PASS_ID)
    parent = ds.rsplit('/', 1)[0]
    for d in (parent, ds):
        out, _e, code = _px_host('zfs list -H -o name ' + shq(d) + ' 2>/dev/null', timeout=30)
        if code != 0 or not out.strip():
            _store_ds_create(d)
    p = '/mnt/' + ds + '/data'
    _o, err, code = _px_host('mkdir -p %s && chown 568:568 %s' % (shq(p), shq(p)), timeout=60)
    if code != 0:
        raise RuntimeError('Dossier de données : ' + (err or '')[-300:])
    busy = []
    if not store_pass_installed():
        out, _e, _c = _px_host("ss -Hltn 2>/dev/null | awk '{print $4}'", timeout=30)
        if any(re.search(r':%d$' % s['port'], l.strip()) for l in (out or '').splitlines()):
            busy.append(s['port'])
    if busy:
        raise RuntimeError('Le port %d est déjà utilisé sur le NAS.' % s['port'])
    _store_pass_save(s)
    return s


def store_pass_installed():
    return STORE_PASS_ID in store_installed_get()


def _store_pass_wait(s, timeout=150):
    end = _st_time.time() + timeout
    while _st_time.time() < end:
        if _store_container_info(STORE_PASS_CONTAINER)[1]:
            _o, _e, code = _px_host('curl -s -m 4 -o /dev/null http://127.0.0.1:%d/alive || wget -q -T 4 -O /dev/null http://127.0.0.1:%d/alive'
                                    % (s['port'], s['port']), timeout=20)
            if code == 0:
                return True
        _st_time.sleep(4)
    return False


# ── administration (API /admin de Vaultwarden, jointe par le réseau des apps) ──
def _pass_http(method, path, body=None, form=None, cookie='', timeout=20):
    import http.client
    conn = http.client.HTTPConnection(STORE_PASS_CONTAINER, 8080, timeout=timeout)
    headers = {'Accept': 'application/json'}
    data = None
    if form is not None:
        from urllib.parse import urlencode
        data = urlencode(form).encode('utf-8'); headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode('utf-8'); headers['Content-Type'] = 'application/json'
    if cookie:
        headers['Cookie'] = cookie
    try:
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        return r.status, raw, r.getheader('Set-Cookie') or ''
    finally:
        conn.close()


def _pass_session(force=False):
    with _PASS_LOCK:
        if not force and _PASS['cookie'] and _st_time.time() < _PASS['until']:
            return _PASS['cookie']
        tok = store_pass_settings()['admin_token']
        code, _raw, sc = _pass_http('POST', '/admin', form={'token': tok, 'redirect': ''})
        m = re.search(r'(VW_ADMIN=[^;]+)', sc or '')
        if not m:
            raise PermissionError('Le coffre refuse le jeton d’administration (réponse %s).' % code)
        _PASS.update(cookie=m.group(1), until=_st_time.time() + 900)
        return _PASS['cookie']


def _pass_err_text(raw):
    """Message d'erreur de Vaultwarden (JSON « message » ou « ErrorModel ») plutôt que la réponse brute."""
    try:
        d = json.loads(raw.decode('utf-8'))
        if isinstance(d, dict):
            m = d.get('message') or d.get('Message') or (d.get('errorModel') or d.get('ErrorModel') or {}).get('message') \
                or d.get('error_description') or d.get('error')
            if m:
                return str(m)[:300]
    except Exception:
        pass
    t = raw[:2000].decode('utf-8', 'replace')
    if re.search(r'<html|<!doctype', t, re.I):
        m = re.search(r'<title>([^<]{1,120})</title>', t, re.I)
        return 'page %s' % (m.group(1).strip() if m else 'HTML')
    return t[:200]


def _pass_admin(method, path, body=None):
    code, raw, _sc = _pass_http(method, '/admin' + path, body=body, cookie=_pass_session())
    if code in (401, 403) or (code in (302, 303)):
        code, raw, _sc = _pass_http(method, '/admin' + path, body=body, cookie=_pass_session(force=True))
    if code >= 400:
        raise RuntimeError('Le coffre a répondu %s : %s' % (code, _pass_err_text(raw)))
    try:
        return json.loads(raw.decode('utf-8') or 'null')
    except Exception:
        return None


def store_pass_state():
    s = _lic_read_json(STORE_PASS_FILE) or {}
    st = {'installed': store_pass_installed(), 'domain': s.get('domain') or '', 'port': int(s.get('port') or STORE_PASS_PORT),
          'job': dict(_NPM_API['job']) or None, 'users': [], 'npm': None,
          'mail': s.get('mail') or {'ok': False, 'from': '', 'why': ''}}
    try:
        n = _lic_read_json(STORE_NPM_FILE) or {}
        st['npm'] = 'module' if STORE_NPM_ID in store_installed_get() else ('linked' if n.get('linked') else None)
    except Exception:
        pass
    if not st['installed']:
        return st
    # La messagerie du NAS a-t-elle changé depuis que le coffre l'a reprise ? (réglée après coup, mot de passe, port…)
    try:
        env, why = _store_pass_smtp_now()
        m = st['mail']
        m['now_ok'], m['now_why'] = bool(env), why
        m['stale'] = bool(env) and _store_pass_mail_sig(env) != m.get('sig', '')
        m.pop('sig', None)
    except Exception:
        pass
    try:
        rows = _pass_admin('GET', '/users') or []
        st['users'] = [{'id': u.get('id') or u.get('Id'), 'email': u.get('email') or u.get('Email'),
                        'name': u.get('name') or u.get('Name') or '',
                        'status': u.get('_status') if u.get('_status') is not None else u.get('status'),
                        'enabled': u.get('userEnabled', u.get('UserEnabled', True)),
                        'twofa': bool(u.get('twoFactorEnabled') or u.get('TwoFactorEnabled')),
                        'last_active': u.get('lastActive') or u.get('last_active') or '',
                        'created': u.get('createdAt') or u.get('created_at') or ''}
                       for u in rows if isinstance(u, dict)]
    except Exception as e:
        st['error'] = 'Le coffre ne répond pas : ' + str(e)[:200]
    return st


def store_pass_invite(body):
    email = str((body or {}).get('email') or '').strip().lower()
    if not re.match(r'^[^@\s]{1,64}@[^@\s]{1,253}\.[a-z]{2,}$', email):
        return 400, {'ok': False, 'error': 'Adresse e-mail invalide.'}
    try:
        _pass_admin('POST', '/invite', {'email': email})
        return 200, {'ok': True}
    except Exception as e:
        t = str(e)
        if re.search(r'already exists', t, re.I):
            return 409, {'ok': False, 'error': 'Cette adresse a déjà un compte.'}
        return 502, {'ok': False, 'error': t[:300]}


def store_pass_test_mail(body):
    """E-mail de test envoyé par le coffre lui-même (API /admin/test/smtp de Vaultwarden) : même chemin que les invitations."""
    email = str((body or {}).get('email') or '').strip().lower()
    if not re.match(r'^[^@\s]{1,64}@[^@\s]{1,253}\.[a-z]{2,}$', email):
        return 400, {'ok': False, 'error': 'Adresse e-mail invalide.'}
    try:
        _pass_admin('POST', '/test/smtp', {'email': email})
        return 200, {'ok': True}
    except Exception as e:
        t = str(e)
        if re.search(r'mail is not enabled', t, re.I):
            return 409, {'ok': False, 'error': 'Le coffre n’a pas de messagerie : utilisez « Activer l’envoi des e-mails ».'}
        return 502, {'ok': False, 'error': 'Le serveur d’envoi a refusé le message. ' + t[:300]}


def store_pass_user(body):
    uid = str((body or {}).get('id') or '')
    act = str((body or {}).get('action') or '')
    if not re.match(r'^[0-9a-f-]{8,64}$', uid) or act not in ('delete', 'disable', 'enable', 'deauth', 'remove-2fa', 'invite/resend'):
        return 400, {'ok': False, 'error': 'Demande invalide.'}
    try:
        # Vaultwarden ne reconnaît ces routes qu'avec Content-Type: application/json (sinon 404) : corps JSON vide
        _pass_admin('POST', '/users/%s/%s' % (uid, act), {})
        return 200, {'ok': True}
    except Exception as e:
        return 502, {'ok': False, 'error': str(e)[:300]}


def store_pass_mail(body=None):
    """Reprend la messagerie actuelle du NAS pour les invitations (redéploie le coffre, quelques secondes)."""
    if not store_pass_installed():
        return 409, {'ok': False, 'error': 'Desktroo Pass n’est pas installé.'}
    smtp, why = _store_pass_smtp()
    if not smtp:
        return 409, {'ok': False, 'error': why}
    try:
        s = store_pass_settings()
        app = store_app_entry(STORE_PASS_ID) or {'id': STORE_PASS_ID, 'version': (store_installed_get().get(STORE_PASS_ID) or {}).get('version')}
        _store_midclt('app.update', 'dt-' + STORE_PASS_ID, {'custom_compose_config': store_pass_compose(app, s)}, job=True, timeout=900)
        _PASS.update(cookie='', until=0)
        return 200, {'ok': True, 'from': smtp.get('SMTP_FROM')}
    except Exception as e:
        return 502, {'ok': False, 'error': str(e)[:300]}


def store_pass_domain(body):
    """Publie le coffre sous un domaine (NPMplus, certificat Let's Encrypt), puis le redéploie avec ce domaine."""
    dom = str((body or {}).get('domain') or '').strip().lower().rstrip('.')
    if not _NPM_DOMAIN_RE.match(dom) or dom.startswith('*.'):
        return 400, {'ok': False, 'error': 'Nom de domaine invalide.'}
    if not store_pass_installed():
        return 409, {'ok': False, 'error': 'Desktroo Pass n’est pas installé.'}
    n = _lic_read_json(STORE_NPM_FILE) or {}
    if STORE_NPM_ID in store_installed_get():
        host = '127.0.0.1'          # NPMplus de Desktroo Apps : réseau de l'hôte
    elif n.get('linked'):
        host = _truenas_ip() or SSH_HOST   # NPMplus relié (app TrueNAS, réseau ponté) : l'adresse du NAS
    else:
        return 409, {'ok': False, 'error': 'NPMplus est nécessaire : installez-le ou reliez-le dans Desktroo Apps.'}
    port = store_pass_settings()['port']

    def then(d):
        s = store_pass_settings(); s['domain'] = d; _store_pass_save(s)
        app = store_app_entry(STORE_PASS_ID) or {'id': STORE_PASS_ID, 'version': (store_installed_get().get(STORE_PASS_ID) or {}).get('version')}
        _store_midclt('app.update', 'dt-' + STORE_PASS_ID, {'custom_compose_config': store_pass_compose(app, s)}, job=True, timeout=900)
        _PASS.update(cookie='', until=0)

    # /admin n'est jamais servi sur Internet : l'administration passe par Desktroo.
    return store_npm_host_add({'domains': [dom], 'host': host, 'port': port, 'https': True},
                              advanced='location /admin { return 404; }', then=then, adopt=True, replace=bool((body or {}).get('replace')))


# ── MDM-STORE-V1 · accès des apps aux dossiers du NAS (« nas_access » dans le catalogue) ────────────────
# Une app qui le déclare voit les dossiers que l'administrateur a cochés, sous /nas/<nom>, avec les droits d'un
# utilisateur TrueNAS qu'il a choisi : le conteneur tourne sous son uid, son groupe et ses groupes secondaires.
# Elle voit donc exactement ce que voit ce compte, ni plus ni moins, et aucune permission du NAS n'est modifiée.
# Lecture seule ou lecture-écriture selon ce que l'app déclare ; l'administrateur peut imposer la lecture seule.
STORE_NAS_DIR = os.path.join(STORE_DIR, 'nas')
_STORE_NAS_SKIP = re.compile(r'(^|/)(\.system|ix-apps|\.ix-apps|ix-applications|desktroo-apps)(/|$)|^boot-pool')


def _store_ds_private(name):
    """Données des apps Desktroo (apps/desktroo) et dataset « apps » qui les contient : jamais proposés à une app, qui lirait
    sinon les coffres et certificats des autres."""
    try:
        base = _store_apps_base()
    except Exception:
        return False
    if not base.endswith('/apps/desktroo'):
        return False
    return name == base or name.startswith(base + '/') or name == base.rsplit('/', 1)[0]


def _store_nas_get(app_id):
    return _lic_read_json(os.path.join(STORE_NAS_DIR, app_id + '.json')) or None


def _store_nas_set(app_id, cfg):
    os.makedirs(STORE_NAS_DIR, exist_ok=True)
    p = os.path.join(STORE_NAS_DIR, app_id + '.json')
    if cfg is None:
        try:
            os.remove(p)
        except OSError:
            pass
    else:
        _lic_write_json(p, cfg)


def store_nas_choices():
    """Comptes et dossiers proposés à la fiche d'une app qui demande l'accès au NAS."""
    users = []
    try:
        rows = _store_midclt('user.query', [['builtin', '=', False], ['locked', '=', False]],
                             {'select': ['username', 'full_name', 'uid']}, timeout=60) or []
        users = [{'username': r['username'], 'full_name': r.get('full_name') or '', 'uid': r.get('uid')}
                 for r in rows if isinstance(r, dict) and r.get('username')]
    except Exception as e:
        log.warning('Magasin : comptes du NAS illisibles : %s', e)
    out, _e, _c = _px_host('zfs list -H -o name,mountpoint -t filesystem 2>/dev/null', timeout=60)
    datasets = []
    for line in (out or '').splitlines():
        name, _s, mp = line.partition('\t')
        name, mp = name.strip(), mp.strip()
        # Dossiers cachés (/mnt/.truenas_containers… : disques des conteneurs et machines virtuelles) : jamais proposés.
        if not mp.startswith('/mnt/') or '/.' in mp or _STORE_NAS_SKIP.search(name) or name.count('/') > 3 or '/' not in name:
            continue
        if _store_ds_private(name):
            continue
        datasets.append({'name': name, 'path': mp})
    datasets.sort(key=lambda d: d['path'].lower())
    return {'users': users, 'datasets': datasets}


def _store_nas_ids(username):
    out, err, code = _px_host('id -u %s && id -g %s && id -G %s' % (shq(username), shq(username), shq(username)), timeout=20)
    lines = (out or '').split()
    if code != 0 or len(lines) < 3 or not all(x.isdigit() for x in lines):
        raise ValueError('Compte introuvable sur le NAS : ' + username)
    uid, gid = int(lines[0]), int(lines[1])
    if uid == 0:
        raise ValueError('Le compte root ne peut pas être prêté à une app.')
    groups = sorted({int(x) for x in lines[2:]} - {gid})
    return uid, gid, groups


def store_nas_validate(app, body):
    """Configuration d'accès à partir du choix de l'administrateur ; ValueError avec un message lisible sinon."""
    want = (app or {}).get('nas_access') or {}
    nas = body if isinstance(body, dict) else {}
    user = str(nas.get('user') or '').strip()
    paths = [str(p).rstrip('/') for p in (nas.get('paths') or []) if str(p).strip()][:20]
    if not user:
        raise ValueError('Choisissez le compte dont l’app prendra les droits.')
    if not paths:
        raise ValueError('Cochez au moins un dossier.')
    ch = store_nas_choices()
    if user not in [u['username'] for u in ch['users']]:
        raise ValueError('Compte non proposé : ' + user)
    allowed = {d['path'] for d in ch['datasets']}
    bad = [p for p in paths if p not in allowed]
    if bad:
        raise ValueError('Dossier non proposé : ' + ', '.join(bad))
    uid, gid, groups = _store_nas_ids(user)
    mode = 'ro' if (str(want.get('mode') or 'ro') != 'rw' or nas.get('readonly')) else 'rw'
    mounts, seen = [], set()
    for p in paths:
        base = re.sub(r'[^A-Za-z0-9_.-]', '_', p.rsplit('/', 1)[-1]) or 'dossier'
        label, i = base, 2
        while label.lower() in seen:
            label = '%s-%d' % (base, i); i += 1
        seen.add(label.lower())
        mounts.append({'path': p, 'label': label, 'mount': '/nas/' + label})
    return {'user': user, 'uid': uid, 'gid': gid, 'groups': groups, 'mode': mode, 'mounts': mounts,
            'at': int(_st_time.time())}


def _store_nas_apply(svc, env, app_id, app=None):
    """Ajoute au service d'une app ses dossiers du NAS et l'identité du compte prêté."""
    cfg = _store_nas_get(app_id)
    if not cfg:
        return None
    svc['user'] = '%d:%d' % (int(cfg['uid']), int(cfg['gid']))
    ue = (app or {}).get('nas_user_env') or {}
    if re.match(r'^[A-Z_][A-Z0-9_]*$', str(ue.get('uid') or '')) and re.match(r'^[A-Z_][A-Z0-9_]*$', str(ue.get('gid') or '')):
        # Image qui démarre en root puis change de compte elle-même (s6-overlay : PhotoPrism) : on lui donne le compte prêté.
        svc['user'] = '0:0'
        env[str(ue['uid'])] = str(int(cfg['uid']))
        env[str(ue['gid'])] = str(int(cfg['gid']))
    if cfg.get('groups'):
        svc['group_add'] = [str(int(g)) for g in cfg['groups']]
    flag = 'ro' if cfg.get('mode') != 'rw' else 'rw'
    for m in cfg.get('mounts') or []:
        svc['volumes'].append('%s:%s:%s' % (m['path'], m['mount'], flag))
    env['DESKTROO_NAS'] = json.dumps([{'label': m['label'], 'mount': m['mount'], 'path': m['path'], 'mode': flag}
                                      for m in cfg.get('mounts') or []], ensure_ascii=False)
    env['DESKTROO_NAS_USER'] = cfg.get('user') or ''
    return cfg


def _store_nas_all(app):
    """« Tout le NAS, sans réglage » (Antivirus) : l'app lit tous les dossiers, en lecture seule, et confie
    à Desktroo les déplacements (quarantaine). Ni compte à choisir, ni dossiers à cocher, ni droits à vérifier."""
    return str(((app or {}).get('nas_access') or {}).get('mode') or '') == 'all'


def _store_nas_all_datasets():
    """Tous les dossiers proposables du NAS (même filtre que le choix manuel), et ceux du plus haut niveau."""
    ds = [d['path'] for d in store_nas_choices()['datasets'] if ':' not in d['path']]
    tops = [p for p in ds if not any(p != q and p.startswith(q + '/') for q in ds)]
    return ds, tops


def _store_nas_apply_all(svc, env, app_id):
    ds, tops = _store_nas_all_datasets()
    # root dans le conteneur, mais sans aucun privilège sauf la lecture : CAP_DAC_READ_SEARCH lit tout fichier,
    # et chaque dossier est monté en lecture seule (chacun explicitement, les sous-dossiers compris).
    svc['user'] = '0:0'
    svc['cap_add'] = ['DAC_READ_SEARCH']
    for p in ds:
        svc['volumes'].append('%s:/nas%s:ro' % (p, p[len('/mnt'):]))
    seen, mounts = set(), []
    for p in tops:
        base = re.sub(r'[^A-Za-z0-9_.-]', '_', p.rsplit('/', 1)[-1]) or 'dossier'
        label, i = base, 2
        while label.lower() in seen:
            label = '%s-%d' % (base, i); i += 1
        seen.add(label.lower())
        mounts.append({'label': label, 'mount': '/nas' + p[len('/mnt'):], 'path': p, 'mode': 'ro'})
    env['DESKTROO_NAS'] = json.dumps(mounts, ensure_ascii=False)
    env['DESKTROO_NAS_MODE'] = 'all'
    env['DESKTROO_NAS_USER'] = ''
    env['DESKTROO_FILEOPS'] = 'http://desktroo-fileops:%d' % PORT


def _store_app_quarantine_dir(app):
    for v in app.get('volumes') or []:
        if str(v.get('mount', '')) == '/data' and re.match(r'^[a-z0-9][a-z0-9_-]{0,30}$', str(v.get('name', ''))):
            return '/mnt/%s/%s/quarantine' % (_store_dataset(app['id']), v['name'])
    return None


def store_sms_hook():
    """MDM-STORE-SMS-V1 : l'app SMS reçoit les alertes du NAS comme un webhook Slack, sur un port ouvert au réseau local
    (host_ports, vers son port 8081) et un jeton propre à cette installation. Rend l'adresse à donner à TrueNAS."""
    app_id = 'sms'
    app = store_app_entry(app_id)
    if not app or app_id not in store_installed_get():
        return {'ok': False, 'installed': False}
    hp = [x for x in _store_host_ports(app) if x['to'] == 8081 and x['proto'] in ('tcp', 'both')]
    if not hp:
        return {'ok': False, 'installed': True}
    ip = _store_bind_ip() or str(_truenas_ip() or SSH_HOST or '')
    return {'ok': True, 'installed': True, 'url': 'http://%s:%d/hook/%s' % (ip, hp[0]['port'], _store_derive(app_id, 'sms-hook'))}


def store_app_quarantine(handler):
    """POST /store/app/quarantine, appelé par une app « tout le NAS » (jamais par le navigateur) :
    { action: isolate|restore, qid, path }. L'app prouve son identité par son secret (X-Desktroo-Secret).
    Desktroo déplace lui-même le fichier : l'app, elle, ne peut rien modifier sur le NAS."""
    app_id = str(handler.headers.get('X-Desktroo-App', ''))
    sec = str(handler.headers.get('X-Desktroo-Secret', ''))
    if not _STORE_ID_RE.match(app_id) or not _login_hmac.compare_digest(sec.encode('utf-8'), store_app_secret(app_id).encode('utf-8')):
        return 403, {'ok': False, 'error': 'Non autorisé.'}
    app = store_app_entry(app_id)
    if not app or not _store_nas_all(app) or app_id not in store_installed_get():
        return 403, {'ok': False, 'error': 'Non autorisé.'}
    try:
        n = int(handler.headers.get('Content-Length', 0) or 0)
        b = json.loads(handler.rfile.read(min(n, 16384)).decode('utf-8')) if n > 0 else {}
    except Exception:
        return 400, {'ok': False, 'error': 'Requête invalide.'}
    qid, path, act = str(b.get('qid') or ''), str(b.get('path') or ''), str(b.get('action') or '')
    if not re.match(r'^[0-9a-f]{32}$', qid):
        return 400, {'ok': False, 'error': 'Identifiant invalide.'}
    qdir = _store_app_quarantine_dir(app)
    if not qdir:
        return 409, {'ok': False, 'error': 'App sans dossier de données.'}
    norm = os.path.normpath(path)
    ds, _tops = _store_nas_all_datasets()
    if not norm.startswith('/mnt/') or norm != path or not any(norm.startswith(d + '/') for d in ds):
        return 400, {'ok': False, 'error': 'Fichier hors des dossiers du NAS.'}
    if os.path.realpath(os.path.dirname(norm)) != os.path.dirname(norm):
        return 400, {'ok': False, 'error': 'Chemin refusé (lien symbolique).'}
    binp, meta = os.path.join(qdir, qid + '.bin'), os.path.join(qdir, qid + '.owner')
    try:
        if act == 'isolate':
            if os.path.islink(norm) or not os.path.isfile(norm):
                return 404, {'ok': False, 'error': 'Fichier introuvable.'}
            st = os.lstat(norm)
            os.makedirs(qdir, mode=0o700, exist_ok=True)
            _sh_shutil.move(norm, binp)
            try:
                os.chmod(binp, 0o600)
            except OSError:
                pass   # dossier à ACL
            with open(meta, 'w') as f:
                json.dump({'uid': st.st_uid, 'gid': st.st_gid, 'mode': st.st_mode & 0o7777}, f)
            return 200, {'ok': True, 'size': st.st_size}
        if act == 'restore':
            if not os.path.isfile(binp):
                return 404, {'ok': False, 'error': 'Élément de quarantaine introuvable.'}
            if os.path.lexists(norm):
                return 409, {'ok': False, 'error': 'Un fichier existe déjà à cet emplacement.'}
            if not os.path.isdir(os.path.dirname(norm)):
                return 409, {'ok': False, 'error': 'Le dossier d’origine n’existe plus.'}
            _sh_shutil.move(binp, norm)
            try:
                with open(meta) as f:
                    o = json.load(f)
                os.chown(norm, int(o['uid']), int(o['gid'])); os.chmod(norm, int(o['mode']))
                os.remove(meta)
            except Exception:
                pass
            return 200, {'ok': True}
        return 400, {'ok': False, 'error': 'Action inconnue.'}
    except OSError as e:
        log.warning('Quarantaine %s : %s', app_id, e)
        return 500, {'ok': False, 'error': 'Déplacement impossible : ' + (e.strerror or str(e))}


def store_nas_update(body):
    """Change le compte ou les dossiers d'une app installée, puis la redéploie."""
    app_id = str((body or {}).get('id') or '')
    reg = store_installed_get()
    if not _STORE_ID_RE.match(app_id) or app_id not in reg:
        return 404, {'ok': False, 'error': 'App non installée.'}
    app = store_app_entry(app_id)
    if not app or not app.get('nas_access'):
        return 409, {'ok': False, 'error': 'Cette app n’utilise pas les dossiers du NAS.'}
    if _store_nas_all(app):
        return 409, {'ok': False, 'error': 'Cette app voit tout le NAS, en lecture seule : il n’y a rien à régler.'}
    if _store_running(app_id):
        return 409, {'ok': False, 'error': 'Une opération est déjà en cours sur cette app.'}
    try:
        cfg = store_nas_validate(app, body)
    except ValueError as e:
        return 400, {'ok': False, 'error': str(e)}
    _store_nas_set(app_id, cfg)
    _store_job_set(app_id, action='nas', state='running', step='Application des nouveaux accès', error='')

    def run():
        try:
            _store_prepare_data(app)
            _store_midclt('app.update', 'dt-' + app_id, {'custom_compose_config': store_compose(app)}, job=True, timeout=900)
            _store_job_set(app_id, state='done', step='Accès mis à jour', error='')
        except Exception as e:
            _store_job_set(app_id, state='error', error=_store_explain(e))
    _threading.Thread(target=run, name='store-nas-' + app_id, daemon=True).start()
    return 202, {'ok': True}


# Kit des apps (servi sous /a/_sdk/ par nginx → fileops). Embarqué ici : rien à distribuer en plus.
_STORE_SDK_JS = "/* Desktroo — kit des apps du Magasin. © 2026 Mdm-services.fr. Tous droits réservés. */\n(function () {\n  'use strict';\n  var parentWin = (window.parent && window.parent !== window) ? window.parent : null;\n  var origin = location.origin, seq = 0, pending = {}, themeFns = [];\n  function post(type, data, id) {\n    if (!parentWin) return;\n    var m = { dk: 1, type: type, data: data || {} };\n    if (id) m.id = id;\n    parentWin.postMessage(m, origin);\n  }\n  function call(type, data) {\n    return new Promise(function (resolve, reject) {\n      if (!parentWin) return reject(new Error('Cette app doit être ouverte dans Desktroo.'));\n      var id = ++seq;\n      pending[id] = { resolve: resolve, reject: reject };\n      post(type, data, id);\n      setTimeout(function () { if (pending[id]) { delete pending[id]; reject(new Error('Pas de réponse du bureau.')); } }, 600000);\n    });\n  }\n  function applyTheme(t) {\n    var r = document.documentElement, vars = (t && t.vars) || {};\n    r.setAttribute('data-theme', t && t.mode === 'light' ? 'light' : 'dark');\n    Object.keys(vars).forEach(function (k) { if (/^--[a-z0-9-]+$/i.test(k)) r.style.setProperty(k, String(vars[k])); });\n    if (t && t.lang) r.lang = t.lang;\n    themeFns.forEach(function (f) { try { f(t); } catch (_) {} });\n  }\n  window.addEventListener('message', function (e) {\n    if (e.source !== parentWin || e.origin !== origin) return;\n    var m = e.data;\n    if (!m || m.dk !== 1) return;\n    if (m.type === 'theme') applyTheme(m.data || {});\n    else if (m.type === 'reply' && pending[m.id]) {\n      var p = pending[m.id]; delete pending[m.id];\n      if (m.error) p.reject(new Error(m.error)); else p.resolve(m.data);\n    }\n  });\n  // Hors du bureau (onglet, écran d'accueil du téléphone) : le thème choisi dans Desktroo sur ce navigateur,\n  // et le manifeste qui permet d'installer l'app comme une application.\n  var appId = (location.pathname.match(/^\\/a\\/([a-z0-9][a-z0-9-]{0,40})\\//) || [])[1] || '';\n  if (!parentWin) {\n    try { var tt = localStorage.getItem('tn-theme'); if (tt === 'light' || tt === 'dark') document.documentElement.setAttribute('data-theme', tt); } catch (_) {}\n    if (appId && !document.querySelector('link[rel=\"manifest\"]')) {\n      var ml = document.createElement('link'); ml.rel = 'manifest'; ml.href = '/a/' + appId + '/desktroo.webmanifest';\n      document.head.appendChild(ml);\n      var ic = document.createElement('link'); ic.rel = 'icon'; ic.href = '/fileops/store/icon/' + appId;\n      document.head.appendChild(ic);\n    }\n  }\n  window.desktroo = {\n    inDesktroo: !!parentWin,\n    appId: appId,\n    // Ferme la session des apps sur cet appareil (hors du bureau) et revient à la page de connexion.\n    logout: function () {\n      fetch('/fileops/store/logout', { credentials: 'same-origin', cache: 'no-store' }).catch(function () {}).then(function () {\n        location.replace('/a/_login?next=' + encodeURIComponent('/a/' + appId + '/'));\n      });\n    },\n    setTitle: function (t) { post('setTitle', { title: String(t || '') }); },\n    notify: function (text, level) { post('notify', { text: String(text || ''), level: level || 'info' }); },\n    close: function () { post('close'); },\n    user: function () { return call('user'); },\n    pickFile: function (opts) { return call('pickFile', opts || {}); },\n    onTheme: function (f) { if (typeof f === 'function') themeFns.push(f); }\n  };\n  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { post('ready', { title: document.title }); });\n  else post('ready', { title: document.title });\n})();\n"
_STORE_SDK_CSS = "/* Desktroo — kit de style des apps du Magasin. © 2026 Mdm-services.fr. Tous droits réservés.\n   Les valeurs ci-dessous servent hors du bureau ; dans Desktroo, le bureau envoie les siennes (thème, accent). */\n:root {\n  --accent: #3ecf8e; --accent-rgb: 62,207,142; --accent2: #55dca0; --accent-ink: #04231a;\n  --bg: #0a1220; --surface: #0f1a2e; --surface-2: #16243f;\n  --text: #e9eef7; --text-dim: #9db0d0; --glass-border: rgba(255,255,255,0.09);\n  --danger: #e05252; --success: #3ecf8e; --warning: #f59e0b;\n  --radius: 16px; --radius-sm: 11px;\n  --font-body: \"Figtree\", system-ui, -apple-system, \"Segoe UI\", sans-serif;\n  --font-display: \"Schibsted Grotesk\", system-ui, -apple-system, sans-serif;\n  --font-mono: ui-monospace, \"SF Mono\", Menlo, Consolas, monospace;\n  color-scheme: dark;\n}\n:root[data-theme=\"light\"] {\n  --accent: #0a8451; --accent-rgb: 10,132,81; --accent2: #0a8451; --accent-ink: #ffffff;\n  --bg: #eef3fb; --surface: #ffffff; --surface-2: #f3f7fc;\n  --text: #1a2440; --text-dim: #5a6b8a; --glass-border: rgba(0,0,0,0.15);\n  color-scheme: light;\n}\n:root[data-theme=\"dark\"] { --surface: #0f1a2e; --surface-2: #16243f; }\n@media (prefers-color-scheme: light) {\n  :root:not([data-theme]) {\n    --bg: #f4f6fa; --surface: #ffffff; --surface-2: #eef2f8;\n    --text: #14213a; --text-dim: #5b6b86; --glass-border: rgba(10,20,40,0.12);\n    color-scheme: light;\n  }\n}\n*, *::before, *::after { box-sizing: border-box; }\n/* Comme les fenêtres du bureau : pas de barre de défilement visible (le défilement reste possible). */\n* { scrollbar-width: none !important; -ms-overflow-style: none !important; }\n*::-webkit-scrollbar { width: 0 !important; height: 0 !important; display: none !important; }\nhtml, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--text); font: 14px/1.5 var(--font-body); }\nh1, h2, h3 { font-family: var(--font-display); line-height: 1.2; margin: 0 0 .5em; }\nh1 { font-size: 22px; } h2 { font-size: 17px; } h3 { font-size: 15px; }\na { color: var(--accent); }\ncode, pre, kbd { font-family: var(--font-mono); font-size: 13px; }\n.dk-muted { color: var(--text-dim); }\n.dk-page { padding: 20px; }\n.dk-toolbar { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--glass-border); }\n.dk-spacer { flex: 1; }\n.dk-card { background: var(--surface); border: 1px solid var(--glass-border); border-radius: var(--radius); padding: 16px; }\n.dk-btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 7px 14px;\n  border-radius: 999px; border: 1px solid var(--glass-border); background: rgba(255,255,255,0.06); color: var(--text);\n  font: 500 12.5px var(--font-body); line-height: 1.2; white-space: nowrap; cursor: pointer; transition: background .15s, border-color .15s, color .15s; }\n.dk-btn:hover { background: rgba(var(--accent-rgb),0.16); border-color: rgba(var(--accent-rgb),0.45); color: var(--accent2); }\n.dk-btn:focus-visible, .dk-input:focus-visible, .dk-nav:focus-visible, .dk-open:focus-visible { outline: 2px solid var(--accent2); outline-offset: 2px; }\n.dk-btn.primary, .dk-open { background: var(--accent); border-color: transparent; color: var(--accent-ink); font-weight: 600; }\n.dk-btn.primary:hover, .dk-open:hover { background: var(--accent2); color: var(--accent-ink); }\n.dk-btn.danger { color: var(--danger); }\n.dk-btn.danger:hover { background: rgba(224,82,82,0.14); border-color: rgba(224,82,82,0.45); color: var(--danger); }\n.dk-btn:disabled { opacity: .5; cursor: default; }\n[data-theme=\"light\"] .dk-btn:not(.primary) { background: #ffffff; border-color: rgba(0,0,0,0.14); box-shadow: 0 1px 2px rgba(0,0,0,0.06); }\n[data-theme=\"light\"] .dk-btn:not(.primary):hover { background: #ecf8f2; border-color: rgba(var(--accent-rgb),0.45); color: var(--accent); }\n.dk-input { width: 100%; min-height: 36px; padding: 0 12px; border-radius: var(--radius-sm); border: 1px solid var(--glass-border);\n  background: var(--surface-2); color: var(--text); font: 14px var(--font-body); }\n.dk-list { list-style: none; margin: 0; padding: 0; }\n.dk-list > li { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-bottom: 1px solid var(--glass-border); }\n.dk-list > li:last-child { border-bottom: 0; }\n\n/* ── Gabarit des fenêtres Desktroo : colonne de navigation à gauche, en-tête, contenu ──────────────\n   <div class=\"dk-shell\"><nav class=\"dk-side\">…<button class=\"dk-nav active\">…</button></nav>\n   <section class=\"dk-main\"><header class=\"dk-head\"><div class=\"dk-head-text\"><h1 class=\"dk-title\">…</h1>\n   <div class=\"dk-sub\">…</div></div><div class=\"dk-head-actions\">…</div></header><div class=\"dk-body\">…</div></section></div> */\n.dk-shell { display: flex; height: 100vh; min-height: 0; }\n.dk-side { width: 236px; flex-shrink: 0; display: flex; flex-direction: column; gap: 2px; padding: 10px 10px 14px; overflow-y: auto;\n  scrollbar-width: none; background: rgba(0,0,0,0.18); border-right: 1px solid rgba(255,255,255,0.07); }\n.dk-side::-webkit-scrollbar { display: none; }\n.dk-side-title { display: flex; align-items: center; justify-content: space-between; padding: 8px 10px 6px; font-size: 10px;\n  font-weight: 700; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.8px; }\n.dk-side-sep { height: 1px; flex-shrink: 0; margin: 10px 10px 4px; background: rgba(255,255,255,0.07); }\n.dk-side-fill { flex: 1 1 auto; min-height: 8px; }\n.dk-side-note { padding: 6px 10px 0; font-size: 11px; color: var(--text-dim); }\n.dk-nav { display: flex; align-items: center; gap: 10px; width: 100%; flex-shrink: 0; padding: 8px 10px; border: 0;\n  border-radius: var(--radius-sm); background: none; color: var(--text); font: inherit; font-size: 13px; text-align: left;\n  cursor: pointer; transition: background .15s, color .15s; }\n.dk-nav:hover { background: rgba(255,255,255,0.07); }\n.dk-nav.active { background: rgba(var(--accent-rgb),0.18); color: var(--accent2); }\n.dk-nav.add { color: var(--accent2); }\n.dk-nav-ic { width: 18px; flex-shrink: 0; text-align: center; opacity: .85; }\n.dk-nav-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }\n.dk-count { padding: 1px 7px; border-radius: 999px; background: rgba(255,255,255,0.08); font-size: 10px; font-weight: 600; color: var(--text-dim); }\n.dk-count:empty { display: none; }\n.dk-main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; }\n/* Fenêtre étroite : les actions passent sous le titre au lieu de l'écraser. */\n.dk-head { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 14px; flex-shrink: 0; padding: 20px 28px 14px; border-bottom: 1px solid rgba(255,255,255,0.08); }\n.dk-head-text { flex: 1 1 220px; min-width: 0; }\n.dk-title { margin: 0; font-family: var(--font-display); font-size: 21px; font-weight: 700; letter-spacing: -0.01em; color: var(--text);\n  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }\n.dk-sub { margin-top: 3px; font-size: 12.5px; color: var(--text-dim); line-height: 1.45; }\n.dk-head-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; flex: 0 1 auto; max-width: 100%; }\n.dk-body { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 18px 28px 28px; }\n.dk-open { display: inline-flex; align-items: center; gap: 7px; padding: 8px 15px; border-radius: 999px; font: 600 12.5px var(--font-body); cursor: pointer; }\n.dk-group-title { display: flex; align-items: center; gap: 12px; margin: 22px 0 8px; font-size: 12px; font-weight: 600; color: var(--text-dim); }\n.dk-group-title::after { content: ''; flex: 1; min-width: 24px; height: 1px; background: rgba(255,255,255,0.08); }\n.dk-group { border: 1px solid var(--glass-border); border-radius: 12px; background: rgba(255,255,255,0.03); }\n[data-theme=\"light\"] .dk-side { background: rgba(13,27,53,0.045); border-right-color: rgba(0,0,0,0.09); }\n[data-theme=\"light\"] .dk-side-sep, [data-theme=\"light\"] .dk-group-title::after { background: rgba(0,0,0,0.09); }\n[data-theme=\"light\"] .dk-nav:hover { background: rgba(0,0,0,0.055); }\n[data-theme=\"light\"] .dk-nav.active { background: rgba(var(--accent-rgb),0.2); }\n[data-theme=\"light\"] .dk-count { background: rgba(0,0,0,0.07); }\n[data-theme=\"light\"] .dk-head { border-bottom-color: rgba(0,0,0,0.09); }\n[data-theme=\"light\"] .dk-group { background: rgba(255,255,255,0.8); border-color: rgba(0,0,0,0.12); }\n@media (max-width: 760px) {\n  .dk-shell { flex-direction: column; height: auto; min-height: 100vh; }\n  .dk-side { width: auto; flex-direction: row; flex-wrap: wrap; gap: 4px; padding: 8px; border-right: 0; border-bottom: 1px solid rgba(255,255,255,0.07); }\n  .dk-side-title, .dk-side-sep, .dk-side-fill, .dk-side-note { display: none; }\n  .dk-nav { width: auto; padding: 6px 10px; }\n  .dk-head { flex-wrap: wrap; padding: 14px 16px 10px; }\n  .dk-head-actions { flex-wrap: wrap; }\n  .dk-body { padding: 12px 16px 20px; }\n}\n"
_STORE_SDK = {
    'desktroo-app.js': ('application/javascript; charset=utf-8', _STORE_SDK_JS.encode('utf-8')),
    'desktroo-app.css': ('text/css; charset=utf-8', _STORE_SDK_CSS.encode('utf-8')),
}
# ── MDM-STORE-V1 (fin) ──────────────────────────────────────────────────────────────────────────


# ── MDM-OFFICE-V1 : bureautique du Magasin (OnlyOffice, ou Euro-Office plus tard) dans les fenêtres du bureau ──────
# Le serveur de documents (dt-onlyoffice, réseau des apps) n'édite que ce que fileops lui confie : une adresse signée
# pour lire le fichier (/office/file/<billet>) et une adresse de retour pour l'enregistrer (/office/save/<billet>),
# toutes deux sur desktroo-fileops, jamais exposées au navigateur. Chaque échange est signé (JWT HS256) avec un secret
# propre à l'installation (_store_derive(app, 'jwt')), posé aussi dans le conteneur (JWT_SECRET=@derive:jwt).
import zipfile as _of_zip, io as _of_io, uuid as _of_uuid

OFFICE_APP_IDS = ('onlyoffice', 'eurooffice')        # même API (DocsAPI) : l'une ou l'autre, installée par le Magasin
OFFICE_PORT = 80                                       # port interne du serveur de documents
OFFICE_SELF = 'http://desktroo-fileops:%d' % PORT      # fileops vu depuis le réseau des apps
OFFICE_TTL = 7 * 86400                                 # validité des billets (une séance d'édition peut durer)
OFFICE_MAX = 1024 * 1024 * 1024
OFFICE_KINDS = {   # extension → (type de document, modifiable)
    'docx': ('word', True), 'docm': ('word', True), 'dotx': ('word', True), 'odt': ('word', True), 'ott': ('word', True),
    'doc': ('word', False), 'rtf': ('word', False),
    'xlsx': ('cell', True), 'xlsm': ('cell', True), 'xltx': ('cell', True), 'ods': ('cell', True), 'ots': ('cell', True),
    'xls': ('cell', False),
    'pptx': ('slide', True), 'pptm': ('slide', True), 'potx': ('slide', True), 'odp': ('slide', True), 'otp': ('slide', True),
    'ppt': ('slide', False),
}
_OFFICE_KEY = _st_hmac.new(_STORE_SECRET, b'desktroo-office-v1', _st_hashlib.sha256).digest()
_OFFICE_LOCK = _threading.Lock()
_OFFICE_SESS_FILE = os.path.join(STORE_DIR, 'office-sessions.json')
_office_cache = {'t': 0.0, 'v': None}


def _office_jwt(payload, secret):
    head = _store_b64e(json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode('utf-8'))
    body = _store_b64e(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    sig = _st_hmac.new(secret.encode('utf-8'), (head + '.' + body).encode('ascii'), _st_hashlib.sha256).digest()
    return head + '.' + body + '.' + _store_b64e(sig)


def _office_jwt_read(token, secret):
    """Contenu d'un JWT HS256 signé avec « secret », sinon None (signature, algorithme ou échéance refusés)."""
    try:
        head, body, sig = str(token or '').strip().split('.')
        if json.loads(_store_b64d(head).decode('utf-8')).get('alg') != 'HS256':
            return None
        want = _st_hmac.new(secret.encode('utf-8'), (head + '.' + body).encode('ascii'), _st_hashlib.sha256).digest()
        if not _st_hmac.compare_digest(_store_b64d(sig), want):
            return None
        d = json.loads(_store_b64d(body).decode('utf-8'))
        if not isinstance(d, dict) or (d.get('exp') and float(d['exp']) < _st_time.time()):
            return None
        return d
    except Exception:
        return None


def _office_ticket(path, app, user):
    raw = json.dumps([os.path.abspath(path), app, str(user or '')[:64], int(_st_time.time()) + OFFICE_TTL],
                     ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return _store_b64e(_st_hmac.new(_OFFICE_KEY, raw, _st_hashlib.sha256).digest()[:16] + raw)


def _office_ticket_read(tok):
    """(chemin, app, utilisateur) d'un billet valide, sinon None."""
    try:
        blob = _store_b64d(tok)
        sig, raw = blob[:16], blob[16:]
        if not _st_hmac.compare_digest(sig, _st_hmac.new(_OFFICE_KEY, raw, _st_hashlib.sha256).digest()[:16]):
            return None
        path, app, user, exp = json.loads(raw.decode('utf-8'))
        if int(exp) < _st_time.time() or app not in OFFICE_APP_IDS:
            return None
        return str(path), str(app), str(user)
    except Exception:
        return None


def _office_ext(path):
    return os.path.basename(str(path)).rsplit('.', 1)[-1].lower() if '.' in os.path.basename(str(path)) else ''


def _office_path_ok(path):
    p = os.path.abspath(str(path or ''))
    return p if (p.startswith('/mnt/') and '/../' not in p + '/') else None


def office_status(force=False):
    """Bureautique installée par le Magasin, et en marche ? (lu au plus toutes les 15 s)"""
    now = _st_time.time()
    if not force and _office_cache['v'] is not None and now - _office_cache['t'] < 15:
        return _office_cache['v']
    reg = store_installed_get()
    app = next((a for a in OFFICE_APP_IDS if a in reg), '')
    out = {'ok': True, 'app': app, 'installed': bool(app), 'running': False, 'busy': False,
           'kinds': {k: v[0] for k, v in OFFICE_KINDS.items()}, 'edit': sorted(k for k, v in OFFICE_KINDS.items() if v[1])}
    if app:
        rec = reg.get(app) or {}
        out.update(name=rec.get('name') or {}, version=rec.get('version') or '', busy=_store_running(app))
        try:
            out['running'] = bool(_store_container_info('dt-' + app)[1])
        except Exception:
            out['running'] = False
        try:
            out['idle'] = store_idle_get(app)
            out['waking'] = bool((_store_waking.get(app) or {}).get('busy'))
        except Exception:
            out['idle'] = None
    _office_cache.update(t=now, v=out)
    return out


def _office_sessions():
    d = _lic_read_json(_OFFICE_SESS_FILE)
    return d if isinstance(d, dict) else {}


def _office_sessions_set(d):
    os.makedirs(STORE_DIR, exist_ok=True)
    _lic_write_json(_OFFICE_SESS_FILE, d)


def _office_doc_key(path, app):
    """Clé du document pour le serveur. Tant qu'une séance est ouverte sur ce fichier, la même clé est rendue : un second
    utilisateur rejoint la séance (coédition) au lieu d'en ouvrir une autre qui écraserait la première à l'enregistrement."""
    st = os.stat(path)
    now = _st_time.time()
    with _OFFICE_LOCK:
        s = _office_sessions()
        for k in [k for k, v in s.items() if now - float((v or {}).get('at', 0)) > 2 * 86400]:
            s.pop(k, None)
        rec = s.get(path)
        if rec and rec.get('app') == app and rec.get('key'):
            rec['at'] = now
        else:
            seed = '%s|%d|%d|%s' % (path, st.st_mtime_ns, st.st_size, app)
            rec = {'key': _st_hashlib.sha1(seed.encode('utf-8')).hexdigest()[:20] + _of_uuid.uuid4().hex[:8], 'app': app, 'at': now}
            s[path] = rec
        _office_sessions_set(s)
        return rec['key']


def _office_session_end(path, key):
    with _OFFICE_LOCK:
        s = _office_sessions()
        if (s.get(path) or {}).get('key') == key:
            s.pop(path, None)
            _office_sessions_set(s)


def _office_session_touch(path, key):
    with _OFFICE_LOCK:
        s = _office_sessions()
        if (s.get(path) or {}).get('key') == key:
            s[path]['at'] = _st_time.time()
            _office_sessions_set(s)


_OFFICE_REGION = {'fr': 'fr-FR', 'en': 'en-GB', 'es': 'es-ES', 'de': 'de-DE', 'it': 'it-IT', 'pt': 'pt-PT', 'nl': 'nl-NL'}


def office_open(handler, body):
    """Configuration signée de l'éditeur pour un fichier du NAS : la page la donne telle quelle à DocsAPI.DocEditor."""
    st = office_status()
    app = st.get('app')
    if not app:
        return 409, {'ok': False, 'error': "Aucune suite bureautique n'est installée : ajoutez OnlyOffice depuis Desktroo Apps."}
    if not st.get('running'):
        return 409, {'ok': False, 'error': "OnlyOffice est arrêté : démarrez-le dans Desktroo Apps."}
    path = _office_path_ok((body or {}).get('path'))
    if not path or not os.path.isfile(path):
        return 404, {'ok': False, 'error': 'Fichier introuvable.'}
    ext = _office_ext(path)
    if ext not in OFFICE_KINDS:
        return 400, {'ok': False, 'error': 'Ce type de fichier ne s’ouvre pas dans OnlyOffice.'}
    dtype, editable = OFFICE_KINDS[ext]
    if editable and not os.access(path, os.W_OK):
        editable = False
    user = _px_by(handler) or 'admin'
    store_activity_touch(app)
    lang = re.sub(r'[^a-zA-Z-]', '', str((body or {}).get('lang') or 'fr'))[:10] or 'fr'
    tok = _office_ticket(path, app, user)
    key = _office_doc_key(path, app)
    name = os.path.basename(path)
    cfg = {
        'documentType': dtype,
        'document': {
            'fileType': ext, 'key': key, 'title': name, 'url': OFFICE_SELF + '/office/file/' + tok,
            'info': {'folder': os.path.dirname(path)},
            'permissions': {'edit': editable, 'download': True, 'print': True, 'comment': editable, 'review': editable,
                            'fillForms': editable, 'modifyFilter': True, 'modifyContentControl': True, 'copy': True},
        },
        'editorConfig': {
            'mode': 'edit' if editable else 'view',
            'lang': lang, 'region': _OFFICE_REGION.get(lang.split('-')[0], 'en-GB'),
            'callbackUrl': OFFICE_SELF + '/office/save/' + tok,
            'user': {'id': re.sub(r'[^A-Za-z0-9._@-]', '_', user)[:64], 'name': user},
            'customization': {
                'autosave': True, 'forcesave': True, 'compactHeader': False, 'feedback': False, 'help': True,
                'integrationMode': 'embed', 'uiTheme': 'theme-light' if (body or {}).get('theme') == 'light' else 'theme-dark',
                'close': {'visible': True, 'text': str((body or {}).get('close_text') or 'Fermer')[:40]},
                'mobile': {'forceView': False}, 'mobileForceView': False,
            },
        },
    }
    secret = _store_derive(app, 'jwt')
    cfg['token'] = _office_jwt(cfg, secret)
    return 200, {'ok': True, 'app': app, 'config': cfg, 'editable': editable,
                 'api': '/a/%s/web-apps/apps/api/documents/api.js' % app}


def _dl_ticket(path, ttl=6 * 3600):
    raw = json.dumps([os.path.abspath(str(path)), int(_px_time.time()) + int(ttl)], ensure_ascii=False).encode('utf-8')
    sig = _px_hmac.new(_PX_MEDIA_KEY, b'dl' + raw, _px_hash.sha256).digest()[:16]
    return _px_b64.urlsafe_b64encode(sig + raw).decode('ascii').rstrip('=')


def _dl_ticket_path(tok):
    try:
        blob = _px_b64.urlsafe_b64decode(str(tok) + '=' * (-len(str(tok)) % 4))
        sig, raw = blob[:16], blob[16:]
        if not _px_hmac.compare_digest(sig, _px_hmac.new(_PX_MEDIA_KEY, b'dl' + raw, _px_hash.sha256).digest()[:16]):
            return None
        p, exp = json.loads(raw.decode('utf-8'))
        return str(p) if int(exp) >= _px_time.time() else None
    except Exception:
        return None


def _dl_serve(handler, tok):
    """Téléchargement direct d'un fichier du NAS vers le navigateur (lien signé, sans jeton dans l'adresse)."""
    from urllib.parse import quote as _q
    path = _dl_ticket_path(tok)
    if not path or not os.path.isfile(path):
        handler.send_response(404)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return
    size = os.path.getsize(path)
    name = os.path.basename(path)
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/octet-stream')
    handler.send_header('Content-Length', str(size))
    handler.send_header('Content-Disposition', "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                        % (name.encode('ascii', 'replace').decode('ascii').replace('"', '_'), _q(name)))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    try:
        with open(path, 'rb') as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass


def office_file_serve(handler, tok):
    t = _office_ticket_read(tok)
    path = t and _office_path_ok(t[0])
    if not path or not os.path.isfile(path):
        handler.send_response(404)
        handler.send_header('Content-Length', '0')
        handler.end_headers()
        return
    size = os.path.getsize(path)
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/octet-stream')
    handler.send_header('Content-Length', str(size))
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _office_fetch(app, url, dest):
    """Télécharge le document enregistré par le serveur. L'adresse qu'il donne est celle vue par le navigateur
    (https://<domaine>/a/<app>/cache/…) : on n'en garde que le chemin, demandé directement à dt-<app>."""
    import urllib.request
    from urllib.parse import urlsplit
    u = urlsplit(str(url or ''))
    path = u.path or '/'
    pre = '/a/%s/' % app
    if path.startswith(pre):
        path = path[len(pre) - 1:]
    if not path.startswith('/') or '..' in path:
        raise ValueError('adresse refusée')
    src = 'http://dt-%s:%d%s%s' % (app, OFFICE_PORT, path, ('?' + u.query) if u.query else '')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    n = 0
    with opener.open(urllib.request.Request(src, headers={'User-Agent': 'Desktroo'}), timeout=120) as r, open(dest, 'wb') as f:
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            n += len(chunk)
            if n > OFFICE_MAX:
                raise ValueError('document trop volumineux')
            f.write(chunk)
    if n == 0:
        raise ValueError('document vide')
    return n


def _office_free_name(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = '%s (%d)%s' % (base, i, ext)
        if not os.path.exists(cand):
            return cand
    return base + '-' + _of_uuid.uuid4().hex[:6] + ext


def _office_write(app, url, target, by=''):
    """Remplace le fichier par la version enregistrée, de façon atomique, en gardant propriétaire et droits."""
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):   # dossier disparu pendant l'édition : rien n'est perdu
        parent = os.path.join(STORE_DIR, 'office-rescue')
        os.makedirs(parent, exist_ok=True)
        target = os.path.join(parent, os.path.basename(target))
    tmp = os.path.join(parent, '.%s.desktroo-%s' % (os.path.basename(target)[:80], _of_uuid.uuid4().hex[:8]))
    try:
        size = _office_fetch(app, url, tmp)
        try:
            st = os.stat(target)
            os.chown(tmp, st.st_uid, st.st_gid)
            os.chmod(tmp, st.st_mode & 0o7777)
        except FileNotFoundError:
            try:
                pst = os.stat(parent)
                os.chown(tmp, pst.st_uid, pst.st_gid)
                os.chmod(tmp, 0o664)
            except OSError:
                pass
        except OSError:
            pass
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    _px_log('office.save', by, path=target, size=size)
    return target


def office_save(handler, tok):
    """Adresse de retour du serveur de documents. Répond toujours {"error": 0} une fois la demande vérifiée et traitée,
    {"error": 1} si elle est refusée ou si l'enregistrement échoue (le serveur garde alors le document et réessaie)."""
    def reply(err):
        b = json.dumps({'error': err}).encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(b)))
        handler.end_headers()
        handler.wfile.write(b)
    t = _office_ticket_read(tok)
    if not t:
        return reply(1)
    path, app, user = t
    store_activity_touch(app)
    try:
        n = int(handler.headers.get('Content-Length', 0) or 0)
        raw = json.loads(handler.rfile.read(min(n, 2 * 1024 * 1024)).decode('utf-8')) if n > 0 else {}
    except Exception:
        return reply(1)
    secret = _store_derive(app, 'jwt')
    data = None
    if isinstance(raw, dict) and raw.get('token'):
        data = _office_jwt_read(raw['token'], secret)
    if data is None:
        auth = str(handler.headers.get('Authorization', '') or '')
        if auth.lower().startswith('bearer '):
            d = _office_jwt_read(auth[7:], secret)
            if isinstance(d, dict):
                data = d.get('payload') if isinstance(d.get('payload'), dict) else d
    if not isinstance(data, dict):
        log.warning('Bureautique : retour non signé refusé pour %s', path)
        return reply(1)
    status, key = int(data.get('status') or 0), str(data.get('key') or '')
    try:
        if status == 1:
            _office_session_touch(path, key)
        elif status in (2, 6):
            target = path
            ft = str(data.get('filetype') or '').lower()
            if ft and ft != _office_ext(path):   # format changé par le serveur : enregistré à côté, l'original reste
                target = _office_free_name(os.path.splitext(path)[0] + '.' + re.sub(r'[^a-z0-9]', '', ft)[:6])
            who = ','.join(str(x) for x in (data.get('users') or [])[:5]) or user
            _office_write(app, data.get('url'), target, by=who)
            if status == 2:
                _office_session_end(path, key)
        elif status in (3, 7):
            log.warning('Bureautique : le serveur signale un échec d’enregistrement (%d) pour %s', status, path)
            if status == 3:
                _office_session_end(path, key)
        elif status == 4:
            _office_session_end(path, key)
    except Exception as e:
        log.warning('Bureautique : enregistrement de %s en échec : %s', path, e)
        return reply(1)
    return reply(0)


# Modèles vides (Office Open XML minimal, A4) : un nouveau document s'ouvre dans OnlyOffice comme dans Word, Excel ou PowerPoint.
_OX_CT = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>%s</Types>'
_OX_OV = '<Override PartName="%s" ContentType="application/vnd.openxmlformats-officedocument.%s+xml"/>'
_OX_RELS = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>'
_OX_REL = '<Relationship Id="%s" Type="http://schemas.openxmlformats.org/%s" Target="%s"/>'
_OX_R = 'officeDocument/2006/relationships/'
_OX_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_OX_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_OX_P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
_OX_RN = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _ox_zip(parts):
    buf = _of_io.BytesIO()
    with _of_zip.ZipFile(buf, 'w', _of_zip.ZIP_DEFLATED) as z:
        for name, text in parts:
            z.writestr(name, text.encode('utf-8'))
    return buf.getvalue()


def _ox_docx(lang):
    W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    styles = (_OX_HEAD + '<w:styles xmlns:w="%s"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/>'
              '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="%s"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
              '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style></w:styles>') % (W, lang)
    doc = (_OX_HEAD + '<w:document xmlns:w="%s"><w:body><w:p/><w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1417" w:right="1417" w:bottom="1417" w:left="1417" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>') % W
    return _ox_zip([
        ('[Content_Types].xml', _OX_CT % (_OX_OV % ('/word/document.xml', 'wordprocessingml.document.main') + _OX_OV % ('/word/styles.xml', 'wordprocessingml.styles'))),
        ('_rels/.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'officeDocument', 'word/document.xml'))),
        ('word/_rels/document.xml.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'styles', 'styles.xml'))),
        ('word/document.xml', doc), ('word/styles.xml', styles)])


def _ox_xlsx(lang):
    S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    sheet = {'fr': 'Feuille1', 'es': 'Hoja1', 'de': 'Tabelle1', 'it': 'Foglio1'}.get(lang.split('-')[0], 'Sheet1')
    styles = (_OX_HEAD + '<styleSheet xmlns="%s"><fonts count="1"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>') % S
    return _ox_zip([
        ('[Content_Types].xml', _OX_CT % (_OX_OV % ('/xl/workbook.xml', 'spreadsheetml.sheet.main') + _OX_OV % ('/xl/worksheets/sheet1.xml', 'spreadsheetml.worksheet')
                                          + _OX_OV % ('/xl/styles.xml', 'spreadsheetml.styles'))),
        ('_rels/.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'officeDocument', 'xl/workbook.xml'))),
        ('xl/_rels/workbook.xml.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'worksheet', 'worksheets/sheet1.xml') + _OX_REL % ('rId2', _OX_R + 'styles', 'styles.xml'))),
        ('xl/workbook.xml', _OX_HEAD + '<workbook xmlns="%s" xmlns:r="%s"><sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>' % (S, _OX_RN, sheet)),
        ('xl/worksheets/sheet1.xml', _OX_HEAD + '<worksheet xmlns="%s"><sheetData/></worksheet>' % S),
        ('xl/styles.xml', styles)])


def _ox_sp(pid, name, ph, x, y, cx, cy, txt=''):
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr>%s</p:nvPr></p:nvSpPr>'
            '<p:spPr>%s</p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p>%s<a:endParaRPr lang="fr-FR"/></a:p></p:txBody></p:sp>') % (
        pid, name, ph, ('<a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>' % (x, y, cx, cy)) if cx else '', txt)


def _ox_pptx(lang):
    ns = 'xmlns:a="%s" xmlns:r="%s" xmlns:p="%s"' % (_OX_A, _OX_RN, _OX_P)
    tree = '<p:cSld%s><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>%s</p:spTree></p:cSld>'
    grp = '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    lvl = lambda sz, f: '<a:lvl1pPr><a:defRPr sz="%d"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+%s-lt"/></a:defRPr></a:lvl1pPr>' % (sz, f)
    master = (_OX_HEAD + '<p:sldMaster %s><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree>' % ns + grp
              + _ox_sp(2, 'Title Placeholder 1', '<p:ph type="title"/>', 838200, 365125, 10515600, 1325563)
              + _ox_sp(3, 'Text Placeholder 2', '<p:ph type="body" idx="1"/>', 838200, 1825625, 10515600, 4351338)
              + '</p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" '
              'accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
              '<p:txStyles><p:titleStyle>%s</p:titleStyle><p:bodyStyle>%s</p:bodyStyle><p:otherStyle>%s</p:otherStyle></p:txStyles></p:sldMaster>' % (
                  lvl(4400, 'mj'), lvl(2800, 'mn'), lvl(1800, 'mn')))
    layout = (_OX_HEAD + '<p:sldLayout %s type="title" preserve="1">' % ns + tree % (' name="Title Slide"',
              _ox_sp(2, 'Title 1', '<p:ph type="ctrTitle"/>', 1524000, 1122363, 9144000, 2387600)
              + _ox_sp(3, 'Subtitle 2', '<p:ph type="subTitle" idx="1"/>', 1524000, 3602038, 9144000, 1655762))
              + '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    slide = (_OX_HEAD + '<p:sld %s>' % ns + tree % ('', _ox_sp(2, 'Title 1', '<p:ph type="ctrTitle"/>', 0, 0, 0, 0)
             + _ox_sp(3, 'Subtitle 2', '<p:ph type="subTitle" idx="1"/>', 0, 0, 0, 0)) + '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
    sys_ = lambda n, c: '<a:%s><a:srgbClr val="%s"/></a:%s>' % (n, c, n)
    clr = ''.join(sys_(n, c) for n, c in (('dk1', '000000'), ('lt1', 'FFFFFF'), ('dk2', '1F2A44'), ('lt2', 'E7E9EE'), ('accent1', '1D7A55'),
                                          ('accent2', '2B6CB0'), ('accent3', 'D97706'), ('accent4', '7C3AED'), ('accent5', 'DB2777'), ('accent6', '0891B2'),
                                          ('hlink', '2B6CB0'), ('folHlink', '7C3AED')))
    font = lambda f: '<a:latin typeface="%s"/><a:ea typeface=""/><a:cs typeface=""/>' % f
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    ln = '<a:ln w="%d"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    theme = (_OX_HEAD + '<a:theme xmlns:a="%s" name="Desktroo"><a:themeElements><a:clrScheme name="Desktroo">%s</a:clrScheme>'
             '<a:fontScheme name="Desktroo"><a:majorFont>%s</a:majorFont><a:minorFont>%s</a:minorFont></a:fontScheme>'
             '<a:fmtScheme name="Desktroo"><a:fillStyleLst>%s</a:fillStyleLst><a:lnStyleLst>%s</a:lnStyleLst>'
             '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
             '<a:bgFillStyleLst>%s</a:bgFillStyleLst></a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>') % (
        _OX_A, clr, font('Calibri Light'), font('Calibri'), fill * 3, ''.join(ln % w for w in (6350, 12700, 19050)), fill * 3)
    pres = (_OX_HEAD + '<p:presentation %s saveSubsetFonts="1"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/>'
            '<p:defaultTextStyle><a:lvl1pPr><a:defRPr lang="%s"/></a:lvl1pPr></p:defaultTextStyle></p:presentation>') % (ns, lang)
    return _ox_zip([
        ('[Content_Types].xml', _OX_CT % (_OX_OV % ('/ppt/presentation.xml', 'presentationml.presentation.main')
                                          + _OX_OV % ('/ppt/slideMasters/slideMaster1.xml', 'presentationml.slideMaster')
                                          + _OX_OV % ('/ppt/slideLayouts/slideLayout1.xml', 'presentationml.slideLayout')
                                          + _OX_OV % ('/ppt/slides/slide1.xml', 'presentationml.slide') + _OX_OV % ('/ppt/theme/theme1.xml', 'theme'))),
        ('_rels/.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'officeDocument', 'ppt/presentation.xml'))),
        ('ppt/_rels/presentation.xml.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'slideMaster', 'slideMasters/slideMaster1.xml')
                                                        + _OX_REL % ('rId2', _OX_R + 'slide', 'slides/slide1.xml') + _OX_REL % ('rId3', _OX_R + 'theme', 'theme/theme1.xml'))),
        ('ppt/presentation.xml', pres),
        ('ppt/slideMasters/slideMaster1.xml', master),
        ('ppt/slideMasters/_rels/slideMaster1.xml.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'slideLayout', '../slideLayouts/slideLayout1.xml')
                                                                     + _OX_REL % ('rId2', _OX_R + 'theme', '../theme/theme1.xml'))),
        ('ppt/slideLayouts/slideLayout1.xml', layout),
        ('ppt/slideLayouts/_rels/slideLayout1.xml.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'slideMaster', '../slideMasters/slideMaster1.xml'))),
        ('ppt/slides/slide1.xml', slide),
        ('ppt/slides/_rels/slide1.xml.rels', _OX_RELS % (_OX_REL % ('rId1', _OX_R + 'slideLayout', '../slideLayouts/slideLayout1.xml'))),
        ('ppt/theme/theme1.xml', theme)])


_OFFICE_TEMPLATES = {'docx': _ox_docx, 'xlsx': _ox_xlsx, 'pptx': _ox_pptx}


def office_new(handler, body):
    """Nouveau document vide dans un dossier du NAS ; rend son chemin (la page l'ouvre aussitôt)."""
    body = body or {}
    kind = str(body.get('kind') or '')
    if kind not in _OFFICE_TEMPLATES:
        return 400, {'ok': False, 'error': 'Type de document inconnu.'}
    d = _office_path_ok(body.get('dir'))
    if not d or not os.path.isdir(d):
        return 404, {'ok': False, 'error': 'Dossier introuvable.'}
    name = str(body.get('name') or '').strip()
    if not name or name in ('.', '..') or re.search(r'[/\\\x00-\x1f]', name) or len(name) > 200:
        return 400, {'ok': False, 'error': 'Nom invalide.'}
    if _office_ext(name) != kind:
        name += '.' + kind
    path = os.path.join(d, name)
    if os.path.exists(path):
        return 409, {'ok': False, 'error': 'Un fichier porte déjà ce nom dans ce dossier.'}
    lang = {'fr': 'fr-FR', 'en': 'en-GB', 'es': 'es-ES'}.get(str(body.get('lang') or 'fr')[:2], 'fr-FR')
    data = _OFFICE_TEMPLATES[kind](lang)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        try:
            pst = os.stat(d)
            os.chown(path, pst.st_uid, pst.st_gid)
        except OSError:
            pass
    except FileExistsError:
        return 409, {'ok': False, 'error': 'Un fichier porte déjà ce nom dans ce dossier.'}
    except OSError as e:
        return 400, {'ok': False, 'error': 'Création impossible : %s' % (e.strerror or e)}
    _px_log('office.new', _px_by(handler), path=path)
    return 200, {'ok': True, 'path': path, 'name': name}
# ── MDM-OFFICE-V1 (fin) ──────────────────────────────────────────────────────────────────────────


# ── MDM-STORE-IDLE-V1 : veille des apps du Magasin ──────────────────────────────────────────────────────────────
# Une app qui le permet (« idle_stop » dans sa fiche du catalogue) s'arrête d'elle-même après un délai sans activité,
# pour libérer la mémoire, et redémarre quand on l'ouvre (icône du bureau, document, adresse directe). L'activité :
# chaque requête vers /a/<app>/ (vérifiée par /store/auth), et pour la bureautique les documents ouverts ou pas encore
# enregistrés (séances). Réglage par app dans Desktroo Apps (data/store/idle.json).
STORE_IDLE_FILE = os.path.join(STORE_DIR, 'idle.json')
STORE_IDLE_MINUTES = (5, 15, 30, 60, 120)
_STORE_IDLE_BOOT = _st_time.time()
_STORE_IDLE_LOCK = _threading.Lock()
_store_activity = {}
_store_waking = {}


def store_activity_touch(app_id):
    _store_activity[str(app_id)] = _st_time.time()


def _store_idle_spec(app):
    s = (app or {}).get('idle_stop')
    if not isinstance(s, dict):
        return None
    try:
        m = int(s.get('minutes') or 15)
    except (TypeError, ValueError):
        m = 15
    return {'default': bool(s.get('default')), 'minutes': m if m in STORE_IDLE_MINUTES else 15}


def _store_idle_all():
    d = _lic_read_json(STORE_IDLE_FILE)
    return d if isinstance(d, dict) else {}


def _store_idle_put(app_id, **kw):
    with _STORE_IDLE_LOCK:
        d = _store_idle_all()
        rec = d.get(app_id) if isinstance(d.get(app_id), dict) else {}
        for k, v in kw.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = v
        d[app_id] = rec
        os.makedirs(STORE_DIR, exist_ok=True)
        _lic_write_json(STORE_IDLE_FILE, d)


def store_idle_get(app_id, app=None):
    """Réglage de veille d'une app : {on, minutes, asleep}, ou None si l'app ne la permet pas."""
    if app is None:
        cat, _e = store_catalog()
        app = next((a for a in (cat or {}).get('apps', []) if isinstance(a, dict) and a.get('id') == app_id), None)
    spec = _store_idle_spec(app)
    if not spec:
        return None
    rec = _store_idle_all().get(app_id) or {}
    on = rec['on'] if isinstance(rec.get('on'), bool) else spec['default']
    m = rec.get('minutes') if rec.get('minutes') in STORE_IDLE_MINUTES else spec['minutes']
    return {'on': on, 'minutes': m, 'asleep': bool(rec.get('asleep'))}


def store_idle_set(body):
    app_id = str((body or {}).get('id') or '')
    if not _STORE_ID_RE.match(app_id) or app_id not in store_installed_get():
        return 404, {'ok': False, 'error': 'App non installée.'}
    if not store_idle_get(app_id):
        return 409, {'ok': False, 'error': 'Cette app ne peut pas se mettre en veille.'}
    try:
        m = int((body or {}).get('minutes') or 15)
    except (TypeError, ValueError):
        m = 15
    if m not in STORE_IDLE_MINUTES:
        return 400, {'ok': False, 'error': 'Délai inconnu.'}
    _store_idle_put(app_id, on=bool((body or {}).get('on')), minutes=m)
    store_activity_touch(app_id)   # le délai repart de maintenant
    return 200, {'ok': True, 'idle': store_idle_get(app_id)}


def _store_idle_busy(app_id):
    """Une opération du Magasin, ou (bureautique) un document ouvert ou pas encore enregistré : pas de veille."""
    if _store_running(app_id) or (_store_waking.get(app_id) or {}).get('busy'):
        return True
    if app_id in OFFICE_APP_IDS:
        now = _st_time.time()
        for rec in _office_sessions().values():
            if (rec or {}).get('app') == app_id and now - float((rec or {}).get('at', 0)) < 12 * 3600:
                return True
    return False


def _store_idle_tick():
    reg = store_installed_get()
    if not reg:
        return
    cat, _e = store_catalog()
    apps = {a.get('id'): a for a in (cat or {}).get('apps', []) if isinstance(a, dict)}
    now = _st_time.time()
    for app_id in list(reg):
        st = store_idle_get(app_id, apps.get(app_id))
        if not st or not st['on']:
            continue
        last = max(_store_activity.get(app_id, 0), _STORE_IDLE_BOOT, float((_store_waking.get(app_id) or {}).get('at', 0)))
        if now - last < st['minutes'] * 60 or _store_idle_busy(app_id):
            continue
        if not _store_container_info('dt-' + app_id)[1]:
            continue
        log.info('Veille : arrêt de %s après %d min sans activité', app_id, st['minutes'])
        try:
            _store_midclt('app.stop', 'dt-' + app_id, job=True, timeout=300)
            _store_idle_put(app_id, asleep=int(now))
            _office_cache['v'] = None
        except Exception as e:
            log.warning('Veille : arrêt de %s impossible : %s', app_id, e)


def _store_idle_loop():
    _st_time.sleep(180)
    while True:
        try:
            _store_idle_tick()
        except Exception as e:
            log.warning('Veille des apps : %s', e)
        _st_time.sleep(60)


def _store_wake_state(app_id, app):
    w = _store_waking.get(app_id) or {}
    if w.get('busy'):
        return 'starting', ''
    if not _store_container_info('dt-' + app_id)[1]:
        return ('error', w['error']) if w.get('error') else ('stopped', '')
    if _store_health(app, tries=1):
        if (_store_idle_all().get(app_id) or {}).get('asleep'):
            _store_idle_put(app_id, asleep=None)
        return 'ready', ''
    return 'starting', ''


def store_wake(body):
    """Réveille une app en veille (ou arrêtée) : rend tout de suite {state: ready | starting | error}."""
    app_id = str((body or {}).get('id') or '')
    if not _STORE_ID_RE.match(app_id) or app_id not in store_installed_get():
        return 404, {'ok': False, 'error': 'App non installée.'}
    app = store_app_entry(app_id)
    if not store_idle_get(app_id, app):
        return 409, {'ok': False, 'error': 'Cette app ne se réveille pas toute seule : démarrez-la dans Desktroo Apps.'}
    store_activity_touch(app_id)
    with _STORE_IDLE_LOCK:
        state, err = _store_wake_state(app_id, app)
        if state != 'stopped' and not (state == 'error' and (body or {}).get('start')):
            return 200, {'ok': state != 'error', 'state': state, 'error': err}
        _store_waking[app_id] = {'busy': True, 'at': _st_time.time(), 'error': ''}

    def run():
        err = ''
        try:
            store_net_ensure()
            _store_midclt('app.start', 'dt-' + app_id, job=True, timeout=600)
            _office_cache['v'] = None
            if not _store_health(app, tries=100):
                err = "L'app a démarré mais ne répond pas encore."
        except Exception as e:
            err = _store_explain(e)
            log.warning('Veille : réveil de %s en échec : %s', app_id, e)
        _store_waking[app_id] = {'busy': False, 'at': _st_time.time(), 'error': err}
        store_activity_touch(app_id)
        if not err:
            _store_idle_put(app_id, asleep=None)
            log.info('Veille : %s réveillée', app_id)
    _threading.Thread(target=run, name='store-wake-' + app_id, daemon=True).start()
    return 200, {'ok': True, 'state': 'starting', 'error': ''}


def store_wake_status(app_id):
    if not _STORE_ID_RE.match(str(app_id or '')) or app_id not in store_installed_get():
        return {'ok': False, 'state': 'error', 'error': 'App non installée.'}
    app = store_app_entry(app_id) or {'id': app_id}
    state, err = _store_wake_state(app_id, app)
    return {'ok': state != 'error', 'state': state, 'error': err}
# ── MDM-STORE-IDLE-V1 (fin) ──────────────────────────────────────────────────────────────────────


class FileOpsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _auth(self):
        tok = (self.headers.get('X-Token', '')
               or self.headers.get('X-Fileops-Token', ''))
        if not _login_hmac.compare_digest(str(tok).encode('utf-8'), TOKEN.encode('utf-8')):
            self._json(403, {'error': 'Unauthorized'})
            return False
        return True

    def _auth_login(self):
        """POST /auth/login : seule route d'écriture ouverte sans jeton. La réponse ne
        porte pas d'en-tête CORS : une page d'un autre site ne peut pas la lire."""
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(min(n, 8192)) if n > 0 else b''
            code, resp = auth_login(json.loads(raw.decode('utf-8')) if raw else {})
        except Exception:
            code, resp = 400, {'error': 'Requête invalide.'}
        body = json.dumps(resp, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        if code == 200 and isinstance(resp, dict) and resp.get('token'):   # MDM-STORE-V1 : session des apps
            self.send_header('Set-Cookie', store_cookie_header(self, store_cookie_make(resp.get('user') or 'admin'), STORE_COOKIE_TTL))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    # ── MDM-SHARE-LINKS-V1 : réponses HTML + streaming ──────────────────────
    def _html(self, code, markup):
        body = markup.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_file(self, path, filename):
        try:
            size = os.path.getsize(path)
            ctype = _sh_mimetypes.guess_type(path)[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition', _share_content_disposition(filename))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with open(path, 'rb') as f:
                _sh_shutil.copyfileobj(f, self.wfile, 256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning('share stream error: %s', e)

    def _stream_zip(self, dirpath, name):
        tmpfd, tmpzip = _tempfile.mkstemp(suffix='.zip', prefix='share-')
        os.close(tmpfd)
        try:
            base = os.path.basename(dirpath.rstrip('/')) or 'archive'
            with _sh_zipfile.ZipFile(tmpzip, 'w', _sh_zipfile.ZIP_STORED, allowZip64=True) as zf:
                for root, _dirs, files in os.walk(dirpath):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        arc = os.path.join(base, os.path.relpath(fp, dirpath))
                        try:
                            zf.write(fp, arc)
                        except OSError:
                            pass
            self._stream_file(tmpzip, (name or 'archive') + '.zip')
        finally:
            try:
                os.unlink(tmpzip)
            except OSError:
                pass

    def _share_public_get(self, pp):
        parts = [x for x in pp.path.split('/') if x]  # ['s', <token>, (download)]
        qs = dict(parse_qsl(pp.query))
        token = parts[1] if len(parts) >= 2 else ''
        action = parts[2] if len(parts) >= 3 else ''
        rec = _share_read().get(token)
        if not rec:
            return self._html(404, _share_error_page('Lien introuvable ou supprimé.'))
        if action == 'download':
            return self._serve_share_file(rec, qs.get('pw', ''), check=(qs.get('check') == '1'))
        valid, _reason = _share_valid(rec)
        return self._html(200 if valid else 410, _share_landing_page(rec))

    def _serve_share_file(self, rec, password, check=False):
        valid, reason = _share_valid(rec)
        if not valid:
            if check:
                return self._json(410, {'error': reason})
            return self._html(410, _share_error_page(reason))
        if rec.get('password') and not _share_verify_pw(password, rec['password']):
            return self._json(403, {'error': 'Mot de passe incorrect'})
        if check:
            return self._json(200, {'ok': True})
        # Incrément atomique + re-vérification de la limite sous verrou
        with _share_lock:
            data = _share_read()
            r2 = data.get(rec['token'])
            if not r2 or r2.get('revoked'):
                return self._html(410, _share_error_page('Lien révoqué.'))
            maxd = r2.get('max_downloads')
            if maxd and int(r2.get('downloads', 0)) >= int(maxd):
                return self._html(410, _share_error_page('Nombre maximum de téléchargements atteint.'))
            r2['downloads'] = int(r2.get('downloads', 0)) + 1
            data[rec['token']] = r2
            _share_write(data)
        if rec.get('is_dir'):
            self._stream_zip(rec['path'], rec.get('name', 'archive'))
        else:
            self._stream_file(rec['path'], rec.get('name', 'download'))

    def do_OPTIONS(self):
        if urlparse(self.path).path.rstrip('/') == '/auth/login':
            self.send_response(204)     # pas de CORS sur la connexion
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'X-Token, X-Fileops-Token, X-Desktroo-User, Content-Type')
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        # MDM-SHARE-LINKS-V1 : routes publiques /s/<token> (sans token fileops)
        _pp = urlparse(self.path)
        if _pp.path == '/s' or _pp.path.startswith('/s/'):
            return self._share_public_get(_pp)
        # Installation comme application : manifeste et icônes, lus par le navigateur sans jeton
        if _pp.path == '/manifest.webmanifest' or _pp.path in _PWA_ICONS:
            if _pp.path in _PWA_ICONS:
                ctype, body = _PWA_ICONS[_pp.path]
                cache = 'public, max-age=86400'
            else:
                ctype, body, cache = 'application/manifest+json; charset=utf-8', json.dumps(_pwa_manifest(), ensure_ascii=False).encode('utf-8'), 'no-cache'
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', cache)
            self.end_headers()
            self.wfile.write(body)
            return
        # MDM-FILES-PLUS-V1 : fichier lu par le navigateur, sur une adresse signée et temporaire
        if _pp.path.startswith('/media/') and not _pp.path.startswith('/media/ticket'):
            return _px_media_serve(self, _pp.path)
        # MDM-STORE-V1 : vérification pour nginx, kit des apps, fin de session — sans jeton
        if _pp.path.startswith('/store/') and _store_public_get(self, _pp.path):
            return
        # MDM-LANGPACKS-V1 : langues ajoutées, lues par l'écran de connexion, donc sans jeton
        if _pp.path.rstrip('/') == '/lang':
            try:
                self._json(200, lang_list())
            except Exception as e:
                self._json(200, {'ok': False, 'total': 0, 'langs': [], 'error': str(e)})
            return
        if _pp.path.startswith('/lang/') and _pp.path.endswith('.json'):
            return _lang_serve(self, _pp.path[6:-5])
        # Couleur du thème : lue par l'écran de connexion, donc sans jeton
        if _pp.path.rstrip('/') == '/branding':
            try:
                self._json(200, _branding_get())
            except Exception:
                self._json(200, {'accent': 'green'})
            return

        # MDM-OFFICE-V1 : le serveur de documents lit le fichier sur une adresse signée (réseau des apps), sans jeton
        if _pp.path.startswith('/office/file/'):
            return office_file_serve(self, _pp.path[len('/office/file/'):].split('/')[0])

        if _pp.path.startswith('/dl/'):
            return _dl_serve(self, _pp.path[4:].split('/')[0])

        if not self._auth():
            return
        p    = urlparse(self.path)
        path = p.path.rstrip('/')
        qs   = dict(parse_qsl(p.query))

        if path == '/dlticket':
            t = qs.get('path', '')
            if not os.path.isfile(t):
                return self._json(404, {'error': 'Fichier introuvable'})
            return self._json(200, {'ok': True, 'url': '/dl/' + _dl_ticket(t) + '/' + os.path.basename(t)})

        if path == '/office/status':   # MDM-OFFICE-V1
            try:
                return self._json(200, office_status(qs.get('force') == '1'))
            except Exception as e:
                return self._json(200, {'ok': False, 'app': '', 'installed': False, 'running': False, 'error': str(e)})

        # MDM-FILES-PLUS-V1
        if _px_get(self, path, qs):
            return

        # MDM-STORE-V1
        if path.startswith('/store/') and _store_get(self, path, qs):
            return

        # Licence d'utilisation : l'accord a-t-il été donné pour la version en cours du texte ?
        # Profil du compte : photo ou avatar
        if path in ('/profile', '/profile/photo'):
            try:
                if path == '/profile':
                    self._json(200, dict(profile_get(self), ok=True))
                else:
                    profile_photo(self)
            except ValueError as e:
                self._json(400, {'ok': False, 'error': str(e)})
            return
        if path == '/terms/status':
            self._json(200, _terms_status())
            return

        # Désinstallation : état des lieux, rien n'est modifié
        if path == '/uninstall/plan':
            try:
                self._json(200, {'ok': True, 'plan': _uninstall_plan()})
            except Exception as e:
                self._json(502, {'ok': False, 'error': str(e)})
            return

        # MDM-DKBACKUP-V1 : sauvegarde de Desktroo (état, téléchargement)
        if path == '/dkbackup/status':
            try:
                self._json(200, dkbackup_status())
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        if path == '/dkbackup/download':
            nm = dict(parse_qsl(p.query)).get('name', '')
            full = os.path.join(_dkbk_settings()['dir'], nm)
            if not DKBK_NAME_RE.match(nm) or not os.path.isfile(full):
                self._json(404, {'ok': False, 'error': 'Sauvegarde introuvable.'})
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/gzip')
            self.send_header('Content-Length', str(os.path.getsize(full)))
            self.send_header('Content-Disposition', 'attachment; filename="%s"' % nm)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                with open(full, 'rb') as fh:
                    _bk_shutil.copyfileobj(fh, self.wfile)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        # MDM-LICENSE-V1 : état de la licence / essai
        if path == '/license/status':
            try:
                try:
                    license_maybe_refresh()   # refresh en ligne en tâche de fond (throttlé)
                except Exception:
                    pass
                # Depuis la 3.0.0, Desktroo ne contacte plus desktroo.fr de lui-meme.
                # Le comptage des installations repose uniquement sur les clés d'activation
                # demandees volontairement : license_maybe_refresh() ci-dessus ne part que
                # si une cle a ete activee. license_trial_sync() reste disponible mais
                # n'est plus appele automatiquement.
                st = license_status()
                st['subscribe_url'] = SUBSCRIBE_URL
                st['manage_url'] = MANAGE_URL
                self._json(200, st)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : liste des sites web (authentifié)
        if path == '/websites':
            try:
                data = _web_read()
                items = []
                for sid, s in data.items():
                    if not isinstance(s, dict):
                        continue
                    it = _web_public(s, sid)
                    if not it.get('app'):
                        try:
                            det = _web_detect_app(s)
                            if det:
                                it['app'] = det
                        except Exception:
                            pass
                    it['has_creds'] = bool(it.get('admin') or it.get('db') or it.get('login_url'))
                    it.pop('admin', None)  # secret : révélé seulement via /websites/credentials
                    items.append(it)
                items.sort(key=lambda x: x.get('created_at') or 0, reverse=True)
                self._json(200, {
                    'ok': True, 'sites': items,
                    'port_min': WEB_PORT_MIN, 'port_max': WEB_PORT_MAX,
                    'php_versions': sorted(WEB_PHP_VERSIONS.keys(), reverse=True),
                    'php_default': WEB_PHP_DEFAULT,
                    'web_dataset': _web_dataset_path(),
                    'proxy': _web_proxy_get(),
                })
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : logs d'un site (access/error, dernières lignes)
        if path == '/websites/logs':
            try:
                sid = qs.get('id', '')
                if not sid:
                    self._json(400, {'error': 'id requis'}); return
                _web_get(sid)  # valide l'existence
                kind = qs.get('kind', 'error')
                try:
                    n = int(qs.get('lines', '200'))
                except ValueError:
                    n = 200
                res = _web_tail_log(sid, kind, n)
                if not res.get('exists'):
                    # auto-réparation : régénère les confs pour activer les logs
                    try:
                        _web_regenerate()
                        res['regenerated'] = True
                    except Exception as e:
                        log.warning('logs regen: %s', e)
                self._json(200, res)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : identifiants d'un site (admin + base)
        if path == '/websites/credentials':
            try:
                sid = qs.get('id', '')
                if not sid:
                    self._json(400, {'error': 'id requis'}); return
                self._json(200, _web_credentials(sid))
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-HA-V1 : liaison avec Home Assistant, appareils connus, état des favoris
        if path == '/ha/config':
            self._json(200, _ha_public())
            return
        if path in ('/ha/entities', '/ha/states'):
            try:
                self._json(200, _ha_entities() if path == '/ha/entities' else _ha_states())
            except Exception as e:
                self._json(200, _ha_fail(e))
            return

        # MDM-MEMORY-V1 : répartition de la mémoire (jauge du bureau), puis qui la consomme (Informations système)
        if path == '/memory/summary':
            try:
                self._json(200, _mem_summary())
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/memory/overview':
            try:
                self._json(200, _mem_overview())
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WALLPAPERS-V1 : liste des fonds d'écran du dossier
        if path == '/wallpapers':
            try:
                self._json(200, _wallpaper_list())
            except Exception as e:
                self._json(200, {'ok': False, 'items': [], 'error': str(e)})
            return
        # MDM-WALLPAPERS-V1 : contenu d'une image (le bureau la charge en blob)
        if path == '/wallpapers/file':
            try:
                full = _wallpaper_path(qs.get('name', ''))
                size = os.path.getsize(full)
                self.send_response(200)
                self.send_header('Content-Type', _WALLPAPER_TYPES[os.path.splitext(full)[1].lower()])
                self.send_header('Content-Length', str(size))
                self.send_header('X-Content-Type-Options', 'nosniff')
                # L'URL porte la date de modification (?v=) : le cache navigateur reste juste.
                self.send_header('Cache-Control', 'private, max-age=86400')
                self.end_headers()
                with open(full, 'rb') as fh:
                    _sh_shutil.copyfileobj(fh, self.wfile, 256 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (FileNotFoundError, ValueError) as e:
                self._json(404, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SELF-UPDATE-V1 : version installée + dernière dispo
        if path == '/version':
            try:
                self._json(200, _version_status())
            except Exception as e:
                self._json(200, {'version': APP_VERSION, 'latest': '', 'update_available': False, 'boot': _BOOT_ID, 'error': str(e)})
            return

        # Notes de version (Markdown brut ; la page les met en forme).
        if path == '/releases':
            try:
                text, src = _releases_text(qs.get('force') in ('1', 'true'), qs.get('lang') or 'fr')
                self._json(200, {'ok': True, 'markdown': text, 'source': src, 'version': APP_VERSION})
            except Exception as e:
                self._json(200, {'ok': False, 'markdown': '', 'source': '', 'version': APP_VERSION, 'error': str(e)})
            return

        # MDM-DOWNLOADS-V1 : liste des téléchargements
        if path == '/downloads/list':
            try:
                self._json(200, _dl_list())
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-PREMIUM-V1 : état des comptes premium configurés
        if path == '/premium/status':
            try:
                self._json(200, {'ok': True, 'providers': _prem_status()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-APPS-V1 : catalogue d'applications + bases de données
        if path == '/apps/catalog':
            cat = {}
            for k, m in APP_CATALOG.items():
                cat[k] = {'label': m['label'], 'db': bool(m.get('db')),
                          'php': m.get('php'), 'ext': m.get('ext', []), 'url': m['url']}
            self._json(200, {'ok': True, 'apps': cat, 'db_available': _HAS_PYMYSQL,
                             'install_profile': _install_profile_read()})
            return

        if path == '/db/list':
            try:
                self._json(200, {'ok': True, 'databases': _db_list()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/db/credentials':
            try:
                self._json(200, {'ok': True, 'credentials': _db_creds_read()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # Compte d'administration de MariaDB (root), révélé à la demande : c'est lui qu'on saisit dans phpMyAdmin
        # pour voir toutes les bases. Comme le reste, la route n'est ouverte qu'au porteur du jeton.
        if path == '/db/root-credentials':
            if not DB_ROOT_PASSWORD:
                self._json(404, {'error': 'Mot de passe MariaDB absent de la configuration (DB_ROOT_PASSWORD).'})
            else:
                self._json(200, {'ok': True, 'credentials': {'host': DB_HOST, 'port': DB_PORT, 'user': 'root',
                                                             'password': DB_ROOT_PASSWORD}})
            return

        # Noms d'affichage des applications
        if path == '/apps/labels':
            try:
                self._json(200, {'ok': True, 'labels': _app_labels_get()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # Widget météo : prévisions pour un point, recherche d'une ville
        if path == '/weather':
            try:
                self._json(200, _weather(qs.get('lat'), qs.get('lon')))
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(502, {'error': str(e)})
            return

        if path == '/weather/search':
            try:
                self._json(200, {'ok': True, 'places': _weather_search(qs.get('q', ''), qs.get('lang', 'fr'))})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(502, {'error': str(e)})
            return

        # Conteneurs Docker de l'hôte : liste complète, charge instantanée, journal
        if path == '/containers':
            try:
                self._json(200, {'ok': True, 'containers': _ct_list()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/containers/stats':
            try:
                self._json(200, {'ok': True, 'stats': _ct_stats()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-CT-FRESH-V1 : images mises à jour depuis moins d'un jour, que TrueNAS annonce encore à tort
        if path == '/containers/images/fresh':
            try:
                self._json(200, _ct_fresh_get())
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-FS-JOBS-V1 : tâches de déplacement / copie en cours (visibles de tous les appareils de l'utilisateur)
        if path == '/fs/jobs':
            self._json(200, {'jobs': _fs_jobs_list(_px_by(self))})
            return

        # MDM-CT-UPDATE-V1 : suivi d'une mise à jour de conteneurs lancée par POST /containers/update
        if path == '/containers/update/status':
            try:
                self._json(200, _ct_update_status(qs.get('project', '')))
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/containers/logs':
            try:
                try:
                    n = int(qs.get('lines', '300'))
                except ValueError:
                    n = 300
                self._json(200, {'ok': True, 'log': _ct_logs(qs.get('id', ''), n)})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/proxy/npm/status':
            res = _npm_status()
            res['ok'] = True
            self._json(200, res)
            return

        # MDM-WEBSITES-V1 : profils PHP (ini + extensions)
        if path == '/php/profiles':
            try:
                data = _php_profiles_read()
                profs = {}
                for ver in WEB_PHP_VERSIONS:
                    p = data.get(ver, {})
                    profs[ver] = {
                        'ini': p.get('ini', {}) if isinstance(p.get('ini'), dict) else {},
                        'extensions': p.get('extensions', []) if isinstance(p.get('extensions'), list) else [],
                    }
                self._json(200, {
                    'ok': True,
                    'versions': sorted(WEB_PHP_VERSIONS.keys(), reverse=True),
                    'default': WEB_PHP_DEFAULT,
                    'profiles': profs,
                })
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SHARE-LINKS-V1 : liste des liens de partage (authentifié)
        if path == '/share/list':
            try:
                data = _share_purge()
                items = [_share_public(r) for r in data.values() if isinstance(r, dict)]
                items.sort(key=lambda x: x.get('created_at') or 0, reverse=True)
                self._json(200, {'ok': True, 'shares': items})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-LIBVIRT-REQUEST-GUARD-V1-BEGIN
        if _libvirt_path_requires_daemon(path):
            try:
                ensure_libvirtd()
            except Exception as error:
                self._json(503, {
                    'api': 'libvirt2' if path.startswith('/libvirt2') else 'libvirt',
                    'error': 'Service QEMU/libvirt indisponible : ' + str(error),
                    'autorecovery': 'failed',
                })
                return
        # MDM-LIBVIRT-REQUEST-GUARD-V1-END

        # MDM-ACCESS-POLICY-V1-GET-BEGIN
        if path == '/access/policy':
            try:
                self._json(200, {'ok': True, 'policy': access_policy_get()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/access/history':
            try:
                limit = int(qs.get('limit', 150) or 150)
                self._json(200, {
                    'ok': True,
                    'history': access_history_get(limit)
                })
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        # MDM-ACCESS-POLICY-V1-GET-END

        # MDM-LIBVIRT2-20260710
        # API V2 VM/QEMU/libvirt.
        # GET /libvirt2/vms
        # GET /libvirt2/vms/{name}
        # MDM-LIBVIRT2-INSTALLATION-ROUTES-20260710
        # GET /libvirt2/isos
        # MDM-LIBVIRT2-NETWORK-ROUTES-20260710
        # GET /libvirt2/networks
        if path == '/libvirt2/networks':
            try:
                self._json(200, {
                    'api': 'libvirt2',
                    'readonly': False,
                    'networks': _lv2_list_network_sources()
                })
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        if path == '/libvirt2/isos':
            try:
                self._json(200, {
                    'api': 'libvirt2',
                    'readonly': False,
                    'iso_dir': ISO_DIR,
                    'isos': _lv2_list_isos()
                })
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        if path == '/libvirt2/vms':
            try:
                detail = qs.get('detail', '0') in ('1', 'true', 'yes')
                names = _lv2_vm_names()
                if detail:
                    vms = []
                    for vm_name in names:
                        try:
                            vms.append(_lv2_get_vm(vm_name, include_xml=False))
                        except Exception as one_e:
                            vms.append({'api': 'libvirt2', 'name': vm_name, 'error': str(one_e)})
                else:
                    vms = [{'api': 'libvirt2', 'name': vm_name} for vm_name in names]
                self._json(200, {'api': 'libvirt2', 'version': 2, 'readonly': False, 'vms': vms})
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # MDM-LIBVIRT2-SNAPSHOTS-GET-FIX-20260710
        # GET /libvirt2/vms/{name}/snapshots
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                self._json(200, _lv2_snapshot_list(name))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        m = re.match(r'^/libvirt2/vms/([^/]+)$', path)
        if m:
            try:
                from urllib.parse import unquote
                vm_name = unquote(m.group(1))
                include_xml = qs.get('xml', '0') in ('1', 'true', 'yes')
                self._json(200, _lv2_get_vm(vm_name, include_xml=include_xml))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        if path == '/list':
            target = qs.get('path', '/')
            try:
                entries = []
                # os.scandir : type d'entrée sans stat (rapide sur gros dossiers,
                # ex. datasets Docker overlay). stat uniquement sur les fichiers.
                with os.scandir(target) as it:
                    for e in it:
                        try:
                            is_dir = e.is_dir(follow_symlinks=False)
                        except OSError:
                            is_dir = False
                        size = 0
                        mtime = 0
                        # Date de modification pour tout le monde (colonne « Modifié »), taille pour les fichiers.
                        try:
                            st = e.stat(follow_symlinks=False)
                            mtime = st.st_mtime
                            if not is_dir:
                                size = st.st_size
                        except OSError:
                            pass
                        entries.append({
                            'name':   e.name,
                            'is_dir': is_dir,
                            'size':   size,
                            'mtime':  mtime,
                        })
                entries.sort(key=lambda x: x['name'].lower())
                self._json(200, entries)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SEARCH-V1 : recherche de fichiers/dossiers par nom
        if path == '/search':
            base = qs.get('path', '/')
            q = (qs.get('q', '') or '').lower()
            try:
                limit = int(qs.get('limit', '500') or 500)
            except ValueError:
                limit = 500
            if not q:
                self._json(200, {'ok': True, 'results': []})
                return
            try:
                results = []
                truncated = False
                lines = None
                # 1) `find` (rapide, parcourt tout l'arbre) — recherche par nom, insensible à la casse
                try:
                    import subprocess
                    esc = ''.join(('\\' + c) if c in '*?[]\\' else c for c in q)
                    proc = subprocess.run(
                        ['find', base, '-iname', '*' + esc + '*'],
                        capture_output=True, text=True, timeout=45,
                    )
                    lines = [ln for ln in proc.stdout.splitlines() if ln]
                except Exception:
                    lines = None
                if lines is not None:
                    if len(lines) > limit:
                        truncated = True
                        lines = lines[:limit]
                    for full in lines:
                        if full == base:
                            continue
                        isd, sz, mt = False, 0, 0
                        try:
                            isd = os.path.isdir(full)
                            st = os.stat(full)
                            mt = st.st_mtime
                            sz = 0 if isd else st.st_size
                        except OSError:
                            pass
                        results.append({'name': os.path.basename(full.rstrip('/')),
                                        'path': full, 'is_dir': isd, 'size': sz, 'mtime': mt})
                else:
                    # 2) Repli Python (si `find` indisponible)
                    import time as _t
                    start = _t.time()
                    for root, dirs, files in os.walk(base):
                        for nm in list(dirs) + list(files):
                            if q in nm.lower():
                                full = os.path.join(root, nm)
                                isd, sz, mt = False, 0, 0
                                try:
                                    isd = os.path.isdir(full)
                                    st = os.stat(full)
                                    mt = st.st_mtime
                                    sz = 0 if isd else st.st_size
                                except OSError:
                                    pass
                                results.append({'name': nm, 'path': full, 'is_dir': isd, 'size': sz, 'mtime': mt})
                                if len(results) >= limit:
                                    break
                        if len(results) >= limit or (_t.time() - start) > 30:
                            truncated = len(results) >= limit
                            break
                results.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
                self._json(200, {'ok': True, 'results': results, 'truncated': truncated})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SEARCH-V1 : sommes de contrôle md5 + sha256 (un seul passage)
        if path == '/checksum':
            target = qs.get('path', '')
            try:
                if not os.path.isfile(target):
                    self._json(404, {'error': 'Fichier introuvable'})
                    return
                import hashlib
                m = hashlib.md5()
                s = hashlib.sha256()
                with open(target, 'rb') as f:
                    for chunk in iter(lambda: f.read(1048576), b''):
                        m.update(chunk); s.update(chunk)
                self._json(200, {'ok': True, 'md5': m.hexdigest(), 'sha256': s.hexdigest(),
                                 'size': os.path.getsize(target)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-PERM-V1 : lecture des permissions/propriétaire d'un élément
        if path == '/perm':
            target = qs.get('path', '')
            try:
                if not os.path.exists(target):
                    self._json(404, {'error': 'Introuvable'})
                    return
                st = os.stat(target)
                import pwd as _pwd, grp as _grp
                try:
                    owner = _pwd.getpwuid(st.st_uid).pw_name
                except Exception:
                    owner = ''
                try:
                    group = _grp.getgrgid(st.st_gid).gr_name
                except Exception:
                    group = ''
                self._json(200, {
                    'ok': True,
                    'mode': oct(st.st_mode & 0o7777)[2:].zfill(4),
                    'uid': st.st_uid, 'gid': st.st_gid,
                    'owner': owner, 'group': group,
                    'is_dir': os.path.isdir(target),
                })
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-EDITOR-V1 : lecture d'un fichier texte pour l'éditeur
        if path == '/read':
            target = qs.get('path', '')
            try:
                if not os.path.isfile(target):
                    self._json(404, {'error': 'Fichier introuvable'})
                    return
                size = os.path.getsize(target)
                if size > 5 * 1024 * 1024:
                    self._json(200, {'ok': True, 'too_large': True, 'size': size})
                    return
                with open(target, 'rb') as f:
                    raw = f.read()
                if b'\x00' in raw:
                    self._json(200, {'ok': True, 'binary': True, 'size': size})
                    return
                try:
                    content = raw.decode('utf-8')
                except UnicodeDecodeError:
                    self._json(200, {'ok': True, 'binary': True, 'size': size})
                    return
                self._json(200, {'ok': True, 'content': content, 'size': size})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/libvirt2/test':
            try:
                out = ssh_ok('echo ok && virsh --version')
                self._json(200, {'ok': True, 'message': out})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/libvirt2/bridges':
            try:
                out, _, _ = ssh_exec("ip -o link show type bridge 2>/dev/null || brctl show 2>/dev/null | awk 'NR>1{print $1}'")
                bridges = []
                for line in out.splitlines():
                    line = line.strip()
                    if not line: continue
                    # ip -o link format: "3: br0: <...>"
                    import re as _re
                    m = _re.match(r'^\d+:\s+(\S+):', line)
                    if m:
                        br = m.group(1).rstrip(':')
                        if br not in ('lo',): bridges.append(br)
                    elif line and ' ' not in line:
                        bridges.append(line)
                self._json(200, {'bridges': bridges})
            except Exception as e:
                self._json(200, {'bridges': []})
            return

        self._json(404, {'error': 'Not found: ' + path})

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        if urlparse(self.path).path.rstrip('/') == '/auth/login':
            return self._auth_login()
        if urlparse(self.path).path.rstrip('/') == '/store/login':   # MDM-STORE-V1 : connexion directe aux apps
            return store_login(self)
        if urlparse(self.path).path.startswith('/office/save/'):   # MDM-OFFICE-V1 : retour du serveur de documents, signé
            return office_save(self, urlparse(self.path).path[len('/office/save/'):].split('/')[0])
        if urlparse(self.path).path.rstrip('/') == '/store/app/quarantine':   # appel d'une app, authentifiée par son secret
            code, resp = store_app_quarantine(self)
            return self._json(code, resp)
        if not self._auth():
            return
        p    = urlparse(self.path)
        path = p.path.rstrip('/')

        # MDM-OFFICE-V1 : ouvrir un fichier dans l'éditeur, créer un document vide
        if path in ('/office/open', '/office/new'):
            try:
                code, resp = (office_open if path == '/office/open' else office_new)(self, self._body())
            except Exception as e:
                code, resp = 500, {'ok': False, 'error': str(e)}
            return self._json(code, resp)

        # MDM-DKBACKUP-V1 : sauvegarde / restauration de Desktroo
        if path.startswith('/dkbackup/'):
            try:
                if path == '/dkbackup/run':
                    code, resp = dkbackup_run(manual=True)
                elif path == '/dkbackup/settings':
                    code, resp = dkbackup_settings_set(self._body())
                elif path == '/dkbackup/delete':
                    code, resp = dkbackup_delete(str(self._body().get('name', '')))
                elif path == '/dkbackup/snap-restore':
                    b = self._body()
                    code, resp = dkbackup_snap_restore(str(b.get('app', '')), str(b.get('snapshot', '')))
                elif path in ('/dkbackup/restore', '/dkbackup/restore-local'):
                    tmpf = None
                    if path == '/dkbackup/restore-local':
                        nm = str(self._body().get('name', ''))
                        full = os.path.join(_dkbk_settings()['dir'], nm)
                        if not DKBK_NAME_RE.match(nm) or not os.path.isfile(full):
                            return self._json(404, {'ok': False, 'error': 'Sauvegarde introuvable.'})
                    else:
                        n = int(self.headers.get('Content-Length', 0))
                        if n <= 0 or n > DKBK_MAX_UPLOAD:
                            return self._json(400, {'ok': False, 'error': 'Fichier absent ou trop volumineux.'})
                        fd, tmpf = _tempfile.mkstemp(prefix='dkup-', suffix='.tar.gz')
                        with os.fdopen(fd, 'wb') as fh:
                            left = n
                            while left > 0:
                                chunk = self.rfile.read(min(1 << 20, left))
                                if not chunk:
                                    break
                                fh.write(chunk); left -= len(chunk)
                        full = tmpf
                    try:
                        code, resp = dkbackup_restore(full)
                    finally:
                        if tmpf:
                            try: os.remove(tmpf)
                            except OSError: pass
                    if code == 200:
                        _schedule_self_restart()
                else:
                    code, resp = 404, {'ok': False, 'error': 'Route inconnue.'}
                self._json(code, resp)
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        # MDM-LICENSE-V1 : activation + garde lecture seule
        if path == '/license/activate':
            try:
                b = self._body()
                code, resp = license_activate(str(b.get('token', '')))
                self._json(code, resp)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        # MDM-LICENSE-V1 : activation par clé (émission en ligne via le serveur)
        if path == '/license/activate-key':
            try:
                b = self._body()
                code, resp = license_activate_key(str(b.get('key', '')))
                self._json(code, resp)
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        # MDM-LICENSE-V1 : refresh manuel du jeton depuis le serveur
        if path == '/license/refresh':
            try:
                ok = license_do_refresh()
                st = license_status()
                st['subscribe_url'] = SUBSCRIBE_URL
                st['manage_url'] = MANAGE_URL
                self._json(200, {'ok': bool(ok), 'status': st})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        if path == '/terms/accept':
            try:
                self._json(200, _terms_accept(self._body()))
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/report':
            if LICENSE_FREE and not _lic_registered():   # 3.1.0 : signalements et idées avec la clé gratuite
                self._json(403, {'ok': False, 'code': 'need_key', 'error': 'Les signalements de bugs et les demandes de fonctionnalités '
                                 'demandent une clé gratuite : demandez-la sur desktroo.fr, puis activez-la dans « À propos » › « Licence ».'})
                return
            try:
                self._json(200, _report_send(self._body()))
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(502, {'error': str(e)})
            return
        if path == '/uninstall/run':
            try:
                self._json(200, _uninstall_run(self._body(), _px_by(self)))
            except ValueError as e:
                self._json(400, {'ok': False, 'error': str(e)})
            except Exception as e:
                self._json(502, {'ok': False, 'error': str(e)})
            return
        if path not in _LIC_READONLY_ALLOW_POST and _lic_is_readonly():
            self._json(402, {'error': 'license_readonly',
                             'message': 'Abonnement requis — mode lecture seule.'})
            return

        # MDM-STORE-V1
        if path.startswith('/store/') and _store_post(self, path):
            return

        # MDM-FILES-PLUS-V1
        if path in _PX_POST and _px_post(self, path):
            return

        # MDM-WALLPAPERS-V1 : ajout d'une image (corps binaire, nom en paramètre)
        # MDM-TN-UPLOAD-V1 : relayer un fichier vers TrueNAS (corps brut ; l'opération et le jeton sont en en-têtes)
        if path == '/truenas/upload':
            try:
                from urllib.parse import unquote as _unq
                n = int(self.headers.get('Content-Length', 0) or 0)
                try:
                    params = json.loads(_unq(self.headers.get('X-TN-Params', '') or '[]'))
                except Exception:
                    raise ValueError('paramètres invalides')
                status, data = _truenas_upload(
                    str(self.headers.get('X-TN-Method', '')), params, str(self.headers.get('X-TN-Token', '')),
                    n, self.rfile, _unq(self.headers.get('X-File-Name', '') or ''))
                if 200 <= status < 300:
                    self._json(200, dict(data, ok=True))
                else:
                    self._json(502, {'error': data.get('error') or ('TrueNAS a répondu %d' % status), 'truenas_status': status})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(502, {'error': 'TrueNAS injoignable pour cet envoi : %s' % e})
            return

        if path == '/wallpapers/upload':
            tmp_path = None
            try:
                qs = dict(parse_qsl(p.query, keep_blank_values=True))
                name = _wallpaper_name(qs.get('name', ''))
                total = int(self.headers.get('Content-Length') or -1)
                if total <= 0:
                    raise ValueError('Fichier vide ou taille absente.')
                if total > WALLPAPER_MAX_BYTES:
                    raise ValueError('Image trop lourde : %d Mo au plus.' % (WALLPAPER_MAX_BYTES // (1024 * 1024)))
                base = _wallpaper_dir(create=True)
                name = _wallpaper_free_name(base, name)
                target = os.path.join(base, name)
                tmp_path = os.path.join(base, '.upload-%d-%d' % (os.getpid(), _threading.get_ident()))
                remaining = total
                with open(tmp_path, 'wb') as out:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise IOError('Envoi interrompu avant la fin.')
                        out.write(chunk)
                        remaining -= len(chunk)
                os.replace(tmp_path, target)
                tmp_path = None
                try:
                    os.chmod(target, 0o644)
                except OSError:
                    pass
                self._json(200, {'ok': True, 'item': _wallpaper_item(target)})
            except ValueError as e:
                self._json(400, {'ok': False, 'error': str(e)})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            return
        # MDM-LANGPACKS-V1 : importer ou retirer une langue ajoutée
        if path in ('/lang/import', '/lang/delete'):
            try:
                b = self._body()
                self._json(200, lang_import(b) if path == '/lang/import' else lang_delete(b.get('code')))
            except FileNotFoundError as e:
                self._json(404, {'ok': False, 'error': str(e)})
            except ValueError as e:
                self._json(400, {'ok': False, 'error': str(e)})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        # MDM-WALLPAPERS-V1 : suppression d'une image du dossier
        if path == '/wallpapers/delete':
            try:
                os.unlink(_wallpaper_path(self._body().get('name', '')))
                self._json(200, {'ok': True})
            except (FileNotFoundError, ValueError) as e:
                self._json(404, {'ok': False, 'error': str(e)})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return

        # MDM-APPS-V1 : bases de données + installation d'applications
        if path == '/apps/labels':
            try:
                b = self._body()
                self._json(200, {'ok': True, 'labels': _app_labels_set(b.get('name'), b.get('label'))})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # Profil du compte : choisir un avatar, envoyer une photo, revenir à l'initiale
        if path == '/profile/favorites':   # favoris de Fichiers du compte
            try:
                self._json(200, profile_favorites_set(self, self._body()))
            except ValueError as e:
                self._json(400, {'ok': False, 'error': str(e)})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        if path == '/profile/avatar':
            try:
                self._json(200, profile_avatar_set(self, self._body()))
            except ValueError as e:
                self._json(400, {'ok': False, 'error': str(e)})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        # Programme bêta : inscription, désinscription, réponse à l'invitation
        if path in ('/update/channel', '/update/beta-offer'):
            try:
                b = self._body()
                code, resp = dist_channel_set(b) if path == '/update/channel' else beta_offer_answer(b)
                self._json(code, resp)
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        if path == '/branding':
            try:
                self._json(200, dict(_branding_set(str(self._body().get('accent') or '')), ok=True))
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-CT-FRESH-V1 : retenir les images qui viennent d'être mises à jour
        if path == '/containers/images/fresh':
            try:
                b = self._body()
                self._json(200, _ct_fresh_add(b.get('items')))
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-CT-UPDATE-V1 : télécharger la nouvelle image et recréer des conteneurs gérés par docker compose
        if path == '/containers/update':
            try:
                b = self._body()
                self._json(200, _ct_update(b.get('ids') if isinstance(b.get('ids'), list) else []))
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/containers/restart':
            try:
                b = self._body()
                _ct_restart(b.get('id'))
                self._json(200, {'ok': True})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/db/create':
            try:
                b = self._body()
                res = _db_create(b.get('name') or None, b.get('user') or None, b.get('password') or None)
                self._json(200, {'ok': True, 'db': res})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/db/repair':
            try:
                self._json(200, _db_repair_root())
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/db/delete':
            try:
                b = self._body()
                _db_delete(b.get('name'), b.get('user'))
                self._json(200, {'ok': True})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/apps/profile':
            try:
                b = self._body()
                self._json(200, {'ok': True, 'profile': _install_profile_save(b)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/apps/install':
            try:
                b = self._body()
                res = _app_install(
                    b.get('app'), b.get('root'), b.get('php_version'),
                    b.get('port'), b.get('server_name'), b.get('name'),
                    b.get('url'), b.get('create_db', True),
                    auto=bool(b.get('auto')), site_host=b.get('site_host'),
                    title=b.get('title'), admin_user=b.get('admin_user'),
                    admin_password=b.get('admin_password'), admin_email=b.get('admin_email'),
                    language=b.get('language'),
                    db_name=b.get('db_name'), db_user=b.get('db_user'),
                    db_password=b.get('db_password'),
                )
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : activer/désactiver le reverse-proxy intégré
        if path == '/websites/proxy':
            try:
                b = self._body()
                res = _web_proxy_set(b.get('enabled', True))
                res['ok'] = True
                self._json(200, res)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : créer le dataset Web avec les bonnes permissions
        if path == '/websites/dataset':
            try:
                b = self._body()
                res = _web_create_dataset(b.get('pool'), b.get('name'))
                res['ok'] = True
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : créer un sous-dossier de site dans le dataset Web
        if path == '/websites/mkdir':
            try:
                b = self._body()
                res = _web_mkdir(b.get('parent'), b.get('name'))
                res['ok'] = True
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : adopter un dossier existant (le deplacer dans le dataset Web)
        if path == '/websites/adopt':
            try:
                b = self._body()
                res = _web_adopt(b.get('path'), bool(b.get('dry_run')))
                res['ok'] = True
                if res.get('moved'):
                    _px_log('web.adopt', _px_by(self), src=str(res.get('from') or ''), path=str(res.get('path') or ''))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : réglages php.ini propres à un site (.user.ini)
        if path == '/websites/php-ini':
            try:
                b = self._body()
                sid = str(b.get('id', ''))
                data = _web_read()
                site = data.get(sid)
                if not site:
                    self._json(404, {'error': 'Site introuvable'})
                    return
                clean = {}
                for k, v in (b.get('ini') or {}).items():
                    k = str(k).strip()
                    vs = str(v).replace('\r', '').replace('\n', '').strip()[:256]
                    if _PHP_INI_KEY_RE.match(k) and vs:
                        clean[k] = vs
                site['php_ini'] = clean
                data[sid] = site
                _web_write(data)
                if site.get('root'):
                    _web_write_user_ini(site['root'], clean)
                ver = str(site.get('php_version') or '')
                if ver:
                    try:
                        os.makedirs(os.path.join(WEB_PHP_DIR, ver), exist_ok=True)
                        with open(os.path.join(WEB_PHP_DIR, ver, '.reload'), 'w', encoding='utf-8') as f:
                            f.write(str(_sh_time.time()))
                    except OSError:
                        pass
                self._json(200, {'ok': True, 'php_ini': clean})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : création / activation / suppression de sites
        if path == '/websites/create':
            try:
                b = self._body()
                sid, clean = _web_create(b)
                self._json(200, {'ok': True, 'id': sid, 'site': _web_public(clean, sid)})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/websites/reimport':
            try:
                imported = _web_reimport()
                self._json(200, {'ok': True, 'imported': imported, 'count': len(imported)})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/websites/toggle':
            try:
                b = self._body()
                sid = str(b.get('id', ''))
                data = _web_read()
                if sid not in data:
                    self._json(404, {'error': 'Site introuvable'})
                    return
                want = bool(b.get('enabled', not data[sid].get('enabled', True)))
                if want:
                    # revalider (port/domaine) avant réactivation
                    check = dict(data[sid])
                    check['enabled'] = True
                    _web_validate(check, data, sid=sid)
                data[sid]['enabled'] = want
                _web_write(data)
                _web_regenerate()
                self._json(200, {'ok': True, 'enabled': want})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/websites/delete':
            try:
                b = self._body()
                sid = str(b.get('id', ''))
                delete_root = bool(b.get('delete_root'))
                delete_db = bool(b.get('delete_db'))
                data = _web_read()
                site = data.get(sid)
                res = {'ok': True}
                if site:
                    del data[sid]
                    _web_write(data)
                    _web_regenerate()
                    # Suppression du dossier (protégé : sous /mnt, profond, pas un point de montage)
                    if delete_root and site.get('root'):
                        root = os.path.realpath(site['root'])
                        base = WEB_ROOT_ALLOWED.rstrip('/')
                        parts = [p for p in root.split('/') if p]
                        if root.startswith(base + '/') and len(parts) >= 3 and not os.path.ismount(root):
                            try:
                                _sh_shutil.rmtree(root)
                                res['root_deleted'] = True
                            except Exception as e:
                                res['root_error'] = str(e)
                        else:
                            res['root_error'] = 'dossier protégé, non supprimé'
                    # Suppression de la base + utilisateur
                    if delete_db:
                        dbinfo = site.get('db') or {}
                        dbname = b.get('db_name') or dbinfo.get('name')
                        dbuser = b.get('db_user') or dbinfo.get('user')
                        if dbname:
                            try:
                                _db_delete(dbname, dbuser)
                                res['db_deleted'] = True
                            except Exception as e:
                                res['db_error'] = str(e)
                self._json(200, res)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : mise à jour du cœur d'une appli
        if path == '/websites/update':
            try:
                b = self._body()
                res = _app_update(str(b.get('id', '')))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : clonage d'un site (fichiers + base)
        if path == '/websites/clone':
            try:
                b = self._body()
                res = _app_clone(str(b.get('id', '')), b.get('name'))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : activer le SSL PrestaShop derrière le proxy
        if path == '/websites/ssl-prestashop':
            try:
                b = self._body()
                res = _app_prestashop_ssl(str(b.get('id', '')), bool(b.get('enable', True)))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : réinitialiser le mot de passe admin d'une appli
        if path == '/websites/admin-reset':
            try:
                b = self._body()
                res = _app_admin_reset(str(b.get('id', '')))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-DOWNLOADS-V1 : gestionnaire de téléchargements
        if path == '/downloads/add':
            try:
                b = self._body()
                self._json(200, {'ok': True, 'download': _dl_add(b.get('url'), b.get('dir'), b.get('filename'), b.get('premium', 'auto'), b.get('connections'), b.get('auto_extract'), b.get('auto_remove'))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/downloads/add-batch':
            try:
                b = self._body()
                res = _dl_add_batch(b.get('urls') or [], b.get('dir'), b.get('premium', 'auto'), b.get('connections'), b.get('auto_extract'), b.get('auto_remove'))
                self._json(200, {'ok': True, 'results': res,
                                 'added': sum(1 for x in res if x.get('ok')),
                                 'failed': sum(1 for x in res if not x.get('ok'))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path in ('/downloads/pause', '/downloads/resume', '/downloads/cancel', '/downloads/remove'):
            try:
                b = self._body()
                did = str(b.get('id', ''))
                if path == '/downloads/pause':
                    res = _dl_pause(did)
                elif path == '/downloads/resume':
                    res = _dl_resume(did)
                elif path == '/downloads/cancel':
                    res = _dl_cancel(did, delete=bool(b.get('delete', True)))
                else:
                    res = _dl_remove(did)
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/downloads/clear':
            try:
                self._json(200, _dl_clear())
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/downloads/settings':
            try:
                b = self._body()
                if b.get('max_concurrent') is not None:
                    _dl_set_max_concurrent(b.get('max_concurrent'))
                if b.get('default_connections') is not None:
                    _dl_set_default_connections(b.get('default_connections'))
                if b.get('default_dir'):
                    _dl_set_default_dir(b.get('default_dir'))
                _dl_set_auto(b.get('auto_extract'), b.get('auto_remove'))
                _dl_settings_save()
                self._json(200, dict({'ok': True}, **_dl_settings_get()))
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SELF-UPDATE-V1 : mise à jour depuis GitHub + redémarrage du sidecar
        if path == '/update':
            gate = _update_gate()
            if not gate.get('ok'):
                self._json(403, gate)
                return
            try:
                updated = _do_update()
                _schedule_self_restart()
                self._json(200, {'ok': True, 'updated': updated, 'restarting': True,
                                 'version': APP_VERSION})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-HA-V1 : relier Home Assistant, choisir les favoris, allumer ou éteindre, oublier la liaison
        if path in ('/ha/config', '/ha/favorites', '/ha/switch', '/ha/forget'):
            try:
                b = self._body()
                if path == '/ha/config':
                    self._json(200, _ha_connect(b))
                elif path == '/ha/favorites':
                    self._json(200, _ha_set_favorites(b.get('entities')))
                elif path == '/ha/switch':
                    self._json(200, _ha_switch(b.get('id'), bool(b.get('on'))))
                else:
                    _ha_save({'url': '', 'token': '', 'entities': []})
                    self._json(200, _ha_public())
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(200, _ha_fail(e))
            return

        # MDM-PREMIUM-V1 : gestion des comptes premium / débrideurs
        if path == '/premium/set':
            try:
                b = self._body()
                _prem_set(str(b.get('provider', '')),
                          {k: b.get(k) for k in ('apikey', 'token', 'login', 'password') if b.get(k) not in ('', None)})
                self._json(200, {'ok': True, 'providers': _prem_status()})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/premium/get':
            try:
                b = self._body()
                self._json(200, {'ok': True, 'fields': _prem_get(str(b.get('provider', '')))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/premium/remove':
            try:
                b = self._body()
                _prem_remove(str(b.get('provider', '')))
                self._json(200, {'ok': True, 'providers': _prem_status()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/premium/test':
            try:
                b = self._body()
                self._json(200, _prem_test(str(b.get('provider', ''))))
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : enregistrer un profil PHP (ini + extensions)
        if path == '/php/profile':
            try:
                b = self._body()
                prof, changed_ext = _php_save(
                    b.get('version', ''), b.get('ini') or {}, b.get('extensions') or []
                )
                self._json(200, {'ok': True, 'profile': prof, 'ext_changed': changed_ext})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SHARE-LINKS-V1 : création / révocation de liens (authentifié)
        if path == '/share/create':
            try:
                b = self._body()
                rec = _share_create(
                    b.get('path', ''),
                    expires_in=b.get('expires_in'),
                    max_downloads=b.get('max_downloads'),
                    password=(b.get('password') or None),
                )
                _px_log('share.create', _px_by(self), path=str(rec.get('path') or ''))
                self._json(200, {
                    'ok': True,
                    'token': rec['token'],
                    'url_path': '/s/' + rec['token'],
                    'share': _share_public(rec),
                })
            except PermissionError as e:
                self._json(403, {'error': str(e)})
            except FileNotFoundError as e:
                self._json(404, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/share/revoke':
            try:
                b = self._body()
                tok = str(b.get('token', ''))
                data = _share_read()
                if tok in data:
                    del data[tok]
                    _share_write(data)
                self._json(200, {'ok': True})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-PERM-V1 : appliquer permissions (chmod) et/ou propriétaire (chown)
        if path == '/perm':
            try:
                body = self._body()
                target = str(body.get('path', ''))
                recursive = bool(body.get('recursive'))
                mode = body.get('mode')
                uid = body.get('uid')
                gid = body.get('gid')
                if not target or not os.path.exists(target):
                    raise ValueError('Introuvable')
                mode_int = None
                if mode not in (None, ''):
                    if not re.match(r'^[0-7]{3,4}$', str(mode)):
                        raise ValueError('Mode octal invalide (ex: 0755)')
                    mode_int = int(str(mode), 8)
                import pwd as _pwd, grp as _grp
                nu = None
                if uid not in (None, ''):
                    nu = int(uid) if str(uid).isdigit() else _pwd.getpwnam(str(uid)).pw_uid
                ng = None
                if gid not in (None, ''):
                    ng = int(gid) if str(gid).isdigit() else _grp.getgrnam(str(gid)).gr_gid

                def _apply(p):
                    if mode_int is not None:
                        os.chmod(p, mode_int)
                    if nu is not None or ng is not None:
                        stt = os.stat(p)
                        os.chown(p, nu if nu is not None else stt.st_uid,
                                 ng if ng is not None else stt.st_gid)

                _apply(target)
                if recursive and os.path.isdir(target):
                    for root, dirs, files in os.walk(target):
                        for d in dirs:
                            try:
                                _apply(os.path.join(root, d))
                            except OSError:
                                pass
                        for f in files:
                            try:
                                _apply(os.path.join(root, f))
                            except OSError:
                                pass
                self._json(200, {'ok': True})
            except (ValueError, KeyError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-EDITOR-V1 : écriture d'un fichier depuis l'éditeur (atomique)
        if path == '/write':
            try:
                body = self._body()
                target = str(body.get('path', ''))
                content = body.get('content', '')
                if not isinstance(content, str):
                    content = str(content)
                if not target:
                    raise ValueError('Chemin requis')
                parent = os.path.dirname(target)
                if not os.path.isdir(parent):
                    raise ValueError('Dossier inexistant : ' + parent)
                tmp = target + '.tmp-edit'
                with open(tmp, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                try:
                    st = os.stat(target)
                    os.chown(tmp, st.st_uid, st.st_gid)
                    os.chmod(tmp, st.st_mode & 0o7777)
                except OSError:
                    pass
                os.replace(tmp, target)
                _px_log('file.write', _px_by(self), path=target)
                self._json(200, {'ok': True, 'size': len(content.encode('utf-8'))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/fs/jobs/cancel':
            try:
                _b = self._body()
                _fs_job_cancel(str(_b.get('id', '')), _px_by(self), str(_b.get('bid', ''))[:32])
                self._json(200, {'ok': True})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-FILEOPS-MOVE-V1 : renommer / déplacer / copier (src -> dst)
        if path == '/rename' or path == '/move' or path == '/copy':
            import shutil as _shutil
            try:
                body = self._body()
                src = str(body.get('src', ''))
                dst = str(body.get('dst', ''))
                if not src or not dst:
                    raise ValueError('Source et destination requises')
                if not os.path.exists(src):
                    raise ValueError('Source introuvable')
                if os.path.realpath(src) == os.path.realpath(dst):
                    raise ValueError('Source et destination identiques')
                bid = str(body.get('bid', ''))[:32]
                _fs_batch_check(bid)
                conflict = str(body.get('conflict', 'error'))
                if os.path.exists(dst):
                    if conflict == 'overwrite':
                        _privileged_remove(dst)
                    elif conflict == 'rename':
                        dst = _unique_path(dst)
                    else:
                        raise ValueError('La destination existe déjà')
                parent = os.path.dirname(dst.rstrip('/'))
                if not os.path.isdir(parent):
                    raise ValueError('Dossier de destination inexistant')
                if path == '/copy':
                    try:
                        if os.path.isdir(src):
                            _shutil.copytree(src, dst, symlinks=True)
                        else:
                            _shutil.copy2(src, dst)
                    except Exception:
                        # ACL ZFS/SMB : délègue la copie au host (vrai root).
                        _cmd = 'rm -rf ' + shq(dst) + '; cp -a ' + shq(src) + ' ' + shq(dst)
                        out, err, code = _fs_job_ssh(_px_by(self), 'copy', src, dst, _cmd, bid=bid)
                        if code != 0 or not os.path.exists(dst):
                            raise RuntimeError('Copie (host) : ' + (err or out or 'échec').strip()[:200])
                else:
                    try:
                        os.rename(src, dst)
                    except OSError:
                        # Inter-dataset ou ACL ZFS/SMB : délègue au host (vrai root, gère les ACL
                        # et la suppression de la source). MDM-MOVE-BCLONE-V1 : d'abord un clonage de
                        # blocs ZFS (cp --reflink=always : quasi instantané, sans place en plus) puis
                        # suppression de la source ; si le pool ne sait pas cloner, 'mv' classique.
                        _mvcmd = ('if cp -a --reflink=always -- ' + shq(src) + ' ' + shq(dst) + ' 2>/dev/null; '
                                  'then rm -rf -- ' + shq(src) + '; '
                                  'else rm -rf -- ' + shq(dst) + '; mv -f -- ' + shq(src) + ' ' + shq(dst) + '; fi')
                        out, err, code = _fs_job_ssh(_px_by(self), 'move', src, dst, _mvcmd, bid=bid)
                        if code != 0:
                            raise RuntimeError('Déplacement (host) : ' + (err or out or 'échec').strip()[:200])
                    # Vérifie que la source a bien disparu.
                    if os.path.exists(src):
                        raise RuntimeError('Déplacement incomplet : la source subsiste (' + src + ')')
                _px_log('file.' + path[1:], _px_by(self), src=src, path=dst)
                self._json(200, {'ok': True, 'dst': dst, 'name': os.path.basename(dst.rstrip('/'))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-ARCHIVE-V1 : compression zip / tar.gz
        if path == '/compress':
            import zipfile as _zf, tarfile as _tf
            try:
                body = self._body()
                items = body.get('items') or ([body.get('path')] if body.get('path') else [])
                items = [str(i) for i in items if i]
                dest = str(body.get('dest', ''))
                fmt = str(body.get('format', 'zip'))
                if not items or not dest:
                    raise ValueError('Éléments et destination requis')
                for it in items:
                    if not os.path.exists(it):
                        raise ValueError('Introuvable : ' + it)
                if not os.path.isdir(os.path.dirname(dest)):
                    raise ValueError('Dossier de destination inexistant')
                if fmt == 'zip':
                    with _zf.ZipFile(dest, 'w', _zf.ZIP_DEFLATED, allowZip64=True) as z:
                        for it in items:
                            base = os.path.basename(it.rstrip('/'))
                            if os.path.isdir(it):
                                for root, _d, files in os.walk(it):
                                    for fn in files:
                                        fp = os.path.join(root, fn)
                                        z.write(fp, os.path.join(base, os.path.relpath(fp, it)))
                            else:
                                z.write(it, base)
                else:
                    modes = {'tar': 'w', 'tar.gz': 'w:gz', 'tgz': 'w:gz',
                             'tar.bz2': 'w:bz2', 'tar.xz': 'w:xz'}
                    with _tf.open(dest, modes.get(fmt, 'w:gz')) as t:
                        for it in items:
                            t.add(it, arcname=os.path.basename(it.rstrip('/')))
                _px_log('file.compress', _px_by(self), path=dest, count=len(items))
                self._json(200, {'ok': True, 'path': dest})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-ARCHIVE-V1 : extraction zip / tar / tgz (protégée contre le zip-slip)
        if path == '/extract':
            import zipfile as _zf, tarfile as _tf
            try:
                body = self._body()
                src = str(body.get('path', ''))
                dest_given = str(body.get('dest', '') or '')
                if not os.path.isfile(src):
                    raise ValueError('Archive introuvable')

                def _decide_dest(names):
                    if dest_given:
                        return dest_given
                    d = os.path.dirname(src)
                    tops = set()
                    for n in names:
                        n = str(n).replace('\\', '/').lstrip('/')
                        if n:
                            tops.add(n.split('/')[0])
                    if len(tops) == 1:
                        return d  # l'archive a déjà son dossier racine
                    low = src.lower()
                    if low.endswith('.tar.gz'):
                        stem = os.path.basename(src)[:-7]
                    elif low.endswith('.tgz'):
                        stem = os.path.basename(src)[:-4]
                    else:
                        stem = os.path.splitext(os.path.basename(src))[0]
                    return os.path.join(d, stem or 'extrait')

                def _do_extract(names, extract_all):
                    dest = _decide_dest(names)
                    os.makedirs(dest, exist_ok=True)
                    dest_real = os.path.realpath(dest)
                    for name in names:
                        p = os.path.realpath(os.path.join(dest_real, name))
                        if not (p == dest_real or p.startswith(dest_real + os.sep)):
                            raise ValueError('Archive non sûre (chemin hors dossier)')
                    extract_all(dest_real)
                    return dest_real

                import shutil as _shutil
                if src.lower().endswith(('.rar', '.7z')) and not _zf.is_zipfile(src):
                    # RAR / 7z : outil externe (7z, bsdtar, unrar, unar) ; en arrière-plan si l'archive est grosse
                    if not any(_shutil.which(t) for t in ('7z', '7za', '7zr', 'bsdtar', 'unrar', 'unar')):
                        raise ValueError('Aucun outil RAR/7z dans le conteneur Desktroo (7z, bsdtar, unrar ou unar) : redémarrez le conteneur fileops pour l’installer, ou extrayez avec un autre outil.')
                    if dest_given:
                        out_dir = dest_given
                    else:
                        out_dir = os.path.join(os.path.dirname(src), os.path.splitext(os.path.basename(src))[0] or 'extrait')
                        _k = 1
                        while os.path.exists(out_dir):
                            out_dir = os.path.join(os.path.dirname(src), (os.path.splitext(os.path.basename(src))[0] or 'extrait') + '.' + str(_k)); _k += 1
                    _res = {}

                    def _bg_extract():
                        try:
                            _dl_extract_external(src, os.path.dirname(src), out_dir)
                            _res['ok'] = True
                        except Exception as _e:
                            _res['err'] = str(_e)
                            log.warning('Extraction de %s : %s', src, _e)
                    _t = _threading.Thread(target=_bg_extract, name='extract-rar', daemon=True)
                    _t.start()
                    _t.join(40)
                    if _res.get('err'):
                        raise ValueError(_res['err'])
                    _px_log('file.extract', _px_by(self), path=out_dir)
                    self._json(200, {'ok': True, 'dest': out_dir, 'running': _t.is_alive()})
                    return
                if _zf.is_zipfile(src):
                    with _zf.ZipFile(src) as z:
                        dest_real = _do_extract(z.namelist(), z.extractall)
                elif _tf.is_tarfile(src):
                    with _tf.open(src) as t:
                        dest_real = _do_extract(t.getnames(), t.extractall)
                else:
                    # Fichier unique compressé (gz / bz2 / xz), pas une archive tar
                    low = src.lower()
                    if low.endswith('.gz'):
                        import gzip as _c; out = src[:-3]
                    elif low.endswith('.bz2'):
                        import bz2 as _c; out = src[:-4]
                    elif low.endswith('.xz') or low.endswith('.lzma'):
                        import lzma as _c; out = os.path.splitext(src)[0]
                    else:
                        raise ValueError('Format non reconnu (zip, tar, gz, bz2, xz)')
                    if not out or out == src:
                        out = src + '.out'
                    _cand = out
                    _n = 1
                    while os.path.exists(_cand):
                        _cand = out + '.' + str(_n)
                        _n += 1
                    out = _cand
                    with _c.open(src, 'rb') as fi, open(out, 'wb') as fo:
                        _shutil.copyfileobj(fi, fo)
                    dest_real = out
                _px_log('file.extract', _px_by(self), path=dest_real)
                self._json(200, {'ok': True, 'dest': dest_real})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-FILEOPS-UPLOAD-V1
        # Upload binaire direct vers /mnt, sans dépendre de l'API REST TrueNAS.
        if path == '/upload':
            tmp_path = None
            try:
                qs = dict(parse_qsl(p.query, keep_blank_values=True))
                requested_path = str(qs.get('path', '') or '').strip()

                if not requested_path.startswith('/mnt/'):
                    raise PermissionError(
                        'Destination refusée : le chemin doit commencer par /mnt/'
                    )

                filename = os.path.basename(requested_path)
                if not filename or filename in ('.', '..'):
                    raise ValueError('Nom de fichier invalide')

                requested_parent = os.path.dirname(requested_path)
                real_parent = os.path.realpath(requested_parent)

                if real_parent != '/mnt' and not real_parent.startswith('/mnt/'):
                    raise PermissionError(
                        'Destination refusée : chemin hors de /mnt'
                    )

                if not os.path.isdir(real_parent):
                    raise FileNotFoundError(
                        'Dossier destination introuvable : ' + real_parent
                    )

                target_path = os.path.join(real_parent, filename)

                if os.path.isdir(target_path):
                    raise IsADirectoryError(
                        'La destination est un dossier : ' + target_path
                    )

                content_length = self.headers.get('Content-Length')
                if content_length is None:
                    raise ValueError('Content-Length absent')

                total_size = int(content_length)
                if total_size < 0:
                    raise ValueError('Taille de fichier invalide')

                tmp_path = (
                    target_path
                    + '.uploading-'
                    + str(os.getpid())
                    + '-'
                    + str(_threading.get_ident())
                )

                remaining = total_size

                with open(tmp_path, 'wb') as output:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))

                        if not chunk:
                            raise IOError(
                                'Upload interrompu avant réception complète'
                            )

                        output.write(chunk)
                        remaining -= len(chunk)

                    output.flush()
                    os.fsync(output.fileno())

                received_size = os.path.getsize(tmp_path)

                if received_size != total_size:
                    raise IOError(
                        'Taille reçue incorrecte : '
                        + str(received_size)
                        + ' au lieu de '
                        + str(total_size)
                    )

                os.replace(tmp_path, target_path)
                tmp_path = None

                try:
                    os.chmod(target_path, 0o644)
                except Exception:
                    pass

                log.info(
                    'Upload terminé : %s (%s octets)',
                    target_path,
                    total_size
                )

                _px_log('file.upload', _px_by(self), path=target_path, size=total_size)
                self._json(200, {
                    'ok': True,
                    'path': target_path,
                    'size': total_size,
                })

            except Exception as exc:
                if tmp_path:
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass

                log.exception('Échec upload File Station')
                self._json(500, {'error': str(exc)})

            return


        # MDM-LIBVIRT-REQUEST-GUARD-V1-BEGIN
        if _libvirt_path_requires_daemon(path):
            try:
                ensure_libvirtd()
            except Exception as error:
                self._json(503, {
                    'api': 'libvirt2' if path.startswith('/libvirt2') else 'libvirt',
                    'error': 'Service QEMU/libvirt indisponible : ' + str(error),
                    'autorecovery': 'failed',
                })
                return
        # MDM-LIBVIRT-REQUEST-GUARD-V1-END

        # MDM-ACCESS-POLICY-V1-POST-BEGIN
        if path == '/access/policy':
            try:
                body = self._body()
                policy = access_policy_set(
                    body.get('subject'),
                    body.get('applications', {}),
                    body.get('actor', 'unknown')
                )
                self._json(200, {'ok': True, 'policy': policy})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/access/history':
            try:
                event = access_history_add(self._body())
                self._json(200, {'ok': True, 'event': event})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        # MDM-ACCESS-POLICY-V1-POST-END

        # MDM-VM-BOOT-IMAGE-20260704: create an empty disk OR clone a selected bootable QCOW2 template.
        if path == '/libvirt2/vms':
            disk_path = None
            xml_path = None
            generated_disk = False
            try:
                body         = self._body()
                name         = re.sub(r'[^a-zA-Z0-9_-]', '-', str(body['name']).strip())
                mem_mb       = int(body.get('memory_mb', 4096))
                vcpus        = int(body.get('vcpus', 2))
                disk_gb      = int(body.get('disk_gb', 60))
                iso_path     = str(body.get('iso_path', '') or '').strip()
                source_image = str(body.get('source_image', '') or '').strip()
                uefi         = bool(body.get('uefi', True))
                secure_boot  = bool(body.get('secure_boot', False))
                tpm          = bool(body.get('tpm', False))
                network      = body.get('network', 'default')
                disk_bus     = body.get('disk_bus', 'sata')
                net_type     = body.get('net_type', 'nat')
                bridge_iface = body.get('bridge_iface', 'br0')
                net_model    = body.get('net_model', 'e1000')
                autostart   = bool(body.get('autostart', False))

                if not name:
                    raise ValueError('Nom de VM vide')
                if mem_mb < 512 or vcpus < 1:
                    raise ValueError('Ressources VM invalides')
                if disk_gb < 5:
                    raise ValueError('La taille de disque minimale est de 5 Go')
                if disk_bus not in ('sata', 'virtio', 'scsi', 'ide'):
                    raise ValueError('Bus disque invalide')

                disk_path = os.path.join(VM_DIR, name + '.qcow2')
                xml_path  = os.path.join(VM_DIR, name + '.xml')
                if os.path.exists(disk_path):
                    raise RuntimeError('Disque "' + disk_path + '" existe deja')

                # Create directory as root (container) — avoids SSH permission issues.
                os.makedirs(VM_DIR, mode=0o777, exist_ok=True)
                try:
                    os.chmod(VM_DIR, 0o777)
                except Exception:
                    pass

                import subprocess as _sp
                if source_image:
                    # A boot image is copied into VM_DIR so the original download/template remains untouched.
                    source_real = os.path.realpath(source_image)
                    allowed_roots = [os.path.realpath(ISO_DIR), os.path.realpath(VM_DIR)]
                    if not any(source_real == root or source_real.startswith(root.rstrip('/') + '/') for root in allowed_roots):
                        raise RuntimeError('L’image doit se trouver dans le stockage autorisé du NAS')
                    if not os.path.isfile(source_real):
                        raise RuntimeError('Image QCOW2 introuvable: ' + source_image)
                    if not source_real.lower().endswith('.qcow2'):
                        raise RuntimeError('Seules les images .qcow2 sont acceptées')

                    info = _sp.run(['qemu-img', 'info', '--output=json', source_real],
                                   capture_output=True, text=True, timeout=60)
                    if info.returncode != 0:
                        raise RuntimeError('qemu-img info: ' + (info.stderr or info.stdout).strip())
                    try:
                        image_info = json.loads(info.stdout)
                    except Exception:
                        raise RuntimeError('Impossible de lire les métadonnées de l’image QCOW2')
                    if str(image_info.get('format', '')).lower() != 'qcow2':
                        raise RuntimeError('Le fichier sélectionné n’est pas une image QCOW2 valide')
                    source_virtual = int(image_info.get('virtual-size') or 0)
                    requested_bytes = disk_gb * 1024 * 1024 * 1024
                    if source_virtual <= 0:
                        raise RuntimeError('Taille virtuelle de l’image QCOW2 invalide')
                    if requested_bytes < source_virtual:
                        minimum_gb = (source_virtual + 1024**3 - 1) // 1024**3
                        raise RuntimeError('La taille finale doit être au moins de ' + str(minimum_gb) + ' Go')

                    clone = _sp.run(['qemu-img', 'convert', '-p', '-O', 'qcow2', source_real, disk_path],
                                    capture_output=True, text=True, timeout=1800)
                    if clone.returncode != 0:
                        raise RuntimeError('qemu-img convert: ' + (clone.stderr or clone.stdout).strip())
                    generated_disk = True
                    if requested_bytes > source_virtual:
                        resize = _sp.run(['qemu-img', 'resize', disk_path, str(disk_gb) + 'G'],
                                         capture_output=True, text=True, timeout=180)
                        if resize.returncode != 0:
                            raise RuntimeError('qemu-img resize: ' + (resize.stderr or resize.stdout).strip())
                else:
                    # Standard VM: create a fresh empty QCOW2.
                    fresh = _sp.run(['qemu-img', 'create', '-f', 'qcow2', disk_path, str(disk_gb) + 'G'],
                                    capture_output=True, text=True, timeout=60)
                    if fresh.returncode != 0:
                        raise RuntimeError('qemu-img: ' + (fresh.stderr or fresh.stdout).strip())
                    generated_disk = True

                xml = build_vm_xml(
                    name, mem_mb, vcpus, disk_path, iso_path or None,
                    uefi, secure_boot, tpm, network, disk_bus, net_type,
                    bridge_iface, net_model,
                    boot_disk_first=bool(source_image)
                )
                with open(xml_path, 'w', encoding='utf-8') as f:
                    f.write(xml)
                log.info('VM XML written to %s:\n%s', xml_path, xml)

                out_define, err_define, code_define = ssh_exec(
                    "sudo -n virsh -c '" + VIRSH_URI + "' define " + shq(xml_path))
                if code_define != 0:
                    full = (err_define or out_define or '').strip()
                    raise RuntimeError('virsh define: ' + ' | '.join(full.splitlines()[:8]))

                # Persistent autostart after host/libvirtd restart.
                if autostart:
                    out_auto, err_auto, code_auto = ssh_exec(
                        "sudo -n virsh -c '" + VIRSH_URI + "' autostart " + shq(name), timeout=30)
                    if code_auto != 0:
                        full = (err_auto or out_auto or '').strip()
                        raise RuntimeError('virsh autostart: ' + ' | '.join(full.splitlines()[:8]))
                _vm_autostart_set(name, autostart)

                self._json(200, {
                    'ok': True,
                    'name': name,
                    'disk': disk_path,
                    'created_from_image': bool(source_image),
                    'autostart': autostart,
                })

            except Exception as e:
                # Do not leave an orphaned managed copy if VM definition fails.
                if generated_disk and disk_path and os.path.exists(disk_path):
                    try:
                        os.remove(disk_path)
                    except Exception:
                        pass
                if xml_path and os.path.exists(xml_path):
                    try:
                        os.remove(xml_path)
                    except Exception:
                        pass
                self._json(500, {'error': str(e)})
            return

        self._json(404, {'error': 'Not found: ' + path})

    def do_PUT(self):
        if not self._auth(): return
        path = self.path.split('?')[0].rstrip('/')

        # MDM-LICENSE-V1 : garde lecture seule
        if _lic_is_readonly():
            self._json(402, {'error': 'license_readonly',
                             'message': 'Abonnement requis — mode lecture seule.'})
            return

        # MDM-LIBVIRT-REQUEST-GUARD-V1-BEGIN
        if _libvirt_path_requires_daemon(path):
            try:
                ensure_libvirtd()
            except Exception as error:
                self._json(503, {
                    'api': 'libvirt2' if path.startswith('/libvirt2') else 'libvirt',
                    'error': 'Service QEMU/libvirt indisponible : ' + str(error),
                    'autorecovery': 'failed',
                })
                return
        # MDM-LIBVIRT-REQUEST-GUARD-V1-END

        # MDM-LIBVIRT2-SNAPSHOTS-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/snapshots/create
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots/create$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_snapshot_create(
                    name,
                    body.get('label', ''),
                    body.get('description', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/snapshots/revert
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots/revert$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                snapshot = _lv2_snapshot_name_norm(body.get('snapshot', ''))

                snap_data = _lv2_snapshot_list(name)
                current = str(snap_data.get('current') or '').strip()

                if current == snapshot:
                    self._json(200, {
                        'api': 'libvirt2',
                        'ok': True,
                        'name': name,
                        'action': 'snapshot_revert',
                        'snapshot': snapshot,
                        'already_current': True,
                        'warning': 'Snapshot déjà courant : aucune restauration nécessaire.',
                        'snapshots': snap_data.get('snapshots', []),
                    })
                    return

                self._json(200, _lv2_snapshot_revert(name, snapshot))
            except Exception as e:
                self._json(500, {
                    'api': 'libvirt2',
                    'error': str(e) or repr(e)
                })
            return

        # PUT /libvirt2/vms/{name}/snapshots/delete
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots/delete$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_snapshot_delete(
                    name,
                    body.get('snapshot', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-SNAPSHOTS-ROUTES-20260710
        # GET /libvirt2/vms/{name}/snapshots
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                self._json(200, _lv2_snapshot_list(name))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # MDM-LIBVIRT2-NETWORK-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/nics/add
        m = re.match(r'^/libvirt2/vms/([^/]+)/nics/add$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_add_nic(
                    name,
                    body.get('source', 'default'),
                    body.get('model', 'virtio'),
                    body.get('net_type', 'nat'),
                    body.get('mac', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/nics/mac
        m = re.match(r'^/libvirt2/vms/([^/]+)/nics/mac$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_set_nic_mac(
                    name,
                    body.get('mac', ''),
                    body.get('new_mac', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/nics/remove
        m = re.match(r'^/libvirt2/vms/([^/]+)/nics/remove$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_remove_nic(
                    name,
                    body.get('mac', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-DISK-REMOVE-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/disks/remove
        m = re.match(r'^/libvirt2/vms/([^/]+)/disks/remove$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_remove_disk(
                    name,
                    body.get('path'),
                    body.get('delete_file', False)
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-DISK-RESIZE-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/disks/resize
        m = re.match(r'^/libvirt2/vms/([^/]+)/disks/resize$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_resize_disk(
                    name,
                    body.get('path'),
                    body.get('new_size_gb')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-DISKS-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/disks/add
        m = re.match(r'^/libvirt2/vms/([^/]+)/disks/add$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_add_disk(
                    name,
                    body.get('size_gb'),
                    body.get('bus', 'virtio'),
                    body.get('label', 'data')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-RESOURCES-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/resources
        m = re.match(r'^/libvirt2/vms/([^/]+)/resources$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_set_resources(
                    name,
                    body.get('vcpus'),
                    body.get('memory_mb')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-STATE-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/state
        m = re.match(r'^/libvirt2/vms/([^/]+)/state$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                action = body.get('action', '')
                self._json(200, _lv2_vm_state_action(name, action))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/autostart
        m = re.match(r'^/libvirt2/vms/([^/]+)/autostart$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                enabled = bool(body.get('enabled', False))
                self._json(200, _lv2_set_autostart(name, enabled))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/repair-uefi — régénère le NVRAM puis redémarre
        m = re.match(r'^/libvirt2/vms/([^/]+)/repair-uefi$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                self._json(200, _lv2_repair_uefi(name))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/disk-bus — change l'interface du disque principal
        m = re.match(r'^/libvirt2/vms/([^/]+)/disk-bus$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_set_disk_bus(name, body.get('bus', '')))
            except (ValueError, PermissionError) as e:
                self._json(400, {'api': 'libvirt2', 'error': str(e)})
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-INSTALLATION-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/iso
        m = re.match(r'^/libvirt2/vms/([^/]+)/iso$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                iso_path = str(body.get('iso_path', '') or '').strip()
                self._json(200, _lv2_set_iso(name, iso_path))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/boot
        m = re.match(r'^/libvirt2/vms/([^/]+)/boot$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                order = body.get('order', [])
                self._json(200, _lv2_set_boot(name, order))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        self._json(404, {'error': 'Not found: ' + path})

    def do_DELETE(self):
        if not self._auth(): return
        path = self.path.split('?')[0].rstrip('/')

        # MDM-LICENSE-V1 : garde lecture seule
        if _lic_is_readonly():
            self._json(402, {'error': 'license_readonly',
                             'message': 'Abonnement requis — mode lecture seule.'})
            return

        # MDM-LIBVIRT-REQUEST-GUARD-V1-BEGIN
        if _libvirt_path_requires_daemon(path):
            try:
                ensure_libvirtd()
            except Exception as error:
                self._json(503, {
                    'api': 'libvirt2' if path.startswith('/libvirt2') else 'libvirt',
                    'error': 'Service QEMU/libvirt indisponible : ' + str(error),
                    'autorecovery': 'failed',
                })
                return
        # MDM-LIBVIRT-REQUEST-GUARD-V1-END

        # File deletion
        if path == '/delete':
            try:
                body = self._body()
                target = body.get('path', '')
                if not target:
                    raise ValueError('Chemin requis')
                # MDM-FILES-PLUS-V1 : « trash » demande la corbeille. Si elle est impossible, on le dit : rien n'est
                # supprimé pour de bon à la place.
                if body.get('trash'):
                    rec = _px_trash(target, _px_by(self))
                    _px_log('file.trash', _px_by(self), path=str(target), is_dir=rec['is_dir'] or None)
                    self._json(200, {'ok': True, 'trashed': True, 'id': rec['id'], 'root': rec['root']})
                    return
                _privileged_remove(target)
                _px_log('file.delete', _px_by(self), path=str(target))
                self._json(200, {'ok': True})
            except _PxNoTrash as e:
                self._json(409, {'error': str(e), 'code': e.code})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # VM deletion
        m = re.match(r'^/libvirt2/vms/([^/]+)$', path)
        if m:
            from urllib.parse import unquote
            name = unquote(m.group(1))
            try:
                v = "sudo -n virsh -c '" + VIRSH_URI + "'"
                # Get disk list before undefine
                disks = virsh_domblklist(name)
                qcow2s = [d['source'] for d in disks
                          if d.get('source', '-') != '-' and d['source'].endswith('.qcow2')]
                # Force stop if running
                ssh_exec(v + ' destroy ' + shq(name))
                # Undefine (try with --nvram for UEFI VMs)
                out, err, code = ssh_exec(
                    v + ' undefine ' + shq(name) + ' --nvram --managed-save --snapshots-metadata'
                )
                if code != 0:
                    ssh_ok(v + ' undefine ' + shq(name))
                # Delete disk files
                for disk in qcow2s:
                    try:
                        os.remove(disk)
                    except Exception:
                        pass
                # Delete XML if present
                xml_path = os.path.join(VM_DIR, name + '.xml')
                try:
                    os.remove(xml_path)
                except Exception:
                    pass
                self._json(200, {'api': 'libvirt2', 'ok': True, 'name': name})
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        self._json(404, {'error': 'Not found: ' + path})


# ── WebSocket server launcher ────────────────────────────────────────────────
def ws_server_start(port):
    import asyncio
    async def _serve():
        try:
            import websockets as _ws
            async with _ws.serve(ws_handler, '0.0.0.0', port):
                await asyncio.Future()
        except Exception as e:
            log.error('WS server error: %s', e)
    asyncio.run(_serve())

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import socketserver as _ss

    HTTP_PORT = int(os.environ.get('FILEOPS_PORT', 8765))
    WS_PORT   = int(os.environ.get('FILEOPS_WS_PORT', 8766))

    class _TCPServer(_ss.ThreadingMixIn, _ss.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    log.info('fileops HTTP :%d  WS :%d', HTTP_PORT, WS_PORT)

    import threading as _th
    t = _th.Thread(target=ws_server_start, args=(WS_PORT,), daemon=True)
    t.start()


# MDM-LIBVIRT2-SAFE-COPY-SNAPSHOTS-20260710
# Snapshots sûrs Desktroo.
# Principe : VM arrêtée uniquement, copie qcow2/XML/NVRAM dans /mnt/Truenas_Stockage/vms/_snapshots/<VM>/<snapshot-id>/
# Pas de virsh snapshot-delete, pas de qemu-img snapshot -d.

import json as _lv2_safe_json
import time as _lv2_safe_time
import re as _lv2_safe_re
import xml.etree.ElementTree as _lv2_safe_ET

_LV2_SAFE_SNAPSHOT_ROOT = VM_DIR.rstrip('/') + '/_snapshots'


def _lv2_safe_snapshot_id(label=""):
    raw = str(label or "").strip().lower()
    raw = raw.replace(" ", "-")
    raw = _lv2_safe_re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-._")
    if not raw:
        raw = "snapshot"
    raw = raw[:40]
    return "safe-" + _lv2_safe_time.strftime("%Y%m%d-%H%M%S") + "-" + raw


def _lv2_safe_vm_dir(name):
    safe = _lv2_safe_re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "").strip())
    if not safe:
        raise RuntimeError("Nom VM invalide")
    return _LV2_SAFE_SNAPSHOT_ROOT.rstrip("/") + "/" + safe


def _lv2_safe_sh(cmd, timeout=30):
    out, err, code = ssh_exec(cmd, timeout=timeout)
    if code != 0:
        msg = (err or out or "").strip()
        if not msg:
            msg = "Commande échouée sans message, code=" + str(code)
        raise RuntimeError(msg)
    return out


def _lv2_safe_file_basename(path):
    path = str(path or "").rstrip("/")
    if not path:
        return ""
    return path.split("/")[-1]


def _lv2_safe_vm_files_from_xml(name):
    xml = _lv2_safe_sh(
        "timeout 20s sudo -n virsh -c " + shq(VIRSH_URI) + " dumpxml " + shq(name),
        timeout=30
    )

    root = _lv2_safe_ET.fromstring(xml)

    disks = []
    for disk in root.findall("./devices/disk"):
        if disk.get("device") != "disk":
            continue
        if disk.get("type") != "file":
            continue

        source = disk.find("source")
        target = disk.find("target")
        driver = disk.find("driver")

        src = ""
        if source is not None:
            src = source.get("file") or ""

        if not src:
            continue

        tgt = ""
        bus = ""
        if target is not None:
            tgt = target.get("dev") or ""
            bus = target.get("bus") or ""

        fmt = ""
        if driver is not None:
            fmt = driver.get("type") or ""

        base = _lv2_safe_file_basename(src)
        if not base:
            base = (tgt or "disk") + ".img"

        dest_name = base
        if tgt:
            dest_name = tgt + "-" + base

        disks.append({
            "source": src,
            "target": tgt,
            "bus": bus,
            "format": fmt,
            "dest": "disks/" + dest_name,
        })

    nvram = ""
    nvram_el = root.find("./os/nvram")
    if nvram_el is not None and nvram_el.text:
        nvram = nvram_el.text.strip()

    nvram_item = None
    if nvram:
        nvram_item = {
            "source": nvram,
            "dest": "nvram/" + _lv2_safe_file_basename(nvram),
        }

    return xml, disks, nvram_item


def _lv2_safe_snapshot_meta_read(vm_dir, snap_id):
    meta_path = vm_dir.rstrip("/") + "/" + snap_id + "/metadata.json"
    cmd = "test -f " + shq(meta_path) + " && cat " + shq(meta_path) + " || true"
    out = ssh_exec(cmd, timeout=10)[0].strip()
    if not out:
        return None
    try:
        return _lv2_safe_json.loads(out)
    except Exception:
        return {
            "id": snap_id,
            "name": snap_id,
            "status": "unknown",
            "error": "metadata.json illisible",
        }


def _lv2_snapshot_list(name):
    vm_dir = _lv2_safe_vm_dir(name)

    cmd = (
        "if [ -d " + shq(vm_dir) + " ]; then "
        "find " + shq(vm_dir) + " -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null | sort -r; "
        "fi"
    )

    out, err, code = ssh_exec(cmd, timeout=15)
    if code != 0:
        raise RuntimeError((err or out or "Impossible de lister les snapshots sûrs").strip())

    items = []
    for snap_id in [x.strip() for x in out.splitlines() if x.strip()]:
        meta = _lv2_safe_snapshot_meta_read(vm_dir, snap_id)
        if not meta:
            meta = {
                "id": snap_id,
                "name": snap_id,
                "status": "unknown",
            }

        items.append({
            "id": meta.get("id") or snap_id,
            "name": meta.get("name") or meta.get("id") or snap_id,
            "label": meta.get("label") or "",
            "description": meta.get("description") or "",
            "status": meta.get("status") or "unknown",
            "created_at": meta.get("created_at") or "",
            "finished_at": meta.get("finished_at") or "",
            "engine": "safe-copy",
            "current": False,
            "state": meta.get("status") or "unknown",
            "creation_time": None,
            "creation_time_iso": meta.get("created_at") or "",
            "parent": "",
            "error": meta.get("error") or "",
            "path": meta.get("path") or (vm_dir.rstrip("/") + "/" + snap_id),
            "disks": meta.get("disks") or [],
            "nvram": meta.get("nvram") or None,
        })

    return {
        "api": "libvirt2",
        "readonly": False,
        "name": name,
        "disabled": False,
        "engine": "safe-copy",
        "current": "",
        "snapshots": items,
        "root": vm_dir,
        "warning": "Snapshots sûrs par copie qcow2/XML/NVRAM. VM arrêtée obligatoire.",
    }


# MDM-LIBVIRT2-SAFE-COPY-SNAPSHOTS-SUDO-COPY-20260710
# Override safe-copy : copie des disques/NVRAM avec sudo pour gérer les permissions libvirt.

def _lv2_snapshot_create(name, label="", description=""):
    _lv2_snapshot_vm_must_be_stopped(name)

    label = str(label or "").strip()
    description = str(description or "").strip()
    snap_id = _lv2_safe_snapshot_id(label)
    vm_dir = _lv2_safe_vm_dir(name)
    snap_dir = vm_dir.rstrip("/") + "/" + snap_id

    xml, disks, nvram = _lv2_safe_vm_files_from_xml(name)

    if not disks:
        raise RuntimeError("Aucun disque fichier détecté pour cette VM")

    created_at = _lv2_safe_time.strftime("%Y-%m-%dT%H:%M:%S%z")

    meta = {
        "id": snap_id,
        "name": snap_id,
        "label": label,
        "description": description,
        "vm": name,
        "engine": "safe-copy",
        "status": "creating",
        "created_at": created_at,
        "finished_at": "",
        "path": snap_dir,
        "domain_xml": "domain.xml",
        "disks": disks,
        "nvram": nvram,
        "error": "",
    }

    meta_json = _lv2_safe_json.dumps(meta, ensure_ascii=False, indent=2)

    copy_lines = []
    copy_lines.append("set -e")
    copy_lines.append("mkdir -p " + shq(snap_dir + "/disks") + " " + shq(snap_dir + "/nvram") + " " + shq(snap_dir + "/logs"))
    copy_lines.append("cat > " + shq(snap_dir + "/metadata.json") + " <<'EOF_META'\n" + meta_json + "\nEOF_META")
    copy_lines.append("sudo -n virsh -c " + shq(VIRSH_URI) + " dumpxml " + shq(name) + " > " + shq(snap_dir + "/domain.xml"))

    for d in disks:
        dest = snap_dir + "/" + d["dest"]
        copy_lines.append("sudo -n test -f " + shq(d["source"]))
        copy_lines.append("sudo -n cp --reflink=auto --sparse=always " + shq(d["source"]) + " " + shq(dest))
        copy_lines.append("sudo -n chown $(id -u):$(id -g) " + shq(dest) + " || true")
        copy_lines.append("chmod u+rw " + shq(dest) + " || true")

    if nvram:
        dest = snap_dir + "/" + nvram["dest"]
        copy_lines.append("if sudo -n test -f " + shq(nvram["source"]) + "; then")
        copy_lines.append("  sudo -n cp --reflink=auto --sparse=always " + shq(nvram["source"]) + " " + shq(dest))
        copy_lines.append("  sudo -n chown $(id -u):$(id -g) " + shq(dest) + " || true")
        copy_lines.append("  chmod u+rw " + shq(dest) + " || true")
        copy_lines.append("fi")

    copy_lines.append(
        "python3 - <<'EOF_DONE'\n"
        "import json, time\n"
        "p=" + repr(snap_dir + "/metadata.json") + "\n"
        "m=json.load(open(p,'r',encoding='utf-8'))\n"
        "m['status']='ready'\n"
        "m['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')\n"
        "m['error']=''\n"
        "json.dump(m,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
        "EOF_DONE"
    )

    script = "\n".join(copy_lines)

    wrapped = (
        "mkdir -p " + shq(snap_dir + "/logs") + "; "
        "cat > " + shq(snap_dir + "/create.sh") + " <<'EOF_SCRIPT'\n"
        + script +
        "\nEOF_SCRIPT\n"
        "chmod +x " + shq(snap_dir + "/create.sh") + "; "
        "( " + shq(snap_dir + "/create.sh") + " > " + shq(snap_dir + "/logs/create.log") + " 2>&1 || "
        "python3 - <<'EOF_ERR'\n"
        "import json, time\n"
        "p=" + repr(snap_dir + "/metadata.json") + "\n"
        "try:\n"
        "    m=json.load(open(p,'r',encoding='utf-8'))\n"
        "except Exception:\n"
        "    m={}\n"
        "m['status']='error'\n"
        "m['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')\n"
        "try:\n"
        "    m['error']=open(" + repr(snap_dir + "/logs/create.log") + ",'r',encoding='utf-8',errors='replace').read()[-4000:]\n"
        "except Exception as e:\n"
        "    m['error']=str(e)\n"
        "json.dump(m,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
        "EOF_ERR\n"
        ") & echo $!"
    )

    out, err, code = ssh_exec(wrapped, timeout=20)
    if code != 0:
        raise RuntimeError((err or out or "Impossible de lancer la création du snapshot sûr").strip())

    return {
        "api": "libvirt2",
        "ok": True,
        "name": name,
        "action": "safe_snapshot_create",
        "engine": "safe-copy",
        "snapshot": snap_id,
        "status": "creating",
        "pid": out.strip(),
        "path": snap_dir,
        "message": "Création du snapshot sûr lancée en tâche de fond.",
        "snapshots": _lv2_snapshot_list(name).get("snapshots", []),
    }


def _lv2_snapshot_delete(name, snapshot):
    _lv2_snapshot_vm_must_be_stopped(name)

    snap_id = _lv2_snapshot_name_norm(snapshot)
    vm_dir = _lv2_safe_vm_dir(name)
    snap_dir = vm_dir.rstrip("/") + "/" + snap_id

    if not snap_dir.startswith(vm_dir.rstrip("/") + "/"):
        raise RuntimeError("Chemin snapshot invalide")

    cmd = (
        "test -d " + shq(snap_dir) + " || { echo 'Snapshot sûr introuvable'; exit 2; }; "
        "if [ -f " + shq(snap_dir + "/metadata.json") + " ]; then "
        "python3 - <<'EOF_MARK'\n"
        "import json\n"
        "p=" + repr(snap_dir + "/metadata.json") + "\n"
        "try:\n"
        "    m=json.load(open(p,'r',encoding='utf-8'))\n"
        "    m['status']='deleting'\n"
        "    m['finished_at']=''\n"
        "    json.dump(m,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
        "except Exception:\n"
        "    pass\n"
        "EOF_MARK\n"
        "fi; "
        "( sudo -n rm -rf -- " + shq(snap_dir) + " ) >/dev/null 2>&1 & echo $!"
    )

    out, err, code = ssh_exec(cmd, timeout=15)
    if code != 0:
        raise RuntimeError((err or out or "Impossible de lancer la suppression du snapshot sûr").strip())

    return {
        "api": "libvirt2",
        "ok": True,
        "name": name,
        "action": "safe_snapshot_delete",
        "engine": "safe-copy",
        "snapshot": snap_id,
        "status": "deleting",
        "pid": out.strip(),
        "message": "Suppression du snapshot sûr lancée en tâche de fond.",
        "snapshots": _lv2_snapshot_list(name).get("snapshots", []),
    }



# MDM-LIBVIRT2-SAFE-COPY-RESTORE-20260711
_LV2_SAFE_RESTORE_BACKUP_ROOT = VM_DIR.rstrip('/') + '/_restore_backups'


def _lv2_safe_restore_relpath(value, prefix):
    value = str(value or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise RuntimeError("Chemin snapshot invalide")
    if prefix and not value.startswith(prefix.rstrip("/") + "/"):
        raise RuntimeError("Chemin snapshot inattendu: " + value)
    return value


def _lv2_snapshot_revert(name, snapshot):
    _lv2_snapshot_vm_must_be_stopped(name)

    snap_id = _lv2_snapshot_name_norm(snapshot)
    vm_dir = _lv2_safe_vm_dir(name)
    snap_dir = vm_dir.rstrip("/") + "/" + snap_id
    if not snap_dir.startswith(vm_dir.rstrip("/") + "/"):
        raise RuntimeError("Chemin snapshot invalide")

    meta = _lv2_safe_snapshot_meta_read(vm_dir, snap_id)
    if not meta:
        raise RuntimeError("Snapshot sûr introuvable")
    if meta.get("engine") != "safe-copy":
        raise RuntimeError("Le snapshot n'est pas de type safe-copy")
    if meta.get("status") != "ready":
        raise RuntimeError("Snapshot non restaurable: " + str(meta.get("status") or "unknown"))
    if str(meta.get("vm") or "") != str(name):
        raise RuntimeError("Le snapshot appartient à une autre VM")

    saved_disks = meta.get("disks")
    if not isinstance(saved_disks, list) or not saved_disks:
        raise RuntimeError("metadata.json ne contient aucun disque")

    _xml, current_disks, current_nvram = _lv2_safe_vm_files_from_xml(name)
    current_by_target = {str(item.get("target") or ""): item for item in current_disks}
    if len(current_by_target) != len(current_disks):
        raise RuntimeError("Cibles de disques actuelles invalides")

    items = []
    for index, saved in enumerate(saved_disks):
        if not isinstance(saved, dict):
            raise RuntimeError("Entrée disque invalide")
        target = str(saved.get("target") or "")
        current = current_by_target.get(target)
        if not target or current is None:
            raise RuntimeError("Le disque " + (target or "?") + " n'existe plus")
        if str(saved.get("source") or "") != str(current.get("source") or ""):
            raise RuntimeError("Le chemin actif du disque " + target + " a changé")
        rel = _lv2_safe_restore_relpath(saved.get("dest"), "disks")
        items.append({
            "snapshot": snap_dir + "/" + rel,
            "active": current["source"],
            "backup": "disk-" + str(index) + "-" + _lv2_safe_file_basename(current["source"]),
        })

    if len(items) != len(current_disks):
        raise RuntimeError("Le nombre de disques du snapshot diffère de la VM actuelle")

    saved_nvram = meta.get("nvram")
    if saved_nvram:
        if not isinstance(saved_nvram, dict) or not current_nvram:
            raise RuntimeError("Configuration NVRAM incompatible")
        if str(saved_nvram.get("source") or "") != str(current_nvram.get("source") or ""):
            raise RuntimeError("Le chemin NVRAM actif a changé")
        rel = _lv2_safe_restore_relpath(saved_nvram.get("dest"), "nvram")
        items.append({
            "snapshot": snap_dir + "/" + rel,
            "active": current_nvram["source"],
            "backup": "nvram-" + _lv2_safe_file_basename(current_nvram["source"]),
        })
    elif current_nvram:
        raise RuntimeError("Le snapshot ne contient pas la NVRAM actuelle")

    domain_rel = _lv2_safe_restore_relpath(meta.get("domain_xml") or "domain.xml", "")
    domain_snapshot = snap_dir + "/" + domain_rel
    restore_id = "restore-" + _lv2_safe_time.strftime("%Y%m%d-%H%M%S")
    safe_vm = _lv2_safe_re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))
    backup_dir = _LV2_SAFE_RESTORE_BACKUP_ROOT + "/" + safe_vm + "/" + restore_id
    domain_backup = backup_dir + "/domain-before.xml"
    script_path = snap_dir + "/" + restore_id + ".sh"
    log_path = snap_dir + "/logs/" + restore_id + ".log"

    lines = [
        "#!/bin/bash",
        "set -Eeuo pipefail",
        "umask 077",
        "mkdir -p " + shq(backup_dir) + " " + shq(snap_dir + "/logs"),
        "sudo -n test -f " + shq(domain_snapshot),
        "sudo -n virsh -c " + shq(VIRSH_URI) + " dumpxml --inactive " + shq(name)
        + " > " + shq(domain_backup),
    ]

    pairs = []
    for item in items:
        backup = backup_dir + "/" + item["backup"]
        temp = item["active"] + ".truenas-" + restore_id
        pairs.append((item, backup, temp))
        lines.extend([
            "sudo -n test -f " + shq(item["snapshot"]),
            "sudo -n test -f " + shq(item["active"]),
            "sudo -n cp --reflink=auto --sparse=always --preserve=all "
            + shq(item["active"]) + " " + shq(backup),
        ])

    lines.extend(["rollback() {", "  rc=$?", "  set +e"])
    for item, backup, temp in pairs:
        lines.extend([
            "  sudo -n rm -f -- " + shq(temp),
            "  sudo -n cp --reflink=auto --sparse=always --preserve=all "
            + shq(backup) + " " + shq(item["active"]),
        ])
    lines.extend([
        "  sudo -n virsh -c " + shq(VIRSH_URI) + " define " + shq(domain_backup) + " >/dev/null 2>&1",
        "  exit $rc",
        "}",
        "trap rollback ERR",
    ])

    for item, _backup, temp in pairs:
        lines.extend([
            "sudo -n rm -f -- " + shq(temp),
            "sudo -n cp --reflink=auto --sparse=always " + shq(item["snapshot"]) + " " + shq(temp),
            "sudo -n chown --reference=" + shq(item["active"]) + " " + shq(temp),
            "sudo -n chmod --reference=" + shq(item["active"]) + " " + shq(temp),
            "sudo -n mv -f -- " + shq(temp) + " " + shq(item["active"]),
        ])

    lines.extend([
        "sudo -n virsh -c " + shq(VIRSH_URI) + " define " + shq(domain_snapshot),
        "trap - ERR",
    ])
    script = "\n".join(lines)

    marked = dict(meta)
    marked["status"] = "restoring"
    marked["error"] = ""
    marked_json = _lv2_safe_json.dumps(marked, ensure_ascii=False, indent=2)

    wrapped = (
        "mkdir -p " + shq(snap_dir + "/logs") + "; "
        "cat > " + shq(snap_dir + "/metadata.json") + " <<'EOF_META'\n"
        + marked_json + "\nEOF_META\n"
        "cat > " + shq(script_path) + " <<'EOF_SCRIPT'\n"
        + script + "\nEOF_SCRIPT\n"
        "chmod 700 " + shq(script_path) + "; "
        "( " + shq(script_path) + " > " + shq(log_path) + " 2>&1; rc=$?; "
        "python3 - " + shq(snap_dir + "/metadata.json") + " " + shq(log_path)
        + " " + shq(backup_dir) + " $rc <<'EOF_STATUS'\n"
        "import json, sys, time\n"
        "p, log, backup, rc = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])\n"
        "m=json.load(open(p,'r',encoding='utf-8'))\n"
        "m['status']='ready'\n"
        "m['last_restore']={'status':'success' if rc == 0 else 'error','finished_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'backup':backup}\n"
        "if rc:\n"
        "    m['error']='Dernière restauration échouée: '+open(log,'r',encoding='utf-8',errors='replace').read()[-4000:]\n"
        "else:\n"
        "    m['error']=''\n"
        "json.dump(m,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
        "EOF_STATUS\n"
        ") & echo $!"
    )

    out, err, code = ssh_exec(wrapped, timeout=20)
    if code != 0:
        raise RuntimeError((err or out or "Impossible de lancer la restauration sûre").strip())

    return {
        "api": "libvirt2",
        "ok": True,
        "name": name,
        "action": "safe_snapshot_restore",
        "engine": "safe-copy",
        "snapshot": snap_id,
        "status": "restoring",
        "pid": out.strip(),
        "backup": backup_dir,
        "log": log_path,
        "message": "Restauration sûre lancée en tâche de fond.",
    }

# MDM-LIBVIRT-WATCHDOG-START-V1-BEGIN
_threading.Thread(
    target=_libvirt_watchdog,
    name='libvirt-autorecover',
    daemon=True,
).start()
# MDM-LIBVIRT-WATCHDOG-START-V1-END

# MDM-HOST-BOOTSTRAP-START-V1 : configure le host (libvirtd/polkit/réseau) au boot.
if os.environ.get('HOST_BOOTSTRAP', '1').lower() not in ('0', 'false', 'no', 'off'):
    _threading.Thread(
        target=_host_bootstrap,
        name='host-bootstrap',
        daemon=True,
    ).start()

# ── Sauvegarde et restauration de Desktroo (MDM-DKBACKUP-V1) ───────────────────────────────────────
# Une archive .tar.gz des réglages de Desktroo (dossier de données + fonds d'écran), rangée dans un dossier du NAS
# et téléchargeable ; en plus, un instantané ZFS horodaté du dataset de chaque app du Magasin (retour en arrière
# instantané, sans copie). La licence et l'identité de l'installation n'en font pas partie : elles restent celles du NAS.
import tarfile as _bk_tarfile
import io as _bk_io
import shutil as _bk_shutil

DKBK_SETTINGS = os.path.join(ACCESS_DATA_DIR, 'dkbackup.json')
DKBK_NAME_RE  = re.compile(r'^desktroo-(?:backup|avant-restauration)-\d{8}-\d{6}\.tar\.gz$')
DKBK_SNAP_RE  = re.compile(r'^dkbak-\d{8}-\d{6}$')
DKBK_INTERVAL = 7 * 86400
DKBK_EXCLUDE  = {'license.json', 'activation-key', 'license-refresh.json', 'install-id', 'trial.json', 'trial-fp.json',
                 'dkbackup.json', 'images-fresh.json', 'activity.jsonl'}
DKBK_EXCLUDE_PATHS = {'store/catalog.json'}
DKBK_MAX_UPLOAD = 512 * 1024 * 1024
_dkbk_lock = _threading.Lock()


def _dkbk_settings():
    d = _lic_read_json(DKBK_SETTINGS)
    d = d if isinstance(d, dict) else {}
    keep = d.get('keep')
    return {'auto': bool(d.get('auto', True)),
            'dir': str(d.get('dir') or os.path.join(APP_DIR or ACCESS_DATA_DIR, 'backups')),
            'keep': int(keep) if isinstance(keep, int) and 1 <= keep <= 60 else 4,
            'last': int(d.get('last') or 0), 'last_error': str(d.get('last_error') or '')}


def _dkbk_save(s):
    _lic_write_json(DKBK_SETTINGS, {k: s[k] for k in ('auto', 'dir', 'keep', 'last', 'last_error')})


def _dkbk_dir_ok(path):
    p = os.path.normpath(str(path or ''))
    return p.startswith('/mnt/') and '..' not in p.split('/') and len(p) < 400


def _dkbk_stamp(ts=None):
    return _lic_time.strftime('%Y%m%d-%H%M%S', _lic_time.localtime(ts if ts else _lic_time.time()))


def _dkbk_included(rel):
    rel = rel.replace('\\', '/')
    if rel in DKBK_EXCLUDE_PATHS or rel.startswith('.'):
        return False
    return os.path.basename(rel) not in DKBK_EXCLUDE and not rel.endswith(('.tmp', '.lock'))


def _dkbk_apps():
    out = []
    for app_id, rec in sorted(store_installed_get().items()):
        ds = str((rec or {}).get('dataset') or '')
        if _STORE_DS_RE.match(ds) and not any(part in ('.', '..') for part in ds.split('/')):
            out.append((app_id, ds))
    return out


def dkbackup_build(dest_path, kind='backup'):
    """Écrit l'archive dans dest_path. Rend le nombre de fichiers.
    L'archive se construit d'abord dans le dossier temporaire du conteneur, puis se copie à sa place : sur un dossier du NAS
    à ACL (SMB/NFSv4), ni le changement de droits ni le renommage d'un fichier ne sont toujours permis ; créer et écrire, si."""
    fd, tmp = _tempfile.mkstemp(prefix='dkbk-', suffix='.tar.gz')
    os.close(fd)
    n = 0
    try:
        meta = {'format': 'desktroo-backup', 'version': 1, 'app_version': APP_VERSION, 'created': int(_lic_time.time()),
                'kind': kind, 'apps': [a for a, _d in _dkbk_apps()]}
        with _bk_tarfile.open(tmp, 'w:gz') as tf:
            raw = json.dumps(meta).encode('utf-8')
            ti = _bk_tarfile.TarInfo('desktroo-backup.json'); ti.size = len(raw); ti.mtime = meta['created']; ti.mode = 0o600
            tf.addfile(ti, _bk_io.BytesIO(raw))
            for base, prefix in ((ACCESS_DATA_DIR, 'data'), (os.path.join(APP_DIR, 'wallpapers') if APP_DIR else '', 'wallpapers'), (LANG_DIR, 'lang')):
                if not base or not os.path.isdir(base):
                    continue
                for root, dirs, files in os.walk(base):
                    dirs[:] = [d for d in dirs if not d.startswith('.')]
                    for fn in files:
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, base)
                        if os.path.islink(full) or (prefix == 'data' and not _dkbk_included(rel)):
                            continue
                        try:
                            tf.add(full, arcname=prefix + '/' + rel.replace(os.sep, '/'), recursive=False)
                            n += 1
                        except OSError:
                            pass
        try:
            with open(tmp, 'rb') as src, open(dest_path, 'wb') as dst:
                _bk_shutil.copyfileobj(src, dst)
        except OSError as e:
            raise OSError('écriture dans %s impossible (%s). Choisissez un autre dossier, ou donnez au compte de Desktroo le droit d\'y écrire.'
                          % (os.path.dirname(dest_path), e.strerror or e))
        try:
            os.chmod(dest_path, 0o600)
        except OSError:
            pass   # dataset à ACL : le mode ne se change pas, le fichier reste utilisable
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return n


def dkbackup_snapshots(create=False, keep=4):
    """Instantanés ZFS 'dkbak-…' des datasets d'apps. create=True en prend un nouveau, puis élague à `keep` par dataset.
    Rend (liste [{app, dataset, name, ts}], erreurs)."""
    snaps, errs = [], []
    stamp = 'dkbak-' + _dkbk_stamp()
    for app_id, ds in _dkbk_apps():
        try:
            if create:
                out, err, code = _px_host('zfs snapshot %s' % shq(ds + '@' + stamp), timeout=60)
                if code != 0 and 'dataset does not exist' not in (err or out or ''):
                    errs.append('%s : %s' % (app_id, (err or out or 'échec').strip()[-160:]))
            out, err, code = _px_host('zfs list -H -p -t snapshot -d 1 -o name,creation -s creation %s 2>/dev/null' % shq(ds), timeout=60)
            rows = []
            for line in (out or '').splitlines():
                nm, _t, cr = line.partition('\t')
                sn = nm.split('@', 1)[-1]
                if DKBK_SNAP_RE.match(sn):
                    rows.append((sn, int(cr.strip() or 0)))
            rows.sort(key=lambda r: r[0])
            if create and len(rows) > keep:
                for sn, _cr in rows[:len(rows) - keep]:
                    _px_host('zfs destroy %s' % shq(ds + '@' + sn), timeout=120)
                rows = rows[len(rows) - keep:]
            for sn, cr in rows:
                snaps.append({'app': app_id, 'dataset': ds, 'name': sn, 'ts': cr})
        except Exception as e:
            errs.append('%s : %s' % (app_id, e))
    return snaps, errs


def dkbackup_list():
    s = _dkbk_settings()
    rows = []
    try:
        for fn in os.listdir(s['dir']):
            if DKBK_NAME_RE.match(fn):
                st = os.stat(os.path.join(s['dir'], fn))
                rows.append({'name': fn, 'size': st.st_size, 'ts': int(st.st_mtime), 'safety': fn.startswith('desktroo-avant')})
    except OSError:
        pass
    rows.sort(key=lambda r: r['name'], reverse=True)
    return rows


def dkbackup_run(manual=False):
    """Archive + instantanés + rotation. Rend (code, dict)."""
    if not _dkbk_lock.acquire(blocking=False):
        return 409, {'ok': False, 'error': 'Une sauvegarde est déjà en cours.'}
    try:
        s = _dkbk_settings()
        if not _dkbk_dir_ok(s['dir']):
            return 400, {'ok': False, 'error': 'Dossier de sauvegarde invalide : il doit se trouver sous /mnt.'}
        try:
            os.makedirs(s['dir'], exist_ok=True)
            name = 'desktroo-backup-%s.tar.gz' % _dkbk_stamp()
            n = dkbackup_build(os.path.join(s['dir'], name))
        except Exception as e:
            s['last_error'] = 'Archive : %s' % e
            _dkbk_save(s)
            return 502, {'ok': False, 'error': s['last_error']}
        allb = dkbackup_list()
        for r in [r for r in allb if not r['safety']][s['keep']:] + [r for r in allb if r['safety']][3:]:
            try:
                os.remove(os.path.join(s['dir'], r['name']))
            except OSError:
                pass
        snaps, errs = dkbackup_snapshots(create=True, keep=s['keep'])
        s['last'] = int(_lic_time.time())
        s['last_error'] = '; '.join(errs)[:400]
        _dkbk_save(s)
        return 200, {'ok': True, 'name': name, 'files': n, 'snapshots': len(snaps), 'warnings': errs}
    finally:
        _dkbk_lock.release()


def dkbackup_status():
    s = _dkbk_settings()
    snaps, _e = dkbackup_snapshots()
    return {'ok': True, 'settings': s, 'backups': dkbackup_list(), 'snapshots': snaps,
            'next': (s['last'] + DKBK_INTERVAL) if s['auto'] else 0, 'interval': DKBK_INTERVAL}


def dkbackup_settings_set(b):
    s = _dkbk_settings()
    if 'auto' in b:
        s['auto'] = bool(b['auto'])
    if 'keep' in b:
        try:
            k = int(b['keep'])
        except Exception:
            return 400, {'ok': False, 'error': 'Nombre de copies invalide.'}
        if not 1 <= k <= 60:
            return 400, {'ok': False, 'error': 'Entre 1 et 60 copies.'}
        s['keep'] = k
    if b.get('dir'):
        d = os.path.normpath(str(b['dir']).strip())
        if not _dkbk_dir_ok(d):
            return 400, {'ok': False, 'error': 'Le dossier doit se trouver sous /mnt.'}
        s['dir'] = d
    _dkbk_save(s)
    return 200, {'ok': True, 'settings': s}


def dkbackup_delete(name):
    s = _dkbk_settings()
    if not DKBK_NAME_RE.match(str(name or '')):
        return 400, {'ok': False, 'error': 'Nom invalide.'}
    try:
        os.remove(os.path.join(s['dir'], name))
    except OSError as e:
        return 404, {'ok': False, 'error': str(e)}
    return 200, {'ok': True}


def dkbackup_restore(path):
    """Remet en place une archive. Garde une copie de sécurité de l'état actuel. Rend (code, dict)."""
    if not _dkbk_lock.acquire(blocking=False):
        return 409, {'ok': False, 'error': 'Une sauvegarde ou une restauration est déjà en cours.'}
    try:
        try:
            tf = _bk_tarfile.open(path, 'r:gz')
        except Exception:
            return 400, {'ok': False, 'error': "Ce fichier n'est pas une sauvegarde Desktroo (.tar.gz)."}
        with tf:
            try:
                mf = tf.extractfile('desktroo-backup.json')
                meta = json.loads(mf.read().decode('utf-8')) if mf else None
            except Exception:
                meta = None
            if not isinstance(meta, dict) or meta.get('format') != 'desktroo-backup':
                return 400, {'ok': False, 'error': "Ce fichier n'est pas une sauvegarde Desktroo."}
            members = []
            for m in tf.getmembers():
                nm = m.name
                if nm == 'desktroo-backup.json':
                    continue
                parts = nm.split('/')
                if (not m.isfile() or nm.startswith('/') or '..' in parts or parts[0] not in ('data', 'wallpapers', 'lang') or len(parts) < 2
                        or (parts[0] == 'data' and not _dkbk_included('/'.join(parts[1:])))):
                    continue
                members.append(m)
            if not members:
                return 400, {'ok': False, 'error': 'La sauvegarde ne contient aucun réglage.'}
            s = _dkbk_settings()
            os.makedirs(s['dir'], exist_ok=True)
            safety = os.path.join(s['dir'], 'desktroo-avant-restauration-%s.tar.gz' % _dkbk_stamp())
            dkbackup_build(safety, kind='avant-restauration')
            stage = _tempfile.mkdtemp(prefix='dkrestore-')
            try:
                for m in members:
                    dst = os.path.join(stage, m.name)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    src = tf.extractfile(m)
                    with open(dst, 'wb') as fh:
                        _bk_shutil.copyfileobj(src, fh)
                n = 0
                for top, base in (('data', ACCESS_DATA_DIR), ('wallpapers', os.path.join(APP_DIR, 'wallpapers') if APP_DIR else ''), ('lang', LANG_DIR)):
                    srcdir = os.path.join(stage, top)
                    if not base or not os.path.isdir(srcdir):
                        continue
                    for root, _d, files in os.walk(srcdir):
                        for fn in files:
                            full = os.path.join(root, fn)
                            dst = os.path.join(base, os.path.relpath(full, srcdir))
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            tmp = dst + '.rst'
                            _bk_shutil.copyfile(full, tmp)
                            os.replace(tmp, dst)
                            n += 1
            finally:
                _bk_shutil.rmtree(stage, ignore_errors=True)
        return 200, {'ok': True, 'files': n, 'safety': os.path.basename(safety), 'apps': meta.get('apps', [])}
    finally:
        _dkbk_lock.release()


def dkbackup_snap_restore(app_id, snap):
    """Remet les données d'une app dans l'état d'un instantané : arrêt, retour en arrière ZFS, redémarrage."""
    reg = store_installed_get()
    rec = reg.get(app_id) if _STORE_ID_RE.match(str(app_id or '')) else None
    ds = str((rec or {}).get('dataset') or '')
    if not rec or not any(a == app_id for a, _d in _dkbk_apps()) or not DKBK_SNAP_RE.match(str(snap or '')):
        return 400, {'ok': False, 'error': 'App ou instantané inconnu.'}
    snaps, _e = dkbackup_snapshots()
    if not any(x['app'] == app_id and x['name'] == snap for x in snaps):
        return 404, {'ok': False, 'error': 'Instantané introuvable.'}
    try:
        try:
            _store_midclt('app.stop', 'dt-' + app_id, job=True, timeout=300)
        except Exception as e:
            log.warning('dkbackup : arrêt de %s : %s', app_id, e)
        out, err, code = _px_host('zfs rollback -r %s' % shq(ds + '@' + snap), timeout=300)
        if code != 0:
            raise RuntimeError((err or out or 'zfs rollback a échoué').strip()[-300:])
        store_net_ensure()
        _store_midclt('app.start', 'dt-' + app_id, job=True, timeout=600)
        return 200, {'ok': True}
    except Exception as e:
        return 502, {'ok': False, 'error': str(e)}


# MDM-UPD-SMS-V1 : prévient par SMS (app SMS installée) des mises à jour disponibles : Desktroo, apps de Desktroo Apps,
# apps TrueNAS et TrueNAS lui-même. Un seul SMS par vérification, et seulement pour ce qui est nouveau depuis le dernier.
# Pour l'éteindre : créer le fichier <données>/store/update-sms-off.
UPD_SMS_FILE = os.path.join(STORE_DIR, 'update-notified.json')
UPD_SMS_OFF = os.path.join(STORE_DIR, 'update-sms-off')


def _upd_sms_pending():
    """{clé: texte} de ce qui attend une mise à jour. Chaque source échoue sans gêner les autres."""
    out = {}
    try:
        v = _version_status()
        if v.get('update_available') and v.get('latest') and v.get('update_allowed', True):
            out['desktroo:' + v['latest']] = 'Desktroo ' + v['latest']
    except Exception as e:
        log.debug('SMS mises à jour (Desktroo) : %s', e)
    try:
        for a in (store_view().get('apps') or []):
            if a.get('update_available') and a.get('update_allowed', True):
                nm = a.get('name')
                nm = (nm.get('fr') or nm.get('en') or a.get('id')) if isinstance(nm, dict) else (nm or a.get('id'))
                out['store:%s:%s' % (a.get('id'), a.get('version'))] = '%s %s' % (nm, a.get('version') or '')
    except Exception as e:
        log.debug('SMS mises à jour (apps) : %s', e)
    try:
        o, _e, code = ssh_exec("sudo -n midclt call app.query '[[\"upgrade_available\",\"=\",true]]' '{\"select\":[\"name\",\"latest_version\"]}'", timeout=60)
        if code == 0 and o.strip():
            for r in json.loads(o):
                out['truenas-app:%s:%s' % (r.get('name'), r.get('latest_version'))] = 'app TrueNAS %s %s' % (r.get('name'), r.get('latest_version') or '')
    except Exception as e:
        log.debug('SMS mises à jour (apps TrueNAS) : %s', e)
    try:
        o, _e, code = ssh_exec("sudo -n midclt call update.check_available", timeout=90)
        if code == 0 and o.strip():
            r = json.loads(o)
            if str(r.get('status')) == 'AVAILABLE':
                ver = str(((r.get('new_version') or {}).get('version')) or r.get('version') or '')
                out['truenas:' + ver] = 'TrueNAS ' + ver
    except Exception as e:
        log.debug('SMS mises à jour (TrueNAS) : %s', e)
    return out


def _upd_sms_tick():
    if os.path.exists(UPD_SMS_OFF):
        return
    hook = store_sms_hook()
    if not hook.get('ok'):
        return
    pending = _upd_sms_pending()
    try:
        with open(UPD_SMS_FILE, encoding='utf-8') as f:
            done = json.load(f)
        if not isinstance(done, dict):
            done = {}
    except Exception:
        done = {}
    new = [(k, t) for k, t in pending.items() if k not in done]
    if new:
        import urllib.request as _ur
        msg = 'NAS : mises à jour disponibles — ' + ' ; '.join(t.strip() for _k, t in new)
        req = _ur.Request(hook['url'], data=json.dumps({'text': msg}).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
        _ur.urlopen(req, timeout=20).read()
        log.info('SMS mises à jour : %s', msg)
    # On garde seulement ce qui est encore en attente : une mise à jour faite puis une nouvelle version préviennent à nouveau.
    keep = {k: done.get(k) or int(_login_time.time()) for k in pending}
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(UPD_SMS_FILE, 'w', encoding='utf-8') as f:
        json.dump(keep, f)


def _upd_sms_loop():
    _login_time.sleep(600)
    while True:
        try:
            _upd_sms_tick()
        except Exception as e:
            log.warning('SMS mises à jour : %s', e)
        _login_time.sleep(6 * 3600)


def _dkbk_loop():
    _lic_time.sleep(120)
    while True:
        try:
            s = _dkbk_settings()
            if s['auto'] and _lic_time.time() - s['last'] >= DKBK_INTERVAL:
                code, out = dkbackup_run()
                log.info('Sauvegarde automatique de Desktroo : %s', out.get('name') or out.get('error'))
        except Exception as e:
            log.warning('Sauvegarde automatique : %s', e)
        _lic_time.sleep(900)


_threading.Thread(target=_dkbk_loop, name='dkbackup', daemon=True).start()
_threading.Thread(target=_upd_sms_loop, name='update-sms', daemon=True).start()   # MDM-UPD-SMS-V1
_threading.Thread(target=_cam_alert_loop, name='camera-alerts', daemon=True).start()   # SMS quand une caméra voit une personne

# MDM-STORE-V1 : réseau des apps et bloc nginx /a/
_threading.Thread(target=_store_boot, name='store-boot', daemon=True).start()
_threading.Thread(target=_store_idle_loop, name='store-idle', daemon=True).start()   # MDM-STORE-IDLE-V1

# MDM-NAS-GATEWAY-V1 : remet nginx.conf sur une adresse du NAS qui répond
_threading.Thread(target=_heal_nginx_conf, name='nas-gateway-heal', daemon=True).start()

# MDM-FILES-PLUS-V1 : la corbeille se vide de ce qui a dépassé la durée de conservation
_threading.Thread(target=_px_trash_loop, name='trash-purge', daemon=True).start()

# MDM-SHARE-LINKS-V1 : purge périodique des liens expirés / épuisés
_threading.Thread(
    target=_share_purge_loop,
    name='share-purge',
    daemon=True,
).start()

with _TCPServer(('', HTTP_PORT), FileOpsHandler) as srv:
        srv.serve_forever()
