# Session Timebox

Running many Claude Code sessions across Terminal tabs makes it easy to lose track of which one is waiting on you, which one is mid-task, and what each is about. Session Timebox is a local board that reads your Claude Code sessions and Slack asks, sorts them by kind of work, and lets you drag them onto a 24-hour calendar to time-box the next few days.

## Run it

Requirements: macOS, Python 3.9+, and the `claude` CLI logged in.

```sh
git clone https://github.com/dushyant-uipath/session-timebox.git ~/timebox
python3 ~/timebox/server.py
```

Open http://localhost:8765. Stop the server with `Ctrl+C`.

To keep it running after you close the terminal:

```sh
nohup python3 ~/timebox/server.py > ~/timebox/server.log 2>&1 &
```

To stop a background server:

```sh
pkill -f "timebox/server.py"
```

Options:

| Flag | Default | What it does |
|---|---|---|
| `--port` | `8765` | Port the board listens on |
| `--days` | `14` | How far back to look for sessions |
| `--allow-origin` | `https://dushyant-uipath.github.io` | Extra web origin allowed to call the server, repeatable |

## Start it at login

Save this as `~/Library/LaunchAgents/com.session-timebox.plist`, replacing `YOUR_USER`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.session-timebox</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOUR_USER/timebox/server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/Users/YOUR_USER/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/YOUR_USER/timebox/server.log</string>
  <key>StandardErrorPath</key><string>/Users/YOUR_USER/timebox/server.log</string>
</dict>
</plist>
```

Then load it with `launchctl load ~/Library/LaunchAgents/com.session-timebox.plist`. The `PATH` entry must include the folder that holds `claude` (`which claude` prints it).

## How the board works

- **Sessions** come from `~/.claude/projects/*/*.jsonl` (`sessions_digest.py`). Subagent and temp sessions are skipped.
- **Live status** comes from `~/.claude/sessions/<pid>.json`, which Claude Code writes for each running process: `busy`, `idle`, or `shell` (background command running).
- **Summaries** come from `claude -p --model haiku` with hooks off and no session saved (`summarize` in `server.py`). Each session gets a 5-word headline, a summary, a next step, a work category, and who acts next. A session is re-summarized only after new messages, at roughly $0.006 per summary.
- **Lanes** follow the work category: Critical & blocking, Iterative bug fixing, Map of work, Others. Dragging a card to another lane, or to Done, holds until that session has new activity. Dragging changes only the board; it never sends anything to the session.
- **Status badges** show who acts next: Needs you, Running, Waiting (CI, review, or someone else), Idle.
- **Go to terminal** switches Terminal.app to the tab running that session, matched by its tty. A session that isn't running opens in a new tab with `claude --resume`. The first click asks macOS for permission to control Terminal.

## To-dos and lanes

- Type in **+ Add to-do** at the top of a lane and press Enter to add a card of your own.
- Double-click a to-do title to rename it, and click the card to add a note.
- Double-click a lane name to rename it, and press Enter to save or Escape to cancel.
- Drag a lane by its header to reorder lanes.
- **+ Add lane** creates a lane. Click a lane's **×** twice to delete it; its to-dos move to Others.
- Others and Done can be renamed but not deleted, because cards without a lane land in them.

## Done and removing cards

- Drag any card to **Done**, or click the **×** on the card, to mark it done and stop tracking it.
- Done keeps cards for 24 hours with a **Restore** button, then hides them for good.
- A refresh keeps done cards in Done, and a Slack refresh skips them because they match by message timestamp.
- A done session comes back, tagged "new activity", only if you send it new messages after marking it done.
- Done sessions stop getting summaries.

## Subtasks

- Session cards list the agent-team members working under that session, read from `<session>/subagents/*.jsonl` and their `.meta.json`, with each agent's latest assigned task.
- A pulsing dot marks an agent active in the last 5 minutes.
- Type in **+ subtask** on any card to add your own; tick it off or double-click to rename it.
- Drag any subtask onto the calendar on its own.
- The **×** on a subtask deletes your own subtasks and stops tracking agent subtasks.

## Time boxes

- Drag a card or subtask onto a day and time to create a 1-hour block, snapped to 15 minutes.
- Drag a block to move it, including to another day. Drag its bottom edge to resize it.
- Remove a block with its **×**, by selecting it and pressing Delete, or by dragging it off the calendar. Each removal shows an **Undo** button for 5 seconds.
- Double-click empty time for a custom block, and type its title.
- Double-click a block to jump to its session or Slack thread.
- Drag the divider between the board and the calendar to resize the calendar. Use the Days buttons for 3, 5, or 7 days and the Zoom buttons for taller hours.
- **Export .ics** downloads every block from today on, for import into Google Calendar.

## Slack

The board reads Slack asks from `slack.json`. The Slack connector loads only in interactive Claude Code sessions, so refresh this file from one by asking Claude to find unanswered Slack mentions and DMs and write them to `~/timebox/slack.json` in this format:

```json
{
  "synced_at": 1791016079,
  "items": [
    {
      "id": "1790957704.631429",
      "who": "Sender name",
      "where": "#channel or DM",
      "headline": "Five words or fewer",
      "summary": "One sentence on what it is about.",
      "ask": "What they want from you",
      "category": "critical_blocking | bug_fixing | map_of_work | other",
      "url": "https://...slack.com/archives/...",
      "ts": "1790957704"
    }
  ]
}
```

## Your data

The server listens on `127.0.0.1` only and answers requests from `localhost` and the origins passed with `--allow-origin`; other web pages get `403`. Runtime files hold your session summaries, Slack asks, and calendar, and `.gitignore` keeps them out of the repository:

| File | Contents |
|---|---|
| `timebox.db` | SQLite: lanes, to-dos, subtasks, card placements, done cards, and time boxes |
| `summaries.json` | Session summaries |
| `slack.json` | Slack asks |
| `server.log` | Server output |

The page sends each change as a small update (`POST /api/op`, applied in one SQLite transaction by `apply_ops` in `server.py`), so two open tabs don't overwrite each other. A `state.json` from an older version is imported into `timebox.db` on first start.
