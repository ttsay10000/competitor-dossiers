from app.rules.talent_rules import detect_capability, is_senior_role
from app.rules.press_rules import classify_press


def test_detect_capability():
    assert detect_capability("VP of Data", None) == "ai_data"
    assert detect_capability("Strategic Finance Manager", None) == "strategy_finance"


def test_is_senior_role():
    assert is_senior_role("Chief Strategy Officer") is True
    assert is_senior_role("VP Partnerships") is True
    assert is_senior_role("Senior Analyst") is False


def test_classify_press():
    assert classify_press({"title": "Announces partnership with X"}) == "partner"
    assert classify_press({"title": "Company completes funding round"}) == "capital"
    assert classify_press({"title": "Repositioning strategy for growth"}) == "narrative"
    assert classify_press({"title": "Summer travel tips"}) is None
