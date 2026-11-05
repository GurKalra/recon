"""The screen can say why the AI was or was not used."""

from app.mappings import ai_status


def test_off_when_the_person_switched_it_off():
    assert ai_status(False, "rules", ["rules: verified"]) == "off"


def test_no_key():
    assert ai_status(True, "rules", ["rules: verified", "no API key: rules only"]) == "no_key"


def test_not_needed_when_the_rules_were_sure():
    assert ai_status(True, "rules", ["rules: verified"]) == "not_needed"


def test_used_when_the_model_answer_won():
    assert ai_status(True, "llm", ["rules: x", "model round 1: verified"]) == "used"
    assert ai_status(True, "llm_repaired", ["rules: x", "model round 2: verified"]) == "used"


def test_asked_but_rules_kept():
    attempts = ["rules: x", "model round 1: bad", "rules answer kept (the model was not better)"]
    assert ai_status(True, "rules", attempts) == "asked_kept"


def test_service_down():
    attempts = ["rules: x", "model unavailable (timeout); rules answer kept"]
    assert ai_status(True, "rules", attempts) == "unavailable"