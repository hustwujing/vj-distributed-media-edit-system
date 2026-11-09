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
    from .workflow_error_catalog import get
    item=get(error_code)
    return Resolution(item.recovery_action,item.recovery_reason)


def descendants(root,edges):
    affected={root}
    while True:
        expanded=affected|{downstream for upstream,downstream in edges if upstream in affected}
        if expanded==affected:return affected
        affected=expanded
