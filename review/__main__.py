import sys

# Windows 콘솔(cp949)에서 한글·기호가 깨지지 않게 UTF-8 로 고정
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from .cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
