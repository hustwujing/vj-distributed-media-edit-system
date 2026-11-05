"""Transport-neutral Gateway response contract."""
from dataclasses import dataclass


@dataclass
class GatewayResult:
    status: int
    headers: dict
    chunks: object
    close: object
