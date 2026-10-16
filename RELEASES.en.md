# Release notes — Desktroo

What changes in every version. Each entry is written for what it brings you;
the grey line underneath gives the technical detail, when there is one.

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
