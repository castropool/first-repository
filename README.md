# TwNewsMonitor — Taiwan news monitor and Word briefings

TwNewsMonitor is a Python 3.12 service that runs continuously. It watches the major Taiwan news outlets, compiles **.docx briefings in Simplified Chinese** (headlines stay verbatim), and pushes them to your phone.

| | |
|---|---|
| **Sources** | 中央社 CNA, 联合新闻网 UDN, 自由时报 LTN, ETtoday, 中时新闻网 China Times, TVBS, Focus Taiwan, Taipei Times, Google News Taiwan (headlines + topic searches) |
| **Collection** | RSS/Atom first, with HTML listing-page scraping as the fallback (trafilatura pulls summaries for scraped articles). Polls every 30 s using conditional GET (ETag / Last-Modified), per-source minimum intervals, per-host request spacing, robots.txt (RFC 9309) and exponential backoff with jitter. |
| **Storage** | SQLite tables for items, HTTP validators and state. Duplicates are dropped by GUID, by normalised link and by content hash. |
| **Briefings** | A **regular** briefing every 60 min (aligned to the clock, configurable), plus an **immediate flash** briefing when a new item matches a watch keyword. The flash has a cooldown that batches bursts of matches. Nothing is generated per poll. The newest N files of each kind are kept. |
| **Push** | Bark (iOS), ServerChan and PushPlus (WeChat), Telegram, or a generic webhook. Several channels can be used at once. Each push carries a short headline list plus the .docx file, or a link to it when the channel can't carry a file that size. |
| **Ops** | Graceful shutdown on SIGINT/SIGTERM (and Ctrl+Break on Windows), JSON logs with rotation, a `--once` smoke-test mode, a systemd user unit, and NSSM or Task Scheduler scripts for Windows. |

```
main.py                     entry point (daemon / --once)
config.yaml                 sources, keywords, schedule, push credentials (placeholders)
requirements.txt
twnews/
  config.py                 YAML + defaults + ${ENV} expansion
  http.py                   robots.txt, rate limiting, conditional GET, retry/backoff
  sources.py                RSS parsing, HTML fallback scraping, trafilatura enrichment
  storage.py                SQLite schema and queries
  textutil.py               Asia/Taipei time, Traditional→Simplified, 1–2 sentence summaries, hashing
  briefing.py               python-docx document builder + retention pruning
  push.py                   Bark / ServerChan / PushPlus / Telegram / webhook channels
  fileserver.py             optional token-protected server so phones can open the .docx
  service.py                scheduler, polling, flash/regular logic, graceful shutdown, --once
deploy/systemd/twnews.service
deploy/windows/{run.bat, install-task.ps1, install-nssm.ps1, keep-wsl-alive.ps1}
tests/test_offline.py       end-to-end tests against a mocked web (no network)
```

---

## 1. Configure

Edit `config.yaml`. Every value has a comment. These are the parts you will normally touch:

1. **Push credentials.** Replace the `YOUR_...` placeholders and set `enabled: true` on each channel you want. To keep secrets out of the file, write `device_key: "${BARK_DEVICE_KEY}"` and set the variable in the environment. The systemd unit reads `~/.config/twnews.env`.
2. **`watch_keywords`.** Matching ignores case and ignores script, so `军演` also matches `軍演` and `TSMC` matches `tsmc`.
3. **`briefing.interval_minutes`** (default 60) and **`briefing.group_by`** (`source` or `topic`, using the `topics:` buckets).
4. **`flash.cooldown_seconds`** (default 120). The first match is pushed straight away; further matches inside the cooldown wait for the next flash. Set it to `0` for one flash per poll cycle that has matches.
5. **`fetch.user_agent`.** Put a real contact address in it.

### Getting the .docx onto your phone

Telegram and the webhook can carry the file itself, up to 50 MB and 10 MB by default. Bark, ServerChan and PushPlus are text-only, so they send a **link**, and you need a URL your phone can reach:

* **Built-in file server (recommended with [Tailscale](https://tailscale.com)).** Set `links.file_server.enabled: true` and a long random `token`, and set `links.public_base_url: http://<tailscale-ip>:8765`. Links then look like `http://<ip>:8765/<token>/taiwan-news_…docx`. The server only serves briefing files, and only under that token.
* **Your own hosting.** Sync `briefings/` to a web folder (OneDrive, Nextcloud, a static host) and set `public_base_url` to that folder's URL.

If no link is configured, the push says the briefing was saved and gives its filename.

---

## 2. Deploy on WSL2 (Ubuntu) with systemd — preferred

Run these in the **Ubuntu (WSL2)** shell unless a step says PowerShell.

```bash
# 2.1 Enable systemd in WSL (once). Recent WSL images may already have it.
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
# then in Windows PowerShell:  wsl --shutdown   and reopen Ubuntu
systemctl --user status >/dev/null && echo "systemd OK"

# 2.2 Python 3.12 (Ubuntu 24.04 ships it; on 22.04 use the deadsnakes PPA)
sudo apt update && sudo apt install -y python3 python3-venv git
python3 --version          # must be 3.12+

# 2.3 Get the code into ~/twnews (keep it on the Linux filesystem, not /mnt/c, for speed)
git clone https://github.com/castropool/first-repository.git ~/twnews
cd ~/twnews
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2.4 Configure, then smoke-test
nano config.yaml
.venv/bin/python main.py --once
```

`--once` prints a table similar to this one:

```
source        status via   fetched   new  notes
中央社          OK     rss         120   120
TVBS新闻        OK     html         48    48
...
9/9 sources OK, 612 items fetched, 598 new, 598 in database.
Sample briefing: /home/you/twnews/briefings/taiwan-news_20261016-221500_sample.docx
Test push via bark: OK (link)
```

Install the user service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/twnews.service ~/.config/systemd/user/
# optional secrets file referenced as ${VAR} from config.yaml:
printf 'BARK_DEVICE_KEY=xxxxxxxx\n' > ~/.config/twnews.env && chmod 600 ~/.config/twnews.env
systemctl --user daemon-reload
systemctl --user enable --now twnews
sudo loginctl enable-linger "$USER"      # start the user service at boot, without a login shell

systemctl --user status twnews
journalctl --user -u twnews -f           # console log
tail -f ~/twnews/logs/twnews.log         # structured JSON log (rotated at 10 MB × 5)
```

The unit expects the project at `~/twnews` with its virtualenv in `~/twnews/.venv`. Edit `WorkingDirectory`/`ExecStart` if yours is elsewhere. `systemctl --user stop twnews` sends SIGTERM; the service then finishes any running poll or briefing (up to 25 s) and exits.

**Keep WSL running.** WSL shuts a distro down soon after the last Windows-side process using it exits, and that stops systemd services too. Register a hidden logon task that keeps the distro alive (Windows PowerShell, from the repo folder, e.g. `\\wsl$\Ubuntu\home\<you>\twnews`):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\keep-wsl-alive.ps1 -Distro Ubuntu
```

The Windows machine itself must stay awake: set *Settings → System → Power → Sleep* to *Never* while plugged in.

**Phone links when running under WSL.** Install Tailscale *inside* WSL (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) and use the WSL node's Tailscale IP in `public_base_url`. Alternatively, put `[wsl2]` and `networkingMode=mirrored` in `%UserProfile%\.wslconfig` and allow TCP 8765 through Windows Firewall, then use the Windows machine's IP.

---

## 3. Windows-native fallback

```powershell
# 3.1 Install Python 3.12+ from python.org (tick "Add python.exe to PATH"), then in the repo folder:
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt     # also installs tzdata, required for Asia/Taipei on Windows

# 3.2 Configure and smoke-test
notepad config.yaml
.venv\Scripts\python main.py --once

# 3.3 Foreground run (Ctrl+C = graceful stop)
deploy\windows\run.bat
```

Then pick **one** of the two ways to run it in the background:

**A. Task Scheduler** (no admin needed). It starts at logon, runs windowless through `pythonw.exe`, and restarts every minute if the process dies:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1
# manage: Start-ScheduledTask / Stop-ScheduledTask / Unregister-ScheduledTask -TaskName TwNewsMonitor
```

Stopping a scheduled task terminates the process without a console signal. SQLite runs in WAL mode, so this is safe, but a briefing that was in progress is lost.

**B. NSSM Windows service** (admin). It starts at boot without a logon, and stops gracefully by sending Ctrl+C and allowing a 30 s timeout:

```powershell
# download nssm.exe from https://nssm.cc and put it on PATH, then in an elevated PowerShell:
powershell -ExecutionPolicy Bypass -File deploy\windows\install-nssm.ps1
# manage: nssm restart|stop|remove TwNewsMonitor
```

Logs are written to `logs\twnews.log` in both cases. NSSM also captures stdout and stderr in `logs\service-*.log`.

---

## 4. How it behaves

* **Polling.** The scheduler ticks every `poll_interval_seconds` (30 s). Each source runs only when its own `min_interval` has passed: 30 s for RSS, 120 s for HTML scraping and Google News. Requests to the same host are at least `host_min_gap_seconds` apart. A 304 response costs almost nothing.
* **Failures.** Each request is retried up to 3 times with exponential backoff and jitter, and `Retry-After` is honoured on 429/503. If every feed of a source fails, the HTML fallback is tried. If that fails too, the source backs off exponentially, up to 15 min.
* **robots.txt** is checked for every URL and cached for 24 h. An unreachable robots.txt counts as *disallow* and is re-checked after 10 min (RFC 9309). Blocked sources are reported in `--once` output and in the logs.
* **First run.** The first fetch of each source fills the database but never triggers a flash, so you don't get flooded by the backlog. Flash also ignores stories older than `flash.max_age_minutes`.
* **Regular briefing window.** A regular briefing covers items fetched since the previous one, and also items published up to `late_grace_minutes` before the window that showed up late in a feed. Empty windows are skipped when `skip_if_empty` is set.
* **Briefing content.** The document starts with a title and the date and time in Asia/Taipei. Items are grouped by source or topic. Each item has a hyperlinked verbatim headline, a 1–2 sentence summary converted to Simplified Chinese (Taiwan phrasing → Mainland phrasing via OpenCC `tw2sp`), its publish time in Taipei time, and its topic and any matched keywords.
* **Retention.** Briefings are pruned to the newest `keep_latest` per kind. Database items older than `storage.retain_days` are deleted daily.

## 5. Limitations and maintenance

* **Feed URLs and HTML patterns change.** They are configured in `config.yaml`, not in code. Run `python main.py --once` after installing, and again whenever a source shows `FAIL` or `0` items. It shows which feeds work and which fell back to HTML. If a site redesigns, adjust `html.link_pattern` (a regex over absolute article URLs) and, optionally, `html.container_xpath`.
* **Feed URLs were not tested live.** The environment this was built in had no access to the news sites. The defaults are the outlets' known public feed and listing URLs, and the HTML fallback covers feeds that have moved. Expect to fix one or two of them after your first `--once` run.
* **English sources** (Focus Taiwan, Taipei Times) keep their English summaries, labelled `（英文原文摘要）`. Machine translation is not included because it would need an external translation or LLM API.
* **Google News** links are Google redirect URLs, and the headlines end with ` - 媒体名`. Google may rate-limit or robots-block automated clients. If that happens the source shows `FAIL` and the other sources keep working.
* **ServerChan** limits titles to 32 characters and **Bark** bodies to about 4 KB. Pushes are kept short, and the full content is in the .docx.

## 6. Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The offline suite mocks the web and push endpoints. It covers RSS parsing, conditional GET/304, robots blocking, HTML fallback and trafilatura enrichment, de-duplication by link and by content hash, flash after bootstrap, the regular briefing (verbatim headlines, simplified summaries, hyperlinks), the Bark, Telegram and webhook payloads, retention pruning and `--once`.
