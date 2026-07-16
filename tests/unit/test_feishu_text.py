"""Tests for Feishu text utilities — <at> tag cleaning."""

from ocl.gateway.feishu.text import clean_at_tags, extract_mentioned_user_ids


def test_clean_at_tag_with_display_name():
    text = '<at user_id="ou_abc">Agent</at> 帮我查 PR'
    assert clean_at_tags(text) == "@Agent 帮我查 PR"


def test_clean_at_tag_with_user_map_overrides_name():
    text = '<at user_id="ou_abc">Some Name</at> hi'
    assert clean_at_tags(text, {"ou_abc": "Alice"}) == "@Alice hi"


def test_clean_at_tag_no_match_returns_unchanged():
    text = "plain text without any mentions"
    assert clean_at_tags(text) == "plain text without any mentions"


def test_clean_at_tag_multiple_mentions():
    text = '<at user_id="ou_a">A</at> and <at user_id="ou_b">B</at>'
    assert clean_at_tags(text) == "@A and @B"


def test_extract_mentioned_user_ids_returns_all():
    text = '<at user_id="ou_abc">A</at> <at user_id="ou_def">B</at>'
    assert extract_mentioned_user_ids(text) == ["ou_abc", "ou_def"]


def test_extract_mentioned_user_ids_empty_when_no_match():
    assert extract_mentioned_user_ids("plain text") == []
