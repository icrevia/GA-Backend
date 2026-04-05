import sys, traceback

modules_to_test = [
    "core.config",
    "core.database",
    "models.user",
    "models.wallet",
    "models.tournament",
    "models.support",
    "models.config",
    "schemas.user",
    "schemas.wallet",
    "services.otp",
    "api.auth",
    "api.router",
    "main",
]

for mod in modules_to_test:
    try:
        __import__(mod)
        print(f"OK   {mod}")
    except Exception as e:
        print(f"FAIL {mod}: {e}")
        traceback.print_exc()
        break
