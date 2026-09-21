import os
from datetime import datetime

import frontmatter


ROOT = os.path.dirname(os.path.abspath(__file__))
BUCKETS_DIR = os.environ.get("OMBRE_BUCKETS_DIR", os.path.join(ROOT, "buckets"))


def _date_from_epoch(ts: float) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()


def _iter_markdown_files(base_dir: str):
    for root, _, files in os.walk(base_dir):
        for name in files:
            if name.endswith(".md"):
                yield os.path.join(root, name)


def main() -> int:
    updated = 0
    scanned = 0
    for path in _iter_markdown_files(BUCKETS_DIR):
        scanned += 1
        post = frontmatter.load(path)
        changed = False
        if not post.get("created_at"):
            post["created_at"] = _date_from_epoch(os.path.getctime(path))
            changed = True
        if not post.get("updated_at"):
            post["updated_at"] = _date_from_epoch(os.path.getmtime(path))
            changed = True
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            updated += 1
    print(f"scanned={scanned} updated={updated} buckets_dir={BUCKETS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
