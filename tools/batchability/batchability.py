"""Whether a client-relevance statement can safely share one tuple query.

A tuple ``( e1, e2, ... )`` answers every item in one evaluation, which is
what makes batching several statements into one BigFix Fast Query worth it:
one ClientQuery round trip instead of one per statement. But the tuple only
answers cleanly when **every item yields exactly one value and none errors**:

- an item that errors errors the whole tuple;
- an item with no values empties the whole tuple;
- an item with several values repeats the tuple once per combination;
- an item whose value displays with ``,``, ``(`` or ``)`` is wrapped in
  parentheses inside a tuple answer (``( a, b )``), so it cannot be split
  back out unambiguously.

:func:`batchability` predicts that statically. It is a pure function of the
text, written against this analyzer's own parse tree and type checker, and it
is deliberately conservative: ``"safe"`` is the only verdict that means
"batch it", and the rules below were measured for the precision of that
verdict, not for recall. Anything else still evaluates correctly on its own;
a caller just sends it separately. A caller that batches must still verify
each tuple answer (one answer, exactly N parts) and fall back when it does not
split, because a static verdict cannot see the endpoint.

The rules, by the verdict they produce (the worst one wins):

``error_risk``
    The statement does not parse or does not type-check (it will error), it
    uses an inspector the target platform does not have, or -- outside an
    error guard -- the checker reports a runtime risk
    (``singular-spelling-mid-chain``, ``singular-over-plural-object``), or it
    looks up a singular object by name (``file "x"``, ``setting "x" of
    client``), reads a singular property of anything but a world object
    (``version of file "x"``), or aggregates a plural into one value
    (``maximum of ...``), any of which errors when the object is missing.
``unknown``
    Plurality or an inspector name could not be determined.
``plural_risk``
    The statement is plural, or a ``whose`` filter outside an error guard
    can leave a singular statement with no value at all.
``split_risk``
    The value's display carries a comma: times, dates, tuples. Batching it
    costs a fallback, never a wrong answer.

Error guards, as measured rather than assumed (see :func:`_unguarded`):
``exists`` / ``number of`` absorb only their operand's outermost step being
missing, the left side of ``|`` is answered for by its right side, and a
``whose`` predicate that errors filters its item out.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from bigfix_relevance_analyzer import inspectors
from bigfix_relevance_analyzer.analyzer import RelevanceAnalysis, analyze
from bigfix_relevance_analyzer.diagnostics import DIAGNOSTICS, Origin
from bigfix_relevance_analyzer.dialect import Dialect
from bigfix_relevance_analyzer.nodes import (
    Bar,
    Binary,
    Cast,
    Exists,
    If,
    It,
    ItemOf,
    Node,
    NumberOf,
    Of,
    Reference,
    Span,
    StringLiteral,
    Whose,
    children,
)
from bigfix_relevance_analyzer.typecheck import AGGREGATES, Plurality

__all__ = ["BatchReason", "Batchability", "BatchabilityKind", "batchability"]

BatchabilityKind = Literal["safe", "error_risk", "unknown", "plural_risk", "split_risk"]

# Worst first: the verdict is the first kind any reason carries.
_SEVERITY: tuple[BatchabilityKind, ...] = (
    "error_risk",
    "unknown",
    "plural_risk",
    "split_risk",
)

# Types whose display always contains ", " -- `Sat, 31 Oct 2026 ...` -- and so
# is wrapped in parentheses inside a tuple answer.
_COMMA_TYPES = frozenset({"time", "date"})

# A tuple item whose display holds any of these is shown wrapped, as
# `( a, b )`, by qna 11.0.7 and REST Fast Query alike.
_WRAP_MARKERS = (",", "(", ")")

# Rule -> the kind it implies.
_RULE_KIND: dict[str, BatchabilityKind] = {
    "parse-error": "error_risk",
    "type-error": "error_risk",
    "runtime-risk": "error_risk",
    "not-on-platform": "error_risk",
    "singular-lookup": "error_risk",
    "singular-property": "error_risk",
    "aggregate-of-plural": "error_risk",
    "unknown-reference": "unknown",
    "unknown-plurality": "unknown",
    "plural-result": "plural_risk",
    "whose-may-be-empty": "plural_risk",
    "comma-in-display": "split_risk",
}


@dataclass(frozen=True, slots=True)
class BatchReason:
    """One rule that fired, and where."""

    rule: str
    """Kebab-case rule name; see the module docstring."""

    span: Span | None
    """The node it fired on; ``None`` for the statement as a whole."""

    detail: str = ""

    @property
    def kind(self) -> BatchabilityKind:
        return _RULE_KIND[self.rule]

    def to_dict(self) -> dict[str, Any]:
        span = None if self.span is None else {"start": self.span.start, "end": self.span.end}
        return {"rule": self.rule, "kind": self.kind, "span": span, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Batchability:
    """The verdict on one statement."""

    kind: BatchabilityKind
    reasons: tuple[BatchReason, ...]
    plurality: Plurality
    """The checker's plurality for the statement as a whole."""

    @property
    def safe(self) -> bool:
        """Whether to put this statement in a tuple batch."""
        return self.kind == "safe"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "plurality": self.plurality.value,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


def batchability(
    text: str, *, dialect: Dialect = Dialect.CLIENT, platform: str | None = None
) -> Batchability:
    """Predict whether ``text`` yields exactly one splittable value, without error.

    ``platform`` (a client platform such as ``"windows"``) also flags an
    inspector that platform lacks; leave it ``None`` when the endpoints are
    mixed or unknown.
    """
    return _classify(analyze(text, dialect, platform), platform)


def _classify(analysis: RelevanceAnalysis, platform: str | None) -> Batchability:
    reasons: list[BatchReason] = []
    if analysis.node is None or analysis.check is None:
        message = analysis.parse_error.message if analysis.parse_error else "no parse"
        reasons.append(BatchReason("parse-error", None, message))
        return _verdict(reasons, Plurality.UNKNOWN)

    check = analysis.check
    value = check.value
    unguarded = list(_unguarded(analysis.node))
    unguarded_spans = {node.span for node in unguarded}
    for diagnostic in check.diagnostics:
        detail = f"{diagnostic.code}: {diagnostic.message}"
        if _is_fault(diagnostic.code):
            reasons.append(BatchReason("type-error", diagnostic.span, detail))
        elif diagnostic.span in unguarded_spans:
            # A risk the runtime raises (`singular-over-plural-object`,
            # `singular-spelling-mid-chain`), unless a guard absorbs it:
            # `exists wmi` is the outermost step of an `exists`.
            reasons.append(BatchReason("runtime-risk", diagnostic.span, detail))
    for phrase in analysis.unknown_references:
        reasons.append(BatchReason("unknown-reference", None, phrase))
    if platform is not None:
        # Wherever it is written -- under `exists`, after `false and` -- an
        # inspector the platform lacks errors ("The operator ... is not
        # defined."); only an `if` branch not taken escapes it, which is how
        # content guards one (`if windows of operating system then ...`).
        reachable = {id(node) for node in _outside_branches(analysis.node)}
        for report in analysis.references:
            # Known, but nothing visible from this platform: not defined there.
            if report.known and not report.visible and id(report.reference) in reachable:
                reasons.append(BatchReason("not-on-platform", report.reference.span, report.phrase))

    if value.plurality is Plurality.PLURAL:
        reasons.append(BatchReason("plural-result", analysis.node.span))
    elif value.plurality is Plurality.UNKNOWN:
        reasons.append(BatchReason("unknown-plurality", analysis.node.span))
    if value.types and value.types & _COMMA_TYPES:
        reasons.append(
            BatchReason("comma-in-display", analysis.node.span, ", ".join(sorted(value.types)))
        )
    if value.tuple_types:
        reasons.append(BatchReason("comma-in-display", analysis.node.span, "tuple"))
    for literal in _answer_literals(analysis.node):
        if any(marker in _unescaped(literal.content) for marker in _WRAP_MARKERS):
            reasons.append(BatchReason("comma-in-display", literal.span, literal.text))

    resolutions = check.resolutions
    for node in unguarded:
        if isinstance(node, Reference):
            rows = resolutions.get(id(node), ())
            if not _written_singular(node, rows):
                continue
            if node.index is not None and any(row.index_type is not None for row in rows):
                reasons.append(BatchReason("singular-lookup", node.span, node.phrase))
            elif any(row.name in _EMPTY_ERRORS for row in rows):
                reasons.append(BatchReason("aggregate-of-plural", node.span, node.phrase))
        elif isinstance(node, Of) and isinstance(node.prop, Reference) and not _world(node.obj):
            rows = resolutions.get(id(node.prop), ())
            if _written_singular(node.prop, rows) and not any(
                row.name in AGGREGATES for row in rows
            ):
                reasons.append(BatchReason("singular-property", node.prop.span, node.prop.phrase))
    for node in _answer_spine(analysis.node):
        if isinstance(node, Whose):
            reasons.append(BatchReason("whose-may-be-empty", node.span))
    return _verdict(reasons, value.plurality)


def _is_fault(code: str) -> bool:
    """A diagnostic the statement cannot run with, as `CheckResult.ok` draws
    the line: type-check origin, except the advisory
    `world-property-not-defined`."""
    return DIAGNOSTICS[code].origin is Origin.TYPE_CHECK and code != "world-property-not-defined"


# The aggregates (named by their plural row name, as AGGREGATES is) that
# error on an empty operand where `concatenation` answers "" and `sum` 0.
# `number of` is its own node (NumberOf) and never errors.
_EMPTY_ERRORS = frozenset({"maxima", "minima", "unique values"})


def _verdict(reasons: list[BatchReason], plurality: Plurality) -> Batchability:
    kinds = {reason.kind for reason in reasons}
    kind: BatchabilityKind = next((k for k in _SEVERITY if k in kinds), "safe")
    return Batchability(kind=kind, reasons=tuple(reasons), plurality=plurality)


def _unguarded(root: Node) -> Iterator[Node]:
    """Every node a runtime error can escape from.

    Measured with qna 11.0.7 (#78), not assumed:

    - `exists x` / `number of x` absorb only ``x``'s *outermost* step being
      missing or empty: ``exists file "/nope"`` is False, but ``exists
      (size of file "/nope")`` and ``exists files of folder "/nope"`` error,
      so everything beneath that step is still walked.
    - `a | b` answers for `a` with `b`; `b` is walked.
    - A `whose` predicate that errors just filters its item out.

    Iterative, as the analyzer's own walks are, so a deep `or` chain is
    never a crash.
    """
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        match node:
            case Exists(operand=operand) | NumberOf(operand=operand):
                stack.extend(reversed(_beneath_outermost_step(operand)))
            case Bar(right=right):
                stack.append(right)
            case Whose(collection=collection):
                stack.append(collection)
            case _:
                stack.extend(reversed(children(node)))


def _beneath_outermost_step(node: Node) -> tuple[Node, ...]:
    """What an `exists` / `number of` operand evaluates before its last step.

    The filter of a `whose` there is skipped like any other predicate.
    """
    match node:
        case Reference():
            return ()
        case Of(prop=Reference() | Whose(collection=Reference()), obj=obj):
            return (obj,)
        case Whose(collection=collection):
            return _beneath_outermost_step(collection)
        case _:
            return (node,)


def _answer_literals(root: Node) -> Iterator[StringLiteral]:
    """String literals whose text ends up in the answer itself: the statement,
    either side of `&` or `|`, either `if` branch, through casts."""
    stack = [root]
    while stack:
        node = stack.pop()
        match node:
            case StringLiteral():
                yield node
            case Binary(op="&", left=left, right=right) | Bar(left=left, right=right):
                stack.extend((left, right))
            case If(then_branch=then_branch, else_branch=else_branch):
                stack.extend((then_branch, else_branch))
            case Cast(operand=operand):
                stack.append(operand)
            case _:
                pass


def _unescaped(content: str) -> str:
    """A literal's text with its ``%xx`` escapes decoded."""
    return re.sub(r"%([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), content)


def _outside_branches(root: Node) -> Iterator[Node]:
    """Every node but those inside an `if`'s then/else branches."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, If):
            stack.append(node.condition)
        else:
            stack.extend(reversed(children(node)))


def _answer_spine(root: Node) -> Iterator[Node]:
    """The nodes whose values *are* the answer, rather than feed something
    that consumes them (`exists`, `number of`, `sum of`, a comparison)."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        match node:
            case Of(prop=prop):
                stack.append(prop)
            case Whose(collection=collection):
                stack.append(collection)
            case Cast(operand=operand):
                stack.append(operand)
            case Bar(left=left, right=right):
                stack.extend((left, right))
            case If(then_branch=then_branch, else_branch=else_branch):
                stack.extend((then_branch, else_branch))
            case _:
                pass


def _written_singular(node: Reference, rows: tuple[inspectors.Inspector, ...]) -> bool:
    """Whether ``node`` is spelled singular. Unresolved names count as singular:
    an unknown name is already reported, and singular is the riskier guess."""
    if not rows:
        return True
    forms = {inspectors.written_form_of(row, node.phrase) for row in rows}
    return inspectors.WrittenForm.PLURAL not in forms


def _world(node: Node) -> bool:
    """Whether ``node`` is a world object every endpoint has: a chain of
    unindexed references with no object (``operating system``, ``client``,
    ``name of operating system``), or a literal."""
    match node:
        case Reference(index=None):
            return True
        case Of(prop=Reference(index=None), obj=obj):
            return _world(obj)
        case ItemOf(operand=operand):
            return _world(operand)
        case It() | Reference():
            return False
        case _:
            return not children(node)
