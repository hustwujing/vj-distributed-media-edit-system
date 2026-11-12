"""Publish frozen media references and register content-addressed Ark Assets."""
from __future__ import annotations

import datetime
import hashlib
import hmac
import io
import json
import os
from pathlib import PurePosixPath
import re
import time
from urllib.parse import quote, urlencode, urlsplit


_EXTENSIONS = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "video/mp4": "mp4", "video/quicktime": "mov",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/flac": "flac",
}


def _sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def configured() -> bool:
    return all(os.environ.get(name) for name in (
        "VIDEO_USE_ARK_ACCESS_KEY", "VIDEO_USE_ARK_SECRET_KEY",
        "VIDEO_USE_ARK_PROJECT_NAME", "VIDEO_USE_ARK_ASSET_GROUP_ID",
        "VIDEO_USE_OBJECT_BUCKET", "VIDEO_USE_OBJECT_ENDPOINT"))


def _control() -> tuple[str, str, str, str, str]:
    endpoint = os.environ.get("VIDEO_USE_ARK_CONTROL_ENDPOINT", "https://open.volcengineapi.com").rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path:
        raise ValueError("Ark Asset 控制地址必须是 HTTPS origin")
    values = (os.environ.get("VIDEO_USE_ARK_ACCESS_KEY", ""),
        os.environ.get("VIDEO_USE_ARK_SECRET_KEY", ""),
        os.environ.get("VIDEO_USE_ARK_PROJECT_NAME", ""),
        os.environ.get("VIDEO_USE_ARK_ASSET_GROUP_ID", ""))
    if not all(values):
        raise ValueError("Ark Asset 缺少 AK/SK、项目或资源组配置")
    return endpoint, parsed.netloc, *values


def _headers(host: str, access_key: str, secret_key: str, action: str,
             body: bytes, now: datetime.datetime) -> tuple[str, dict[str, str]]:
    xdate = now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date = xdate[:8]
    payload_hash = hashlib.sha256(body).hexdigest()
    query = urlencode({"Action": action, "Version": "2024-01-01"})
    canonical_headers = (f"content-type:application/json\nhost:{host}\n"
        f"x-content-sha256:{payload_hash}\nx-date:{xdate}\n")
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = f"POST\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{date}/cn-beijing/ark/request"
    string_to_sign = (f"HMAC-SHA256\n{xdate}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}")
    signing = _sign(_sign(_sign(_sign(secret_key.encode(), date), "cn-beijing"), "ark"), "request")
    signature = hmac.new(signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (f"HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")
    return query, {"Content-Type": "application/json", "Host": host,
        "X-Content-Sha256": payload_hash, "X-Date": xdate, "Authorization": authorization}


def _call(client, action: str, payload: dict) -> dict:
    endpoint, host, access_key, secret_key, project, _ = _control()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    query, headers = _headers(host, access_key, secret_key, action, body,
        datetime.datetime.now(datetime.timezone.utc))
    response = client.post(endpoint + "/?" + query, data=body, headers=headers,
        timeout=(10, 30), allow_redirects=False)
    try:
        value = response.json()
    except ValueError as exc:
        raise ValueError("Ark Asset 返回了无效响应") from exc
    metadata = value.get("ResponseMetadata", {}) if isinstance(value, dict) else {}
    error = metadata.get("Error", {}) if isinstance(metadata, dict) else {}
    if response.status_code >= 400 or error:
        code = str(error.get("Code") or f"http_{response.status_code}")
        raise ValueError("Ark Asset 请求失败: " + re.sub(r"[^A-Za-z0-9_.-]", "_", code)[:80])
    result = value.get("Result", {}) if isinstance(value, dict) else {}
    if not isinstance(result, dict):
        raise ValueError("Ark Asset 缺少结果")
    return result


def _asset_name(digest: str) -> str:
    return "distributed-agent-" + digest[:40]


def _find(client, digest: str) -> dict | None:
    _, _, _, _, project, group = _control()
    name = _asset_name(digest)
    result = _call(client, "ListAssets", {"Filter": {"GroupType": "AIGC",
        "GroupIds": [group], "Name": name}, "MaxResults": 20,
        "SortBy": "CreateTime", "SortOrder": "Desc", "ProjectName": project})
    return next((item for item in result.get("Items", []) if isinstance(item, dict)
        and item.get("Name") == name and item.get("GroupId") == group), None)


def _create(client, reference: dict) -> str:
    _, _, _, _, project, group = _control()
    result = _call(client, "CreateAsset", {"GroupId": group,
        "URL": reference["public_url"], "Name": _asset_name(reference["sha256"]),
        "AssetType": reference["kind"].title(), "ProjectName": project})
    identifier = str(result.get("Id") or "")
    if not re.fullmatch(r"asset-[A-Za-z0-9-]{4,160}", identifier):
        raise ValueError("Ark Asset 未返回合法资源 ID")
    return identifier


def _activate(client, reference: dict) -> str:
    _, _, _, _, project, _ = _control()
    found = _find(client, reference["sha256"])
    identifier = str(found.get("Id") or "") if found else _create(client, reference)
    if not re.fullmatch(r"asset-[A-Za-z0-9-]{4,160}", identifier):
        raise ValueError("Ark Asset 资源 ID 无效")
    deadline = time.monotonic() + float(os.environ.get("VIDEO_USE_ARK_ASSET_MAX_WAIT_SECONDS", "900"))
    interval = max(.2, float(os.environ.get("VIDEO_USE_ARK_ASSET_POLL_SECONDS", "2")))
    while True:
        value = _call(client, "GetAsset", {"Id": identifier, "ProjectName": project})
        status = str(value.get("Status") or "").lower()
        if status == "active":
            return "asset://" + identifier
        if status == "failed":
            error = value.get("Error") if isinstance(value.get("Error"), dict) else {}
            code = re.sub(r"[^A-Za-z0-9_.-]", "_", str(error.get("Code") or "processing_failed"))[:80]
            raise ValueError("Ark Asset 激活失败: " + code)
        if time.monotonic() >= deadline:
            raise TimeoutError("Ark Asset 激活超时")
        time.sleep(interval)


def _publish(reference: dict) -> dict:
    import boto3
    from botocore.config import Config
    bucket = os.environ.get("VIDEO_USE_OBJECT_BUCKET", "")
    endpoint = os.environ.get("VIDEO_USE_OBJECT_ENDPOINT", "")
    if not bucket or not endpoint:
        raise ValueError("媒体引用发布缺少 BOSS 配置")
    digest, kind, mime, data = (reference["sha256"], reference["kind"],
        reference["mime"], reference["bytes"])
    extension = _EXTENSIONS.get(mime)
    if not extension or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("媒体引用格式或摘要无效")
    prefix = os.environ.get("VIDEO_USE_MEDIA_REFERENCE_PREFIX", "aniforge").strip("/")
    key = f"{prefix}/{kind}/{digest[:2]}/{digest}.{extension}"
    if str(PurePosixPath(key)) != key or ".." in PurePosixPath(key).parts:
        raise ValueError("媒体引用对象路径无效")
    client = boto3.client("s3", endpoint_url=endpoint,
        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30,
            retries={"max_attempts": 2}, s3={"addressing_style": "path"}))
    metadata = {"sha256": digest, "size": str(len(data)), "kind": kind}
    try:
        client.put_object(Bucket=bucket, Key=key, Body=io.BytesIO(data), ContentType=mime,
            Metadata=metadata, IfNoneMatch="*")
    except Exception as exc:
        status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status not in (409, 412):
            raise
    head = client.head_object(Bucket=bucket, Key=key)
    actual = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()}
    if (int(head.get("ContentLength", 0)) != len(data)
            or str(head.get("ContentType", "")).split(";", 1)[0].lower() != mime
            or actual != metadata):
        raise ValueError("BOSS 媒体引用元数据校验失败")
    response = client.get_object(Bucket=bucket, Key=key)
    try:
        payload = response["Body"].read(len(data) + 1)
    finally:
        response["Body"].close()
    if len(payload) != len(data) or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("BOSS 媒体引用内容校验失败")
    public = os.environ.get("VIDEO_USE_MEDIA_REFERENCE_PUBLIC_ENDPOINT",
        "https://boss-shjd.biliapi.net").rstrip("/")
    parsed = urlsplit(public)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path:
        raise ValueError("媒体引用公开地址必须是 HTTPS origin")
    return {**reference, "public_url": f"{public}/{quote(bucket, safe='')}/{quote(key, safe='/')}"}


def prepare(client, references: list[dict]) -> list[dict]:
    if references and not configured():
        raise ValueError("多媒体参考生成尚未配置 Ark Asset")
    output = []
    for reference in references:
        published = _publish(reference)
        output.append({**published, "asset_uri": _activate(client, published)})
    return output
