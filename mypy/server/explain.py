from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, TypedDict

from mypy.nodes import (
    ClassDef,
    Decorator,
    FuncDef,
    MypyFile,
    OverloadedFuncDef,
    SymbolNode,
    TypeInfo,
    Var,
)

SCHEMA_VERSION = 1


class InvalidationReason(str, Enum):
    FILE_MODIFIED = "file_modified"
    FILE_ADDED = "file_added"
    FILE_DELETED = "file_deleted"
    DEPENDENCY_MISSING = "dependency_missing"
    SYMBOL_CHANGED = "symbol_changed"
    BODY_CHANGED = "body_changed"
    MRO_CHANGED = "mro_changed"
    TRIGGER_FIRED = "trigger_fired"
    BLOCKER_ERROR = "blocker_error"
    INITIAL_CHECK = "initial_check"


class LocationDict(TypedDict):
    path: str
    line: int | None
    column: int | None


class CausalLinkDict(TypedDict):
    reason: str
    detail: str
    trigger: str | None
    upstream_target: str | None


class RecheckedTargetDict(TypedDict):
    target: str
    kind: str
    location: LocationDict
    signature_changed: bool
    emitted_triggers: list[str]
    causes: list[CausalLinkDict]


class SummaryDict(TypedDict):
    files_changed: int
    files_removed: int
    targets_rechecked: int
    triggers_fired: int
    truncated_count: int


class UpdateExplanationDict(TypedDict):
    version: int
    update_id: int
    is_initial: bool
    duration_ms: float
    changed_files: list[str]
    removed_files: list[str]
    query_target: str | None
    rechecked: bool
    summary: SummaryDict
    targets: dict[str, RecheckedTargetDict]


@dataclass(frozen=True)
class SourceLocation:
    path: str
    line: int | None = None
    column: int | None = None

    def format(self) -> str:
        if self.line is None:
            return self.path
        return f"{self.path}:{self.line}"


@dataclass
class CausalLink:
    
    reason: InvalidationReason
    detail: str
    trigger: str | None = None
    upstream_target: str | None = None


@dataclass
class RecheckedTarget:
    """A unit of code (module, class, function, method, or attribute) reanalyzed during an update."""

    target: str
    kind: str  # "module" | "class" | "function" | "method" | "attribute" | "symbol"
    location: SourceLocation
    causes: list[CausalLink] = field(default_factory=list)
    signature_changed: bool = False
    emitted_triggers: list[str] = field(default_factory=list)

    def add_cause(self, link: CausalLink) -> None:
        for existing in self.causes:
            if (
                existing.reason == link.reason
                and existing.detail == link.detail
                and existing.trigger == link.trigger
                and existing.upstream_target == link.upstream_target
            ):
                return
        self.causes.append(link)

    def add_emitted_trigger(self, trigger: str) -> None:
        if trigger not in self.emitted_triggers:
            self.emitted_triggers.append(trigger)


@dataclass
class UpdateExplanation:
    update_id: int
    changed_files: list[str]
    removed_files: list[str]
    targets: dict[str, RecheckedTarget] = field(default_factory=dict)
    is_initial: bool = False
    duration_ms: float = 0.0
    truncated_count: int = 0
    query_target: str | None = None
    rechecked: bool = True

    def to_dict(self) -> dict[str, Any]:
        triggers_fired = sum(len(t.emitted_triggers) for t in self.targets.values())
        return {
            "version": SCHEMA_VERSION,
            "update_id": self.update_id,
            "is_initial": self.is_initial,
            "duration_ms": round(self.duration_ms, 2),
            "changed_files": list(self.changed_files),
            "removed_files": list(self.removed_files),
            "query_target": self.query_target,
            "rechecked": self.rechecked,
            "summary": {
                "files_changed": len(self.changed_files),
                "files_removed": len(self.removed_files),
                "targets_rechecked": len(self.targets),
                "triggers_fired": triggers_fired,
                "truncated_count": self.truncated_count,
            },
            "targets": {
                name: {
                    "target": t.target,
                    "kind": t.kind,
                    "location": {
                        "path": t.location.path,
                        "line": t.location.line,
                        "column": t.location.column,
                    },
                    "signature_changed": t.signature_changed,
                    "emitted_triggers": list(t.emitted_triggers),
                    "causes": [
                        {
                            "reason": c.reason.value,
                            "detail": c.detail,
                            "trigger": c.trigger,
                            "upstream_target": c.upstream_target,
                        }
                        for c in t.causes
                    ],
                }
                for name, t in self.targets.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UpdateExplanation:
        targets: dict[str, RecheckedTarget] = {}
        for name, t_data in data.get("targets", {}).items():
            loc_data = t_data["location"]
            causes = [
                CausalLink(
                    reason=InvalidationReason(c["reason"]),
                    detail=c["detail"],
                    trigger=c.get("trigger"),
                    upstream_target=c.get("upstream_target"),
                )
                for c in t_data.get("causes", [])
            ]
            targets[name] = RecheckedTarget(
                target=t_data["target"],
                kind=t_data["kind"],
                location=SourceLocation(
                    path=loc_data["path"],
                    line=loc_data.get("line"),
                    column=loc_data.get("column"),
                ),
                causes=causes,
                signature_changed=t_data.get("signature_changed", False),
                emitted_triggers=list(t_data.get("emitted_triggers", [])),
            )
        summary = data.get("summary", {})
        return cls(
            update_id=data["update_id"],
            changed_files=list(data.get("changed_files", [])),
            removed_files=list(data.get("removed_files", [])),
            targets=targets,
            is_initial=data.get("is_initial", False),
            duration_ms=data.get("duration_ms", 0.0),
            truncated_count=summary.get("truncated_count", 0),
            query_target=data.get("query_target"),
            rechecked=data.get("rechecked", True),
        )


def _classify_symbol_node(node: SymbolNode | None, is_inside_class: bool) -> str:
    if isinstance(node, MypyFile):
        return "module"
    if isinstance(node, TypeInfo):
        return "class"
    if isinstance(node, (FuncDef, OverloadedFuncDef, Decorator)):
        return "method" if is_inside_class else "function"
    if isinstance(node, Var):
        return "attribute" if is_inside_class else "symbol"
    return "symbol"


def resolve_target_info(
    target: str, modules: dict[str, MypyFile]
) -> tuple[str, SourceLocation] | None:
    parts = target.split(".")
    mod_name: str | None = None
    remainder: list[str] = []
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in modules:
            mod_name = candidate
            remainder = parts[i:]
            break

    if mod_name is None:
        return None

    mypy_file = modules[mod_name]
    file_path = mypy_file.path or "<unknown>"

    if not remainder:
        return ("module", SourceLocation(path=file_path, line=1, column=0))

    sym_table = mypy_file.names
    node: SymbolNode | None = None
    inside_class = False

    for idx, part in enumerate(remainder):
        sym = sym_table.get(part)
        if sym is None or sym.node is None:
            line = node.line if node is not None and node.line >= 0 else None
            col = node.column if node is not None and node.column >= 0 else None
            return ("symbol", SourceLocation(path=file_path, line=line, column=col))

        node = sym.node
        if isinstance(node, TypeInfo):
            if idx < len(remainder) - 1:
                inside_class = True
                sym_table = node.names
            else:
                line = node.line if node.line >= 0 else None
                col = node.column if node.column >= 0 else None
                return ("class", SourceLocation(path=file_path, line=line, column=col))
        else:
            break

    kind = _classify_symbol_node(node, inside_class)
    line = node.line if node is not None and node.line >= 0 else None
    col = node.column if node is not None and node.column >= 0 else None
    return (kind, SourceLocation(path=file_path, line=line, column=col))


def collect_func_bodies(file_node: MypyFile) -> dict[str, str]:
    bodies: dict[str, str] = {}

    def _visit_func(prefix: str, node: FuncDef | Decorator | OverloadedFuncDef) -> None:
        if isinstance(node, Decorator):
            _visit_func(prefix, node.func)
        elif isinstance(node, OverloadedFuncDef):
            for idx, item in enumerate(node.items):
                if isinstance(item, (FuncDef, Decorator)):
                    _visit_func(f"{prefix}#{idx}", item)
        elif isinstance(node, FuncDef):
            bodies[prefix] = str(node.body)

    for stmt in file_node.defs:
        if isinstance(stmt, ClassDef):
            cls_prefix = f"{file_node.fullname}.{stmt.name}"
            for member in stmt.defs.body:
                if isinstance(member, (FuncDef, Decorator, OverloadedFuncDef)):
                    _visit_func(f"{cls_prefix}.{member.name}", member)
        elif isinstance(stmt, (FuncDef, Decorator, OverloadedFuncDef)):
            _visit_func(f"{file_node.fullname}.{stmt.name}", stmt)

    return bodies


def find_body_only_changes(
    old_bodies: dict[str, str],
    new_file: MypyFile,
    signature_changed_targets: set[str],
) -> list[str]:
    new_bodies = collect_func_bodies(new_file)
    modified: list[str] = []
    for qualname, new_fp in new_bodies.items():
        base_qualname = qualname.split("#", 1)[0]
        if base_qualname in signature_changed_targets:
            continue
        old_fp = old_bodies.get(qualname)
        if old_fp is not None and old_fp != new_fp and base_qualname not in modified:
            modified.append(base_qualname)
    return modified


class ExplanationRecorder:
    def __init__(self, update_id: int, is_initial: bool = False) -> None:
        self.update_id = update_id
        self.is_initial = is_initial
        self._start_time = time.perf_counter()
        self.changed_files: list[str] = []
        self.removed_files: list[str] = []
        self.targets: dict[str, RecheckedTarget] = {}
        self.trigger_origins: dict[str, str] = {}
        self._pre_locations: dict[str, tuple[str, SourceLocation]] = {}

    def cache_locations_before_discard(
        self, target_names: list[str], modules: dict[str, MypyFile]
    ) -> None:
        for name in target_names:
            if name not in self._pre_locations:
                info = resolve_target_info(name, modules)
                if info is not None:
                    self._pre_locations[name] = info

    def _get_or_create_target(
        self,
        target: str,
        kind: str = "symbol",
        location: SourceLocation | None = None,
    ) -> RecheckedTarget:
        existing = self.targets.get(target)
        if existing is not None:
            if location is not None and existing.location.line is None:
                existing.location = location
            if kind != "symbol" and existing.kind == "symbol":
                existing.kind = kind
            return existing

        if location is None and target in self._pre_locations:
            kind, location = self._pre_locations[target]

        created = RecheckedTarget(
            target=target,
            kind=kind,
            location=location or SourceLocation(path="<unknown>"),
        )
        self.targets[target] = created
        return created

    def record_file_change(
        self,
        module_name: str,
        file_path: str,
        reason: InvalidationReason,
        detail: str | None = None,
    ) -> None:
        if reason in (InvalidationReason.FILE_MODIFIED, InvalidationReason.FILE_ADDED):
            if file_path not in self.changed_files:
                self.changed_files.append(file_path)
        elif reason == InvalidationReason.FILE_DELETED:
            if file_path not in self.removed_files:
                self.removed_files.append(file_path)

        default_details = {
            InvalidationReason.FILE_MODIFIED: "Source file modified on disk",
            InvalidationReason.FILE_ADDED: "New module added to build",
            InvalidationReason.FILE_DELETED: "Source file deleted",
            InvalidationReason.BLOCKER_ERROR: "Blocking error in module",
            InvalidationReason.DEPENDENCY_MISSING: "Imported dependency became missing",
            InvalidationReason.INITIAL_CHECK: "Initial daemon check",
        }
        target_obj = self._get_or_create_target(
            module_name,
            kind="module",
            location=SourceLocation(path=file_path, line=1, column=0),
        )
        target_obj.add_cause(
            CausalLink(
                reason=reason,
                detail=detail or default_details.get(reason, reason.value),
            )
        )

    def record_symbol_diff(
        self,
        target: str,
        trigger: str,
        detail: str,
        upstream_module: str | None = None,
        is_mro: bool = False,
    ) -> None:
        target_obj = self._get_or_create_target(target)
        target_obj.signature_changed = True
        target_obj.add_emitted_trigger(trigger)
        target_obj.add_cause(
            CausalLink(
                reason=InvalidationReason.MRO_CHANGED if is_mro else InvalidationReason.SYMBOL_CHANGED,
                detail=detail,
                trigger=trigger,
                upstream_target=upstream_module,
            )
        )
        self.trigger_origins[trigger] = target

    def record_body_change(
        self,
        target: str,
        upstream_module: str | None = None,
        detail: str = "function body modified (signature unchanged)",
    ) -> None:
        target_obj = self._get_or_create_target(target)
        target_obj.add_cause(
            CausalLink(
                reason=InvalidationReason.BODY_CHANGED,
                detail=detail,
                upstream_target=upstream_module,
            )
        )

    def register_trigger_origin(self, trigger: str, origin_target: str) -> None:
        self.trigger_origins[trigger] = origin_target
        target_obj = self._get_or_create_target(origin_target)
        target_obj.signature_changed = True
        target_obj.add_emitted_trigger(trigger)

    def record_trigger_propagation(self, trigger: str, dependent_target: str) -> None:
        origin = self.trigger_origins.get(trigger)
        if origin == dependent_target:
            return
        target_obj = self._get_or_create_target(dependent_target)
        if any(
            c.reason
            in (
                InvalidationReason.SYMBOL_CHANGED,
                InvalidationReason.MRO_CHANGED,
                InvalidationReason.BODY_CHANGED,
                InvalidationReason.FILE_MODIFIED,
                InvalidationReason.FILE_ADDED,
                InvalidationReason.FILE_DELETED,
            )
            for c in target_obj.causes
        ):
            return
        target_obj.add_cause(
            CausalLink(
                reason=InvalidationReason.TRIGGER_FIRED,
                detail=f"depends on {trigger}",
                trigger=trigger,
                upstream_target=origin,
            )
        )

    def merge_from(self, previous: UpdateExplanation) -> None:
        """Merge an earlier wave's explanation within the same user command."""
        self.update_id = previous.update_id
        self.changed_files = list(
            dict.fromkeys(previous.changed_files + self.changed_files)
        )
        self.removed_files = list(
            dict.fromkeys(previous.removed_files + self.removed_files)
        )
        merged_targets: dict[str, RecheckedTarget] = {}
        for name, prev_t in previous.targets.items():
            copy_t = RecheckedTarget(
                target=prev_t.target,
                kind=prev_t.kind,
                location=prev_t.location,
                causes=list(prev_t.causes),
                signature_changed=prev_t.signature_changed,
                emitted_triggers=list(prev_t.emitted_triggers),
            )
            merged_targets[name] = copy_t
        for name, cur_t in self.targets.items():
            if name not in merged_targets:
                merged_targets[name] = cur_t
            else:
                dst = merged_targets[name]
                if cur_t.location.line is not None:
                    dst.location = cur_t.location
                if cur_t.kind != "symbol":
                    dst.kind = cur_t.kind
                dst.signature_changed = dst.signature_changed or cur_t.signature_changed
                for trig in cur_t.emitted_triggers:
                    dst.add_emitted_trigger(trig)
                for cause in cur_t.causes:
                    dst.add_cause(cause)
        self.targets = merged_targets

    def finalize(self, modules: dict[str, MypyFile]) -> UpdateExplanation:
        for name, target_obj in self.targets.items():
            resolved = resolve_target_info(name, modules)
            if resolved is not None:
                kind, loc = resolved
                target_obj.kind = kind
                if loc.line is not None or target_obj.location.line is None:
                    target_obj.location = loc
            elif name in self._pre_locations:
                kind, loc = self._pre_locations[name]
                target_obj.kind = kind
                target_obj.location = loc

        duration_ms = (time.perf_counter() - self._start_time) * 1000.0
        return UpdateExplanation(
            update_id=self.update_id,
            changed_files=self.changed_files,
            removed_files=self.removed_files,
            targets=self.targets,
            is_initial=self.is_initial,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Reverse Causal Query (RCQ)
# ---------------------------------------------------------------------------


def filter_explanation_for_target(
    explanation: UpdateExplanation,
    query: str,
) -> UpdateExplanation:
    prefix = f"{query}."
    seeds = [
        name
        for name in explanation.targets
        if name == query or name.startswith(prefix)
    ]

    if not seeds:
        return UpdateExplanation(
            update_id=explanation.update_id,
            changed_files=[],
            removed_files=[],
            targets={},
            is_initial=explanation.is_initial,
            duration_ms=explanation.duration_ms,
            query_target=query,
            rechecked=False,
        )

    kept: dict[str, RecheckedTarget] = {}
    queue: deque[str] = deque(seeds)

    while queue:
        current_name = queue.popleft()
        if current_name in kept:
            continue
        node = explanation.targets.get(current_name)
        if node is None:
            continue

        kept[current_name] = node
        for link in node.causes:
            if link.upstream_target and link.upstream_target not in kept:
                queue.append(link.upstream_target)

    relevant_paths = {t.location.path for t in kept.values()}
    relevant_changed = [f for f in explanation.changed_files if f in relevant_paths]
    relevant_removed = [f for f in explanation.removed_files if f in relevant_paths]

    return UpdateExplanation(
        update_id=explanation.update_id,
        changed_files=relevant_changed,
        removed_files=relevant_removed,
        targets=kept,
        is_initial=explanation.is_initial,
        duration_ms=explanation.duration_ms,
        query_target=query,
        rechecked=True,
    )


# ---------------------------------------------------------------------------
# Terminal Tree & Reverse Renderers
# ---------------------------------------------------------------------------


def render_tree(explanation: UpdateExplanation, max_depth: int | None = None) -> str:
    """Render an UpdateExplanation as a top-down dependency tree."""
    lines: list[str] = []

    if explanation.query_target is not None:
        if not explanation.rechecked or not explanation.targets:
            lines.append(
                f"Update #{explanation.update_id} — Target '{explanation.query_target}' "
                f"was not rechecked (cached analysis reused)."
            )
            return "\n".join(lines)
        lines.append(
            f"Update #{explanation.update_id} — Filtered for: {explanation.query_target} "
            f"({len(explanation.targets)} targets in chain, {explanation.duration_ms:.0f} ms)"
        )
    else:
        n_files = len(explanation.changed_files) + len(explanation.removed_files)
        lines.append(
            f"Update #{explanation.update_id}: {n_files} file(s) changed, "
            f"{len(explanation.targets)} target(s) rechecked ({explanation.duration_ms:.0f} ms)"
        )

    if explanation.is_initial:
        lines.append("  [initial_check] Cold daemon start; full dependency graph built.")
        return "\n".join(lines)

    if not explanation.targets:
        lines.append("  No targets rechecked.")
        return "\n".join(lines)

    children_map: dict[str, list[tuple[CausalLink, str]]] = {
        name: [] for name in explanation.targets
    }
    roots: list[str] = []

    for name, target_obj in explanation.targets.items():
        has_in_graph_parent = False
        has_root_cause = False
        for link in target_obj.causes:
            if link.upstream_target and link.upstream_target in explanation.targets:
                children_map[link.upstream_target].append((link, name))
                has_in_graph_parent = True
            else:
                has_root_cause = True
        if not has_in_graph_parent or has_root_cause:
            roots.append(name)

    rendered_expanded: set[str] = set()
    truncated_descendants: set[str] = set()

    def _mark_subtree_truncated(node_name: str, seen: set[str]) -> None:
        if node_name in seen:
            return
        seen.add(node_name)
        truncated_descendants.add(node_name)
        for _, child in children_map.get(node_name, []):
            _mark_subtree_truncated(child, seen)

    def _render_node(
        target_name: str,
        incoming_link: CausalLink | None,
        prefix: str,
        is_last: bool,
        ancestors: set[str],
        depth: int,
    ) -> None:
        target_obj = explanation.targets[target_name]
        connector = "└─ " if is_last else "├─ "
        child_prefix = prefix + ("   " if is_last else "│  ")

        if incoming_link is None:
            root_reason = (
                target_obj.causes[0].reason.value if target_obj.causes else "root"
            )
            lines.append("")
            lines.append(
                f"Root change: {target_obj.location.path} ({root_reason}) [{target_name}]"
            )
            child_prefix = ""
        else:
            if (
                incoming_link.trigger
                and incoming_link.reason == InvalidationReason.TRIGGER_FIRED
            ):
                lines.append(f"{prefix}{connector}Trigger {incoming_link.trigger}")
                lines.append(
                    f"{child_prefix}└─ [{incoming_link.reason.value}] "
                    f"{target_name} ({target_obj.location.format()})"
                )
                child_prefix = child_prefix + "   "
            else:
                lines.append(
                    f"{prefix}{connector}[{incoming_link.reason.value}] "
                    f"{target_name} ({target_obj.location.format()})"
                )
                lines.append(f"{child_prefix}Reason: {incoming_link.detail}")

            for extra in target_obj.causes:
                if extra is not incoming_link and extra.upstream_target is None:
                    lines.append(
                        f"{child_prefix}└─ [{extra.reason.value}] {extra.detail}"
                    )

        if target_name in ancestors:
            lines.append(f"{child_prefix}└─ (cycle -> {target_name})")
            return

        if target_name in rendered_expanded and incoming_link is not None:
            lines.append(f"{child_prefix}└─ (already shown above)")
            return

        rendered_expanded.add(target_name)
        children = children_map.get(target_name, [])

        if max_depth is not None and depth >= max_depth:
            if children:
                lines.append(
                    f"{child_prefix}└─ (... {len(children)} downstream target(s) hidden by --depth {max_depth})"
                )
                for _, child_name in children:
                    _mark_subtree_truncated(child_name, set())
            return

        if not children and incoming_link is not None:
            if not target_obj.signature_changed:
                lines.append(
                    f"{child_prefix}└─ (signature unchanged; propagation stopped)"
                )
            return

        next_ancestors = ancestors | {target_name}
        for idx, (edge_link, child_name) in enumerate(children):
            _render_node(
                child_name,
                edge_link,
                child_prefix,
                idx == len(children) - 1,
                next_ancestors,
                depth + 1,
            )

    for root_name in roots:
        _render_node(root_name, None, "", True, set(), 0)

    for name in explanation.targets:
        if name not in rendered_expanded and name not in truncated_descendants:
            _render_node(name, None, "", True, set(), 0)

    return "\n".join(lines)


def render_reverse(explanation: UpdateExplanation, max_depth: int | None = None) -> str:
    query = explanation.query_target or "<unknown>"
    lines: list[str] = []

    if not explanation.rechecked or not explanation.targets:
        lines.append(
            f"Update #{explanation.update_id} — Reverse causal query for: {query}"
        )
        lines.append("")
        lines.append(
            f"Target '{query}' was not rechecked in Update #{explanation.update_id} "
            f"(cached analysis reused)."
        )
        return "\n".join(lines)

    prefix = f"{query}."
    seeds = [
        name
        for name in explanation.targets
        if name == query or name.startswith(prefix)
    ]

    lines.append(
        f"Update #{explanation.update_id} — Reverse causal query for: {query}"
    )

    def _walk_upstream(
        current: str, indent: str, ancestors: set[str], hop: int
    ) -> None:
        if current not in explanation.targets:
            return
        if current in ancestors:
            lines.append(f"{indent}▲ (cycle detected at {current})")
            return
        if max_depth is not None and hop >= max_depth:
            lines.append(f"{indent}▲ (... truncated at --depth {max_depth})")
            return

        node = explanation.targets[current]
        upstream_links = [
            c
            for c in node.causes
            if c.upstream_target and c.upstream_target in explanation.targets
        ]
        local_links = [c for c in node.causes if c.upstream_target is None]

        for local in local_links:
            lines.append(f"{indent}│ [{local.reason.value}] {local.detail}")

        next_ancestors = ancestors | {current}
        for idx, link in enumerate(upstream_links):
            via = f" via {link.trigger}" if link.trigger else ""
            lines.append(f"{indent}▲ caused by [{link.reason.value}]{via}")
            parent_name = link.upstream_target
            assert parent_name is not None
            parent_obj = explanation.targets[parent_name]
            lines.append(
                f"{indent}[upstream] {parent_name} ({parent_obj.location.format()})"
            )
            branch_indent = indent + ("  " if len(upstream_links) > 1 else "")
            _walk_upstream(parent_name, branch_indent, next_ancestors, hop + 1)

    for seed in seeds:
        seed_obj = explanation.targets[seed]
        lines.append("")
        lines.append(f"Target: {seed} ({seed_obj.location.format()})")
        _walk_upstream(seed, "  ", set(), 0)

    return "\n".join(lines)