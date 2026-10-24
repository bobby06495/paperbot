"""Named building blocks that rules.json strategies are assembled from.

A block is a small class registered under a type name for one kind:

  entry   a check on one symbol (filters run in the listed order, cheapest first)
  stop    computes the initial protective stop for an entry
  manage  adjusts an open position (partial profit, breakeven, trailing stop)
  exit    closes a position (force close at the end of the day, ...)

    @register("entry", "rsi_below")
    class RsiBelow(Block):
        PARAMS = {"period": (int, 2), "max": (float, REQUIRED)}
        def check(self, ctx): ...

rules.json then uses {"type": "rsi_below", "period": 2, "max": 10}. build()
validates every setting at startup: unknown type, unknown setting, missing
required setting, wrong type or a number <= 0 is a ConfigError naming the
exact place (e.g. strategies.x.entry[1]). Every block also takes an optional
"label" (a short tag such as "D2" that prefixes its log reasons).
"""
from paperbot.core.risk import ConfigError

KINDS = ("entry", "stop", "manage", "exit")
REQUIRED = object()
_REGISTRY: dict[str, dict[str, type]] = {kind: {} for kind in KINDS}


def register(kind: str, name: str):
    def decorate(cls):
        if name in _REGISTRY[kind]:
            raise ValueError(f"{kind} block {name!r} registered twice")
        cls.kind, cls.type = kind, name
        _REGISTRY[kind][name] = cls
        return cls
    return decorate


def names(kind: str) -> list[str]:
    return sorted(_REGISTRY[kind])


class Block:
    """Base class: PARAMS maps setting -> (int | float | bool | str, default or REQUIRED).
    Values are kept as written in rules.json (so log reasons show them unchanged)."""
    PARAMS: dict[str, tuple[type, object]] = {}
    NEEDS = "any"  # "intraday": needs today's intraday bars (not allowed on a 1d strategy)
    kind = type = ""

    def __init__(self, label: str = "", **params):
        self.label = label
        self.prefix = f"{label} " if label else ""
        for name, value in params.items():
            setattr(self, name, value)

    def validate(self, where: str):
        """Extra checks between settings; raise ConfigError(where: ...)."""

    def describe(self) -> str:
        settings = ", ".join(f"{k}={getattr(self, k)}" for k in self.PARAMS
                             if getattr(self, k) is not None)
        return f"{self.type}({settings})" if settings else self.type


def _check_value(where: str, name: str, value, kind: type):
    if kind is bool:
        ok = isinstance(value, bool)
    elif kind is str:
        ok = isinstance(value, str) and value.strip() != ""
    elif kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
    else:  # float: any number > 0
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    if not ok:
        expected = {bool: "true or false", str: "a non-empty text", int: "a whole number > 0",
                    float: "a number > 0"}[kind]
        raise ConfigError(f"{where}: {name} must be {expected}, got {value!r}")


def build(kind: str, spec, where: str) -> Block:
    """One block instance from its rules.json object, fully validated."""
    if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
        raise ConfigError(f'{where}: must be an object with a "type"')
    name = spec["type"]
    cls = _REGISTRY[kind].get(name)
    if cls is None:
        raise ConfigError(f'{where}: unknown {kind} type "{name}"; valid: {", ".join(names(kind))}')
    given = {k: v for k, v in spec.items() if k != "type"}
    label = given.pop("label", "")
    if not isinstance(label, str):
        raise ConfigError(f"{where}: label must be text, got {label!r}")
    unknown = sorted(set(given) - set(cls.PARAMS))
    if unknown:
        valid = ", ".join(sorted(cls.PARAMS)) or "none"
        raise ConfigError(f"{where}: {name} has unknown settings {unknown}; valid: {valid}")
    params = {}
    for setting, (setting_type, default) in cls.PARAMS.items():
        if setting in given and not (given[setting] is None and default is None):
            _check_value(where, setting, given[setting], setting_type)
            params[setting] = given[setting]
        elif default is REQUIRED:
            raise ConfigError(f"{where}: {name} is missing the setting {setting!r}")
        else:
            params[setting] = default
    block = cls(label=label, **params)
    block.validate(where)
    return block
