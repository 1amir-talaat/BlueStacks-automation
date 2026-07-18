# GetSMS / TempSMS — Ad-Limit Bypass via Root

## Overview

GetSMS and TempSMS are virtual-number apps that reward users with coins for watching ads.
Each account has a **daily ad cap** enforced both locally (SharedPreferences) and
server-side (account ID). Once the cap is hit the app shows a "daily limit reached"
dialog or enters an infinite "Loading..." state.

With root access on BlueStacks we can modify the app's internal storage to:

1. **Reset local ad-limit flags** (keeps existing coins, may be re-detected server-side)
2. **Generate a brand-new account** by changing the device ID and deleting the local
   database — the app re-registers with a fresh server-side identity and zero ad history
   (coins are lost)

---

## Storage Layout

Both apps store state under `/data/data/<package>/`.

### GetSMS (`com.virtualnumber.sms`)

| Path | Contents |
|------|----------|
| `shared_prefs/default.xml` | Daily-limit flags |
| `shared_prefs/com.virtualnumber.sms.xml` | Device ID + head info |
| `shared_prefs/admob.xml` | Google AdMob session counters |
| `databases/temp_number` | SQLite DB with `accounts` table (user ID, balance, JWT) |

### TempSMS (`com.secondphone.tempsms`)

| Path | Contents |
|------|----------|
| `shared_prefs/default.xml` | Daily-limit flags (different key names) |
| `shared_prefs/com.secondphone.tempsms.xml` | Device ID + head info |
| `shared_prefs/admob.xml` | Google AdMob session counters |
| `databases/temp_sms` | SQLite DB with `accounts` table |

---

## SharedPreferences Keys

### `default.xml` — Daily Limit Flags

**GetSMS** uses:
```xml
<boolean name="daily_reward_max_reached" value="false" />
<int name="the_daily_reward_day" value="20260725" />
```

**TempSMS** uses:
```xml
<int name="ad_watch_progress_current" value="0" />
<int name="ad_watch_progress_target" value="1" />
<long name="ad_watch_last_reward_at" value="1784365475" />
<boolean name="ad_watch_daily_limit_reached" value="false" />
<int name="ad_watch_daily_limit_day" value="20260718" />
```

| Key | Type | Meaning |
|-----|------|---------|
| `daily_reward_max_reached` / `ad_watch_daily_limit_reached` | boolean | `true` when daily cap is hit — blocks ad button |
| `the_daily_reward_day` / `ad_watch_daily_limit_day` | int (YYYYMMDD) | Date the limit was reached or last ad was watched |
| `ad_watch_progress_current` | int | Ads watched today so far |
| `ad_watch_progress_target` | int | Daily ad target |
| `ad_watch_last_reward_at` | long | Unix timestamp of last reward |

### `<package>.xml` — Device Identity

```xml
<string name="key_device_id">17997a02-abe4-3b3f-b8ce-592ce8685d55</string>
<string name="KEY_HEAD_INFO">HeadBean{deviceVersion=samsung-SM-G998B, sysVersion=9, ...}</string>
```

`key_device_id` is sent to the server in the JWT token payload as `device_uuid`.
Changing this + deleting the database forces the server to treat the app as a
brand-new device, creating a fresh account.

### `admob.xml` — AdMob Counters

```xml
<int name="request_in_session_count" value="36" />
<long name="first_ad_req_time_ms" value="1784364971414" />
<long name="app_last_background_time_ms" value="1784157978610" />
```

These track ad request frequency within a session. Resetting
`request_in_session_count` to 0 can help avoid rate-limiting by the ad SDK.

---

## Database Schema

Both apps use Room with the same schema:

```sql
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    account_id TEXT,
    balance REAL NOT NULL,
    token TEXT
);
```

The `token` column stores a JWT:
```
Header:    {"typ":"JWT","alg":"HS256"}
Payload:   {"user_id":126582,"device_uuid":"17997a02-...","exp":...,"iat":...}
```

The `device_uuid` in the JWT must match `key_device_id` in SharedPreferences.
If they mismatch the server may reject requests.

---

## Two Bypass Strategies

### Strategy A: Reset Local Flags (keeps coins)

**What it does:** Sets `daily_reward_max_reached=false` and resets the day counter
without touching the account or device ID.

**Pros:** Preserves existing coin balance.

**Cons:** Server-side tracking may re-trigger the limit quickly since the account
history is unchanged.

**When to use:** Early in the day-trick cycle, before the server fully locks the
account. As a "soft reset" between ad batches.

### Strategy B: New Account (loses coins, full reset)

**What it does:**
1. Force-stop the app
2. Generate a new UUID for `key_device_id`
3. Write the new device ID to `<package>.xml`
4. Reset `default.xml` flags
5. Delete the database files (`temp_number` / `temp_sms` + WAL/SHM)
6. Clear Google measurement prefs
7. Relaunch the app (it registers as a new user)

**Pros:** Completely fresh server-side identity, unlimited daily ads.

**Cons:** Loses all accumulated coins.

**When to use:** When the server has fully locked the account and local resets no
longer work. As a "hard reset" to start earning from scratch.

---

## ADB Commands Reference

All commands require `su -c` (Magisk root shell on the BlueStacks instance).

```bash
# Force-stop an app
su -c 'am force-stop com.virtualnumber.sms'

# Read a SharedPreferences file
su -c 'cat /data/data/com.virtualnumber.sms/shared_prefs/default.xml'

# Write a file via /sdcard (push from host, then copy as root)
adb push local_file.xml /sdcard/_temp.xml
su -c 'cp /sdcard/_temp.xml /data/data/com.virtualnumber.sms/shared_prefs/default.xml'
su -c 'chmod 660 /data/data/com.virtualnumber.sms/shared_prefs/default.xml'

# Delete database (hard reset)
su -c 'rm -f /data/data/com.virtualnumber.sms/databases/temp_number*'

# Clear Google measurement prefs
su -c 'rm -f /data/data/com.virtualnumber.sms/shared_prefs/com.google.android.gms.measurement.prefs.xml'

# Launch an app
am start -n com.virtualnumber.sms/com.sms.activate.ui.SplashActivity

# Query the database
su -c 'sqlite3 /data/data/com.virtualnumber.sms/databases/temp_number "SELECT * FROM accounts;"'
```

---

## BlueStacks Root Setup

1. Download `blueStackRoot.cmd` from
   [Jordan231111/BluestacksRoot v7](https://github.com/Jordan231111/BluestacksRoot/releases/tag/v7)
2. Close all BlueStacks instances
3. Right-click → **Run as administrator**
4. Pick option **1** (Pie64 / Android 9)
5. Wait for `VERIFY PASS`
6. Launch the instance — `su` should now work via Magisk

**Note:** BlueStacks 5.22.210+ requires the v7 tool which patches the HD-Player
anti-tamper check. Older versions can use the manual Kitsune Mask method.

---

## Automation Integration

The `automation/ad_reset_root.py` module provides:

```python
from automation.ad_reset_root import reset_app_ads, reset_all_ads

# Soft reset (keeps coins)
reset_app_ads(adb, "getsms")

# Hard reset (new account)
reset_app_ads(adb, "getsms", preserve_device_id=False, new_device_id=str(uuid.uuid4()))
```

**Recommended workflow:**

1. Watch ads normally via the date trick
2. When daily limit is hit → soft reset (reset local flags)
3. If server re-detects → hard reset (new account)
4. Repeat cycle

---

## Key File Paths (Quick Reference)

```
GetSMS:
  /data/data/com.virtualnumber.sms/shared_prefs/default.xml
  /data/data/com.virtualnumber.sms/shared_prefs/com.virtualnumber.sms.xml
  /data/data/com.virtualnumber.sms/shared_prefs/admob.xml
  /data/data/com.virtualnumber.sms/databases/temp_number

TempSMS:
  /data/data/com.secondphone.tempsms/shared_prefs/default.xml
  /data/data/com.secondphone.tempsms/shared_prefs/com.secondphone.tempsms.xml
  /data/data/com.secondphone.tempsms/shared_prefs/admob.xml
  /data/data/com.secondphone.tempsms/databases/temp_sms
```
