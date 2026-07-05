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

- `d` discover and reconnect BlueStacks instances
- `c` connect all discovered instances
- `a` start automation on all instances
- `s` start the selected instance
- `x` stop the selected instance
- `t` switch the selected stopped instance between apps
- `g` reset online instance dates to Cairo time
- `l` close the selected app
- `e` export selected instance logs to `logs/`
- `f` export all logs to `logs/`
- `y` export selected logs and copy them to the Windows clipboard
- `v` show selected-instance logs
- `b` show logs from all instances
- `w` show warnings/errors only
- `n` / `p` move selection next/previous
- `r` refresh status
- `q` quit

Legacy plain console mode is still available:

```bash
python main.py --legacy
```
