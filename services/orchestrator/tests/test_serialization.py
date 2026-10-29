import pytest
from pydantic import BaseModel
import json

# Adjust the import path according to your project structure
from services.orchestrator.src.memory.persistence import (
    serialize_message,
    deserialize_message,
    MessageSerialisationError
)

class MockModel(BaseModel):
    field: str
    value: int

class MockCustomObject:
    def __init__(self):
        self.custom_attr = "custom_value"

def test_roundtrip_text_message():
    msg = {"role": "user", "content": "Hello, world!"}
    serialized = serialize_message(msg)
    deserialized = deserialize_message(serialized)
    assert deserialized == msg

def test_roundtrip_tool_use_block():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I will use a tool."},
            {"type": "tool_use", "id": "call_123", "name": "get_weather", "input": {"location": "London"}}
        ]
    }
    serialized = serialize_message(msg)
    deserialized = deserialize_message(serialized)
    assert deserialized == msg

def test_roundtrip_tool_result_block():
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_123", "content": "Sunny"}
        ]
    }
    serialized = serialize_message(msg)
    deserialized = deserialize_message(serialized)
    assert deserialized == msg

def test_roundtrip_image_block_base64():
    msg = {
        "role": "user",
        "content": [
            {
                "type": "image", 
                "source": {"type": "base64", "media_type": "image/jpeg", "data": "aGVsbG8="}
            }
        ]
    }
    serialized = serialize_message(msg)
    deserialized = deserialize_message(serialized)
    assert deserialized == msg

def test_roundtrip_pydantic_model_in_input():
    model_instance = MockModel(field="test", value=42)
    msg = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "call_456", "name": "do_something", "input": model_instance}
        ]
    }
    serialized = serialize_message(msg)
    deserialized = deserialize_message(serialized)
    
    # Pydantic models are serialized to dictionaries via the custom encoder
    expected_msg = {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "call_456", "name": "do_something", "input": {"field": "test", "value": 42}}
        ]
    }
    assert deserialized == expected_msg

def test_roundtrip_deeply_nested_lists():
    msg = {
        "role": "user",
        "content": [{"nested": [[{"deep": "value"}], 1, 2, 3]}]
    }
    serialized = serialize_message(msg)
    deserialized = deserialize_message(serialized)
    assert deserialized == msg

def test_corrupted_json_raises_domain_error():
    corrupted_json = '{"role": "user", "content": "Unclosed string}'
    with pytest.raises(MessageSerialisationError, match="deserialize failed"):
        deserialize_message(corrupted_json)

def test_fallback_encoder_types():
    # Test bytes fallback
    msg_with_bytes = {"role": "user", "content": b"byte data"}
    assert deserialize_message(serialize_message(msg_with_bytes)) == {"role": "user", "content": "byte data"}
    
    # Test __dict__ fallback
    msg_with_custom = {"role": "user", "content": MockCustomObject()}
    assert deserialize_message(serialize_message(msg_with_custom)) == {"role": "user", "content": {"custom_attr": "custom_value"}}
