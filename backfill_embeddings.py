#!/usr/bin/env python3
"""
Backfill embeddings for existing buckets.
???????? embedding?

Usage:
    OMBRE_BUCKETS_DIR=/data OMBRE_EMBEDDING_API_KEY=xxx python backfill_embeddings.py [--batch-size 20] [--dry-run]

Existing vectors from another model are automatically rebuilt.
"""

import asyncio
import argparse
import sys
from typing import Any

sys.path.insert(0, ".")
from utils import load_config
from bucket_manager import BucketManager, _is_sealed_bucket
from embedding_engine import EmbeddingEngine


async def backfill_batch(
    bucket_mgr: BucketManager,
    engine: EmbeddingEngine,
    limit: int = 20,
) -> dict[str, Any]:
    """Generate a bounded batch of missing vectors without modifying buckets."""
    if not engine.enabled:
        raise RuntimeError("Embedding engine is not enabled")

    limit = max(1, min(int(limit), 50))
    all_buckets = await bucket_mgr.list_all(include_archive=True)
    eligible = [
        bucket
        for bucket in all_buckets
        if str(bucket.get("content", "")).strip()
        and not _is_sealed_bucket(bucket)
    ]

    missing = []
    for bucket in eligible:
        if await engine.get_embedding(bucket["id"]) is None:
            missing.append(bucket)

    success = 0
    failed = 0
    for bucket in missing[:limit]:
        if await engine.generate_and_store(bucket["id"], bucket["content"]):
            success += 1
        else:
            failed += 1

    remaining = 0
    for bucket in eligible:
        if await engine.get_embedding(bucket["id"]) is None:
            remaining += 1

    return {
        "model": engine.model,
        "total_buckets": len(all_buckets),
        "eligible_buckets": len(eligible),
        "empty_skipped": len(all_buckets) - len(eligible),
        "indexed_total": len(eligible) - remaining,
        "attempted": min(limit, len(missing)),
        "success": success,
        "failed": failed,
        "remaining": remaining,
        "last_error": engine.last_error if failed else "",
        "error_details": engine.last_error_details if failed else {},
    }


async def backfill(batch_size: int = 20, dry_run: bool = False):
    config = load_config()
    bucket_mgr = BucketManager(config)
    engine = EmbeddingEngine(config)

    if not engine.enabled:
        print("ERROR: Embedding engine not enabled (missing API key?)")
        return

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    print(f"Total buckets: {len(all_buckets)}")

    # get_embedding only returns vectors for the currently configured model.
    eligible = [
        bucket
        for bucket in all_buckets
        if str(bucket.get("content", "")).strip()
        and not _is_sealed_bucket(bucket)
    ]
    missing = []
    for b in eligible:
        emb = await engine.get_embedding(b["id"])
        if emb is None:
            missing.append(b)

    print(f"Missing embeddings: {len(missing)}")

    if dry_run:
        for b in missing[:10]:
            print(f"  would embed: {b['id']} ({b['metadata'].get('name', '?')})")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")
        return

    success = 0
    failed = 0
    while True:
        result = await backfill_batch(bucket_mgr, engine, batch_size)
        success += result["success"]
        failed += result["failed"]
        print(
            f"Batch: {result['success']} success, {result['failed']} failed, "
            f"{result['remaining']} remaining"
        )
        if result["remaining"] == 0 or result["attempted"] == 0:
            print(
                f"\n=== Done: {success} newly indexed, {failed} failed, "
                f"{result['indexed_total']} indexed total, "
                f"{result['empty_skipped']} empty skipped ==="
            )
            break
        await asyncio.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(backfill(batch_size=args.batch_size, dry_run=args.dry_run))
