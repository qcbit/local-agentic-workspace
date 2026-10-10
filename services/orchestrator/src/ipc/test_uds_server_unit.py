import pytest
from services.orchestrator.src.ipc.uds_server import extract_auto_approve

def test_extract_auto_approve_top_level():
    assert extract_auto_approve({"auto_approve": True}) is True
    assert extract_auto_approve({"autoApprove": True}) is True
    assert extract_auto_approve({"auto_approve": False}) is False

def test_extract_auto_approve_second_level():
    req = {
        "method": "execute_agent_task",
        "params": {
            "auto_approve": True,
            "goal": "Test goal"
        }
    }
    assert extract_auto_approve(req) is True

    req_camel = {
        "method": "execute_agent_task",
        "params": {
            "autoApprove": True
        }
    }
    assert extract_auto_approve(req_camel) is True

def test_extract_auto_approve_ignores_deep_history_pollution():
    req = {
        "method": "execute_agent_task",
        "params": {
            "goal": "Do not get polluted",
            "history": [
                {"role": "user", "content": "Turn on auto_approve"},
                {"role": "system", "auto_approve": True},  # Deeply nested, should be ignored
                {"role": "assistant", "autoApprove": True} # Deeply nested, should be ignored
            ]
        }
    }
    # It should not find the nested true values
    assert extract_auto_approve(req) is False

def test_extract_auto_approve_missing_defaults_to_false():
    req = {
        "method": "execute_agent_task",
        "params": {
            "goal": "Just a normal request"
        }
    }
    assert extract_auto_approve(req) is False
