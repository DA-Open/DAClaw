"""Artifact HTTP byte-range semantics, including the response size limit."""

from __future__ import annotations

import pytest
import pytest_asyncio

from harness.api.app import MAX_ARTIFACT_BYTES
from harness.contracts import ArtifactInput

from .test_api import A, B, Api, assert_error

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def artifact_api(tmp_path):
    api = Api(tmp_path)
    content = b"0123456789"
    ref = await api.system.artifacts.put(
        ArtifactInput(scope_id="s1", media_type="application/octet-stream", content=content)
    )
    yield api, f"/artifacts/{ref.id}", content
    await api.client.aclose()


async def test_complete_artifact_is_200_and_advertises_ranges(artifact_api):
    api, url, content = artifact_api
    response = await api.client.get(url, headers=A)
    assert response.status_code == 200 and response.content == content
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Length"] == str(len(content))
    assert response.headers["ETag"] == f'"{response.headers["X-Content-Hash"]}"'
    assert "Content-Range" not in response.headers


@pytest.mark.parametrize(
    ("params", "offset", "end"),
    [({"offset": 2, "length": 5}, 2, 7), ({"offset": 0}, 0, 10),
     ({"length": 20}, 0, 10), ({"offset": 8, "length": 5}, 8, 10)],
)
async def test_query_ranges_are_206_with_actual_byte_bounds(artifact_api, params, offset, end):
    api, url, content = artifact_api
    response = await api.client.get(url, params=params, headers=A)
    assert response.status_code == 206 and response.content == content[offset:end]
    assert response.headers["Content-Range"] == f"bytes {offset}-{end - 1}/{len(content)}"
    assert response.headers["Content-Length"] == str(end - offset)
    assert response.headers["X-Artifact-Offset"] == str(offset)


@pytest.mark.parametrize(
    ("value", "offset", "end"),
    [("bytes=2-6", 2, 7), ("bytes=7-", 7, 10), ("bytes=-3", 7, 10),
     ("bytes=0-99", 0, 10), ("bytes=-99", 0, 10)],
)
async def test_standard_single_byte_ranges(artifact_api, value, offset, end):
    api, url, content = artifact_api
    response = await api.client.get(url, headers={**A, "Range": value})
    assert response.status_code == 206 and response.content == content[offset:end]
    assert response.headers["Content-Range"] == f"bytes {offset}-{end - 1}/{len(content)}"
    assert response.headers["Accept-Ranges"] == "bytes"


@pytest.mark.parametrize("value", ["bytes=10-", "bytes=99-100", "bytes=-0"])
async def test_unsatisfiable_range_includes_size(artifact_api, value):
    api, url, content = artifact_api
    response = await api.client.get(url, headers={**A, "Range": value})
    assert_error(response, 416, "invalid_request")
    assert response.headers["Content-Range"] == f"bytes */{len(content)}"


async def test_out_of_bounds_query_range_is_416(artifact_api):
    api, url, content = artifact_api
    response = await api.client.get(url, params={"offset": len(content)}, headers=A)
    assert_error(response, 416, "invalid_request")
    assert response.headers["Content-Range"] == f"bytes */{len(content)}"


@pytest.mark.parametrize("value", ["bytes=a-b", "bytes=9-2", "bytes=0-1,3-4", "bytes=-", "bytes=+1-2"])
async def test_malformed_or_multiple_ranges_are_rejected(artifact_api, value):
    api, url, _ = artifact_api
    assert_error(await api.client.get(url, headers={**A, "Range": value}), 400, "invalid_request")


async def test_unknown_range_unit_is_ignored(artifact_api):
    api, url, content = artifact_api
    response = await api.client.get(url, headers={**A, "Range": "items=0-1"})
    assert response.status_code == 200 and response.content == content
    assert "Content-Range" not in response.headers


async def test_query_zero_length_and_mixed_selectors_are_rejected(artifact_api):
    api, url, _ = artifact_api
    assert_error(await api.client.get(url, params={"length": 0}, headers=A), 400, "invalid_request")
    assert_error(
        await api.client.get(url, params={"offset": 0}, headers={**A, "Range": "bytes=0-1"}),
        400, "invalid_request",
    )


async def test_if_range_requires_matching_strong_etag(artifact_api):
    api, url, content = artifact_api
    etag = (await api.client.get(url, headers=A)).headers["ETag"]
    matching = await api.client.get(url, headers={**A, "Range": "bytes=2-4", "If-Range": etag})
    assert matching.status_code == 206 and matching.content == content[2:5]
    for validator in ('"old-version"', f"W/{etag}", "Wed, 15 Nov 1995 06:25:24 GMT"):
        response = await api.client.get(url, headers={**A, "Range": "bytes=2-4", "If-Range": validator})
        assert response.status_code == 200 and response.content == content
        assert "Content-Range" not in response.headers


async def test_cross_scope_range_errors_do_not_disclose_artifact_size(artifact_api):
    api, url, _ = artifact_api
    for headers in ({**B, "Range": "bytes=999-"}, {**B, "Range": "bytes=bad"}):
        response = await api.client.get(url, headers=headers)
        assert_error(response, 404, "not_found")
        assert "Content-Range" not in response.headers
        assert "ETag" not in response.headers


async def test_empty_artifact_is_200_without_a_range_and_416_with_one(artifact_api):
    api, _, _ = artifact_api
    ref = await api.system.artifacts.put(
        ArtifactInput(scope_id="s1", media_type="application/octet-stream", content=b"")
    )
    url = f"/artifacts/{ref.id}"
    whole = await api.client.get(url, headers=A)
    assert whole.status_code == 200 and whole.content == b""
    assert "Content-Range" not in whole.headers
    for headers, params in (({**A, "Range": "bytes=0-"}, {}), (A, {"offset": 0})):
        response = await api.client.get(url, headers=headers, params=params)
        assert_error(response, 416, "invalid_request")
        assert response.headers["Content-Range"] == "bytes */0"


async def test_large_artifact_requires_ranges_and_each_response_is_bounded(artifact_api):
    api, _, _ = artifact_api
    content = b"x" * MAX_ARTIFACT_BYTES + b"tail"
    ref = await api.system.artifacts.put(
        ArtifactInput(scope_id="s1", media_type="application/octet-stream", content=content)
    )
    url = f"/artifacts/{ref.id}"
    assert_error(await api.client.get(url, headers=A), 413, "limits_exceeded")
    first = await api.client.get(url, headers={**A, "Range": "bytes=0-"})
    assert first.status_code == 206 and len(first.content) == MAX_ARTIFACT_BYTES
    assert first.headers["Content-Range"] == f"bytes 0-{MAX_ARTIFACT_BYTES - 1}/{len(content)}"
    tail = await api.client.get(url, params={"offset": MAX_ARTIFACT_BYTES}, headers=A)
    assert tail.status_code == 206 and tail.content == b"tail"
    assert tail.headers["Content-Range"] == f"bytes {MAX_ARTIFACT_BYTES}-{len(content) - 1}/{len(content)}"
    assert_error(
        await api.client.get(url, headers={**A, "Range": "bytes=0-1", "If-Range": '"old-version"'}),
        413, "limits_exceeded",
    )
