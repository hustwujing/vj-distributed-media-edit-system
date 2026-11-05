"""Deterministic recovery policy; model diagnosis is evidence, never authority."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Action=Literal['retry_same_revision','manager_revision','external_reconcile','user_decision','stop']


@dataclass(frozen=True)
class Resolution:
    action:Action
    reason:str


def resolve(error_code,phase='local',classification='') -> Resolution:
    if error_code in ('submission_unknown','result_unavailable'):
        return Resolution('external_reconcile','External side effect state must be reconciled before any replacement call')
    if error_code=='model_token_budget_exhausted':
        return Resolution('user_decision','The frozen workflow model-token budget is exhausted')
    if error_code=='review_contract_exhausted':
        return Resolution('user_decision','The Reviewer exhausted its bounded contract-correction attempts')
    if error_code=='planning_contract_exhausted':
        return Resolution('user_decision','The Manager exhausted its bounded contract-correction attempts')
    if error_code=='budget_exhausted':
        return Resolution('user_decision','The frozen workflow operation budget is exhausted')
    if error_code in ('poll_failed','collect_failed'):
        return Resolution('retry_same_revision','The existing external handle can be queried or collected again')
    if error_code in ('technical_validation_failed','quality_failed','model_result','pre_submit','execution_failed',
                      'provider_failed','agent_failed','review_incomplete','invalid_result_manifest','missing_proposal'):
        return Resolution('manager_revision','A new plan or immutable task revision is required within the frozen authorization')
    return Resolution('stop','No automatic recovery policy is registered for this error')


def descendants(root,edges):
    affected={root}
    while True:
        expanded=affected|{downstream for upstream,downstream in edges if upstream in affected}
        if expanded==affected:return affected
        affected=expanded
