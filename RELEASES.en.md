# Release notes — Desktroo

What changes in every version. Each entry is written for what it brings you;
the grey line underneath gives the technical detail, when there is one.

## 2.54.50 — 18 October 2026

### Improved
- **The file picker now uses the desktop's colors.** The browser's grey default button, on the Restore and Update pages, becomes a rounded button like the others.

### Fixed
- **Kernel and hardware: no more “NotImplemented.service” error.** The “Processor power saving” setting (a service that does not exist on Linux) is removed from the page, and when TrueNAS saves a setting but cannot restart a missing service, Desktroo says so instead of showing an error.
- **The Desktroo backup failed on an ACL-based NAS folder** (“Operation not permitted” on the file being written). Desktroo no longer forces the file's permissions when the folder does not allow it: the backup goes through.

## 2.54.49 — 18 October 2026

### New
- **Desktroo backup and restore, in the same place as TrueNAS's.** In System configuration, the “Backup” and “Restore” pages have a Desktroo section. One click on “Back up Desktroo now” makes a file with your settings (shares, sites, app access and secrets, wallpapers, list of installed apps), kept in a NAS folder you choose and downloadable. Each backup also takes a ZFS snapshot of each app's data, which you can go back to in one click.
- **Automatic weekly backup**, with the number of copies to keep (4 by default). Desktroo does it itself: no task to create in TrueNAS.
- **Safe restore.** A safety copy of the current state is made just before, then Desktroo restarts. The license and the installation identity are not included: they stay the NAS's.
  > `.tar.gz` archive (`data/` + `wallpapers/`, license, `install-id`, trial and caches excluded), `dkbak-YYYYMMDD-HHMMSS` snapshots on `apps/desktroo/<app>`, going back with `zfs rollback` while the app is stopped. `/dkbackup/*` routes.

### Fixed
- **Installing an app failed on a NAS whose “apps” dataset uses POSIX ACLs** (“aclmode: Must be set to DISCARD when acltype is POSIX or OFF”), for example VPN (WireGuard). Desktroo now creates its datasets with the right value.

## 2.54.48 — 18 October 2026

### Fixed
- **The 30-day trial can no longer be extended.** The trial start is now registered on desktroo.fr, tied to an irreversible fingerprint of your NAS (a hash: no hardware identifier is sent in clear). Deleting a file or setting the clock back no longer gives days back. If desktroo.fr cannot be reached at first launch, Desktroo gives you 3 provisional days to reach the server; the trial then runs 30 days from the first launch. Existing installations keep their trial date.
  > Server: `/trial` route (Desktroo Licenses plugin 1.4.0), `desktroo_trials` table, RSA-signed token `kind: trial`. NAS: `_lic_fingerprint` (DMI UUID, else `boot-pool` GUID + host machine-id), `_lic_trial_state` (token, provisional mode, `.dk-t0` witness, clock-rollback guard), `license_trial_sync`.

## 2.54.47 — 18 October 2026

### Improved
- **New apps' data now goes in your “apps” dataset.** Instead of a separate dataset (`desktroo-apps`), an app installed from Desktroo Apps keeps its data in `<pool>/apps/desktroo/<app>`, next to your other apps. Apps already installed do not move, and data kept at uninstall is found where it is. Without an “apps” dataset on the pool, the old location is kept.
  > `_store_dataset`: the install registry decides for an existing app; otherwise `apps/desktroo/<app>` if `<pool>/apps` is a ZFS dataset. Cascading creation (`apps/desktroo`, then the app).
- **Apps' data is never offered to another app.** The `apps/desktroo` folder and the `apps` dataset that contains it are removed from the NAS folder choice: an app that reads “the whole NAS” does not see the other apps' vaults and certificates.

## 2.54.46 — 18 October 2026

### Fixed
- **Signing in from an Android phone.** The phone's autofill could copy the password into the “API key” field: the key, which takes priority, made the sign-in fail (“Incorrect TrueNAS credentials”). This field is no longer autofilled, it is ignored if it repeats the password, and a key refused while a password is entered is replaced by a second attempt with the password.
  > `login-apikey` field: `type="text"` masked with `-webkit-text-security`, `autocomplete="off"` and password-manager exclusion attributes.
- **More forgiving credential pasting.** Invisible characters and spaces at the end of a password, common after copy-paste, are removed; if TrueNAS refuses, Desktroo tries once more with the text exactly as pasted. The keyboard no longer adds capital letters or autocorrect in these fields.

### Improved
- **Desktroo Apps shows its version**: the Desktroo version and the catalog date, at the bottom of the left column.
- **Installing a single app: an explanation instead of an error page** when Desktroo itself is open as an installed application. Chrome does not allow installation from that kind of window: Desktroo has to be opened in the browser.

## 2.54.44 — 17 October 2026

### New
- **Every Desktroo app installs as a real computer application.** “Desktroo Files”, “Desktroo Jellyfin”, “Desktroo Antivirus”… : its own window, its icon on the Windows desktop and taskbar, with no tab or address bar around it. Right-click an app in the drawer, use the install button in the Files address bar, or the “A single app” list in the Control Panel to open the install page.
  > Address `/k/<app>/` with its own manifest (`/fileops/store/kiosk-manifest/<app>`): `id` and scope are specific to each app, so an installed app no longer “captures” the others. Drawer apps (`dkapp-<id>`) are mapped back to `<id>`.
- **Nice application icons.** Windows cannot build a shortcut from an SVG icon (it showed a grey tile with a letter): Desktroo draws sharp PNG icons from each app's icon and the NAS stores them for the manifests.
  > 192 and 512 PNGs generated by the browser (canvas) and sent to `/store/kiosk-icon`, `kiosk-icons` cache.
- **In application mode, nothing but the application.** The desktop, its icons and its widgets are hidden; a small loading spinner shows while signing in, then the application opens on its own.
- **Tasks: “Clear the list”.** In the tasks window (Failed and Finished tabs), a button hides the tasks you have already seen and resets the counter. A new failure shows up as usual.
  > TrueNAS cannot delete task history: the hiding is remembered in this browser.
- **Direct address for apps.** Apps that open on their own address show a “Direct address” row with a button to copy it.
- **Antivirus: clearer quarantine.** The list shows each file's size and original folder, and confirmation happens in place (new app version 1.0.3).

### Fixed
- **Files now opens on its own in application mode.** The window was requested before it was built and stayed closed.
- **Jellyfin: unreadable text on the light theme** in a title's page.

## 2.54.38 — 17 October 2026

### New
- **Jellyfin: your family media library, from Desktroo Apps.** Jellyfin sorts your NAS movies, shows, music and videos, fetches posters and summaries, and streams them to the TV, phone, tablet or browser. A Desktroo window replaces its administration interface: your libraries in the left column, a home page with the latest additions of each, search, a detailed page for every title (summary, cast, seasons and episodes) and your favorites.
  > `jellyfin` app page (official image pinned by digest, Jellyfin 12.2). The NAS creates the administrator account itself at first start and drives Jellyfin's API; the password stays on the NAS (`/store/jellyfin/api`).
- **Watch your movies in the Desktroo window, without opening Jellyfin.** Playback resumes where you left off (even from another device), episodes play one after another, and “Continue watching” and “Next episodes” appear on the home page. Formats the browser cannot play (MKV, x265, 5.1 sound…) are converted on the fly.
  > Stream relayed through a signed, temporary address (`/store/jellyfin/stream/<ticket>`, Range supported): the browser has neither a Jellyfin token nor direct access to the local network. Direct play for mp4/webm in h264/vp9/av1 + aac/mp3/opus, otherwise transcoded `stream.mp4` (h264 + aac). Position saved through `/Sessions/Playing*`.
- **Family accounts with an age limit**, created and set from the window: a child account only sees suitable titles.
- **Taking over an old Jellyfin server.** After restoring a backup, the window spots library folders that are no longer reachable (e.g. `/media/series`) and offers, in one click, the matching NAS folder.
  > `repair` action: name matching against the `/nas/…` folders; replacement through `POST/DELETE /Library/VirtualFolders/Paths`, then a scan.
- **Publish Jellyfin under a domain name** from the window (“Domain” tab), with NPMplus.
- **Subtitles and audio tracks** in the player: pick the sound and subtitle language during playback, and Desktroo remembers your preferred language. Image subtitles (PGS, VobSub) are burned into the video.
  > Text subtitles relayed as WebVTT (`/store/jellyfin/sub/<ticket>`); an audio track or image subtitle restarts the conversion at the same position.
- **A fuller player**: playback speed, a “Next episode” button, “Skip intro” and “Skip recap” when Jellyfin has detected them, keyboard shortcuts (space, arrows, F, M, N, Esc) and, for music, the album cover.
- **Favourites, History, Playlists and search everywhere** in the left column; in each library, sorting, a filter (watched, unwatched, favourites) and a genre filter.
- **Title page**: “Add to a playlist”, “Fix identification” (pick the right title from the results) and “Refresh” the information.
- **Jellyfin backups from the window**: create a backup on demand, see the list and restore (accounts, settings, history).
  > Uses Jellyfin 10.11+ built-in backup (`/Backup`).
- **Activity**: current sessions (with stop playback), connected devices (revoke), activity log and the latest server errors.
- **Accounts**: choose which libraries each account can see, and change its password.

### Fixed
- **The “Open” links of Desktroo Apps apps lead to the NAS address on your network**, no longer to the domain name you use to open Desktroo. This is the case of the NPMplus administration (port 8181), which did not answer through the domain.
  > New `dkNasHost` helper: NAS IP from `nas_ip`, otherwise `truenas_ip`; the domain name is only a last resort.

## 2.54.19 — 17 October 2026

### Fixed
- **The ✓ “On the desktop” badge is no longer cut off** on Desktroo Apps app icons (Domains, Authenticator, Desktroo Pass, Antivirus, Uptime Kuma…), in the applications menu.
- **Desktroo Apps icons now have the same weight as system icons**: slightly smaller drawings and thinner strokes, instead of visibly bigger and thicker icons.
  > Store icons were reworked (drawing scaled to 78%, strokes ×0.7); they arrive with the store publication.

### Improved
- **Desktroo Apps can host new kinds of apps** that read your NAS folders read-only while keeping their own internal rights.
  > New app-page field `nas_user_env`: the borrowed account is passed to the app as environment variables (UID/GID) instead of replacing the container user.

## 2.54.15 — 16 October 2026

### New
- **Uptime Kuma: watch your sites and services from Desktroo Apps.** Uptime Kuma continuously checks that your websites, servers and devices respond, and alerts you by email, Telegram, SMS or a hundred other ways as soon as a service goes down. A Desktroo window shows the status of all your services at a glance: online, offline, response time, 30-day availability, days left before the certificate expires. Creating or editing a service is done in Uptime Kuma (“Full administration”).
  > `uptime-kuma` app page (official image pinned by digest, SQLite database enforced). The window reads the status in Prometheus format (`/metrics`) with a read-only API key created by the user, kept on the NAS and never sent back to the browser (`/store/uptime-kuma/api`).
- **An “Update available” badge for Desktroo Apps apps, in the application manager.** It shows in the list, in the left column and on the app page, with a button that opens Desktroo Apps updates directly.

### Improved
- **Easier-to-read app pages.** Descriptions are laid out in paragraphs, in French and in English.
- **Desktroo Apps refreshes by itself.** States (running, stopped, installed…) update on their own: no more closing and reopening the window to see a change.
- **Wider app compatibility.** Desktroo Apps accepts new container rights (CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID) for apps that need them.

### Fixed
- **Installing an app onto an existing data folder no longer fails** (“Path … already exists” error after an interrupted install).
- **“Installed, but not responding yet” no longer stays on screen** when the app does respond.

## 2.54.0 — 16 October 2026

### New
- **VPN (WireGuard): a dedicated Desktroo window.** Add a phone or a computer in a few clicks, show its QR code to scan in the WireGuard app, get or copy its configuration file, and turn a device on, off or delete it. The list shows who is connected and how much traffic each device used. “Open” opens this window; the full wg-easy administration remains available from its button.
  > The NAS relays requests to wg-easy’s API (`/store/wireguard/api`, routes limited to devices) with the administrator account from the app’s page: the browser never sees it.

## 2.53.4 — 16 October 2026

### Fixed
- **VPN (WireGuard): "Open" leads to the administration** (direct address on your local network) instead of going back to the desktop home.

## 2.53.3 — 16 October 2026

### Fixed
- **VPN (WireGuard): administration reachable**: the wg-easy interface cannot be displayed inside the desktop. It is now reachable from your local network, at the address shown in the app page (port 51821, never opened to the Internet). A dedicated Desktroo window will replace it.
  > A published port can now target a different port inside the container (`to` in the catalog).

## 2.53.2 — 16 October 2026

### Fixed
- **VPN (WireGuard) did not start**: its network interface could not set the container firewall. The app now gets the network right it was missing (NET_RAW), in addition to those already planned. If you installed it in test, "Update" fixes the installation.

## 2.53.1 — 16 October 2026

### Improved
- **Beta channel test**: this version changes nothing but its number. It checks that a test NAS receives versions before everyone else.

## 2.53.0 — 16 October 2026

### New
- **VPN (WireGuard), in test**: reach your NAS and your network from outside. At install, Desktroo asks for your router's public address, then generates the administrator account (shown in the app page). A dedicated Desktroo window will follow.
  > wg-easy engine (AGPL-3.0, official image pinned by digest). Extended network rights (NET_ADMIN, SYS_MODULE), IP forwarding and the kernel modules folder read-only; UDP port 51820 to forward to the NAS.
- **Beta channel for updates**: a test NAS (one that already has the `data/store/beta` file) receives the versions on the distribution repository's `beta` branch, before they are published to everyone. Other NAS see nothing.
  > `publish-dist.ps1 -Beta` publishes to the `beta` branch only; a normal publish updates `main` then realigns `beta` on it. Without a `beta` branch, the test NAS follows stable.
- **Settings asked before install**: a Store app can now ask for a value (public address, for example), checked before starting.
  > Catalog `install_fields`; values kept on the NAS; per-installation derived secrets (`@derive:`).

### Improved
- **Files: "Restore" when the recycle bin is browsed as a folder**: when you browse `.desktroo-corbeille` directly, right-click now offers to put the item back where it came from.
  > Same restore as the Recycle bin window (original location checked, unique name if needed).

## 2.52.0 — 16 October 2026

### New
- **Connect an existing AdGuard Home** (on a box, a virtual machine, another computer): the same Desktroo window manages it remotely. Enter its address, username and password in the AdGuard Home page; an icon appears on the desktop.
  > Credentials stay on the NAS (owner-only file); the browser never sees them. Only AdGuard `/control/…` paths are relayed; the NAS itself, loopback and internal addresses are refused; no redirect is followed.

### Fixed
- **Desktroo Pass with a domain already configured in NPMplus**: "This domain is already published" blocked step 1. The domain is now taken over (certificate kept). If it points elsewhere, Desktroo asks for confirmation before redirecting it to the vault.

## 2.51.0 — 16 October 2026

### New
- **AdGuard Home has its own Desktroo window**, in the same style as the others: Overview (queries, blocks, chart, most active sites and devices, pause protection), Query log (with one-click “Block” and “Allow”), Block lists (add, turn on or off, personal rules) and Settings (DNS servers tested before saving, retention period).
  > The window talks to AdGuard's API through Desktroo's proxy (desktop session): no password, no open port. AdGuard's full interface remains available from Settings.

## 2.50.1 — 16 October 2026

### Fixed
- **An app that offers a network service (AdGuard Home) no longer clashes with the virtual machines' internal network.** Its DNS port is opened on the NAS address only, not on every address, so it coexists with TrueNAS's virtual-machine DNS server.
- **When a requested port is really taken, Desktroo names the program using it** (for example "53 (pihole-FTL)").

## 2.50.0 — 16 October 2026

### New
- **AdGuard Home in Desktroo Apps (testing).** A DNS server that blocks ads, trackers and dangerous sites for every device on the network: just set the NAS address as the DNS server in your router. Installed in one click, with no setup wizard and no second login.
  > Unmodified official `adguard/adguardhome` image (GPL-3.0). New catalog field `host_ports`: an app can open ports on the local network (here 53, TCP and UDP); Desktroo checks the port is free before installing and says so clearly if not. Other fields: `cap_add` (NET_BIND_SERVICE only) and `seed` (starting configuration file, written once).
- **App pages show the ports opened on the network and the free software used**, with a short explanation before installing.

## 2.49.1 — 16 October 2026

### Improved
- **The published-domains list is clearer**: one domain per line, its destination underneath, the certificate status on the right. Renew, Disable and Remove appear when you hover a row (always visible on tablets and phones).

## 2.49.0 — 16 October 2026

### Improved
- **The Antivirus needs no setup any more.** Install it: it scans every NAS folder, with no account to pick and no rights to check. It reads your files but cannot change them; when it finds a dangerous file, Desktroo moves it to quarantine, and puts it back if you ask.
  > The app runs with no privilege except reading, each folder mounted read-only. It asks Desktroo for moves, and Desktroo checks its identity and the path first.
- **A NPMplus installed on another machine can be linked to Desktroo** (box, virtual machine, other computer on the network). In Desktroo Apps, NPMplus page, "Link a remote NPMplus": its administration address and an administrator account. Your domains and certificates are then managed in the Domains window, like a local NPMplus; sites are sent to this NAS's address.
  > Desktroo only talks to that NPMplus's API, without touching its installation. The account must not use two-factor authentication, and the machine must be able to reach the NAS.
- **No need to go through TrueNAS to manage an account any more.** From Access rights, you edit the account (name, e-mail, groups, home folder), change its password and manage group members, in Desktroo's Users window.
- **The read-only option can no longer be ticked by mistake**, for apps that ask to change your folders: access is chosen from a list right under the account.
- **Widget shadows now fade into the desktop** instead of stopping in straight lines at the edges of the widget zones.
- **Hidden NAS folders are no longer offered to apps** (container and virtual machine disks).

## 2.48.0 — 15 October 2026

### New
- **Desktroo Apps apps can work on your NAS folders** when they need to. At install time you choose the NAS account whose rights the app takes and the folders it sees; it can only open what that account can open. You can change this later from its page.
  > The container runs with the chosen account's id and groups; each folder is mounted under /nas, read-only if you ask.

## 2.47.1 — 15 October 2026

### Improved
- **Desktroo Pass sends invitations by e-mail**, using the mail settings already set for the NAS alerts. The person receives a link to create their account; an invitation can be resent.
  > Gmail or Outlook mail connected through OAuth cannot be used by the vault: it needs an SMTP server with a password. Without mail, invitations still work: the person creates their account at the vault address.

## 2.47.0 — 15 October 2026

### New
- **Desktroo Pass, the NAS password vault** (Desktroo Apps). It works with the official, free Bitwarden apps: autofill in the browser, on the phone and on the PC. Everyone has their own vault, encrypted with their own master password.
  > Built on Vaultwarden. Desktroo installs it, publishes it over https under your domain through NPMplus, and administers it from its window: accounts by invitation only, administration page never served on the Internet.

## 2.46.3 — 15 October 2026

### Fixed
- **An app installed from Desktroo Apps always gets its desktop icon**, even if the Desktroo Apps window was closed during the installation. Removed afterwards, it does not come back.

## 2.46.2 — 15 October 2026

### Improved
- **Progress for “Update all”.** The Alerts window stays open with one line per application: waiting, in progress, up to date, or the reason for a failure, plus a progress bar.
- **“Update all” no longer trips on a stopped application**: TrueNAS refuses to update it, so Desktroo leaves it aside and tells you.
- **The reverse proxy is updated last.** If you open Desktroo through it (NPMplus, for example), the connection lost during its update no longer interrupts the others: the page reconnects and carries on.
  > An update that has started continues on the TrueNAS side even when the page loses the connection; Desktroo says so instead of showing “WS_UNAVAILABLE”.

## 2.46.1 — 15 October 2026

### New
- **Update all.** In Alerts, one button updates every application waiting for it, one after the other, showing progress.
  > New version in the TrueNAS catalog: `app.upgrade`; new images for a custom application: pull then redeploy.

### Improved
- **Desktroo Apps apps show their real name** in alerts and in the application list: “Web server” instead of “Dt-webserver”.

## 2.46.0 — 15 October 2026

### New
- **Desktroo Apps: apps for your NAS, included in the subscription.** A new window lists the Mdm-services.fr apps. They install in one click, with no configuration or port to choose, open in a desktop window with your session, and update from Desktroo. You choose which accounts can use them.
  > Signed catalog, checked by the NAS before any use. Each app runs in its own isolated container, with its data in `<pool>/desktroo-apps/<app>` and a ZFS snapshot before each update. Expired subscription: installed apps keep running; only installing and updating require a subscription.
- **Some apps also open without the desktop**, in a tab or from the phone's home screen, at the Desktroo address followed by `/a/<app>/`. Sign in with your TrueNAS account, two-factor code included, and “Stay signed in” for 30 days.
- **The web server becomes an optional module.** If you host nothing on the NAS, it no longer runs for nothing; otherwise, install it from Desktroo Apps and the Websites window appears.
  > An existing installation is moved automatically, once, on the first start of this version: same containers, same sites, same databases. Sites are down for about ten seconds. If anything fails, everything is put back as it was. An installation made as a TrueNAS app (Custom App) is not moved.
- **NPMplus as a module.** The reverse proxy that publishes your sites under your domains, over https, installs in one click (ports 8090, 8453 and 8181, leaving 80 and 443 to TrueNAS). An NPMplus installed by hand can be taken over, with its domains and certificates; an NPMplus installed as a TrueNAS app can be linked.
- **Domains window.** Your published domains, the site each one leads to and the state of its certificate. Publishing a domain, requesting its Let's Encrypt certificate, renewing or removing it all happen without leaving Desktroo. Each site's page offers “Publish this site with NPMplus”.
  > Desktroo drives NPMplus through its API, from the NAS; the NPMplus interface remains available for advanced settings.
- **Delete several snapshots at once.** A checkbox on each row in Snapshots, “Select all” for what is shown, and Shift + click to select a range. A bar shows how many are selected and the space freed, with the delete button.
  > Tip: filter first (for example `auto-2026-09`), then “Select all” only takes the visible rows. A snapshot TrueNAS refuses to delete (held, or the origin of a clone) stays selected, with the reason. The space shown is a minimum: what several snapshots share is only freed when they all go.

### Improved
- **An app's state reads plainly** in Desktroo Apps: “Starting…”, “Running”, “Stopped”…
- **Installing an application in Websites no longer creates a duplicate**: once the site is ready, the “Install” button becomes “View the site”.
- **Uninstalling Desktroo also removes the Desktroo Apps apps**, their data staying on the NAS.
- **The desktop no longer loses its service** when the service restarts with another address on the Docker network.
  > nginx is reloaded on every fileops start: it reads the address again instead of answering 502.
- **The mail test message goes to the address you choose.** Desktroo suggests the administrator account's address, and the test works even when no account has one. It then warns you that TrueNAS alerts have nowhere to go.
  > Before, the test failed with “None of the local administrators has an e-mail address configured”. The most common sending errors (credentials rejected, encryption, server not answering) are now explained in plain words.
- **Port and encryption kept in step.** Choosing an encryption suggests the matching port (587 for STARTTLS, 465 for direct SSL/TLS, 25 without encryption), and a combination that cannot work is flagged before you save.

## 2.45.0 — 14 October 2026

### New
- **Uninstall Desktroo from the app.** About → Uninstall Desktroo: the window shows what will be removed, what stays, and how much space you get back. You type the name of the NAS to confirm.
  > Removes the containers, their Docker images (except those another app uses), the autostart task and the application files. Website databases, settings and the bin only go if you tick their box. Virtual machines, website files and everything Desktroo created in TrueNAS stay in place.
- **The licence is released when you uninstall**: you can activate it on another NAS without writing to us.
  > The subscription itself carries on: manage it from the customer portal.
- **The same uninstaller on the command line**, if the interface no longer opens: `sudo bash uninstall.sh`, in the install folder.
  > `--plan` checks what is there without changing anything. Also works for a Custom App install.

## 2.44.7 — 14 October 2026

### New
- **Exclude files from a cloud backup.** A “Do not copy” field in the form: one pattern per line, and anything matching is skipped.
  > Feeds the TrueNAS `exclude` field, which Desktroo never sent — you had to go through the TrueNAS interface. Useful when the destination rejects certain file names, such as a Nextcloud rejecting the equals sign.

## 2.44.6 — 13 October 2026

### New
- **Release notes are in the application.** About → What's new: see what the installed version brings, and what you would gain by updating.
  > Read from `RELEASES.md` in the distribution repository, falling back to the local copy when the NAS has no Internet access.

## 2.44.5 — 13 October 2026

### New
- **Choose where an application stores its data.** At install time, for each storage entry: the volume managed by TrueNAS, or a folder of your choice on the NAS.
  > Exposes the `storage.*.type` field of the TrueNAS catalogue, which the application used to force. An empty or relative folder is refused before the install rather than during it.
- **Create a folder from the folder picker**, without leaving the form you are filling in. A “+ Folder” button, everywhere the picker opens.

### Fixed
- **Change the Home Assistant address without re-entering the token.** Leave the field empty: the stored token is reused, and your device selection is kept.
  > The token was only carried over when the address stayed the same, while the field promised the opposite. The connection is now verified before it is saved: an address pointing at another server is rejected by that server, and nothing is written.
- **A clear message when the token is badly pasted** (a space or a line break), instead of the message asking for a token you already had.

### Improved
- **The Storage widget reacts faster and does less work.** A pool turning degraded goes red right away, and the widgets stop working while the tab is in the background.
  > One grouped read of the pools instead of one call per pool, a subscription to state changes capped at one refresh every 10 seconds, and a lock against two concurrent passes.

## 2.44.4 — 13 October 2026

### New
- **Stop a running task from the system bar.** The panel now mirrors the four TrueNAS lists — running, waiting, failed, succeeded — with a stop button on every task that accepts one.
  > Stopping is only offered when TrueNAS marks the job as abortable. Live tasks are refreshed on every tick, history at most every 150 seconds.
- **The indicator also reports failures.** A task that failed less than seven days ago and that you have not seen yet lights up the indicator, even when nothing is running.
- **Stop a backup or a replication** from its page, in Data protection.
- **“Serve on the web” moves the folder into the Web dataset**, with a confirmation naming the exact destination and warning you if the folder changes pool.
  > A site has to live in the Web dataset: it is the only area Desktroo knows how to find again, and the only one where permissions are guaranteed.

### Fixed
- **A cloud backup still running is no longer reported as finished.** After an hour of waiting, Desktroo used to display “finished” without checking.
  > Waiting on a job returns the same empty value on timeout and on success without a result. The job state is now read back to tell them apart.
- A task you have just stopped is no longer reported as an error.

## 2.44.3 — 12 October 2026

### Fixed
- **The account shown in the system bar is really yours.** Desktroo could display “root”, in particular when reopening a tab or the browser.
  > The name now comes from TrueNAS itself. It fed the activity log, the trash, the account preselected for an API key, and the acceptance of the terms.
- **The update is no longer offered when it is already installed.**

## 2.44.2 — 12 October 2026

### Fixed
- **System information is no longer cut off.** Tile subtitles wrap onto two lines instead of being truncated with no way to read them.

## 2.44.1 — 12 October 2026

### Fixed
- **Disk names are readable in Reports**, without the technical label and the serial number that overflowed the legend.
- **Windows get their size back** instead of reopening empty.
- Truncated labels in the side navigation show their full name on hover.
- Several texts left in French in English mode, and an overflow on memory with ZFS cache.

## 2.44.0 — 12 October 2026

### New
- **Desktroo works from a phone.** A dedicated layout: bottom bar, tab navigation, back button and touch-friendly menus.

---

_Versions before 2.44.0 have no published notes._
