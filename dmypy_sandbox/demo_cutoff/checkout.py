from pricing import tax_rate

def total(amount: float) -> float:
    return amount * (1.0 + tax_rate())
