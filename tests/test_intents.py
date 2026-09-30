from services.banking_agent.graph import classify_intent


def test_routes_core_skills() -> None:
    assert classify_intent("Какой остаток на моём счёте?") == "accounts"
    assert classify_intent("Покажи историю покупок") == "operations"
    assert classify_intent("Что с моим инвестиционным портфелем?") == "investments"
    assert classify_intent("Спланируй путешествие") == "travel"


def test_structured_action_overrides_prompt() -> None:
    assert classify_intent("просто привет", has_action=True) == "action"
    assert classify_intent("покажи баланс", has_confirmation=True) == "action"
