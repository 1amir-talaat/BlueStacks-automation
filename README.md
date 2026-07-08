# BlueStacks Ad Automation

Automate daily ad-watching tasks across multiple BlueStacks instances to farm coins/rewards from mobile apps.

## What It Does

- Controls multiple BlueStacks instances simultaneously via ADB
- Launches apps and clicks the "watch ad" button
- Handles ad popups (close button, Google Play redirects, "Continue" prompts)
- Detects ad completion and claims rewards
- Loops through ~15 ads per app per day
- Switches apps when one stops giving rewards
- Changes system date to reset daily limits

## Requirements

- Windows with BlueStacks installed
- ADB enabled in each BlueStacks instance (Settings > Advanced > ADB)
- Python 3.10+

## Setup

```bash
pip install -r requirements.txt
```

## Configuration

Edit `config.py` to set:
- ADB ports for each BlueStacks instance
- App package names
- Button coordinates (will be calibrated per app)
- Timing values

## Usage

```bash
python main.py
```

`python main.py` opens the live terminal dashboard by default.

### TUI Controls

- `1` **batch run (1 at a time)** — open the first BlueStacks instance, farm ads until it finishes, stop it, then move to the next instance, and so on
- `2` **batch run (2 at a time)** — keep up to two instances busy; when one finishes, start the next pending instance
- `0` stop the active batch (also stops its instances)
- `d` discover and reconnect BlueStacks instances
- `c` connect all discovered instances
- `j` / `k` select an installed BlueStacks instance
- `m` start the selected BlueStacks instance
- `h` start all detected BlueStacks instances
- `z` open BlueStacks Multi-instance Manager
- `a` start automation on all *already online* instances (not a queued batch)
- `s` start the selected instance
- `x` stop the selected instance
- `t` switch the selected stopped instance between apps
- `g` reset online instance dates to Cairo time
- `l` close the selected app
- `e` export selected instance logs to `logs/`
- `f` export all logs to `logs/`
- `u` copy currently visible log rows
- `y` copy the full current log view to the Windows clipboard
- `v` show selected-instance logs
- `b` show logs from all instances
- `w` show warnings/errors only
- `n` / `p` move selection next/previous
- `r` refresh status
- `q` quit

### Batch mode

Batch mode walks every installed BlueStacks instance (from `bluestacks.conf`):

1. Starts the next instance(s) with `HD-Player`
2. Waits for ADB, settles, and starts ad automation
3. When a worker finishes (both apps exhausted / `done_today`, or stop), closes that instance
4. Fills free concurrency slots from the remaining queue until the list is done

Do not use `a`/`s` while a batch is running. Discover (`d`) keeps live trackers intact during a batch.

Legacy plain console mode is still available:

```bash
python main.py --legacy
```
