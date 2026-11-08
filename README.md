# Audiobook sorter

Files new audiobooks into your Audiobookshelf library on the NAS:

```
audiobooks/<Author>/<Series>/<N - Title>/<files>     (book in a series)
audiobooks/<Author>/<Title>/<files>                  (standalone)
```

Books come from two places:
- **qBittorrent** (optional). Finished torrents in the `audiobooks` category are virus-scanned
  and hard-linked into the library, so they keep seeding. Once qBittorrent's share limit is
  reached, they are removed.
- **An inbox folder** (`source_dir`) for books you drop in by hand. These are moved.

A small web page lists books needing review, the sorted history, and anything quarantined.

The usual setup runs everything on the QNAP (see [Run on QNAP](#run-on-qnap)). The script
also still runs on Windows on its own.

---

## How a torrent is handled
1. **Download.** qBittorrent downloads into `torrents/incomplete`, then moves the finished
   files to `torrents/audiobooks`.
2. **File-type filter.** Only audio, covers, `.cue`, `.nfo`, `.txt`, `.pdf`, playlists and
   checksums are allowed. If the torrent has anything else (`.exe`, `.zip`, `.lnk`, …), or
   an "mp3" that is really a program, the whole torrent goes to `torrents/quarantine` and is
   removed from qBittorrent.
3. **ClamAV.** Same outcome if a virus is found. If ClamAV can't be reached, the torrent
   waits; nothing unscanned is ever moved.
4. **Metadata.** Tags are checked against Audible (see below).
5. **Placement.** A good match is hard-linked into the library and tagged `sorted` in
   qBittorrent. Anything uncertain is hard-linked into `torrents/review` and tagged `review`.
6. **Seed limit.** When qBittorrent stops the torrent because it reached its share limit
   (ratio or time), the sorter:
   - checks the library/review copy is complete;
   - deletes the torrent and its download.

   A torrent you stop by hand is left alone.

A hard link is a second name for the same file. It uses no extra space, and deleting the
download doesn't affect the library copy. For hard links to work, the downloads and the
library must be on the same NAS volume, and inside one container mount (`/data` below).
Otherwise the sorter copies the files and logs a warning.

> Keep Audiobookshelf's **"embed metadata"** feature off. It rewrites audio files, which
> would also change the seeding copy and break the torrent.

## What counts as a book
- An audio file sitting directly in the inbox, or a single-file torrent.
- A folder of chapter files (e.g. 115 MP3s with the same album tag). Subfolders named
  `CD1`, `Disc 2`, or `Part 3` are treated as one book. Everything in the folder (cover,
  .cue, .nfo) goes with it.
- **Collections are split automatically:**
  - A folder whose subfolders each hold a book becomes one book per subfolder.
  - A folder of files with different album tags becomes one book per album.
  - A folder of book-length files (each over 2.5 h, each with its own title) becomes one
    book per file.

## How metadata is decided
1. **Tags.** Read from the first audio file: author (album artist/artist), title (album, or
   the title tag for single files), series (`SERIES`/`MVNM`/`©mvn`) and position
   (`SERIES-PART`/`MVIN`).
2. **Names.** If there are no tags, the name is read instead: `Author - Title`,
   `Author - Series 01 - Title` or `Series - 01 - Title`. The parent folder's `Author - …`
   name is a backup author.
3. **Audible check.** Audible is searched with a few versions of the title. A book is
   accepted when:
   - the title **and** author match at `match_threshold` or above; and
   - the book number agrees with any number in the tags or name.

   Audible's spelling, series and book number are then used.
4. **Folder names.** They follow your library's style: `01 - Title`, with the series name
   taken off the front of the title.
5. **Otherwise: review.** The book goes to review, and `review.log` (plus the web page) says
   why. Decide on the web page (see below), or by hand:
   - correct the tags or rename the folder;
   - move it from the review folder into the inbox, and it is sorted again.

---

## Web page
`http://<NAS>:8090` (or `http://localhost:8090` on Windows), enabled by the `[web]` section.

| Page | Shows |
|---|---|
| **Needs review** | Every book waiting for you, with the reasons it didn't match, what the tags said, the closest Audible match, and where the files are, plus buttons to decide (below). An item disappears once it is sorted, rejected or moved out of the review folder. |
| **Sorted** | History, newest first, with search. It shows whether each torrent is still seeding or has been removed. |
| **Quarantine** | What was blocked and why. |

The header shows whether qBittorrent and ClamAV are reachable, and how recent the virus
signatures are.

### Deciding on review items
With a password set, each review card has three buttons:

| Button | What it does |
|---|---|
| **Accept match** | Files the book as the closest Audible match. The card shows the exact folder first, e.g. `Brandon Sanderson/The Stormlight Archive/05 - Wind and Truth`. Only shown when Audible had a candidate. |
| **Edit and sort** | Opens a form (author, series, book number, title), pre-filled with the Audible candidate or the tags. The book is filed exactly as you type it. |
| **Reject** | Asks once more, then deletes the review copy. For a torrent, only the review copy goes: the torrent seeds until its limit and is then removed with its download. For a book from the inbox it's the only copy, so it's gone for good; the page says so before you confirm. |

Accepted and edited books move from the review folder into the library and show up on the
Sorted page. A torrent keeps seeding from its download, and is removed at its seed limit as
usual. If the destination already has audio, nothing moves and the page tells you why.

**Security.** The buttons only appear when `password` is set under `[web]` (or the
`SORTER_UI_PASSWORD` environment variable); without one the page stays read-only. Every form
carries a secret token, and requests from other websites are refused. Only reach the page
over your LAN or Tailscale; never forward the port on your router.

---

## Run on QNAP

### 1. Put the code on GitHub (you do this once)
There's a local git repository with one commit. Create an **empty public** repository on
GitHub (no README or licence), then run:

```bash
git remote add origin https://github.com/YOUR_GITHUB_USER/audiobook-sorter.git
```

```bash
git push -u origin main
```

GitHub Actions (**Actions** tab) builds the image `ghcr.io/YOUR_GITHUB_USER/audiobook-sorter`
for both Intel and ARM NASes. Once that run is green:
1. Open your GitHub profile → **Packages** → `audiobook-sorter` → **Package settings**.
2. Set **Change visibility** to **Public**, so the NAS can pull the image without a login.

Each later push rebuilds the image. To update the NAS, re-pull it in Container Station.

`config.toml`, logs and the history database are git-ignored. Paths and passwords never
leave your machines.

### 2. Prepare the NAS (QTS)
1. **Create a user for the containers.** Control Panel → Users → Create:
   - name `media`;
   - no admin rights;
   - read/write on the `Container` share only.

   Note its UID and GID: SSH in and run `id media`, or use the user list. Put them in
   `docker-compose.yml` in place of `1001` / `100`.
2. **Create the folders** (File Station, inside the `Container` share):
   ```
   torrents/incomplete   torrents/audiobooks   torrents/inbox
   torrents/review       torrents/quarantine
   qbittorrent           clamav                audiobook-sorter
   ```
3. **Add the config.** Copy `config.example.toml` to `Container/audiobook-sorter/config.toml`.
   Its paths are already set for this layout.

### 3. Create the containers
Container Station → **Applications** → **Create**:
1. Paste `docker-compose.yml`.
2. Replace `YOUR_GITHUB_USER` and the UID/GID.
3. Set `TZ` to your timezone.
4. Create the application.

It starts three containers:

| Container | Port | Notes |
|---|---|---|
| `qbittorrent` | 8081 (Web UI; QTS already uses 8080), 6881 (torrents) | Saves to `/data/torrents`. |
| `clamav` | none published | Updates its signatures by itself. The first start takes a few minutes while it downloads them; the sorter waits. |
| `audiobook-sorter` | 8090 | Runs as the `media` user with a read-only filesystem and no extra privileges. |

If creation fails with `address already in use`, another app on the NAS has that port.
Pick another port in `docker-compose.yml`, delete the half-created application in Container
Station, and create it again. For qBittorrent's Web UI change all three together: `WEBUI_PORT`,
both sides of the port mapping (e.g. `8082:8082`), and `url` in `config.toml`. If the two sides
differ, qBittorrent's page just says "Unauthorized".

#### Fix "Permission denied"
The sorter runs as the UID/GID in `docker-compose.yml` (`user:`), and it must be able to write
to `/config` (the log and history database), the torrents folders and the library. Folders
made in File Station belong to your admin account, so it usually can't. Fix it over SSH:

1. Check the numbers: `id media`. They must match `user:` and `PUID`/`PGID` in the compose
   file. If they don't, edit the compose file and recreate the application.
2. Make `media` the owner of the folders the sorter writes to:
   ```bash
   sudo chown -R media:everyone /share/Container/audiobook-sorter /share/Container/torrents /share/Container/audiobookshelf/audiobooks
   ```
   ```bash
   sudo chmod -R u+rwX,g+rX /share/Container/audiobook-sorter /share/Container/torrents /share/Container/audiobookshelf/audiobooks
   ```
3. Let ClamAV read the downloads. It scans as uid 100 inside its container, and QTS's
   folder ACLs refuse that user even when the permission bits allow everyone (the sorter's
   log says `clamd could not scan: ... File path check failure: Permission denied`). Add
   a read-only ACL entry, inherited by new downloads:
   ```bash
   sudo setfacl -R -m u:100:rX -m d:u:100:rX /share/Container/torrents
   ```
   Check: `docker exec -u clamav clamav ls /data/torrents/audiobooks` must list the files.
4. Restart the `audiobook-sorter` container.

Audiobookshelf only needs to read the library, so it keeps working.

### 4. Set up qBittorrent (`http://QNAP-NV:8081`)
The first login password is in the container's log.

- **Downloads**
  - Default save path: `/data/torrents/audiobooks`.
  - Tick "Keep incomplete torrents in" and set it to `/data/torrents/incomplete`.
- **Categories.** Right-click **Categories** → Add: name `audiobooks`, save path
  `/data/torrents/audiobooks`.
- **BitTorrent → Seeding limits**
  - "When ratio reaches" e.g. `2` and/or "When total seeding time reaches" e.g. `20160`
    minutes (14 days).
  - Then: **Stop torrent** (called *Pause torrent* in v4). Don't pick "Remove": the sorter
    does the removing, after checking the library copy.
- **Web UI**
  - Set your own password.
  - Untick "Use UPnP / NAT-PMP to forward the port from my router".
  - Tick "Bypass authentication for clients in whitelisted IP subnets" and enter
    `172.30.50.0/24`. This lets the sorter in without storing a password. Alternatively,
    put the username/password in `config.toml`.
- **Connection.** Untick UPnP. Forward only the torrent port (6881) on your router, never
  8081 or 8090.

### 5. Send magnets from Firefox (Torrent Control)
Torrent Control → Options:
- Client: **qBittorrent**.
- Address: `http://QNAP-NV:8081`, or the NAS's Tailscale address when away from home.
- Your qBittorrent username and password.
- Category `audiobooks`. Turn on the context-menu / click-to-add options you like.

Clicking a magnet link then queues it on the NAS straight into the audiobooks category.

### 6. Check it works
1. **Dry run.** In Container Station, open the sorter container's terminal (or SSH) and run:
   ```bash
   docker exec audiobook-sorter python /app/audiobook_sorter.py --config /config/config.toml --once --dry-run
   ```
   This prints what it would do, without changing anything.
2. **Virus scan test.** Put the
   [EICAR test file](https://www.eicar.org/download-anti-malware-testfile/) in
   `torrents/inbox/test/`. It's a harmless string every antivirus flags. Within a minute it
   should land in `torrents/quarantine`, and show on the Quarantine page.
3. **Torrent test.** Add a free public-domain LibriVox torrent with a low ratio limit
   (right-click → Limit share ratio → 0.1). It should:
   - appear in the library and Audiobookshelf;
   - after the limit, disappear from qBittorrent and `torrents/audiobooks`;
   - still play in Audiobookshelf.

### 7. Keep the NAS secure
- **Accounts:** disable the default `admin` account, and turn on 2-step verification for
  yours.
- **Updates:** turn on automatic firmware and app updates.
- **No port forwarding:**
  - turn off myQNAPcloud's auto router configuration (UPnP);
  - use Tailscale for remote access.
- **Security Counselor:** run it, using the Basic or Intermediate policy.
- **Snapshots:** daily on the volume, kept 14 days. They're your undo for ransomware or
  mistakes.
- **Malware Remover:** schedule it daily.
- **Antivirus:** schedule a weekly QTS antivirus scan of `Container/audiobookshelf`, as a
  second layer.

---

## Run on Windows (standalone)
1. Install Python 3.11+, then `python -m pip install -r requirements.txt`.
2. Copy `config.example.toml` to `config.toml`:
   - set `source_dir` and `library_dir`;
   - delete the `[qbittorrent]` section;
   - set `clamd_host = ""` (file-type filter only).
3. Try a dry run: `python audiobook_sorter.py --once --dry-run`. Add `-v` to see the log
   lines too.
4. Run it for real: `python audiobook_sorter.py`. It watches until you press Ctrl+C.

Windows paths in `config.toml` go in 'single quotes' (e.g. `'G:\Torrents\AUDIOBOOKS'`).

**Start it at logon** with Task Scheduler → Create Task:
- Trigger: *At log on*.
- Action:
  - program: the full path to `pythonw.exe`;
  - arguments: `"C:\path\to\audiobook_sorter.py"`;
  - start in: the script's folder.
- Untick "Stop the task if it runs longer than…".

## Inbox details
- A book in the inbox is moved once it has stopped changing for `settle_seconds`.
  - On the same volume it is renamed (instant).
  - Otherwise it is copied, its size checked, and the original deleted.
- **Leftover folders.** Once a folder has no audio left (only covers, .txt, .nfo …), it is
  deleted. Folders the sorter didn't empty itself are only cleaned up after 10 minutes
  without changes.
- **Retries.** If the NAS, Audible or ClamAV can't be reached, the book stays put and is
  retried later.
- **Duplicates.** A book whose destination already has audio goes to review as a duplicate.

The log is `audiobook_sorter.log`: in `/config` on the NAS, next to the script on Windows.
The history database `sorter.db` is in the same folder.
