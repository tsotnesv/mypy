from time_resources import Deadline

def compute_timeout(d: Deadline) -> int:
    return d.seconds() + 5

def format_banner(name: str) -> str:
    return f"[{name}]"
