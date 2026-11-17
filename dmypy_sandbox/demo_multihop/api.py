from pipeline import ACTIVE_RETRIES

def handle_request() -> int:
    return ACTIVE_RETRIES + 1
