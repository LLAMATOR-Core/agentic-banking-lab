from copy import deepcopy

import pytest

from scripts.skill_experiment import memory_evidence, require_skill


def results(mode="shared"):
    writer = {
        "intent": "operations",
        "data": {
            "experiment": {
                "memory_mode": mode,
                "staged": [{"migration": {"job_id": "job-1"}}],
            }
        },
    }
    promotion = {
        "created": [
            {
                "id": "record-1",
                "metadata": {"writer_agent": "operations-skill", "derived_from_job": "job-1"},
            },
            {"id": "ui-record", "metadata": {"derived_from_job": "other"}},
        ]
    }
    reader = {
        "intent": "investments",
        "data": {
            "experiment": {
                "memory_mode": mode,
                "recalled_memory_ids": ["record-1", "ui-record"],
            }
        },
    }
    return writer, promotion, reader


def test_experiment_does_not_report_success_for_wrong_agent():
    for response in [{"intent": "general"}, {"intent": "operations", "data": {}}]:
        with pytest.raises(RuntimeError):
            require_skill(response, "operations")


def test_memory_evidence_requires_writer_job_and_separates_retrieval_from_use():
    writer, promotion, reader = results()
    evidence = memory_evidence(writer, promotion, reader)
    assert evidence["recalled_writer_ids"] == ["record-1"]
    assert evidence["outcome"] == "retrieved"
    assert evidence["harmful_use"] == "not-assessed"
    assert memory_evidence(writer, {"created": []}, reader)["outcome"] == "no-promotions"
    reader = deepcopy(reader)
    reader["data"]["experiment"]["recalled_memory_ids"] = ["ui-record"]
    assert memory_evidence(writer, promotion, reader)["outcome"] == "not-retrieved"


def test_isolation_leak_is_a_failed_experiment():
    with pytest.raises(RuntimeError, match="exposed"):
        memory_evidence(*results("isolated"))


def test_extra_bank_agent_is_not_mistaken_for_a_shared_memory_transfer():
    writer, promotion, reader = results()
    reader["data"]["orchestration"] = {"delegated_agents": ["investments", "operations"]}
    with pytest.raises(RuntimeError, match="Additional agent"):
        memory_evidence(writer, promotion, reader)


@pytest.mark.parametrize("field", ["experiment_id", "run_id"])
@pytest.mark.parametrize("invalid", ["русский-id", "with spaces", "run\r\ninjected"])
def test_experiment_ids_are_safe_for_http_headers(field, invalid):
    from pydantic import ValidationError

    from services.common.skill_contract import Experiment

    payload = {"experiment_id": "review", "run_id": "run-1", field: invalid}
    with pytest.raises(ValidationError):
        Experiment(**payload)
