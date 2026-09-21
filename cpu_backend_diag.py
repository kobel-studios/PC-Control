"""Diagnose CpuOcBackend.connect() step by step. Run elevated."""
import ctypes, os, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def step(name, fn):
    try:
        r = fn()
        print(f"OK   {name}: {r}")
        return r
    except Exception:
        print(f"FAIL {name}:")
        traceback.print_exc()
        return None

lib = step("OleDLL", lambda: ctypes.OleDLL(r"C:\Program Files\PawnIO\PawnIOLib.dll"))
if lib:
    from ctypes import wintypes as wt
    lib.pawnio_open.argtypes = [ctypes.POINTER(wt.HANDLE)]
    lib.pawnio_open.restype = ctypes.c_long
    h = wt.HANDLE(0)
    step("pawnio_open", lambda: lib.pawnio_open(ctypes.byref(h)))
    print("handle:", h.value)
    if h.value:
        for p in [r"C:\Users\jacks\HQ\pawnio_modules_024\IntelMSR.bin",
                  r"C:\Users\jacks\HQ\CoolingControl\IntelMSR.bin"]:
            print(p, "exists:", os.path.exists(p))
        lib.pawnio_load.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t]
        lib.pawnio_load.restype = ctypes.c_long
        for p in [r"C:\Users\jacks\HQ\pawnio_modules_024\IntelMSR.bin",
                  r"C:\Users\jacks\HQ\CoolingControl\IntelMSR.bin"]:
            if not os.path.exists(p):
                continue
            data = open(p, "rb").read()
            blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            step(f"pawnio_load {p}", lambda: lib.pawnio_load(h, blob, len(data)))
        lib.pawnio_close.argtypes = [wt.HANDLE]
        lib.pawnio_close.restype = ctypes.c_long
        step("pawnio_close", lambda: lib.pawnio_close(h))

print("admin:", ctypes.windll.shell32.IsUserAnAdmin())
input("Press Enter to exit")
