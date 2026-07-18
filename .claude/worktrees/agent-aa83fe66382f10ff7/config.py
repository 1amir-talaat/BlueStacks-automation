ADB_PATH = r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe"

APPS = {
    "getsms": {
        "package": "com.virtualnumber.sms",
        "activity": "com.sms.activate.ui.SplashActivity",
        "name": "GetSMS",
        "coin_icon_coords": (413, 76),   # top-right coin pill on home screen
        "watch_now_coords": (426, 925),  # "Watch Now" blue button on Purchase Coins page
        "continue_coords": (470, 35),
        "reward_x_coords": (490, 35),
        "google_play_x_coords": (490, 260),
        "ok_button_coords": (410, 580),
    },
    "tempsms": {
        "package": "com.secondphone.tempsms",
        "activity": "com.temp.sms.activity.SplashActivity",
        "name": "TempSMS",
        "coin_icon_coords": (430, 76),   # top-right coin pill on home screen
        "watch_now_coords": (431, 932),  # "Watch Now" pink button on Purchase Coins page
        "continue_coords": (470, 35),
        "reward_x_coords": (490, 35),
        "google_play_x_coords": (490, 260),
        "ok_button_coords": (410, 580),
    },
}

AD_TIMING = {
    "ad_load_wait": 3,
    "ad_poll_interval": 3,
    "ad_timeout": 60,
    "reward_wait": 2,
    "loop_delay": 1.5,
    "max_retries": 3,
}
