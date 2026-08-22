# Using Ownfoil

Once Ownfoil is running, the Shop Web UI is accessible with your computer/server IP and port, by navigating to `http://<computer/server IP>:8465`, i.e. `http://localhost:8465` from the same computer or `http://192.168.1.100:8465` from a device in your network.

# First steps

1. Open the Web UI.
2. Go to `Admin` → `Settings` and __create an admin user__. Until you do, authentication is disabled and anyone who can reach the Web UI can change the configuration of your shop.
3. Upload your [console keys](#console-keys) under `Titles`, so your files can be identified whatever their name.
4. Configure the [workers](#workers) to optimize identification for large libraries.
5. Add the directories containing your games under `Library` → `Paths`. Ownfoil scans a path as soon as you add it.
6. Open the `Setup` page and configure your client on your Nintendo Switch with the values it shows you.

By default the organizer and compression features are disabled until you turn them on, so your library isn't modified unexpectedly.

# Console keys

Console keys are the keys dumped from your own Nintendo Switch. Ownfoil uses them to decrypt the metadata inside your files, which is how it identifies a game regardless of what the file is called.

Upload your `prod.keys` (or any `.keys` / `.txt` file with the same content) in the `Settings` under `Titles`, then hit `Submit`. Ownfoil checks every master key revision it contains and tells you if one is missing or wrong. A missing revision means the games released after it cannot be decrypted, so they will not be identified.

Without keys, Ownfoil falls back to reading the filename, and every file __must contain `[TITLEID][vVERSION]`__ or it won't be recognized.

Three features need valid keys and are unavailable without them:

* identification of files that aren't named `[TITLEID][vVERSION]`
* [file verification](#file-verification), which is disabled when no valid keys are loaded
* [file compression](#file-compression), which has to decrypt the content to recompress it

# The Web UI

There are three main pages: the library view, the setup guide, and under `Admin` the settings, the task list and the stats.

## Library view

The landing page: one card per game. Each card carries three indicators - base game owned, updates current, DLC set complete - plus a live badge while something of that game is downloading. Clicking a card opens the game's detail page.

You can filter by update currency and by DLC completion, and there is a search box, a card and an icon view, a card size slider and a page size selector. Note the flags are blacklist-aware: a blacklisted DLC never keeps a game from reading complete.

## Game detail page

Everything about one game family, reached by clicking its card in the library: the header shows the art, title id, publisher, release date and the three overall indicators; below, the base game (with the files that carry it and their sizes), every update version with its release date and owned/missing state, and every DLC - owned, missing, or blacklisted with its note. Admins can blacklist/unblacklist a DLC right from this page.

## Setup page

This is the page to use when configuring a client on your Nintendo Switch. It prints the configuration tables for Tinfoil, Sphaira and CyberFoil, already filled in with your own address and port, and the exact menu path to follow in each app.

It has two tabs. `Local Access` is for when your Switch and your Ownfoil server are on the same network, and uses your server's LAN IP. `Remote Access` is for reaching your shop over the internet, and uses the `Shop URL` you configured in the [Shop](#shop) settings.

## Tasks page

Everything Ownfoil does in the background is a task, and this page shows them live: what is queued, what is running with its progress, what is scheduled to run later, and what failed. Failed tasks stay until you dismiss them, so you can see what went wrong earlier.

The worker section shows what each worker process is doing right now. How many there are is configured in [Workers](#workers), you can use it to see if the backlog of tasks can be optimized.

## Downloads page

Admin only. Every transfer the downloaders have picked: torrents handed to qBittorrent and direct Ghost eShop downloads, with live progress for the latter. A row flips to `completed` when the library watcher has identified the landed file - not merely when the transfer finished.

- **Pause / resume** per row: pausing a Ghost eShop transfer cancels it and keeps the partial file; resuming continues from it when still valid (an expired token restarts the transfer from scratch). Torrents pause and resume in qBittorrent - including pauses made from qBittorrent's own UI, which the next pass reflects.
- **Stop all** pauses every unfinished download (queued and transferring, both sources) - rows stay, each resumable.
- **Delete finished** clears the completed rows; failed ones stay retryable until dismissed.

## Manage Library page

Admin only, right below Settings in the menu. Everything about library content in one place:

- live summary tiles (missing content, transfers in flight, failures)
- one card per download source (Ghost eShop, torrents) with its readiness, schedule and a manual **Update now**
- a unified live table of pending and in-flight work - missing targets and active transfers together, with progress bars and per-row actions (retry a failure)
- **Add content**: search the Ghost eShop catalog by name (structured base/updates/DLCs with sizes and owned badges; queueing downloads exactly the chosen entries right away) or search trackers through Jackett and hand a pick to qBittorrent

The old Update Library and Add Content addresses redirect here.

## Activity page

Admin only, live. Who connects to the shop (and from which Switch, when the client sends its device id), every file download with its user and size, and web logins - successes and failures. Filter by kind; the newest 1000 events are kept, so the page doubles as a lightweight audit trail.

## Stats page

Admin only. Library-wide figures plus the breakdowns: files by extension, by library, by verification verdict, and apps by type. The **Unidentified files** section lists each file ownfoil could not match to a title together with the error that last failed it - typically a truncated download or a keys file missing the title's master key (fix the keys and the file is retried automatically).

## Settings page

Admin only, and a single scrolling page.

# Clients

Ownfoil supports multiple clients to install content on your Nintendo Switch. They can be enabled and disabled individually in the [Shop](#client-access) settings.

## [Tinfoil:](https://tinfoil.io/Download)
- `HTTP` / `HTTPS` protocol support
- User authentication
- Shop browsing with icons and banners
- [Content filtering](#content-filters) (games, updates, DLC, multi-content) based on URL
- New games, DLC, Updates, Recommended and XCI sections
- Compressed content (NSZ and XCZ) support
- Encrypted shop support
- Client side Host verification for secure connections
- Tinfoil shop customization

## [Sphaira:](https://github.com/ITotalJustice/sphaira)
- `HTTP` / `HTTPS` protocol support
- User authentication
- Directory-based file browsing
- [Content filtering](#content-filters) (games, updates, DLC, multi-content) based on URL
- Compressed content (NSZ and XCZ) support

Sphaira browses your shop as a folder tree rather than a shop listing, so what you see is the layout of your library on disk. Opening a file shows a preview of its content, to actually install it press `Options` → `Install`.

Sphaira identifies itself in its requests since version `1.0.6`, which Ownfoil needs to serve the shop. Be sure to use an up to date version if encountering issues.

## [CyberFoil:](https://github.com/luketanti/CyberFoil)
- `HTTP` / `HTTPS` protocol support
- User authentication
- Shop browsing with icons and Sections (Updates, DLC)
- Compressed content (NSZ and XCZ) support
- Client side Host verification for secure connections
- Custom welcome message (MOTD)

> [!TIP]
> Check the `Setup` page in the Web UI for specific instructions on configuring each app, using local or remote access.

## Content filters

Adding a path to the shop URL configured in your client filters what it serves:

| Path | What you get |
| --- | --- |
| `/` | Everything, including files Ownfoil could not identify |
| `/base` | Games only |
| `/update` | Updates only |
| `/dlc` | DLC only |
| `/multi` | Files containing more than one content, typically an `xci` bundling a game with its update and DLC |

This is how you get several shops out of one Ownfoil: add one entry per filter in your client and you can browse your updates without scrolling past every game you own.

Note that only the unfiltered shop shows unidentified files. If a file shows up in the root shop but in none of the filters, it was not identified.

# Settings reference

The sections below follow the order of the `Settings` page.

## Authentication

Ownfoil requires an `admin` user to be created to enable authentication for your Shop. Create the first one here, it will have admin rights, then you can add more users to your shop the same way.

Each user has up to three permissions:

| Permission | Grants |
| --- | --- |
| `Shop` | Access to the shop from a client, to file downloads, and to the library page. |
| `Admin` | Access to the `Settings` and `Tasks` pages and everything that changes the configuration. Admins get the other two permissions automatically. |
| `Backup` | Reserved for a future feature, it currently grants nothing. |

You can also create users from [environment variables](./Install.md#environment-variables) at startup, which is handy to avoid ever running an instance without an admin.

## Library

This section is where you tell Ownfoil what your library is, how to watch it, and how to automatically manage the files in it.

Every file goes through the same pipeline, in order: it is __identified__ (what game, which version, base/update/DLC it contains), __verified__, __organized__, then __compressed__. A step that is disabled or already done is skipped, so a settled library does nothing at all. This is why enabling compression on an existing library starts compressing everything.

### Paths

Add the directories containing your content here. The path has to already exist on the machine running Ownfoil: with Docker that means the path *inside* the container, so `/games`, not the path on your NAS.

Ownfoil scans a path as soon as you add it, looking for `nsp`, `nsz`, `xci` and `xcz` files. The arrows button on a row rescans that one path, and `Scan library` rescans all of them. If enabled, the file watcher below detects changes automatically.

### File watcher

| Setting | Default | Description |
| --- | --- | --- |
| `Enable file watcher` | enabled | Watch your library paths for changes. |
| `Polling interval` | `60` seconds | How often network paths are checked. |

Files moved, renamed, added or removed are reflected directly in your library, without a scan.

Local directories are watched through the operating system, which notifies Ownfoil the moment something changes and costs nothing while idle, the polling interval is not used for them at all. Network filesystems (NFS, SMB, a mapped Windows drive) cannot do that, so they are checked at the interval you set instead, which is why a change on a NAS takes up to one interval to show up. Lower it if you want your library to react faster, at the cost of walking the whole tree more often.

### Management

Automated library management. Everything in this section shares the `Submit` button at the bottom.

| Setting | Default | Description |
| --- | --- | --- |
| `Delete older updates` | disabled | Deletes older update files when a newer version of the same update is in your library. |

#### File compression

| Setting | Default | Description |
| --- | --- | --- |
| `Compress files` | disabled | Compress files after they are added and organized. |
| `Compression level` | `18` | zstandard level, `1` to `22`. Higher is smaller but slower. |

Compression converts `nsp` to `nsz` and `xci` to `xcz`, which saves storage space depending on the game. Both formats install fine on all supported clients.

The compressed file is written next to the original, then every piece of content inside it is decompressed again and hashed, and compared against the same content in the source file. The original is only deleted once every one of them matches. If anything fails the original is left exactly where it was and the incomplete output is removed. A file that [verification](#file-verification) found to be corrupt is not compressed.

> [!TIP]
> Compression needs valid [console keys](#console-keys).

<details>
<summary>Advanced compression options</summary>

| Setting | Default | Description |
| --- | --- | --- |
| `Long-distance mode` | disabled | Better ratio on large files, at a higher memory use. |
| `Compression mode` | `auto` | `solid` gives the best ratio, `block` allows random reads so a file can be streamed or mounted without decompressing it fully. `auto` uses solid for `nsp` and block for `xci`. |
| `Block size exponent` | `20` | Block size is `2^x`, from `14` to `32`. `20` is 1 MB. Only used in block mode. |
| `Threads per file` | `0` | Threads a single compression uses, `0` picks automatically. |

`Threads per file` multiplies with the number of tasks running at once, so a high value combined with several [I/O tasks](#workers) will oversubscribe your CPU.

</details>

#### File verification

| Setting | Default | Description |
| --- | --- | --- |
| `Verify files` | enabled | Check that your files are original and intact. |
| Depth | `Full hash` | `Signature only` or `Full hash`. |

One aspect of library curation is to know whether or not the files in your collection are valid. This means different things for different users: some want to ensure every file is original and intact, while for others a modified but working file is enough. File verification is a tedious chore, but a mandatory one to make sure your backups will install and work, or if they need to be replaced.

`Signature only` checks that the file decrypts and that every content header is signed by Nintendo. It is near instant, and it catches a file that isn't what it claims to be. `Full hash` also reads every byte and compares it against the hash the file itself declares, which is the only way to catch actual corruption.

After files have been been successfully decrypted and identified, automatic verification will run in a background task. Using `signature` and content `hash` validation, each file will be classified as either:
- `valid`: original content from Nintendo, intact.
- `repack`: content has been modified, but metadata has been updated to allow integrity verification and it matches.
- `modified`: content has been modified but integrity metadata has not been updated.
- `corrupt`: the integrity of the content cannot be verified.

A `corrupt` file should be deleted or replaced, as installation will fail, but anything else should be fine to keep.

Verification is disabled entirely, with the controls greyed out, when no valid [console keys](#console-keys) are loaded.

#### Organizer

| Setting | Default | Description |
| --- | --- | --- |
| `Enable organizer` | disabled | Move identified files into the paths built from the templates below. |
| `Remove empty folders` | disabled | Remove folders left empty after files are moved out of them. |
| `Windows compatible filenames` | disabled | Use filenames a Windows system can read. |

Once a file is identified, the organizer renders the matching template, and moves the file there if it isn't already. Paths are relative to the library path the file is in, and the extension is added automatically.

By default, file names are sanitized based on the system running Ownfoil, for compatible characters. If Ownfoil is running on a Linux server but you are accessing your library on Windows (with an SMB or NFS mount), you can force the organizer to comply with file names that will render correctly in Windows.

##### Templates

| Template | Default |
| --- | --- |
| `Base template` | `{titleName}/{titleName} [{appId}][v{appVersion}]` |
| `Update template` | `{titleName}/{titleName} [{appId}][v{appVersion}]` |
| `DLC template` | `{titleName}/{appName} [{appId}][v{appVersion}]` |
| `Multi-content template` | `{titleName}/{titleName} [{titleId}]` |

Available in every template:

| Attribute | Description |
| --- | --- |
| `{extension}` | File extension, e.g. `nsp`, `nsz` |
| `{titleId}` | Title ID of the game |
| `{titleName}` | Name of the game title |

Available in the base, update and DLC templates:

| Attribute | Description |
| --- | --- |
| `{appId}` | Application ID |
| `{appVersion}` | Application version |
| `{patchLevel}` | Patch level |
| `{appName}` | Name of the application, the title or the DLC name |

> [!CAUTION]
> Make sure the templates contain `[{appId}][v{appVersion}]` for Tinfoil to recognize the apps.

## Titles

| Setting | Default | Description |
| --- | --- | --- |
| `Library Region` | `US` | Region used to get games informations. |
| `Library Language` | `en` | Language used to get games informations. |

This is the region and language of your shop, and for now it is the same for all users. It matches the names, descriptions and artwork for identified files. Changing it re-downloads titledb and, if the organizer is on, renames your files to match the new names.

The available languages depend on the region you pick.

`Console Keys file` is where you upload your keys - see [Console keys](#console-keys). Below it, `Master key revisions` reports what Ownfoil found in the file you uploaded, and names any revision that is missing or invalid.

## Shop

| Setting | Default | Description |
| --- | --- | --- |
| `Shop URL` | empty | The hostname your shop is reachable at from the internet, e.g. `shop.domain.tld`. |
| `Public shop` | disabled | Serve the shop to clients without authentication. |
| `Message of the day` | `Welcome to your own shop!` | Message presented in clients after successfully loading your shop. |

The MOTD is shown by Tinfoil and CyberFoil. Sphaira browses files and does not display it.

Setting `Shop URL` lets Ownfoil tell the client which address the shop is supposed to be served from, and the client refuses to load it from anywhere else. That is what stops someone who got hold of your URL and credentials from rebroadcasting your shop as their own.

It only works on secure requests: if your reverse proxy still answers on plain `http`, or doesn't send `X-Forwarded-Proto`, none of this applies and your shop is unprotected. See [Remote access and HTTPS](./Install.md#remote-access-and-https).

### Client access

| Setting | Default | Description |
| --- | --- | --- |
| `Tinfoil` → `Enabled` | enabled | Allow Tinfoil to access the shop. |
| `Tinfoil` → `Encrypt shop` | enabled | Serve the shop listing encrypted. |
| `Sphaira` → `Enabled` | enabled | Allow Sphaira to access the shop. |
| `CyberFoil` → `Enabled` | enabled | Allow CyberFoil to access the shop. |

Disabling a client makes Ownfoil refuse it with a message.

`Encrypt shop` compresses and encrypts the shop listing in a way only a real Tinfoil build can read, so only a Tinfoil client will be able to inspect the shop's content. Note that if a client other than Tinfoil is enabled (and not encryption is available), the shop content will be served in clear through their headers, rendering encryption useless.

## Scheduler

| Setting | Default | Description |
| --- | --- | --- |
| `TitleDB update interval` | `12h` | How often to refresh titledb and re-process your library. |

The interval is a number followed by a unit: `s`, `m`, `h` or `d`, so `30m`, `2h` and `1d` are all valid. Set it to `0` to disable the periodic run entirely.

TitleDB is updated upstream once per day, this scheduled task will sync your local reference and add the new titles, updates and DLCs. Libraries will be re-processed after the update, for example to identify files to titles that were not available before.

## Workers

| Setting | Default | Description |
| --- | --- | --- |
| `Worker count` | `2` | Number of worker processes. |
| `Max concurrent I/O tasks` | `1` | How many disk-heavy tasks run at once. |

Workers are the processes that run everything in the background. More of them means more tasks in parallel like identifying, scanning, compressing.

The second setting ensures CPU/disk heavy tasks do not loose the benefit of parallelism. Compression, decompression and verification read and write multi-GB files, and running several of them at once can be detrimental. If the files are on the same hard drive, parallel tasks will make the heads seek back and forth and will make processing time significantly longer.  So this limit caps them independently of the worker count, while light tasks keep flowing. Pick it based on where your library lives:

* __Network share or a single hard drive: `1`__ - avoids seek thrashing, the safe default.
* __SATA SSD: `2` to `3`__ - no seek penalty, so you are limited by CPU.
* __NVMe with spare CPU cores: raise it towards your core count__ - each compression uses about 3 to 4 threads, so the CPU, not the disk, is the ceiling.

## Downloader

Two download sources coexist under one section. Each has its own enable switch, credentials, schedule interval and manual trigger, and both draw from the same missing-content work list without ever racing for the same target (one row per app id + version).

### Blacklist

App ids on the blacklist (Settings → Blacklist, or inline from a game's detail page) are deliberately unwanted content - typically language-pack DLCs. A blacklisted app stops counting against its game's complete/up-to-date indicators, never appears as missing content, and no source will download it; a file that lands for one anyway is still identified and served. The section also imports the switch-library-updater `blacklist.json` format (ids or `{id, note}` objects) in one upload.

### Torrents source

| Setting | Default | Description |
| --- | --- | --- |
| `Enable torrents source` | off | Run the periodic scan and allow manual runs. |
| `Jackett URL` / `API key` | - | Tracker search backend. |
| `qBittorrent URL` / credentials | - | Where torrents are handed to. |
| `qBittorrent save path` | `/games` | Must be the same volume as a library path, as qBittorrent sees it. |
| `Min seeders` / `Max size` / `Preferred extensions` / `Indexers` | `3` / no limit / `nsz, nsp, xcz, xci` / all | Result filters; results failing any of them are ignored. |
| `Run interval` | `1h` | Periodic pass; `0` disables it. |

Each pass searches every missing update/DLC (app id first, then game name), ranks the matches and adds the best torrent. A bundle torrent covering several targets is added once.

### Ghost eShop source

| Setting | Default | Description |
| --- | --- | --- |
| `Enable Ghost eShop source` | off | Run the periodic scan and allow manual runs. |
| `Portal URL` | `https://pro.nlib.cc` | The Ghost eShop PRO portal. |
| `Username` / `Password` | - | Your PRO credentials (password kept out of the UI once saved). |
| `Catalog language` | `en` | Language for catalog names. |
| `Run interval` | `24h` | Periodic pass; `0` disables it. |
| `Download root` | first library path | Where per-game folders are created. |

Downloads go straight into `<library>/<game name>/` with the same folder naming the organizer uses, resumable across runs if a transfer is interrupted. Console keys must be loaded for the landed files to be identified.

Each file downloads as its own task in the I/O concurrency group - the same `Max concurrent I/O tasks` budget as verification and compression ([Workers](#workers)) - so a big sync takes turns with the rest of the pipeline instead of hammering the disk alongside it. The torrents lane is different: its pass only talks to the Jackett/qBittorrent APIs, and the actual disk writes belong to qBittorrent, which manages its own pace.
