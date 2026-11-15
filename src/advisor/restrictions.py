"""Restrictions: which champions an adventure (and its patron) lets into the formation.

The Formation Search's hard filter. `allows` never guesses in the player's favour: a rule it can't
evaluate (an unknown identifier in an expression, a stat the character sheet doesn't give) counts
as not met, so a recommendation never breaks a restriction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone

from advisor.catalogue import Champion, Restriction

_STATS = ("str", "dex", "con", "int", "wis", "cha")
_STANDARD_RACES = frozenset(
    {"human", "elf", "half-elf", "dwarf", "halfling", "gnome", "half-orc", "tiefling", "dragonborn"}
)


def allows(restrictions: Iterable[Restriction], champion: Champion, now: int | None = None) -> bool:
    """Whether every rule lets the champion in. `now` (Unix seconds) is the snapshot's clock, for
    date-based rules."""
    return all(_allowed_by(r, champion, now) for r in restrictions)


def failed(restrictions: Iterable[Restriction], champion: Champion, now: int | None = None) -> tuple[Restriction, ...]:
    return tuple(r for r in restrictions if not _allowed_by(r, champion, now))


def describe(restriction: Restriction, names: dict[int, str] | None = None) -> str:
    """A short plain-language form, e.g. "only Lawful or Good champions"."""
    parts = []
    tags = restriction.tags
    if restriction.expr and (only_tags := _TAGS_ONLY.fullmatch(restriction.expr)):
        tags = (*tags, *re.findall(r"`([^`]*)`", only_tags.group(0)))
    if tags:
        parts.append(" or ".join(_tag_text(t) for t in tags) + " champions")
    if restriction.stats:
        joiner = " or " if restriction.stats_any else " and "
        checks = joiner.join(f"{stat.upper()} {_COMPARISON_TEXT.get(comp, comp)} {value}" for stat, comp, value in restriction.stats)
        parts.append(f"champions with {checks}")
    if restriction.expr and not _TAGS_ONLY.fullmatch(restriction.expr):
        parts.append(f"champions matching {_plain_expr(restriction.expr)}")
    if restriction.ids:
        parts.append(", ".join(sorted((names or {}).get(i, f"champion {i}") for i in restriction.ids)))
    text = " or ".join(parts) or "named champions"
    return f"only {text}" if restriction.kind == "only_allow" else f"no {text}"


_COMPARISON_TEXT = {">=": "of at least", "<=": "of at most", ">": "above", "<": "below", "==": "of"}


# An expression that is only `HasTag(...) || HasTag(...)`.
_TAGS_ONLY = re.compile(r"\s*HasTag\(`[^`]*`\)(\s*\|\|\s*HasTag\(`[^`]*`\))*\s*")


def _tag_text(tag: str) -> str:
    return tag.upper() if tag == "dps" else tag.replace("_", " ").title()


def _plain_expr(expr: str) -> str:
    expr = re.sub(r"HasTag\(`([^`]*)`\)", lambda m: _tag_text(m.group(1)), expr)
    return expr.replace("||", " or ").replace("&&", " and ").replace("  ", " ").strip()


def _allowed_by(restriction: Restriction, champion: Champion, now: int | None) -> bool:
    matches = _matches(restriction, champion, now)
    return matches if restriction.kind == "only_allow" else not matches


def _matches(restriction: Restriction, champion: Champion, now: int | None) -> bool:
    if champion.id in restriction.ids:
        return True
    if any(tag in champion.tags for tag in restriction.tags):
        return True
    if restriction.stats:
        checks = [_compare(champion.ability_scores.get(stat), comp, value) for stat, comp, value in restriction.stats]
        if (any if restriction.stats_any else all)(checks):
            return True
    if restriction.expr:
        try:
            return bool(_Expression(restriction.expr, champion, now).evaluate())
        except _Unknown:
            # Unknown in a disallow rule: treat as matching, so the champion stays out either way.
            return restriction.kind == "disallow"
    return False


def _compare(left, comp: str, right) -> bool:
    if left is None or right is None:
        return False
    return {
        ">=": left >= right,
        "<=": left <= right,
        ">": left > right,
        "<": left < right,
        "==": left == right,
        "!=": left != right,
    }.get(comp, False)


class _Unknown(Exception):
    """The expression uses something the Catalogue doesn't know."""


_TOKEN = re.compile(r"\s*(`[^`]*`|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z_0-9]*|\|\||&&|>=|<=|==|!=|[()!<>+\-*,])")


class _Expression:
    """Evaluates the game's restriction expressions (`HasTag(\\`evil\\`) || (DEX>=13&&CHA>=13)`)."""

    def __init__(self, text: str, champion: Champion, now: int | None) -> None:
        self.tokens = []
        pos = 0
        while pos < len(text):
            match = _TOKEN.match(text, pos)
            if not match:
                if text[pos:].strip() == "":
                    break
                raise _Unknown(text[pos:])
            self.tokens.append(match.group(1))
            pos = match.end()
        self.pos = 0
        self.champion = champion
        self.now = now

    def evaluate(self):
        value = self._or()
        if self.pos != len(self.tokens):
            raise _Unknown("trailing tokens")
        return value

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, expected=None):
        token = self._peek()
        if token is None or (expected is not None and token != expected):
            raise _Unknown(f"expected {expected}")
        self.pos += 1
        return token

    def _or(self):
        value = self._and()
        while self._peek() == "||":
            self._take()
            right = self._settled(self._and, bool(value), stops=("||", ")"))
            value = bool(value) or bool(right)
        return value

    def _and(self):
        value = self._comparison()
        while self._peek() == "&&":
            self._take()
            right = self._settled(self._comparison, not value, stops=("||", "&&", ")"))
            value = bool(value) and bool(right)
        return value

    def _settled(self, parse, settled: bool, stops: tuple[str, ...]):
        """Parse the right-hand side; when the left already decides the result, an unknown there
        doesn't matter (but the tokens must still be consumed)."""
        if not settled:
            return parse()
        start = self.pos
        try:
            return parse()
        except _Unknown:
            self.pos = start
            self._skip_operand(stops)
            return False

    def _skip_operand(self, stops: tuple[str, ...]):
        depth = 0
        while (token := self._peek()) is not None:
            if depth == 0 and token in stops:
                return
            depth += token == "("
            depth -= token == ")"
            self.pos += 1

    def _comparison(self):
        left = self._sum()
        if self._peek() in (">=", "<=", "==", "!=", ">", "<"):
            comp = self._take()
            return _compare(left, comp, self._sum())
        return left

    def _sum(self):
        value = self._unary()
        while self._peek() in ("+", "-", "*"):
            op = self._take()
            right = self._unary()
            value = value + right if op == "+" else value - right if op == "-" else value * right
        return value

    def _unary(self):
        token = self._peek()
        if token == "!":
            self._take()
            return not self._unary()
        if token == "-":
            self._take()
            return -self._unary()
        if token == "(":
            self._take()
            value = self._or()
            self._take(")")
            return value
        return self._atom()

    def _atom(self):
        token = self._take()
        if token[0].isdigit():
            return float(token)
        if token.startswith("`"):
            return token[1:-1]
        if self._peek() == "(":
            self._take()
            args = []
            while self._peek() != ")":
                args.append(self._or())
                if self._peek() == ",":
                    self._take()
            self._take(")")
            return self._call(token, args)
        return self._identifier(token)

    def _call(self, name: str, args: list):
        if name == "HasTag" and len(args) == 1:
            return args[0] in self.champion.tags
        if name == "GetStat" and len(args) == 1:
            return self._stat(str(args[0]).lower())
        if name == "abs" and len(args) == 1:
            return abs(args[0])
        raise _Unknown(name)

    def _stat(self, stat: str):
        value = self.champion.ability_scores.get(stat)
        if value is None:
            raise _Unknown(stat)
        return value

    def _identifier(self, name: str):
        lower = name.lower()
        if lower in _STATS:
            return self._stat(lower)
        if lower == "hero_id":
            return self.champion.id
        if lower == "max_age":
            if self.champion.age is None:
                raise _Unknown(name)
            return self.champion.age
        if lower == "has_non_standard_race":
            return not (self.champion.tags & _STANDARD_RACES)
        if lower == "years_since_release_or_rework":
            return self._years_since_release_or_rework()
        raise _Unknown(name)

    def _years_since_release_or_rework(self):
        dates = [d for d in (self.champion.date_available, self.champion.last_rework_date) if d]
        if not dates or self.now is None:
            raise _Unknown("dates")
        latest = max(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() for d in dates)
        return (self.now - latest) / (365.25 * 86400)
