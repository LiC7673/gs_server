"""
test_anysplat.py - AnySplat API smoke test
=========================================

This wrapper uses the current reconstruction flow:
upload images -> create task -> start /reconstruction/start/{task_id}
-> poll status -> download result through /files download APIs.

Usage:
  python test_anysplat.py
  python test_anysplat.py --image-dir /path/to/images --base-url http://127.0.0.1:8000/api/v1
"""

import sys

from scripts.test_reconstruction_algorithms import main


DEFAULT_IMAGE_DIR = "/data1/lzh/lzy/AnySplat/examples/vrnerf/riverview/"


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--algorithms" not in args:
        args.extend(["--algorithms", "anysplat"])
    if "--image-dir" not in args:
        args.extend(["--image-dir", DEFAULT_IMAGE_DIR])
    sys.argv = [sys.argv[0], *args]
    raise SystemExit(main())
