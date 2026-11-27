import json
import sys
from pathlib import Path
from .app import run


def main():
    path = Path(sys.argv[1]).resolve()
    request = json.loads(path.read_text(encoding="utf-8"))
    result = run(request, path.parent.parent)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
