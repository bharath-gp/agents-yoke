import argparse
import concurrent.futures
import glob
import os
import re
import subprocess
import sys
import time

FIRST = [
    "test_review",
    "test_models",
    "test_parallel",
    "test_tracker",
    "test_branch_points",
    "test_gates",
]


def run_module(repo_root, mod):
    cmd = [sys.executable, "-m", "unittest", "-q", "tests." + mod]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    return mod, proc


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Run test modules in parallel.")
    parser.add_argument("-k", dest="keyword", default=None, help="Filter test modules by substring.")
    args = parser.parse_args(argv)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests_dir = os.path.join(repo_root, "tests")
    test_files = glob.glob(os.path.join(tests_dir, "test_*.py"))
    modules = [os.path.splitext(os.path.basename(p))[0] for p in test_files]

    keyword = args.keyword
    if keyword:
        if keyword.endswith(".py"):
            keyword = keyword[:-3]
        modules = [m for m in modules if keyword in m]

    first_mods = [m for m in FIRST if m in modules]
    rest_mods = sorted(m for m in modules if m not in FIRST)
    ordered = first_mods + rest_mods

    start_time = time.monotonic()
    results = []
    max_workers = os.cpu_count() or 4

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(run_module, repo_root, mod) for mod in ordered]
        for fut in futures:
            results.append(fut.result())

    elapsed = time.monotonic() - start_time

    total_tests = 0
    failed_modules = []
    failed_outputs = []

    for mod, proc in results:
        combined = (proc.stdout or "") + (proc.stderr or "")
        matches = re.findall(r"^Ran (\d+) tests?", combined, re.MULTILINE)
        if proc.returncode != 0 or not matches:
            failed_modules.append(mod)
            failed_outputs.append((mod, combined))
        if matches:
            total_tests += int(matches[-1])

    for mod, output in failed_outputs:
        print(f"=== {mod} ===")
        if output:
            print(output.rstrip())

    print(f"Ran {total_tests} tests in {elapsed:.1f}s")
    if failed_modules:
        print(f"FAILED (modules: {', '.join(failed_modules)})")
        return 1
    else:
        print("OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
