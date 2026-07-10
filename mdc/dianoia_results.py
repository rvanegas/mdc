"""Consumer-side TypedDict schemas for dianoia agent result_content fields.

Mirrors dianoia/schemas/agent_results.py without importing from that package.
Update both files together when dianoia's agent output schemas change.
"""
from __future__ import annotations

from typing import TypedDict


# --- Formalizer ---

class FormalizationItem(TypedDict):
    symbol: str
    ascii: str
    json_structure: str


class DefinitionItem(TypedDict):
    symbol: str
    value: str
    arity: int  # predicates only; absent on constants


class DefinitionsDict(TypedDict):
    predicates: list[DefinitionItem]
    constants: list[DefinitionItem]


class FormalizerResult(TypedDict):
    formalizations: list[FormalizationItem]
    definitions: DefinitionsDict
    confidence: float
    reasoning: str


# --- Form evaluator ---

class PropositionEvaluation(TypedDict):
    symbol: str
    validity: float
    reasoning: str


class FormalEvalResult(TypedDict):
    proposition_evaluations: list[PropositionEvaluation]
    argument_validity: float
    logical_issues: list[str]
    recommendations: list[str]


# --- Content evaluator ---

class TruthEvaluation(TypedDict):
    symbol: str
    truth_value: float
    reasoning: str


class ContentValidityEvaluation(TypedDict):
    symbol: str
    validity_value: float
    reasoning: str


class IncoherentSet(TypedDict):
    symbols: list[str]
    incoherence_value: float


class PhrasingEvaluation(TypedDict):
    symbol: str
    issues: list[str]
    recommendation: str


class PhrasingEvalResult(TypedDict):
    phrasing_evaluations: list[PhrasingEvaluation]


class ContentEvalResult(TypedDict):
    truth_evaluations: list[TruthEvaluation]
    validity_evaluations: list[ContentValidityEvaluation]
    incoherent_sets: list[IncoherentSet]
    logical_issues: list[str]
    recommendations: list[str]


# --- Improver ---

class ExpectedConclusionImprovement(TypedDict):
    truth_score_improvement: str
    content_validity_improvement: str
    formal_validity_improvement: str


class PropositionChange(TypedDict):
    symbol: str | None
    proposition: str
    type: str  # "new" | "rewrite"
    original_symbol: str | None
    original_proposition: str | None
    justifies_symbol: str | None
    justification_suggestions: list[str]


class ImprovementRecommendation(TypedDict):
    id: str
    reasoning: str
    impact: str  # "high" | "medium" | "low"
    target_proposition: str
    expected_conclusion_improvement: ExpectedConclusionImprovement
    propositions: list[PropositionChange]


class ImproverResult(TypedDict):
    recommendations: list[ImprovementRecommendation]
