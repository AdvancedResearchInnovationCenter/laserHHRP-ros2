"""Run each test of a detector test script separately (its __main__ order) and classify it:

  PASS    ran and passed
  SKIP    the test returned after printing "skip ... not copied here" (its bags are absent): NOT a pass
  NODATA  the test needs a file or bag that is absent (FileNotFoundError, or rosbag2 "No storage could be
          initialized"): NOT a pass
  FAIL    anything else

Exit code: 1 if any FAIL, 3 if none failed but some did not run (SKIP/NODATA), 0 only if all passed.
"""
import contextlib
import importlib.util
import io
import os
import sys
import time
import traceback

MISSING_BAG = "No storage could be initialized"


def main() -> int:
    path, plan = sys.argv[1], sys.argv[2]
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    spec = importlib.util.spec_from_file_location("t", path)
    t = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(t)
    counts = {"PASS": 0, "SKIP": 0, "NODATA": 0, "FAIL": 0}
    for name in (s.strip() for s in plan.split(";") if s.strip()):
        t0, out = time.time(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                eval(name, vars(t))
            skips = [line for line in out.getvalue().splitlines() if line.startswith("skip ")]
            tag = "SKIP" if skips and time.time() - t0 < 0.5 else "PASS"
            print(f"{tag:<6} {name} ({time.time() - t0:.1f}s)" + (f"  [{'; '.join(skips)}]" if skips else ""))
        except (FileNotFoundError, OSError) as e:
            tag = "NODATA"
            print(f"NODATA {name}: {e}")
        except RuntimeError as e:
            tag = "NODATA" if MISSING_BAG in str(e) else "FAIL"
            print(f"{tag:<6} {name}: RuntimeError: {e}")
        except Exception as e:
            tag = "FAIL"
            tb = traceback.extract_tb(e.__traceback__)[-1]
            print(f"FAIL   {name}: {type(e).__name__}: {e} @ {os.path.basename(tb.filename)}:{tb.lineno}")
        counts[tag] += 1
    not_run = counts["SKIP"] + counts["NODATA"]
    print(f"summary: {counts['PASS']} passed, {counts['FAIL']} failed, {not_run} not run (SKIP/NODATA)")
    return 1 if counts["FAIL"] else 3 if not_run else 0


if __name__ == "__main__":
    sys.exit(main())
