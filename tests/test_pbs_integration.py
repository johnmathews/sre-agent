"""Integration tests for PBS tools with mocked HTTP responses."""

from typing import Any

import httpx
import pytest
import respx

from src.agent.tools.pbs import (
    pbs_datastore_status,
    pbs_list_backups,
    pbs_list_tasks,
)


@pytest.fixture(autouse=True)
def _use_mock_settings(mock_settings: Any) -> None:
    """Automatically use mock settings for all tests in this module."""


@pytest.mark.integration
class TestPbsDatastoreStatus:
    @respx.mock
    async def test_successful_status(self) -> None:
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "store": "backups",
                            "total": 2199023255552,
                            "used": 1099511627776,
                            "avail": 1099511627776,
                            "gc-status": {"last-run-state": "ok"},
                        }
                    ]
                },
            )
        )

        result = await pbs_datastore_status.ainvoke({})
        assert "1 datastore(s)" in result
        assert "backups" in result
        assert "50.0%" in result

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await pbs_datastore_status.ainvoke({})
        assert "Cannot connect" in result

    @respx.mock
    async def test_timeout(self) -> None:
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(
            side_effect=httpx.ReadTimeout("Read timed out")
        )

        result = await pbs_datastore_status.ainvoke({})
        assert "timed out" in result

    @respx.mock
    async def test_auth_error(self) -> None:
        respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(
            return_value=httpx.Response(401, text="authentication failure")
        )

        result = await pbs_datastore_status.ainvoke({})
        assert "401" in result

    @respx.mock
    async def test_sends_auth_header(self) -> None:
        route = respx.get("https://pbs.test:8007/api2/json/status/datastore-usage").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        await pbs_datastore_status.ainvoke({})
        assert route.called
        auth_header = route.calls.last.request.headers["authorization"]
        assert auth_header.startswith("PBSAPIToken=")


@pytest.mark.integration
class TestPbsListBackups:
    @respx.mock
    async def test_successful_listing(self) -> None:
        respx.get("https://pbs.test:8007/api2/json/admin/datastore/backups/groups").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "backup-type": "vm",
                            "backup-id": "100",
                            "backup-count": 5,
                            "last-backup": 1700000000,
                            "owner": "root@pam",
                        },
                        {
                            "backup-type": "ct",
                            "backup-id": "101",
                            "backup-count": 3,
                            "last-backup": 1700000000,
                        },
                    ]
                },
            )
        )

        result = await pbs_list_backups.ainvoke({})
        assert "2 backup group(s)" in result

    @respx.mock
    async def test_custom_datastore(self) -> None:
        route = respx.get("https://pbs.test:8007/api2/json/admin/datastore/other-store/groups").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        result = await pbs_list_backups.ainvoke({"datastore": "other-store"})
        assert route.called
        assert "other-store" in result

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get("https://pbs.test:8007/api2/json/admin/datastore/backups/groups").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await pbs_list_backups.ainvoke({})
        assert "Cannot connect" in result

    async def test_no_datastore_configured(self, mock_settings: Any) -> None:
        mock_settings.pbs_default_datastore = ""
        result = await pbs_list_backups.ainvoke({})
        assert "No datastore specified" in result or "not configured" in result.lower()


@pytest.mark.integration
class TestPbsListTasks:
    @respx.mock
    async def test_successful_task_list(self) -> None:
        respx.get("https://pbs.test:8007/api2/json/nodes/localhost/tasks").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "worker_type": "backup",
                            "worker_id": "vm/100",
                            "status": "OK",
                            "user": "root@pam",
                            "starttime": 1700000000,
                            "endtime": 1700003600,
                        },
                        {
                            "worker_type": "garbage_collection",
                            "worker_id": "backups",
                            "status": "OK",
                            "user": "root@pam",
                            "starttime": 1700004000,
                            "endtime": 1700005000,
                        },
                    ]
                },
            )
        )

        result = await pbs_list_tasks.ainvoke({"limit": 20})
        assert "2 recent PBS task(s)" in result
        assert "[OK]" in result
        assert "backup" in result

    @respx.mock
    async def test_errors_only_filter(self) -> None:
        route = respx.get("https://pbs.test:8007/api2/json/nodes/localhost/tasks").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        await pbs_list_tasks.ainvoke({"limit": 10, "errors_only": True})
        assert route.called
        assert route.calls.last.request.url.params["errors"] == "1"

    @respx.mock
    async def test_sends_limit_param(self) -> None:
        route = respx.get("https://pbs.test:8007/api2/json/nodes/localhost/tasks").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        await pbs_list_tasks.ainvoke({"limit": 5})
        assert route.calls.last.request.url.params["limit"] == "5"

    @respx.mock
    async def test_connect_error(self) -> None:
        respx.get("https://pbs.test:8007/api2/json/nodes/localhost/tasks").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        result = await pbs_list_tasks.ainvoke({})
        assert "Cannot connect" in result


@pytest.mark.integration
class TestPbsNotConfigured:
    async def test_datastore_status_not_configured(self, mock_settings: Any) -> None:
        mock_settings.pbs_url = ""
        result = await pbs_datastore_status.ainvoke({})
        assert "not configured" in result.lower()

    async def test_list_tasks_not_configured(self, mock_settings: Any) -> None:
        mock_settings.pbs_url = ""
        result = await pbs_list_tasks.ainvoke({})
        assert "not configured" in result.lower()


@pytest.mark.integration
class TestPbsLatestBackupTime:
    GROUPS = "https://pbs.test:8007/api2/json/admin/datastore/backups/groups"

    @respx.mock
    async def test_finds_the_guest_group(self) -> None:
        from datetime import UTC, datetime

        from src.agent.tools.pbs import pbs_latest_backup_time

        route = respx.get(self.GROUPS).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"backup-type": "ct", "backup-id": "114", "last-backup": 1},
                        {"backup-type": "ct", "backup-id": "115", "last-backup": 1_790_000_000},
                    ]
                },
            )
        )
        assert await pbs_latest_backup_time("ct", "115") == datetime.fromtimestamp(1_790_000_000, UTC)
        assert route.calls.last.request.headers["Authorization"].startswith("PBSAPIToken=")

    @respx.mock
    async def test_unknown_guest_is_none(self) -> None:
        from src.agent.tools.pbs import pbs_latest_backup_time

        respx.get(self.GROUPS).mock(return_value=httpx.Response(200, json={"data": []}))
        assert await pbs_latest_backup_time("vm", "999") is None

    @respx.mock
    async def test_redirect_is_an_error_not_followed(self) -> None:
        from src.agent.tools.pbs import pbs_latest_backup_time

        respx.get(self.GROUPS).mock(return_value=httpx.Response(302, headers={"Location": "http://evil.test/"}))
        with pytest.raises(ValueError, match="HTTP 302"):
            await pbs_latest_backup_time("ct", "115")

    @respx.mock
    async def test_oversized_body_rejected(self) -> None:
        from src.agent.tools.pbs import pbs_latest_backup_time
        from src.upgrades.http import ResponseTooLarge

        respx.get(self.GROUPS).mock(return_value=httpx.Response(200, content=b"x" * (6 * 1024 * 1024)))
        with pytest.raises(ResponseTooLarge):
            await pbs_latest_backup_time("ct", "115")
