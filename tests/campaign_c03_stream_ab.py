"""Compare structured streaming transport overhead on one frozen C03 request."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=2)
    return parser.parse_args()


def initial_snapshot(path):
    audit = json.loads(path.read_text())
    snapshots = audit["database"]["runs"][0]["audit"]["planner_snapshots"]
    initial = next(row for row in snapshots if json.loads(row["snapshot_json"])["graph_version"] == 0)
    return json.loads(initial["snapshot_json"])


class RedisPublisher:
    def __init__(self, bus, run_id):
        self.bus = bus
        self.run_id = run_id + ":ab:" + uuid.uuid4().hex
        self.calls = 0
        self.seconds = 0.0

    def publish(self, delta):
        started = time.perf_counter()
        self.bus.publish(self.run_id, "text_delta", {"delta": delta, "published_at": time.time()})
        self.seconds += time.perf_counter() - started
        self.calls += 1

    def close(self):
        client = self.bus._get()
        if client is not None:
            client.unlink(self.bus.key(self.run_id))


def main():
    args = arguments()
    sys.path.insert(0, str(args.code_root.resolve()))
    from webapp import workflow_graph as graph
    from webapp.agent_profiles import profiles
    from webapp.agent_stream import bus
    from webapp.egress_policy import assert_no_credentials, provider_base, secrets, session
    from webapp.model_adapters import StructuredStream, get as adapter_for
    from webapp.model_tool_protocol import request as tool_request, response as tool_response
    from webapp.specialist_profiles import get as specialist
    from webapp.workflow_agents import model_instructions, model_output_limit
    from webapp.workflow_model_protocol import ToolProtocol
    from webapp.workflow_plan_draft import EMPTY

    frozen = initial_snapshot(args.source)
    profile = specialist("media_planner")
    configuration = profiles()["claude55"]
    adapter = adapter_for(configuration.protocol)
    base, key = configuration.credentials()
    protocol = ToolProtocol(profile, frozen, {"control_kind": "PLAN"}, draft=dict(EMPTY))
    instructions = model_instructions(profile, control_kind="PLAN", native_tools=True)
    conversation = graph.encode(protocol.context())
    definitions = protocol.definitions()

    base_body = adapter.conversation(instructions, conversation)
    base_body = tool_request(adapter, base_body, definitions, strict=True)
    base_body = adapter.prepare(
        base_body,
        configuration.model,
        model_output_limit(profile, protocol),
        configuration.reasoning_effort,
    )
    assert_no_credentials(graph.encode(base_body), (*secrets(), key))

    arms = ["stream_publish", "stream_no_publish", "non_stream"]
    rows = []
    args.output.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeat):
        order = arms if repeat % 2 == 0 else list(reversed(arms))
        for arm in order:
            body = dict(base_body)
            body["stream"] = arm != "non_stream"
            publisher = RedisPublisher(bus(), frozen["run_id"]) if arm == "stream_publish" else None
            first_chunk = None
            first_public = None
            chunks = 0
            response_bytes = 0
            public_chars = 0
            started = time.perf_counter()

            def public_delta(delta):
                nonlocal first_public, public_chars
                if first_public is None:
                    first_public = time.perf_counter()
                public_chars += len(delta)
                if publisher is not None:
                    publisher.publish(delta)

            parser = StructuredStream(adapter.protocol, public_delta) if arm != "non_stream" else None
            row = {
                "arm": arm,
                "repeat": repeat + 1,
                "model": configuration.model,
                "reasoning_effort": configuration.reasoning_effort,
                "request_sha256": graph.digest(body),
                "request_without_stream_sha256": graph.digest({k: v for k, v in body.items() if k != "stream"}),
                "started_at": time.time(),
            }
            try:
                with session() as client:
                    with client.post(
                        provider_base(base) + adapter.path,
                        json=body,
                        headers=adapter.headers(key),
                        stream=True,
                        timeout=(10, 1200),
                        allow_redirects=False,
                    ) as response:
                        row["http_status"] = response.status_code
                        row["headers_seconds"] = time.perf_counter() - started
                        response.raise_for_status()
                        data = bytearray()
                        for chunk in response.iter_content(8192):
                            if not chunk:
                                continue
                            now = time.perf_counter()
                            if first_chunk is None:
                                first_chunk = now
                            chunks += 1
                            response_bytes += len(chunk)
                            data.extend(chunk)
                            if parser is not None:
                                parser.feed(chunk)
                        payload = parser.finish() if parser is not None else json.loads(data)
                name, tool_arguments = tool_response(adapter, payload)
                protocol.decode(name, tool_arguments)
                row.update(
                    valid=True,
                    tool=name,
                    response_model=payload.get("model"),
                    usage=payload.get("usage"),
                )
            except Exception as exc:
                row.update(valid=False, error_type=type(exc).__name__, error=str(exc)[:500])
            finally:
                ended = time.perf_counter()
                if publisher is not None:
                    publisher.close()
                row.update(
                    total_seconds=ended - started,
                    first_chunk_seconds=None if first_chunk is None else first_chunk - started,
                    first_public_seconds=None if first_public is None else first_public - started,
                    chunks=chunks,
                    response_bytes=response_bytes,
                    public_characters=public_chars,
                    publisher_calls=publisher.calls if publisher is not None else 0,
                    publisher_seconds=publisher.seconds if publisher is not None else 0.0,
                )
                rows.append(row)
                (args.output / f"{repeat + 1}-{arm}.json").write_text(
                    json.dumps(row, ensure_ascii=False, indent=2) + "\n"
                )
                print(json.dumps(row, ensure_ascii=False), flush=True)

    summary = {}
    for arm in arms:
        values = [row["total_seconds"] for row in rows if row["arm"] == arm]
        summary[arm] = {
            "runs": len(values),
            "valid": sum(row["valid"] for row in rows if row["arm"] == arm),
            "mean_seconds": statistics.mean(values),
            "median_seconds": statistics.median(values),
        }
    result = {
        "source": str(args.source),
        "code_root": str(args.code_root),
        "request_without_stream_sha256": rows[0]["request_without_stream_sha256"],
        "rows": rows,
        "summary": summary,
    }
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
