# voa_logging.py
import boto3
import uuid
import time
import json

# Connect to DynamoDB
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
table = dynamodb.Table("VOA_Interactions")

# Validation
def validate_log_data(session_id, subsystem, event_type, event_value):
    """
    Validate inputs before logging to DynamoDB.
    Returns (True, None) if valid, else (False, error_message)
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return False, "session_id must be a non-empty string."
    if not isinstance(subsystem, str) or not subsystem.strip():
        return False, "subsystem must be a non-empty string."
    if not isinstance(event_type, str) or not event_type.strip():
        return False, "event_type must be a non-empty string."
    if not isinstance(event_value, (str, int, bool, dict, list)):
        return False, "event_value must be a string, int, bool, dict, or list."
    return True, None

# Log Entry Creation
def create_log_entry(session_id, subsystem, event_type, event_value):
    """
    Creates a validated, structured log entry for DynamoDB.
    Converts dicts/lists to JSON for safe storage.
    """
    is_valid, error_message = validate_log_data(session_id, subsystem, event_type, event_value)
    if not is_valid:
        raise ValueError(f"Invalid log data: {error_message}")

    # Serialize complex event_value safely
    if isinstance(event_value, (dict, list)):
        event_value = json.dumps(event_value)

    return {
        "session_id": session_id,              # Partition key
        "timestamp": str(int(time.time())),    # Sort key
        "log_id": str(uuid.uuid4()),           # Unique ID for tracing
        "subsystem": subsystem,                # e.g., OrderEngine, VoiceSubsystem
        "event_type": event_type,              # e.g., ItemAdded, STT_Transcribed
        "event_value": str(event_value)
    }

#Main Log Function
def log_event(session_id, subsystem, event_type, event_value):
    """
    Validates and writes a structured log to DynamoDB.
    """
    try:
        log = create_log_entry(session_id, subsystem, event_type, event_value)
        table.put_item(Item=log)
        print(f"[LOGGED] {subsystem} - {event_type}: {event_value}")
    except Exception as e:
        print(f"[ERROR] Failed to log event ({subsystem} / {event_type}): {e}")

# Subsystem-Specific Logging Helpers 

# Order Engine 
def log_order_event(session_id, event_type, item_name=None, quantity=None, price=None, total=None):
    """
    Log events related to the order engine — adding items, totals, etc.
    """
    event_details = {
        "item_name": item_name,
        "quantity": quantity,
        "price": price,
        "total": total
    }
    event_details = {k: v for k, v in event_details.items() if v is not None}
    log_event(session_id, "OrderEngine", event_type, event_details)

# Voice / LLM / TTS Subsystem
def log_voice_event(session_id, event_type, transcript=None, llm_response=None, tts_path=None, duration=None, error=None):
    """
    Log voice pipeline events — STT → LLM → TTS chain.
    """
    event_details = {
        "transcript": transcript,
        "llm_response": llm_response,
        "tts_output_path": tts_path,
        "audio_duration": duration,
        "error": error
    }
    event_details = {k: v for k, v in event_details.items() if v is not None}
    log_event(session_id, "VoiceSubsystem", event_type, event_details)
 
# Example Tests
def run_tests():
    print("\n--- Running Enhanced Logging Tests ---\n")

    # Voice Subsystem (STT)
    log_voice_event(
        "session_001",
        "STT_Transcribed",
        transcript="I’d like a cheeseburger and fries."
    )

    # Voice Subsystem (LLM Response)
    log_voice_event(
        "session_001",
        "LLM_Response",
        transcript="I’d like a cheeseburger and fries.",
        llm_response="Sure! One cheeseburger and fries. Would you like a drink with that?"
    )

    # Voice Subsystem (TTS Output)
    log_voice_event(
        "session_001",
        "TTS_Generated",
        llm_response="Sure! One cheeseburger and fries.",
        tts_path="reply.wav",
        duration=2.8
    )

    # Order Engine Event (Add item)
    log_order_event(
        "session_002",
        "ItemAdded",
        item_name="Cheeseburger",
        quantity=1,
        price=5.99
    )

    # Order Engine Event (Total calculation)
    log_order_event(
        "session_002",
        "OrderTotalCalculated",
        total=12.47
    )

    # Error handling example
    log_voice_event(
        "session_003",
        "STT_Error",
        error="Microphone input timeout"
    )

if __name__ == "__main__":
    run_tests()
