"""Registered routing and independent operation side-effect policy.

Public metadata contains identities, never endpoints or credentials. A call
freezes these fields together with its private protocol handle before submission.
"""
from dataclasses import dataclass
from typing import Callable
import os
import re


@dataclass(frozen=True)
class RouteAdapter:
    endpoint: Callable[[], tuple[str, str]]
    cleanup_category: str


ROUTES: dict[str, RouteAdapter] = {}


def register_route(name, adapter):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name) or name in ROUTES:
        raise ValueError('Invalid or duplicate route adapter')
    if not callable(adapter.endpoint) or adapter.cleanup_category not in ('job_api', 'generation_protocol', 'unqueryable_sync'):
        raise ValueError('Route requires explicit endpoint and supported cleanup policy')
    ROUTES[name] = adapter


def route(name):
    if name not in ROUTES:
        raise ValueError('Unregistered provider route adapter: ' + str(name))
    return ROUTES[name]


def image_endpoint(category):
    from .image_providers import ENDPOINTS
    return ENDPOINTS[category]


def generation_endpoint():
    from .generation_provider import endpoint
    return endpoint(), 'source_image'


register_route('image_edit', RouteAdapter(lambda: image_endpoint('edit'), 'job_api'))
register_route('image_decompose', RouteAdapter(lambda: image_endpoint('decompose'), 'job_api'))
register_route('generation_jobs', RouteAdapter(generation_endpoint, 'job_api'))
register_route('generation', RouteAdapter(generation_endpoint, 'generation_protocol'))
register_route('openai_images', RouteAdapter(lambda: (os.environ.get('VIDEO_USE_IMAGE_GENERATION_URL') or os.environ.get('VIDEO_USE_MODEL_BASE_URL', ''), 'source_image'), 'unqueryable_sync'))
register_route('elevenlabs', RouteAdapter(lambda: ('https://api.elevenlabs.io', 'source'), 'unqueryable_sync'))


def metadata(operation):
    return {name: getattr(operation, name) for name in (
        'external_side_effects', 'budget_group', 'concurrency_group',
        'route_adapter', 'provider_identity', 'cleanup_category', 'reuse_safe')}


def validate(operation):
    fields = (operation.budget_group, operation.concurrency_group,
              operation.route_adapter, operation.provider_identity, operation.cleanup_category)
    if not operation.external_side_effects:
        if any(fields):
            raise ValueError('Local operations cannot declare external provider policy')
        return
    if not all(fields) or any(not re.fullmatch(r'[a-z][a-z0-9_.-]{0,99}', value) for value in fields):
        raise ValueError('External operations require complete public provider metadata')
    if route(operation.route_adapter).cleanup_category != operation.cleanup_category:
        raise ValueError('Cleanup policy does not match registered route adapter')
    if operation.reuse_safe:
        raise ValueError('External side effects cannot be bypassed by result reuse')
