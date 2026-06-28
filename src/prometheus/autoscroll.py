#!/usr/bin/env python3
"""Auto-scroll QQ guild channel using ydotool."""
import subprocess
import time

from . import config


def main() -> None:
    ydotool = config.get("ydotool_bin", "/usr/bin/ydotool")
    socket = config.get("ydotool_socket", "/tmp/.ydotool_socket")
    feeds_path = config.data_dir() / "feeds.jsonl"

    max_iter = config.get("autoscroll_max_iterations", 50000)
    interval = config.get("autoscroll_interval_sec", 1)
    log_every = config.get("autoscroll_log_every", 20)

    def count_feeds() -> int:
        try:
            with open(feeds_path) as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    print(f"Auto-scroll started (max {max_iter}, every {interval}s). Ctrl+C to stop.")
    start = count_feeds()
    for i in range(1, max_iter + 1):
        subprocess.run(
            [ydotool, "key", "104B"],
            env={"YDOTOOL_SOCKET": socket},
            capture_output=True,
        )
        subprocess.run(
            [ydotool, "click", "4"],
            env={"YDOTOOL_SOCKET": socket},
            capture_output=True,
        )
        time.sleep(interval)
        if i % log_every == 0:
            cur = count_feeds()
            print(f"  scroll #{i}: {cur} feeds (+{cur - start})")

    print(f"Done. Total: {count_feeds()}")


if __name__ == "__main__":
    main()
