# Release notes — Desktroo

What changes in every version. Each entry is written for what it brings you;
the grey line underneath gives the technical detail, when there is one.

## 2.60.30 — October 22, 2026

### New
- **Cameras: roomier add-camera window**, split into sections (identity, connection, recording, detection).
- **Cameras: per-camera detection settings** — choose which objects to track and a low / normal / high sensitivity.
- **Cameras: zoom in the enlarged view** (wheel, buttons, pinch, double-click, drag to pan).
- **Cameras: SMS when a person is detected** (RTSP cameras, per-camera option, at most one SMS every 5 minutes per camera), through Desktroo's SMS gateway.
  PTZ is not supported yet.

## 2.60.29 — 22 October 2026

### Fixed
- **Cameras: the enlarged view's video stream works without opening many connections to the camera** (one continuous connection instead of a picture requested every two seconds, which Tapo cameras ended up refusing). The camera configuration refreshes itself after a Desktroo update.

## 2.60.28 — 22 October 2026

### New
- **Cameras: real video in the enlarged view.** Clicking a camera now shows its continuous video stream instead of a refreshed still picture. If the stream cannot start, the window falls back to still pictures and says why.

## 2.60.27 — 22 October 2026

### Fixed
- **Cameras: Tapo cameras whose password was refused ("401 Unauthorized")** are now tried with the three authentication forms accepted by different firmwares (password, MD5 hash, SHA-256 hash).

## 2.60.26 — 22 October 2026

### Improved
- **Cameras: more precise error message when no picture arrives.**

## 2.60.25 — 22 October 2026

### Improved
- **Cameras: when no picture arrives, the window shows the reason given by the engine** (password refused, camera unreachable…) instead of staying blank.

## 2.60.24 — 22 October 2026

### New
- **Cameras: network search.** The "Search the network for cameras" button finds RTSP cameras and Tapo cameras and pre-fills the add form.

## 2.60.23 — 22 October 2026

### New
- **Cameras: battery and solar Tapo cameras are supported** (in testing). They connect with their IP address and the Tapo account password, without RTSP. To spare the battery, the picture is only requested when you click the camera.

## 2.60.22 — 22 October 2026

### New
- **Cameras: a new app in Desktroo Apps.** Your IP cameras live, continuous or motion-triggered recording, recognition of people, vehicles and animals. Everything stays on the NAS, with no account or cloud. Its Desktroo window lets you add a camera by its RTSP address (with a test), watch all cameras live, browse detected events and play recordings.

## 2.60.20 — 22 October 2026

### New
- **Jellyfin: the rest of Jellyfin's features are now in the Desktroo window.** Scheduled tasks (run, stop, schedules), plugins (install, enable, uninstall, repositories), transcoding (hardware acceleration), server and network (with Jellyfin restart), API keys and Quick Connect, and Live TV (tuners, guide, channels, recordings).
- **Jellyfin: on a title's page**, edit its information, pick another poster, search subtitles, create a collection and delete it permanently.
- **Jellyfin: per-library options, detailed per-account rights, message and pause on live playbacks.**

## 2.60.19 — 22 October 2026

### Fixed
- **Jellyfin: the home counters (movies, series, episodes, songs, albums) are right.** They could show far more than the real content (840 movies for 506 in the library). They now count the way the libraries do.
  > Jellyfin's `/Items/Counts` adds up duplicate items; replaced by `/Items?Recursive=true&Limit=0&IncludeItemTypes=…` (`TotalRecordCount`), with `/Items/Counts` as fallback.

## 2.60.18 — 22 October 2026

### Improved
- **Jellyfin: every library type is offered.** Desktroo's list lacked “Music videos” and “Mixed movies and shows”: both are added, and “Home videos” becomes “Home videos and photos”, as in Jellyfin.
  > `_JF_TYPES` + `musicvideos` (`MusicVideo`) and `mixed` (created without `collectionType`).

## 2.60.17 — 22 October 2026

### Fixed
- **An operation started from this device no longer shows twice.** Its progress indicator is enough; the “Operations running on the NAS” panel only shows those started from another device.

## 2.60.16 — 22 October 2026

### Fixed
- **Cancelling a move or a copy now really stops the whole operation.** When you move a selection of files, Desktroo sends them one by one: cancelling the current file let the next one start. The operation now carries a shared identifier; “Cancel” (in the “Operations running on the NAS” panel, or on the progress indicator of the device that started it) stops the current file and refuses all the following ones, even if the operation was started from another device.
  > `bid` field sent with every `/move` and `/copy`, server-side `_FS_BATCH_CANCELLED` registry (1 h), `POST /fs/jobs/cancel` accepts `id` or `bid`. A page opened before this version does not send `bid`: closing it is enough to stop its loop.

## 2.60.15 — 22 October 2026

### New
- **Long moves and copies are visible and cancellable from any device.** An “Operations running on the NAS” panel lists what is running (name, destination, duration) with a “Cancel” button, even if the operation was started from another computer or phone of the same user, or if the page was closed.
  > Server-side `_FS_JOBS` registry (`_fs_job_ssh`), `GET /fs/jobs` and `POST /fs/jobs/cancel` routes (stops the `cp`/`mv` on the NAS through a per-job pattern), polled every 8 s by `fsJobsPoll`. Only covers operations handed to the host (across datasets, ACLs); quick operations do not show.

## 2.60.14 — 22 October 2026

### New
- **A text message when an update is available.** With the SMS app installed, Desktroo texts you about an update for Desktroo, a Desktroo Apps app, a TrueNAS app or TrueNAS itself: one message per check (every 6 hours), and only for what is new.
  > `_upd_sms_loop`: sources `_version_status`, `store_view`, `midclt app.query` (`upgrade_available`) and `update.check_available`; memory in `store/update-notified.json`; sent to the SMS app webhook. To turn it off, create the file `store/update-sms-off`.

## 2.60.13 — 22 October 2026

### Improved
- **Moving files from one dataset to another in the same pool is now almost instant.** Before, Desktroo copied every file then deleted the original (several hours for a movie library). It now uses ZFS block cloning; if the pool cannot do it, it works as before.
  > `/move`: host fallback `cp -a --reflink=always` then `rm -rf` of the source, otherwise `mv -f` (partial destination cleaned in between).

## 2.60.12 — 21 October 2026

### Improved
- **“Select all” button in Files.** One click selects the whole folder (a second click deselects); Ctrl+A already worked. On a phone, once a selection has started, tapping a file checks or unchecks it instead of opening it.
  > `fsSelectAllToggle()`, same selection as Ctrl+A.
- **Alert settings in French.** TrueNAS alert categories and types (“Certificate Is Expiring”, “Pool Status Is Not Healthy”…) are now translated when the interface is in French.
  > `AL_FR` table (≈250 titles from the TrueNAS repository) applied by `alTr()`; an unknown title stays in English.

## 2.60.11 — 21 October 2026

### Fixed
- **Installing a “linked” NPMplus, AdGuard Home or Pi-hole as a computer app.** A linked app (installed outside Desktroo Apps, for example NPMplus on another machine) was installed with the Desktroo icon, and the browser only offered “Open in the Desktroo app” instead of adding a new app. It now has its own name and icon.
  > `store_kiosk_manifest` and `store_kiosk_icon_put` only knew the local registry apps: linked apps, injected only into the displayed list, had no manifest (404) and no PNG icon. New `_store_kiosk_inst` function.

## 2.60.10 — 19 October 2026

### New
- **Pi-hole in Desktroo Apps.** An ad and tracker filter for your whole network, like AdGuard Home, with its own Desktroo window: overview with chart, query log (block or allow a site in one click), block lists, sites allowed or blocked by hand, protection pause, upstream DNS servers.
  > `pihole` catalog entry (test channel, `pihole/pihole` image, port 53, `FTLCONF_*`, derived API password). The `dkPh*` window goes through `/store/pihole/api`: the NAS opens the v6 API session (sid, renewed on expiry) and only relays allow-listed paths; only the `dns.upstreams` setting can be changed.
- **Pi-hole on another machine.** If you already run Pi-hole (Raspberry Pi, virtual machine…), “Link an existing Pi-hole” controls it from the same window. The password stays on the NAS.
  > `/store/pihole/link`, `/unlink`; `data/store/pihole.json` as 0600; version 6 required; entry injected into the installed apps list as for AdGuard.

### Improved
- **Pi-hole and AdGuard Home: a clear message instead of a port error.** Both use port 53 and cannot run together on the same NAS. If one is installed, installing the other is refused with an explanation and what to do (uninstall the first, or link a server running on another machine).
  > `_store_dns_conflict` before the generic “port already in use” message; an app whose image digest is not pinned yet is cleanly refused.

## 2.60.9 — 19 October 2026

### Fixed
- **“Link an existing AdGuard Home” failed with “Operation not permitted”.** Desktroo did save the link, then tried to restrict the file's permissions, which ACL-based NAS folders refuse. It now skips that step: the link is saved. Same fix for the antivirus putting a file in quarantine.
  > `os.chmod` made optional after creating the file as 0600 (`store_ag_link`, quarantine `isolate`).

## 2.60.8 — 19 October 2026

### Fixed
- **“Advanced settings” for an NPMplus installed on another machine.** In Desktroo Apps, the button opened the administration at the NAS address (port 81) instead of the linked NPMplus's own; it now opens the right address, like the Domains window.
  > dkStoreNpmOpen uses the linked NPMplus address (installed.remote, else npm.linked.url) before falling back to the NAS address and the administration port.

## 2.60.7 — 19 October 2026

### Improved
- **A single setting for app access.** “Access rights” and “Who can use it” (app page in Desktroo Apps) are now the same setting, enforced by the NAS: a denied account cannot open the app, not even by its address. A new account has access unless it is denied; a denial can also come from a group. The lists saved so far are carried over as they are on update: nobody gains or loses access.
  > /store/auth check based on data/access-policy.json (same rule as acEffectiveApplication: account, then groups, denial first, otherwise allowed; root and truenas_admin always allowed). One-time carry-over of data/store/access.json as explicit denials for administrators missing from the lists, file renamed access.json.repris. NAS accounts read at most once a minute.

## 2.60.6 — 19 October 2026

### Improved
- **Simpler users.** A “NAS administrator” switch in the account page grants or removes administration rights, needed to open Desktroo; the account list and page show this role. The personal groups TrueNAS creates with each account, under the same name (account fabien, group fabien), are no longer shown: a group is now always a set of people, in Users as in Access rights.
  > FULL_ADMIN role carried by the system group builtin_administrators, hidden until now. Personal group: an account's primary group with the same name; its memberships are kept on save. You cannot remove your own rights.

### Fixed
- **Sign-in of an account without administration rights.** The message said “Incorrect TrueNAS credentials” although the password was right: it now says the account may lack NAS administration rights.
  > TrueNAS refuses API sign-in (AUTH_ERR) to an account without any privilege, just as for a wrong password.

## 2.60.5 — 19 October 2026

### New
- **NAS alerts by text message.** With the new SMS app in Desktroo Apps, “SMS” becomes a kind of alert service in Alert configuration: the NAS warns you by text, through Free Mobile (free, for subscribers, to your own number) or OVHcloud (to the numbers of your choice). The channel and numbers are set in the SMS app, which can also send texts with an OVHcloud account.
  > For TrueNAS, a “Slack” service whose address is where the SMS app receives alerts: port 8135 open on the local network only, derived token unique to the installation (DESKTROO_HOOK_TOKEN). Address returned by GET /store/sms/hook.

## 2.60.4 — 19 October 2026

### New
- **App sleep: the NAS's memory freed when an app is not in use.** OnlyOffice stops by itself after 15 minutes without activity, and restarts by itself when opened: from its icon, by opening a document in Files or through its address. You then wait a few tens of seconds. No document is lost: sleep waits until every document is closed and saved. The delay (5 min to 2 h) can be set, or sleep turned off, in the app's page in Desktroo Apps, where the app shows as “Asleep”.
  > Activity recorded on every request to /a/<app>/ (/store/auth check) and, for office, on every exchange with the document server; no stop while an OnlyOffice session is open. Checked every minute, stopped with app.stop; woken by POST /store/wake (state GET /store/wake), and from the “app not responding” page (nginx block V6, which passes the app). Capability declared by “idle_stop” in the catalog entry; setting in data/store/idle.json.

## 2.60.3 — 18 October 2026

### Improved
- **OnlyOffice for everyone.** The OnlyOffice app leaves the test channel: it appears in Desktroo Apps on every NAS, whether or not it has joined the beta programme.

### Fixed
- **Icons of apps installed as applications.** Desktroo Apps and Access rights now have their icon when installed on a computer or phone, and the browser console no longer shows a series of 404 errors each time the desktop opens.
  > Preparing the PNG icons requested Store apps by their internal id (dkapp-…) and tried two addresses for each app: it now asks for the short name, at the one right address, and remembers apps without an icon until the next version. System icons added for store and access.

## 2.60.2 — 18 October 2026

### Fixed
- **OnlyOffice documents stayed on the loading screen.** The editor now opens all the way. If a document still does not open after a minute and a half, a banner says so and offers to try again.
  > OnlyOffice's service worker re-read the editor's static files without the apps session cookie, so nginx refused them. These files (web-apps, sdkjs, fonts, dictionaries) are now served without the session, read-only (nginx block V5); document, saving and live connection stay behind the session. “Try again” also removes OnlyOffice's service worker.

## 2.60.1 — 18 October 2026

### New
- **Favorites in Files.** A “Favorites” section between Locations and Pools keeps your preferred datasets and folders at hand: the + button lets you tick datasets in the list of all those on the NAS, and right-clicking a folder adds or removes it. Drag a favorite to reorder it; drop files on it to move them there. Favorites follow your account on every browser.
  > Kept in the account profile (data/profiles, fs_favorites field, 60 at most); route POST /profile/favorites. Internal datasets (.system, ix-apps) are left out of the list.

## 2.60.0 — 18 October 2026

### New
- **Your Word, Excel and PowerPoint documents open on the desktop.** Install OnlyOffice from Desktroo Apps (test channel for now): double-clicking a document in Files opens it in a Desktroo window, with no extra tab. It saves itself to the NAS, in its place, and several people can edit it at the same time. OpenDocument files (odt, ods, odp) open too; older formats (doc, xls, ppt) are read-only.
  > The document server runs in the dt-onlyoffice container (apps network, port 80), served under /a/onlyoffice/ behind the desktop session (nginx block V4, X-Forwarded-Host with the path). fileops signs the editor configuration (JWT HS256, secret unique to the installation), serves the file on a signed address and receives the save (/office/file, /office/save), replaced atomically keeping owner and permissions. Two openings of the same file join the same session (co-editing).
- **Create a document, a spreadsheet or a presentation.** The OnlyOffice app on the desktop offers three buttons and the list of your recent documents; in Files, right-clicking in a folder adds “New Word document”, “New Excel spreadsheet” and “New PowerPoint presentation”.
  > Blank templates generated by fileops (Office Open XML, A4), given to the folder's owner. Route POST /office/new.

### Improved
- **You can see the bin being emptied.** Items are deleted one after another: the current one is marked, the list empties as it goes, and a bar and the header show progress (“Deleting: 2 of 7, 11.2 GB freed”). At the end, the space freed is announced.
  > One /trash/purge call per item instead of a single one for everything: a large folder can take several seconds, and the user knows which one.

### Fixed
- **Dates and numbers on a browser with an oddly described language.** A browser language the date system does not recognise (“en-US@posix”) made some windows fail to display; the en-GB format is then used.

## 2.59.0 — 18 October 2026

### New
- **A photo or an avatar for your account.** Click your name at the top right, then “Profile picture”: choose one of the 18 suggested avatars or a photo of your own. It is shown in the taskbar, for this account, on every browser.
  > The photo is cropped to the centre and shrunk to 256 px in the browser, then kept on the NAS (data/profiles, included in the Desktroo backup). Routes GET /profile, GET /profile/photo, POST /profile/avatar.

### Improved
- **Account menu.** It now shows the real name and initial of the signed-in account, and its role read from TrueNAS (administrator, read-only…) instead of a fixed “R … Administrator”. “Language” opens the language choice (System configuration → Language and time zone) instead of switching straight to English.

### Fixed
- **Desktroo Apps in the desktop language.** App names and descriptions in the Store stayed in French for Spanish: they are now translated, and an added language shows English rather than French when the app has no translation.
  > Catalogue (store/desktroo-store/catalog.json): “es” entry added to every text (name, tagline, description, ports, setup notes). dkStoreName: desktop language, else English, else French. To be published with publish-store.ps1.
- **No more “unreleased” next to “Installed”.** A published version now keeps its date in the release notes.
  > publish-dist.ps1 dates the entry of the published version (“à paraître” / “unreleased” replaced by today’s date, in the source repository as in the dist). The desktop no longer shows these words for versions already published without a date.
- **The bin shows its real size.** It showed less than the uninstall page (7.6 GB versus 11.7 GB): a large folder was only partly measured when it was deleted (“≥ 5.2 GB”). Its exact size is now calculated right after, and the header gives the space used on disk, which is what “Empty the bin” frees.
  > Full size of a partly measured item calculated in the background and saved in its record; disk space via du -sk of each .desktroo-corbeille folder (same measure as uninstall.sh), recalculated when the content changes. The list refreshes itself during the calculation.

## 2.58.1 — 18 October 2026

### Fixed
- **“See what the update brings” now shows the new version.** The release notes shown sometimes dated from before its release: they stopped at the installed version.
  > The notes are read again as soon as they do not contain the latest version announced by version.txt, on the desktop side (page cache) as well as in the service (15-minute cache); a copy still cached by GitHub is bypassed.

## 2.58.0 — 18 October 2026

### New
- **Beta programme.** Those who wish get new Desktroo versions a few days before everyone else, and help polish them. The invitation appears once in the alerts; you can join or leave at any time in About → Beta programme.
  > Joining creates data/store/beta: updates then come from the “beta” branch of the distribution repository (publish-dist.ps1 -Beta), and Desktroo Apps applications in testing become visible. After leaving, the NAS keeps its version until the next, newer stable version. The answer to the invitation is recorded in data/store/beta-offer. Betas are numbered 2.58.1b1, 2.58.1b2…, before the stable 2.58.1: the beta number is now compared correctly (a 2.58.1b1 used to rank above 2.58.1).

## 2.57.1 — 18 October 2026

### New
- **Desktroo in Spanish.** Spanish joins French and English: the whole desktop and the installation wizard, picked automatically for a browser set to Spanish, or from the login screen and System configuration → Language and time zone.
  > Embedded dictionary (dk-i18n-es, I18N_ES in the wizard), on top of English. An imported “es” file still takes priority, to correct a text. Dates and numbers in es-ES format (or the browser’s, if it is Spanish-speaking).

## 2.57.0 — 18 October 2026

### New
- **Translating Desktroo no longer needs special tools.** The template to translate can now be downloaded as a spreadsheet (.xlsx, for Excel, LibreOffice or Google Sheets), as XLIFF (.xlf, the format of translation tools such as OmegaT, Poedit, Weblate or Crowdin) or as JSON. It also comes in batches of 500 texts, small enough for an online translation service, which refuses large requests.
  > System configuration → Language and time zone → “Translate”: choose the language, the texts (the next 500, a batch or everything) and the format. In Google Sheets, the =GOOGLETRANSLATE() formula suggests a first translation of the column, to review. Import accepts .json, .xlsx, .csv and .xlf.
- **An import adds to the language instead of replacing it.** Batches can be imported one after another; “Replace the whole language” starts from scratch.
- **Release notes get their own window.** On the left, the list of versions leads straight to each one; a filter shows only what is new, improved or fixed, and technical details (the grey lines) hide with one click. A “Back to top” button appears as soon as you scroll down.
  > About → New and updated. The version you are reading is highlighted in the left column. In an added language, the notes are shown in English.

## 2.56.1 — 18 October 2026

### Fixed
- **Desktroo Pass: “Resend the invitation”, “Disable”, “Re-enable”, “Remove 2FA” and “Delete” work.** The vault answered “404” with an HTML page.
  > Vaultwarden only accepts these actions with a JSON body (Content-Type: application/json); Desktroo sent none. An HTML error page is no longer shown as is in the message.

## 2.56.0 — 18 October 2026

### New
- **Add your language to Desktroo.** Desktroo comes in French and English; anyone can now translate it into their own language, without waiting for an update. System configuration → Language and time zone → “Download the template to translate”, translate the file, then “Import a language file…”. The language is offered on the login screen and in the settings, to everyone who opens this desktop; a browser set to that language shows it on its own.
  > The file is <install>/lang/<code>.json: the French texts as keys, the translation as values. Anything not translated stays in English, and “Updated template” gives the file back completed with the texts added by later versions. The file is checked and cleaned on import (formatting tags only). Added languages are part of the Desktroo backup and stay in place across updates.
- **Licence agreement 1.1.** It now allows you to create, use and share language files. The desktop asks you to accept it once more.
- **A link to Desktroo Apps** under “See also” in the App catalog and in the Applications manager.

### Fixed
- **A window can no longer be shrunk until its content is cut off.** Two-pane managers that had no minimum size (Desktroo Apps and its app windows such as Desktroo Pass, TrueNAS updates, Activity log, Health check, Websites, Downloads) stop at 640 × 420, never larger than the screen.
  > The bottom-right corner handle now respects each window’s minimum size, like the edges do.
- **Desktroo Pass says clearly when invitations are not sent by email.** The banner explains that the invited person receives nothing until sending is turned on, and the button is now called “Turn on sending emails”. The vault also tells you when the NAS mail settings changed since it took them over, and a “Send a test email” button checks sending through the same path as invitations.
  > The test goes through Vaultwarden (/admin/test/smtp); its error message is shown as is. Once sending is on, “Resend the invitation” sends the email to people already invited.
- **The applications list shows their icons.** Desktroo Apps apps use the Store icon, and a custom application uses the one of the app with the same name in the TrueNAS catalog (Nextcloud, n8n, OnlyOffice…). The others keep their initial.

## 2.55.0 — 18 October 2026

### New
- **Place widgets anywhere on the desktop.** Drag a widget by its title and drop it wherever you like: it snaps to the edges and to other widgets. Pull its bottom-right corner to make it wider or narrower. Desktop icons move aside so they never end up underneath.
  > A widget placed near an edge stays there when the window is resized, and is brought back on screen if it no longer fits. Control panel → Desktroo desktop → Widgets → “Arrange widgets” puts them back in columns along the edges. After the update, widgets start where they are today. On a phone, they stay stacked as before.

## 2.54.51 — 18 October 2026

### Fixed
- **Deleting a dataset that contains others.** TrueNAS answered “filesystem has children”. Desktroo now lists the child datasets that will go with it, and asks you to retype the dataset's name to confirm before deleting everything.
- **The Desktroo backup also works on a NAS folder with restrictive permissions.** The archive is now prepared elsewhere, then copied into the chosen folder: no more renaming or changing a file's permissions, which some (ACL) folders refuse. If the folder really does not accept writing, the message says which one and what to do.

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
