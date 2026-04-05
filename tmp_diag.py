import traceback, sys
try:
    import main
    print("IMPORT OK")
except Exception as e:
    traceback.print_exc(file=sys.stdout)
