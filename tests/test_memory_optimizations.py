from datetime import datetime, timedelta

import frontmatter
import pytest


@pytest.mark.asyncio
async def test_exact_keyword_outranks_content_match(bucket_mgr):
    keyword_id = await bucket_mgr.create(
        content="普通人物资料",
        tags=["婷"],
        importance=5,
        domain=["人物"],
    )
    await bucket_mgr.create(
        content="这段正文提到了婷，但不是关键词桶。",
        tags=["其他"],
        importance=5,
        domain=["杂项"],
    )

    results = await bucket_mgr.search("婷", limit=10)

    assert results
    assert results[0]["id"] == keyword_id


@pytest.mark.asyncio
async def test_dormant_is_excluded_unless_requested(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="霁的生日资料",
        tags=["霁", "生日"],
        importance=2,
        domain=["人物"],
    )
    await bucket_mgr.set_dormant(bucket_id, True)

    hidden = await bucket_mgr.search("霁", limit=10)
    visible = await bucket_mgr.search("霁", limit=10, include_dormant=True)

    assert all(bucket["id"] != bucket_id for bucket in hidden)
    assert any(bucket["id"] == bucket_id for bucket in visible)


@pytest.mark.asyncio
async def test_touch_records_access_without_clearing_dormant_by_default(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="待恢复记忆",
        tags=["恢复"],
        importance=2,
        domain=["测试"],
    )
    await bucket_mgr.set_dormant(bucket_id, True)
    path = bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    old_time = (datetime.now() - timedelta(days=31)).isoformat()
    post["last_active"] = old_time
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    before = await bucket_mgr.get(bucket_id)
    await bucket_mgr.touch(bucket_id)
    bucket = await bucket_mgr.get(bucket_id)

    assert bucket["metadata"]["dormant"] is True
    assert bucket["metadata"]["activation_count"] == (
        before["metadata"]["activation_count"] + 1
    )
    assert bucket["metadata"]["last_active"] != old_time


@pytest.mark.asyncio
async def test_touch_clears_dormant_only_when_explicitly_requested(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="显式唤醒测试",
        tags=["唤醒"],
        importance=2,
        domain=["测试"],
    )
    await bucket_mgr.set_dormant(bucket_id, True)

    await bucket_mgr.touch(bucket_id, wake_dormant=True)
    bucket = await bucket_mgr.get(bucket_id)

    assert bucket["metadata"]["dormant"] is False


@pytest.mark.asyncio
async def test_set_dormant_does_not_refresh_last_active(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="旧记忆",
        tags=["旧"],
        importance=2,
        domain=["测试"],
    )
    path = bucket_mgr._find_bucket_file(bucket_id)
    post = frontmatter.load(path)
    old_time = (datetime.now() - timedelta(days=31)).isoformat()
    post["last_active"] = old_time
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))

    await bucket_mgr.set_dormant(bucket_id, True)
    bucket = await bucket_mgr.get(bucket_id)

    assert bucket["metadata"]["last_active"] == old_time
    assert bucket["metadata"]["dormant"] is True
