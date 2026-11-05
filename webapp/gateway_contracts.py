"""Transport-neutral Gateway response contract."""
from dataclasses import dataclass
import json


@dataclass
class GatewayResult:
    status: int
    headers: dict
    chunks: object
    close: object


def json_result(status,value):
    data=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    return GatewayResult(status,{'Content-Type':'application/json','Content-Length':str(len(data))},[data],lambda:None)
