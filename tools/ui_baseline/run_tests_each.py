"""Run each test of a detector test script separately (its __main__ order); PASS / SKIP (no data) / NODATA / FAIL."""
import contextlib, importlib.util, io, os, sys, time, traceback
path, plan = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
spec = importlib.util.spec_from_file_location("t", path); t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)
for name in (s.strip() for s in plan.split(";")):
    t0, out = time.time(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            eval(name, vars(t))
        skips = [l for l in out.getvalue().splitlines() if l.startswith("skip ")]
        tag = "SKIP " if skips and time.time() - t0 < 0.5 else "PASS "
        print(f"{tag} {name} ({time.time()-t0:.1f}s)" + (f"  [{'; '.join(skips)}]" if skips else ""))
    except (FileNotFoundError, OSError) as e:
        print(f"NODATA {name}: {e}")
    except Exception as e:
        tb = traceback.extract_tb(e.__traceback__)[-1]
        print(f"FAIL  {name}: {type(e).__name__}: {e} @ {os.path.basename(tb.filename)}:{tb.lineno}")
