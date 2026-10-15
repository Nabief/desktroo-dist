# Release notes — Desktroo

What changes in every version. Each entry is written for what it brings you;
the grey line underneath gives the technical detail, when there is one.

## 2.45.1 — unreleased

### New
- **Delete several snapshots at once.** A checkbox on each row in Snapshots, “Select all” for what is shown, and Shift + click to select a range. A bar shows how many are selected and the space freed, with the delete button.
  > Tip: filter first (for example `auto-2026-09`), then “Select all” only takes the visible rows. A snapshot TrueNAS refuses to delete (held, or the origin of a clone) stays selected, with the reason. The space shown is a minimum: what several snapshots share is only freed when they all go.

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
