from auth import verify
from audit import log_token

def process(raw: int) -> str:
    tok = verify(raw)
    return log_token(tok)