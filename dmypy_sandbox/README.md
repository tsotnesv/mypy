# Incremental Invalidation Explanations (`python -m mypy.dmypy explain`)

## Why This Exists

Mypy's daemon (`dmypy`) keeps a fine-grained dependency graph in memory so it can re-analyze individual functions, methods, classes, and variables rather than whole files. When you edit a file, `dmypy` compares symbol table snapshots, fires triggers only when an externally visible signature or MRO changes, and rechecks only the downstream targets affected by those triggers.

Until now, those decisions happened silently. **`dmypy explain`** makes the daemon's reasoning visible so you can answer:

* **What triggered a recheck?** See the exact chain from file edit (`file_modified`, `file_added`, `file_deleted`) $\rightarrow$ symbol diff (`symbol_changed`, `mro_changed`, `body_changed`) $\rightarrow$ fired trigger $\rightarrow$ affected caller (`trigger_fired`).
* **Where did propagation stop?** See early cutoff in action when a body-only edit—or a downstream function whose own signature didn't change—halts further invalidation.
* **What was skipped via cache?** Any file or function missing from the tree was **not rechecked**. Querying it directly confirms cached analysis was reused.

---

## Running Your Fork (`python -m mypy.dmypy`)

If a system-wide `mypy` is installed on your `PATH`, running bare `dmypy` will invoke the upstream binary rather than your local checkout. Create a virtual environment and install the fork in editable mode so `dmypy` and `python -m mypy.dmypy` resolve to this repository from any subdirectory:

```bash
unset PYTHONPATH
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Prefix commands with `python -m mypy.dmypy` from your repository environment (or define a temporary alias in your terminal before starting a demo):



```bash
# Optional shorthand for your current shell session:
alias dmypy="python -m mypy.dmypy"
```

All commands in the guide below use `python -m mypy.dmypy` explicitly so they work out of the box.

---

## Command Reference

### 1. Post-(re)check Queries (`python -m mypy.dmypy explain`)

Inspects the cached explanation from the most recent `check` or `recheck` without running another type-checking pass.

| Option | Default | What It Does |
| :--- | :--- | :--- |
| `--target <TARGET>` | `None` | Focuses on one fully qualified symbol (e.g. `service.compute_timeout`) and its upstream causes, or confirms its cached analysis was reused. |
| `--reverse` | `False` | Walks **bottom-up** from `--target` back to the root file edit (requires `--target`). Ideal for debugging *"why did this function break?"* |
| `--format {text,json}` | `text` | Outputs either a human-readable ASCII tree (`text`) or a structured JSON payload (`json`, schema `version: 1`). |
| `--depth <N>` | `None` | Limits tree/trace depth in `text` mode and summarizes collapsed hops. |

### 2. Inline Flags on `check` and `recheck`

| Command | What It Does |
| :--- | :--- |
| `python -m mypy.dmypy check --explain <files...>` | Type-checks files and appends the top-down invalidation tree (`initial_check` on a cold daemon). |
| `python -m mypy.dmypy check --explain-target <TARGET> <files...>` | Type-checks files and appends the filtered causal chain for `<TARGET>`. |
| `python -m mypy.dmypy recheck --explain` | Runs an incremental recheck and appends the full top-down invalidation tree. |
| `python -m mypy.dmypy recheck --explain-target <TARGET>` | Runs an incremental recheck and appends the filtered causal chain for `<TARGET>`. |

---

## Interactive Walkthrough Demos

### Demo 1: Surgical Invalidation & Cache Reuse Proof

**Scenario:** Change a method's return type in `time_resources.py` and verify that only its direct caller (`service.compute_timeout`) is rechecked—while sibling functions and unrelated files stay cached.

#### Prepared Files (`demo_surgical/`)

**`demo_surgical/time_resources.py`**
```python
class Deadline:
    def seconds(self) -> int:
        return 30

# --- Step 2 Edit: replace Deadline above with this ---
# class Deadline:
#     def seconds(self) -> str:
#         return "30s"
```

**`demo_surgical/service.py`**
```python
from time_resources import Deadline

def compute_timeout(d: Deadline) -> int:
    return d.seconds() + 5

def format_banner(name: str) -> str:
    return f"[{name}]"
```

**`demo_surgical/metrics.py`**
```python
def record_heartbeat() -> bool:
    return True
```

#### Step-by-Step Commands

```bash
cd demo_surgical

# 1. Start the daemon and build the initial dependency graph
python -m mypy.dmypy start -- --follow-imports=error
python -m mypy.dmypy check time_resources.py service.py metrics.py

# 2. Uncomment the `-> str` version of Deadline.seconds in time_resources.py, then recheck:
python -m mypy.dmypy recheck --explain

# 3. Prove that sibling functions and unrelated modules reused cached analysis:
python -m mypy.dmypy explain --target service.format_banner
python -m mypy.dmypy explain --target metrics

python -m mypy.dmypy stop
```

#### What to Point Out
* `service.compute_timeout` is rechecked and flags the `+ 5` type error.
* `service.format_banner` and `metrics` do not appear in the invalidation tree—querying them prints `was not rechecked (cached analysis reused)`.

---

### Demo 2: Body-Only Edit (Early Cutoff)

**Scenario:** Refactor the internal implementation of `tax_rate()` without changing its `() -> float` signature. Propagation stops at the function boundary.

#### Prepared Files (`demo_cutoff/`)

**`demo_cutoff/pricing.py`**
```python
def tax_rate() -> float:
    return 0.19

# --- Step 2 Edit: replace tax_rate above with this ---
# def tax_rate() -> float:
#     base = 0.20
#     return base + 0.01
```

**`demo_cutoff/checkout.py`**
```python
from pricing import tax_rate

def total(amount: float) -> float:
    return amount * (1.0 + tax_rate())
```

#### Step-by-Step Commands

```bash
cd demo_cutoff

# 1. Cold start and initial check
python -m mypy.dmypy start -- --follow-imports=error
python -m mypy.dmypy check pricing.py checkout.py

# 2. Apply the body-only edit in pricing.py, then recheck:
python -m mypy.dmypy recheck --explain

# 3. Confirm checkout.total was untouched:
python -m mypy.dmypy explain --target checkout.total

python -m mypy.dmypy stop
```

#### What to Point Out
* The tree reports `[body_changed] pricing.tax_rate` followed immediately by `(signature unchanged; propagation stopped)`.
* No triggers fire and `checkout.total` is never re-analyzed.

---

### Demo 3: Multi-Hop Inferred Types & Reverse Causal Query (`--reverse`)

**Scenario:** Change an unannotated constant (`DEFAULT_RETRIES = 3` $\rightarrow$ `"three"`), watch mypy propagate the inferred type across two modules (`config` $\rightarrow$ `pipeline` $\rightarrow$ `api`), and trace backwards from the failing endpoint.

#### Prepared Files (`demo_multihop/`)

**`demo_multihop/config.py`**
```python
DEFAULT_RETRIES = 3

# --- Step 2 Edit: replace DEFAULT_RETRIES above with this ---
# DEFAULT_RETRIES = "three"
```

**`demo_multihop/pipeline.py`**
```python
from config import DEFAULT_RETRIES

ACTIVE_RETRIES = DEFAULT_RETRIES
```

**`demo_multihop/api.py`**
```python
from pipeline import ACTIVE_RETRIES

def handle_request() -> int:
    return ACTIVE_RETRIES + 1
```

#### Step-by-Step Commands

```bash
cd demo_multihop

# 1. Cold start and initial check
python -m mypy.dmypy start -- --follow-imports=error
python -m mypy.dmypy check config.py pipeline.py api.py

# 2. Change DEFAULT_RETRIES to "three" in config.py, then recheck with target filter:
python -m mypy.dmypy recheck --explain-target api.handle_request

# 3. Trace bottom-up from the broken endpoint to the root edit:
python -m mypy.dmypy explain --target api.handle_request --reverse

python -m mypy.dmypy stop
```

#### What to Point Out
* Shows 2-hop deferred reprocessing: `config.DEFAULT_RETRIES` $\rightarrow$ `pipeline.ACTIVE_RETRIES` $\rightarrow$ `api.handle_request`.
* `--reverse` starts at `api.handle_request` and walks upward directly to `config.py:1`.

---

### Demo 4: Diamond Dependencies (`--depth` & `--reverse`)

**Scenario:** Two intermediate modules (`auth.py` and `audit.py`) both depend on `core.TokenId` and converge inside `handler.process`. Use `--depth` to summarize fan-out and `--reverse` to inspect both converging paths.

#### Prepared Files (`demo_diamond/`)

**`demo_diamond/core.py`**
```python
TokenId = int

# --- Step 2 Edit: replace TokenId above with this ---
# TokenId = bytes
```

**`demo_diamond/auth.py`**
```python
from core import TokenId

def verify(t: TokenId) -> TokenId:
    return t
```

**`demo_diamond/audit.py`**
```python
from core import TokenId

def log_token(t: TokenId) -> str:
    return str(t)
```

**`demo_diamond/handler.py`**
```python
from auth import verify
from audit import log_token

def process(raw: int) -> str:
    tok = verify(raw)
    return log_token(tok)
```

#### Step-by-Step Commands

```bash
cd demo_diamond

# 1. Cold start and initial check
python -m mypy.dmypy start -- --follow-imports=error
python -m mypy.dmypy check core.py auth.py audit.py handler.py

# 2. Change TokenId to `bytes` in core.py, then inspect with a depth cap:
python -m mypy.dmypy recheck --explain --depth 1

# 3. View both converging upstream branches leading into handler.process:
python -m mypy.dmypy explain --target handler.process --reverse
```

*(Keep the daemon running for Demo 5, or run `python -m mypy.dmypy stop` if finishing here.)*

#### What to Point Out
* `--depth 1` collapses deeper hops cleanly (`... downstream target(s) hidden by --depth 1`) without turning hidden descendants into false top-level roots.
* `--reverse` on `handler.process` expands both converging paths (`auth.verify` and `audit.log_token`) back to `core.TokenId`.

---

### Demo 5: Machine-Readable Telemetry (`--format json`)

**Scenario:** Export the structured `UpdateExplanation` JSON (`version: 1`) from Demo 4 for IDE diagnostics, CI telemetry, or selective test runners.

#### Step-by-Step Commands

```bash
# Run right after Demo 4 while the daemon is still running:
python -m mypy.dmypy explain --format json

# Stop the daemon when finished:
python -m mypy.dmypy stop
```

#### What to Point Out
* Outputs structured JSON with `update_id`, `duration_ms`, `changed_files`, `summary` (`targets_rechecked`, `triggers_fired`), and per-target `location`, `signature_changed`, `emitted_triggers`, and `causes`.
