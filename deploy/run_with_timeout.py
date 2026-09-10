"""Portable command timeout for the local macOS/Linux SSH release driver."""
import os
import signal
import subprocess
import sys

TIMEOUT_EXIT_CODE = 124


def main():
    duration = int(sys.argv[1])
    process = subprocess.Popen(sys.argv[2:], start_new_session=True)
    try:
        return process.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        print('Remote installation wait timed out; inspect production before another deployment.',
              file=sys.stderr)
        return TIMEOUT_EXIT_CODE
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise


if __name__ == '__main__':
    sys.exit(main())
