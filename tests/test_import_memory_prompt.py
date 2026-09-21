from import_memory import IMPORT_EXTRACT_PROMPT


def test_preserve_raw_prompt_describes_extracted_content_bypass_not_transcript():
    prompt = IMPORT_EXTRACT_PROMPT

    assert "提取结果中的 content" in prompt
    assert "跳过后续合并/脱水摘要" in prompt
    assert "上传文件原文" in prompt
    assert "不可变来源证据" in prompt
    assert "保留原文不摘要" not in prompt


def test_extraction_prompt_keeps_distinct_facts_and_events_separate():
    prompt = IMPORT_EXTRACT_PROMPT

    assert "明确描述同一底层事实或事件" in prompt
    assert "仅同一话题不够" in prompt
    for distinction in ("不同日期", "事件", "人物/实体", "状态", "计划与结果", "修正/否定", "改变的观点"):
        assert distinction in prompt
    assert "同一话题的零散信息整合为一条记忆" not in prompt
