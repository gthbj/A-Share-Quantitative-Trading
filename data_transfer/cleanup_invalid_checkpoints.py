import json
from pathlib import Path


def main() -> int:
    root = Path("/mnt/localssd/work/table_manifests")
    removed = 0
    kept = 0

    for path in root.glob("*.done"):
        ok = True
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                local_path = Path(json.loads(line)["local_path"])
                if not local_path.exists():
                    ok = False
                    break
        except Exception:
            ok = False
        if ok:
            kept += 1
        else:
            path.unlink(missing_ok=True)
            removed += 1

    print(f"checkpoint kept={kept} removed={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
