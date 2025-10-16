import boto3
import uuid
import time

# Connect to DynamoDB
dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
table = dynamodb.Table("VOA_Interactions") 


def validate_log_data(session_id, subsystem, event_type, event_value):
    """
    Validate inputs before logging to DynamoDB.
    Returns a tuple (bool, error_message). If valid, bool=True and error_message=None.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return False, "session_id must be a non-empty string."

    if not isinstance(subsystem, str) or not subsystem.strip():
        return False, "subsystem must be a non-empty string."

    if not isinstance(event_type, str) or not event_type.strip():
        return False, "event_type must be a non-empty string."

    # Event value can be string, int, or bool — enforce only those
    if not isinstance(event_value, (str, int, bool)):
        return False, "event_value must be a string, int, or bool."

    return True, None


def create_log_entry(session_id, subsystem, event_type, event_value):
    """
    Creates a validated log entry for DynamoDB.
    Converts timestamp to string since your Sort Key is a String.
    """
    # Validate input first
    is_valid, error_message = validate_log_data(session_id, subsystem, event_type, event_value)
    if not is_valid:
        raise ValueError(f"Invalid log data: {error_message}")

    return {
        "session_id": session_id,              # Partition key
        "timestamp": str(int(time.time())),    # Sort key, stored as string
        "log_id": str(uuid.uuid4()),           # Unique identifier
        "subsystem": subsystem,
        "event_type": event_type,
        "event_value": str(event_value)        # Store as string for safety
    }


def log_event(session_id, subsystem, event_type, event_value):
    """
    Main logging function to validate, creates log entry, and writes to DynamoDB
    """
    try:
        log = create_log_entry(session_id, subsystem, event_type, event_value)
        table.put_item(Item=log)
        print("Log stored successfully:", log)
    except Exception as e:
        print("Failed to log event:", str(e))


# Test
def run_tests():
    print("\n--- Running Logging Function Tests ---\n")

    #Normal Case
    log_event("session_001", "InputLayer", "VoiceCaptured", "hello world")

    #Empty session_id (should raise error)
    try:
        log_event("", "InputLayer", "VoiceCaptured", "test input")
    except ValueError as e:
        print(f"Expected error: {e}")

    #Invalid session_id type (int)
    try:
        log_event(123, "AIEngine", "STT_Result", "recognized text")
    except ValueError as e:
        print(f"Expected error: {e}")

    #Very large event_value
    log_event("session_002", "System", "DebugLog", "A"*5000)

    #Boolean value
    log_event("session_003", "Avatar", "AnimationTriggered", True)

    #Integer value
    log_event("session_004", "System", "ResponseTimeMS", 250)

    #Stress Test: multiple logs
    for i in range(10):
        log_event(f"session_loop_{i}", "TestSubsystem", "Iteration", f"event_{i}")

if __name__ == "__main__":
    run_tests()


