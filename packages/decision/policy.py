"""YAML-driven mapping of detector flags to decision recommendations."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence

from .model import Decision

try:  # pragma: no cover - optional dependency
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    yaml = None


DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "decision_policy.yaml"


@dataclass(frozen=True)
class DecisionRule:
    """Single rule used to transform a flag into a decision."""

    reason: str
    outcome: str
    notes: Optional[str] = None
    price_markdown_pct: Optional[float] = None
    suggested_qty: Optional[float] = None
    perishable: Optional[bool] = None
    category_in: Optional[frozenset[str]] = None
    category_not_in: Optional[frozenset[str]] = None

    def matches(self, flag: Mapping[str, object]) -> bool:
        if self.reason and str(flag.get("reason") or "") != self.reason:
            return False
        if self.perishable is not None and self.perishable != _is_perishable(flag):
            return False
        category = _coerce_optional_str(flag.get("category"))
        if self.category_in is not None and category not in self.category_in:
            return False
        if self.category_not_in is not None and category in self.category_not_in:
            return False
        return True


@dataclass(frozen=True)
class DecisionPolicy:
    """Collection of decision rules with sensible defaults."""

    rules: Sequence[DecisionRule] = field(default_factory=list)
    default_outcome: str = "DIVERT"
    default_notes: Optional[str] = None
    default_price_markdown_pct: Optional[float] = None

    def match(self, flag: Mapping[str, object]) -> Optional[DecisionRule]:
        for rule in self.rules:
            if rule.matches(flag):
                return rule
        return None


class DecisionMapper:
    """Translate detector flags into actionable decisions."""

    def __init__(self, policy: DecisionPolicy) -> None:
        self._policy = policy

    @classmethod
    def from_path(cls, path: Path | None = None) -> "DecisionMapper":
        policy = load_policy(path)
        return cls(policy)

    def map_flag(self, flag: Mapping[str, object]) -> Decision:
        rule = self._policy.match(flag)
        outcome = (rule.outcome if rule else self._policy.default_outcome) or "DIVERT"
        notes = _resolve_notes(rule, self._policy)
        price_markdown_pct = _resolve_price_markdown_pct(rule, self._policy)
        suggested_qty = _resolve_suggested_qty(rule, flag)
        default_code = _coerce_optional_str(flag.get("default_code"))
        lot = _resolve_lot(flag)
        reason = str(flag.get("reason") or "")
        return Decision(
            default_code=default_code,
            lot=lot,
            reason=reason,
            outcome=outcome,
            suggested_qty=suggested_qty,
            notes=notes,
            price_markdown_pct=price_markdown_pct,
        )

    def map_flags(self, flags: Iterable[Mapping[str, object]]) -> List[Decision]:
        return [self.map_flag(flag) for flag in flags]


def load_policy(path: Path | None = None) -> DecisionPolicy:
    """Load a decision policy from YAML."""

    policy_path = path or DEFAULT_POLICY_PATH
    if not policy_path.exists():
        return DecisionPolicy()
    text = policy_path.read_text(encoding="utf-8")
    data = _load_yaml(text)
    if not isinstance(data, Mapping):
        raise ValueError("Decision policy must be a mapping at the top level")

    default_section = _get_mapping(data, "default")
    rules_section = data.get("rules", [])
    if not isinstance(rules_section, Sequence):
        raise ValueError("Decision policy 'rules' must be a list of mappings")

    policy = DecisionPolicy(
        rules=[_parse_rule(entry) for entry in rules_section],
        default_outcome=_coerce_outcome(default_section.get("outcome"), fallback="DIVERT"),
        default_notes=_coerce_optional_str(default_section.get("notes")),
        default_price_markdown_pct=_coerce_optional_float(default_section.get("price_markdown_pct")),
    )
    return policy


# Helpers ------------------------------------------------------------------ #


def _resolve_notes(rule: Optional[DecisionRule], policy: DecisionPolicy) -> Optional[str]:
    if rule and rule.notes is not None:
        return rule.notes
    return policy.default_notes


def _resolve_price_markdown_pct(
    rule: Optional[DecisionRule], policy: DecisionPolicy
) -> Optional[float]:
    if rule and rule.price_markdown_pct is not None:
        return rule.price_markdown_pct
    return policy.default_price_markdown_pct


def _resolve_suggested_qty(rule: Optional[DecisionRule], flag: Mapping[str, object]) -> Optional[float]:
    if rule and rule.suggested_qty is not None:
        return rule.suggested_qty
    quantity = flag.get("quantity")
    try:
        return _coerce_optional_float(quantity)
    except ValueError:
        return None


def _resolve_lot(flag: Mapping[str, object]) -> Optional[str]:
    lot_value = flag.get("lot")
    if lot_value not in (None, ""):
        return str(lot_value)
    lots_value = flag.get("lots")
    if isinstance(lots_value, Sequence):
        for candidate in lots_value:
            candidate_str = _coerce_optional_str(candidate)
            if candidate_str:
                return candidate_str
    return None


def _parse_rule(data: object) -> DecisionRule:
    if not isinstance(data, Mapping):
        raise ValueError("Decision policy rules must be mappings")
    reason = _coerce_reason(data.get("reason"))
    outcome = _coerce_outcome(data.get("outcome"), fallback="DIVERT")
    notes = _coerce_optional_str(data.get("notes"))
    price_markdown_pct = _coerce_optional_float(data.get("price_markdown_pct"))
    suggested_qty = _coerce_optional_float(data.get("suggested_qty"))
    perishable = _coerce_optional_bool(data.get("perishable"))
    category_in = _coerce_optional_str_set(data.get("category_in"))
    category_not_in = _coerce_optional_str_set(data.get("category_not_in"))
    return DecisionRule(
        reason=reason,
        outcome=outcome,
        notes=notes,
        price_markdown_pct=price_markdown_pct,
        suggested_qty=suggested_qty,
        perishable=perishable,
        category_in=category_in,
        category_not_in=category_not_in,
    )


def _coerce_reason(value: object) -> str:
    reason = _coerce_optional_str(value)
    if not reason:
        raise ValueError("Decision rule must include a reason")
    return reason


def _coerce_outcome(value: object, *, fallback: str) -> str:
    outcome = _coerce_optional_str(value)
    if not outcome:
        return fallback
    return outcome.upper()


def _coerce_optional_bool(value: object) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _coerce_optional_str(value: object) -> Optional[str]:
    if value in (None, "", "null", "None"):
        return None
    return str(value)


def _coerce_optional_float(value: object) -> Optional[float]:
    if value in (None, "", "null", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid numeric value: {value!r}") from None


def _coerce_optional_str_set(value: object) -> Optional[frozenset[str]]:
    if value in (None, "", "null", "None"):
        return None
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, Sequence):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError(f"Invalid string set value: {value!r}")
    if not items:
        return None
    return frozenset(items)


def _is_perishable(flag: Mapping[str, object]) -> bool:
    life_date = flag.get("life_date")
    if life_date not in (None, "", "null", "None"):
        return True
    metrics = flag.get("metrics")
    if isinstance(metrics, Mapping):
        metric_life_date = metrics.get("life_date")
        if metric_life_date not in (None, "", "null", "None"):
            return True
        days_until_expiry = metrics.get("days_until_expiry")
        if days_until_expiry not in (None, "", "null", "None"):
            return True
    return False


def _load_yaml(text: str) -> object:
    if yaml is not None:
        return yaml.safe_load(text) or {}
    return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> Dict[str, object]:
    root: Dict[str, object] = {}
    stack: List[tuple[int, Dict[str, object]]] = [(-1, root)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1] if stack else root

        if not value:
            new_map: Dict[str, object] = {}
            parent[key] = new_map
            stack.append((indent, new_map))
        else:
            parent[key] = _parse_scalar(value)
    return root


def _parse_scalar(value: str) -> object:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        pass
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def _get_mapping(data: Mapping[str, object], key: str) -> MutableMapping[str, object]:
    value = data.get(key)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


__all__ = ["DecisionMapper", "DecisionPolicy", "DecisionRule", "DEFAULT_POLICY_PATH", "load_policy"]
