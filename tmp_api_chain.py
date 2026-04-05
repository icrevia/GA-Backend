import sys, traceback

# Test each API module
mods = [
    "api.auth",
    "api.users",
    "api.tournaments",
    "api.wallet",
    "api.ws",
    "api.admin",
    "api.support",
    "api.notifications",
    "api.referral",
    "api.router",
]

for mod in mods:
    try:
        __import__(mod)
        print(f"OK   {mod}")
    except Exception as e:
        print(f"FAIL {mod}")
        traceback.print_exc(file=sys.stdout)
        break
