from memu.next_intent import classify_text_to_intent, build_proactive_drafts


def test_classify_text_to_intent_keywords():
    assert classify_text_to_intent("what is the system status?") == "status_check"
    assert classify_text_to_intent("please fix this outage") == "remediation"
    assert classify_text_to_intent("deploy this to production") == "deploy"
    assert classify_text_to_intent("make a roadmap") == "planning"


def test_build_proactive_drafts_shape():
    predictions = [
        {"predicted_intent": "status_check", "confidence": 0.8},
        {"predicted_intent": "remediation", "confidence": 0.6},
    ]
    drafts = build_proactive_drafts(predictions, signal="railway seems down")
    assert len(drafts) == 2
    assert drafts[0]["intent"] == "status_check"
    assert "proactive_draft" in drafts[0]
    assert drafts[1]["intent"] == "remediation"
