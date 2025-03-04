import json
import os
import pytz
from django.utils import timezone as djangotime
from unittest.mock import patch
from itertools import cycle

from django.conf import settings
from logs.models import PendingAction
from model_bakery import baker
from packaging import version as pyver
from tacticalrmm.test import TacticalTestCase
from winupdate.models import WinUpdatePolicy
from winupdate.serializers import WinUpdatePolicySerializer

from .models import Agent, AgentCustomField, AgentHistory, Note
from .serializers import (
    AgentHistorySerializer,
    AgentSerializer,
    AgentHostnameSerializer,
    AgentNoteSerializer,
)
from .tasks import auto_self_agent_update_task


base_url = "/agents"


class TestAgentsList(TacticalTestCase):
    def setUp(self):
        self.authenticate()
        self.setup_coresettings()

    def test_get_agents(self):
        url = f"{base_url}/"

        # 36 total agents
        company1 = baker.make("clients.Client")
        company2 = baker.make("clients.Client")
        site1 = baker.make("clients.Site", client=company1)
        site2 = baker.make("clients.Site", client=company1)
        site3 = baker.make("clients.Site", client=company2)

        baker.make_recipe(
            "agents.online_agent", site=site1, monitoring_type="server", _quantity=15
        )
        baker.make_recipe(
            "agents.online_agent",
            site=site2,
            monitoring_type="workstation",
            _quantity=10,
        )
        baker.make_recipe(
            "agents.online_agent",
            site=site3,
            monitoring_type="server",
            _quantity=4,
        )
        baker.make_recipe(
            "agents.online_agent",
            site=site3,
            monitoring_type="workstation",
            _quantity=7,
        )

        # test all agents
        r = self.client.get(url, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 36)  # type: ignore

        # test client1
        r = self.client.get(f"{url}?client={company1.pk}", format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 25)  # type: ignore

        # test site3
        r = self.client.get(f"{url}?site={site3.pk}", format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 11)  # type: ignore

        # test with no details
        r = self.client.get(f"{url}?site={site3.pk}&detail=false", format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 11)  # type: ignore

        # make sure data is returned with the AgentHostnameSerializer
        agents = Agent.objects.filter(site=site3)
        serializer = AgentHostnameSerializer(agents, many=True)
        self.assertEqual(r.data, serializer.data)  # type: ignore

        self.check_not_authenticated("get", url)


class TestAgentViews(TacticalTestCase):
    def setUp(self):
        self.authenticate()
        self.setup_coresettings()

        client = baker.make("clients.Client", name="Google")
        site = baker.make("clients.Site", client=client, name="LA Office")
        self.agent = baker.make_recipe(
            "agents.online_agent", site=site, version="1.1.1"
        )
        baker.make_recipe("winupdate.winupdate_policy", agent=self.agent)

    def test_get_agent(self):
        url = f"{base_url}/{self.agent.agent_id}/"

        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)

        self.check_not_authenticated("get", url)

    def test_edit_agent(self):
        # setup data
        site = baker.make("clients.Site", name="Ny Office")

        url = f"{base_url}/{self.agent.agent_id}/"

        data = {
            "site": site.id,  # type: ignore
            "monitoring_type": "workstation",
            "description": "asjdk234andasd",
            "offline_time": 4,
            "overdue_time": 300,
            "check_interval": 60,
            "overdue_email_alert": True,
            "overdue_text_alert": False,
            "winupdatepolicy": [
                {
                    "critical": "approve",
                    "important": "approve",
                    "moderate": "manual",
                    "low": "ignore",
                    "other": "ignore",
                    "run_time_hour": 5,
                    "run_time_days": [2, 3, 6],
                    "reboot_after_install": "required",
                    "reprocess_failed": True,
                    "reprocess_failed_times": 13,
                    "email_if_fail": True,
                    "agent": self.agent.pk,
                }
            ],
        }

        r = self.client.put(url, data, format="json")
        self.assertEqual(r.status_code, 200)

        agent = Agent.objects.get(pk=self.agent.pk)
        data = AgentSerializer(agent).data
        self.assertEqual(data["site"], site.id)  # type: ignore

        policy = WinUpdatePolicy.objects.get(agent=self.agent)
        data = WinUpdatePolicySerializer(policy).data
        self.assertEqual(data["run_time_days"], [2, 3, 6])

        # test adding custom fields
        field = baker.make("core.CustomField", model="agent", type="number")
        data = {
            "site": site.id,  # type: ignore
            "description": "asjdk234andasd",
            "custom_fields": [{"field": field.id, "string_value": "123"}],  # type: ignore
        }

        r = self.client.put(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            AgentCustomField.objects.filter(agent=self.agent, field=field).exists()
        )

        # test edit custom field
        data = {
            "site": site.id,  # type: ignore
            "description": "asjdk234andasd",
            "custom_fields": [{"field": field.id, "string_value": "456"}],  # type: ignore
        }

        r = self.client.put(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            AgentCustomField.objects.get(agent=agent, field=field).value,
            "456",
        )
        self.check_not_authenticated("put", url)

    @patch("agents.models.Agent.nats_cmd")
    @patch("agents.views.reload_nats")
    def test_agent_uninstall(self, reload_nats, nats_cmd):
        url = f"{base_url}/{self.agent.agent_id}/"

        r = self.client.delete(url, format="json")
        self.assertEqual(r.status_code, 200)

        nats_cmd.assert_called_with({"func": "uninstall"}, wait=False)
        reload_nats.assert_called_once()

        self.check_not_authenticated("delete", url)

    def test_get_patch_policy(self):
        # make sure get_patch_policy doesn't error out when agent has policy with
        # an empty patch policy
        policy = baker.make("automation.Policy")
        self.agent.policy = policy
        self.agent.save(update_fields=["policy"])
        _ = self.agent.get_patch_policy()

        self.agent.monitoring_type = "workstation"
        self.agent.save(update_fields=["monitoring_type"])
        _ = self.agent.get_patch_policy()

        self.agent.policy = None
        self.agent.save(update_fields=["policy"])

        self.coresettings.server_policy = policy
        self.coresettings.workstation_policy = policy
        self.coresettings.save(update_fields=["server_policy", "workstation_policy"])
        _ = self.agent.get_patch_policy()

        self.agent.monitoring_type = "server"
        self.agent.save(update_fields=["monitoring_type"])
        _ = self.agent.get_patch_policy()

    def test_get_agent_versions(self):
        url = "/agents/versions/"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        assert any(i["hostname"] == self.agent.hostname for i in r.json()["agents"])

        self.check_not_authenticated("get", url)

    @patch("agents.tasks.send_agent_update_task.delay")
    def test_update_agents(self, mock_task):
        url = f"{base_url}/update/"
        baker.make_recipe(
            "agents.agent",
            operating_system="Windows 10 Pro, 64 bit (build 19041.450)",
            version=settings.LATEST_AGENT_VER,
            _quantity=15,
        )
        baker.make_recipe(
            "agents.agent",
            operating_system="Windows 10 Pro, 64 bit (build 19041.450)",
            version="1.3.0",
            _quantity=15,
        )

        agent_ids: list[str] = list(
            Agent.objects.only("agent_id", "version").values_list("agent_id", flat=True)
        )

        data = {"agent_ids": agent_ids}
        expected: list[str] = [
            i.agent_id
            for i in Agent.objects.only("agent_id", "version")
            if pyver.parse(i.version) < pyver.parse(settings.LATEST_AGENT_VER)
        ]

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)

        mock_task.assert_called_with(agent_ids=expected)

        self.check_not_authenticated("post", url)

    @patch("time.sleep", return_value=None)
    @patch("agents.models.Agent.nats_cmd")
    def test_agent_ping(self, nats_cmd, mock_sleep):
        url = f"{base_url}/{self.agent.agent_id}/ping/"

        nats_cmd.return_value = "timeout"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        ret = {"name": self.agent.hostname, "status": "offline"}
        self.assertEqual(r.json(), ret)

        nats_cmd.return_value = "natsdown"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        ret = {"name": self.agent.hostname, "status": "offline"}
        self.assertEqual(r.json(), ret)

        nats_cmd.return_value = "pong"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        ret = {"name": self.agent.hostname, "status": "online"}
        self.assertEqual(r.json(), ret)

        nats_cmd.return_value = "asdasjdaksdasd"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        ret = {"name": self.agent.hostname, "status": "offline"}
        self.assertEqual(r.json(), ret)

        self.check_not_authenticated("get", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_get_processes(self, mock_ret):
        agent = baker.make_recipe("agents.online_agent", version="1.2.0")
        url = f"{base_url}/{agent.agent_id}/processes/"

        with open(
            os.path.join(settings.BASE_DIR, "tacticalrmm/test_data/procs.json")
        ) as f:
            mock_ret.return_value = json.load(f)

        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        assert any(i["name"] == "Registry" for i in mock_ret.return_value)
        assert any(i["membytes"] == 434655234324 for i in mock_ret.return_value)

        mock_ret.return_value = "timeout"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 400)

        self.check_not_authenticated("get", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_kill_process(self, nats_cmd):
        url = f"{base_url}/{self.agent.agent_id}/processes/123/"

        nats_cmd.return_value = "ok"
        r = self.client.delete(url)
        self.assertEqual(r.status_code, 200)

        nats_cmd.return_value = "timeout"
        r = self.client.delete(url)
        self.assertEqual(r.status_code, 400)

        nats_cmd.return_value = "process doesn't exist"
        r = self.client.delete(url)
        self.assertEqual(r.status_code, 400)

        self.check_not_authenticated("delete", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_get_event_log(self, nats_cmd):
        url = f"/agents/{self.agent.agent_id}/eventlog/Application/22/"

        with open(
            os.path.join(settings.BASE_DIR, "tacticalrmm/test_data/appeventlog.json")
        ) as f:
            nats_cmd.return_value = json.load(f)

        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        nats_cmd.assert_called_with(
            {
                "func": "eventlog",
                "timeout": 30,
                "payload": {
                    "logname": "Application",
                    "days": str(22),
                },
            },
            timeout=32,
        )

        url = f"{base_url}/{self.agent.agent_id}/eventlog/Security/6/"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        nats_cmd.assert_called_with(
            {
                "func": "eventlog",
                "timeout": 180,
                "payload": {
                    "logname": "Security",
                    "days": str(6),
                },
            },
            timeout=182,
        )

        nats_cmd.return_value = "timeout"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 400)

        self.check_not_authenticated("get", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_reboot_now(self, nats_cmd):
        url = f"{base_url}/{self.agent.agent_id}/reboot/"

        nats_cmd.return_value = "ok"
        r = self.client.post(url, format="json")
        self.assertEqual(r.status_code, 200)
        nats_cmd.assert_called_with({"func": "rebootnow"}, timeout=10)

        nats_cmd.return_value = "timeout"
        r = self.client.post(url, format="json")
        self.assertEqual(r.status_code, 400)

        self.check_not_authenticated("post", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_send_raw_cmd(self, mock_ret):
        url = f"{base_url}/{self.agent.agent_id}/cmd/"

        data = {
            "cmd": "ipconfig",
            "shell": "cmd",
            "timeout": 30,
        }
        mock_ret.return_value = "nt authority\\system"
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.data, str)  # type: ignore

        mock_ret.return_value = "timeout"
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 400)

        self.check_not_authenticated("post", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_reboot_later(self, nats_cmd):
        url = f"{base_url}/{self.agent.agent_id}/reboot/"

        data = {
            "datetime": "2025-08-29 18:41",
        }

        nats_cmd.return_value = "ok"
        r = self.client.patch(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["time"], "August 29, 2025 at 06:41 PM")  # type: ignore
        self.assertEqual(r.data["agent"], self.agent.hostname)  # type: ignore

        nats_data = {
            "func": "schedtask",
            "schedtaskpayload": {
                "type": "schedreboot",
                "deleteafter": True,
                "trigger": "once",
                "name": r.data["task_name"],  # type: ignore
                "year": 2025,
                "month": "August",
                "day": 29,
                "hour": 18,
                "min": 41,
            },
        }
        nats_cmd.assert_called_with(nats_data, timeout=10)

        nats_cmd.return_value = "error creating task"
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 400)

        data_invalid = {
            "datetime": "rm -rf /",
        }
        r = self.client.patch(url, data_invalid, format="json")

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data, "Invalid date")  # type: ignore

        self.check_not_authenticated("patch", url)

    @patch("os.path.exists")
    def test_install_agent(self, mock_file_exists):
        url = f"{base_url}/installer/"

        site = baker.make("clients.Site")
        data = {
            "client": site.client.id,  # type: ignore
            "site": site.id,  # type: ignore
            "arch": "64",
            "expires": 23,
            "installMethod": "manual",
            "api": "https://api.example.com",
            "agenttype": "server",
            "rdp": 1,
            "ping": 0,
            "power": 0,
            "fileName": "rmm-client-site-server.exe",
        }

        mock_file_exists.return_value = False
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 400)

        mock_file_exists.return_value = True
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)

        data["arch"] = "32"
        mock_file_exists.return_value = False
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 400)

        data["arch"] = "64"
        mock_file_exists.return_value = True
        r = self.client.post(url, data, format="json")
        self.assertIn("rdp", r.json()["cmd"])
        self.assertNotIn("power", r.json()["cmd"])

        data.update({"ping": 1, "power": 1})
        r = self.client.post(url, data, format="json")
        self.assertIn("power", r.json()["cmd"])
        self.assertIn("ping", r.json()["cmd"])

        data["installMethod"] = "powershell"
        self.assertEqual(r.status_code, 200)

        self.check_not_authenticated("post", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_recover(self, nats_cmd):
        from agents.models import RecoveryAction

        RecoveryAction.objects.all().delete()
        agent = baker.make_recipe("agents.online_agent")
        url = f"{base_url}/{agent.agent_id}/recover/"

        # test mesh realtime
        data = {"cmd": None, "mode": "mesh"}
        nats_cmd.return_value = "ok"
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RecoveryAction.objects.count(), 0)
        nats_cmd.assert_called_with(
            {"func": "recover", "payload": {"mode": "mesh"}}, timeout=10
        )
        nats_cmd.reset_mock()

        # test mesh with agent rpc not working
        data = {"cmd": None, "mode": "mesh"}
        nats_cmd.return_value = "timeout"
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RecoveryAction.objects.count(), 1)
        mesh_recovery = RecoveryAction.objects.first()
        self.assertEqual(mesh_recovery.mode, "mesh")  # type: ignore
        nats_cmd.reset_mock()
        RecoveryAction.objects.all().delete()

        # test tacagent realtime
        data = {"cmd": None, "mode": "tacagent"}
        nats_cmd.return_value = "ok"
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RecoveryAction.objects.count(), 0)
        nats_cmd.assert_called_with(
            {"func": "recover", "payload": {"mode": "tacagent"}}, timeout=10
        )
        nats_cmd.reset_mock()

        # test tacagent with rpc not working
        data = {"cmd": None, "mode": "tacagent"}
        nats_cmd.return_value = "timeout"
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RecoveryAction.objects.count(), 0)
        nats_cmd.reset_mock()

        # test shell cmd without command
        data = {"cmd": None, "mode": "command"}
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(RecoveryAction.objects.count(), 0)

        # test shell cmd
        data = {"cmd": "shutdown /r /t 10 /f", "mode": "command"}
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(RecoveryAction.objects.count(), 1)
        cmd_recovery = RecoveryAction.objects.first()
        self.assertEqual(cmd_recovery.mode, "command")  # type: ignore
        self.assertEqual(cmd_recovery.command, "shutdown /r /t 10 /f")  # type: ignore

    @patch("agents.models.Agent.get_login_token")
    def test_meshcentral_tabs(self, mock_token):
        url = f"{base_url}/{self.agent.agent_id}/meshcentral/"
        mock_token.return_value = "askjh1k238uasdhk487234jadhsajksdhasd"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)

        # TODO
        # decode the cookie

        self.assertIn("&viewmode=13", r.data["file"])  # type: ignore
        self.assertIn("&viewmode=12", r.data["terminal"])  # type: ignore
        self.assertIn("&viewmode=11", r.data["control"])  # type: ignore

        self.assertIn("&gotonode=", r.data["file"])  # type: ignore
        self.assertIn("&gotonode=", r.data["terminal"])  # type: ignore
        self.assertIn("&gotonode=", r.data["control"])  # type: ignore

        self.assertIn("?login=", r.data["file"])  # type: ignore
        self.assertIn("?login=", r.data["terminal"])  # type: ignore
        self.assertIn("?login=", r.data["control"])  # type: ignore

        self.assertEqual(self.agent.hostname, r.data["hostname"])  # type: ignore
        self.assertEqual(self.agent.client.name, r.data["client"])  # type: ignore
        self.assertEqual(self.agent.site.name, r.data["site"])  # type: ignore

        self.assertEqual(r.status_code, 200)

        mock_token.return_value = "err"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 400)

        self.check_not_authenticated("get", url)

    @patch("agents.models.Agent.nats_cmd")
    def test_recover_mesh(self, nats_cmd):
        url = f"{base_url}/{self.agent.agent_id}/meshcentral/recover/"
        nats_cmd.return_value = "ok"
        r = self.client.post(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.agent.hostname, r.data)  # type: ignore
        nats_cmd.assert_called_with(
            {"func": "recover", "payload": {"mode": "mesh"}}, timeout=90
        )

        nats_cmd.return_value = "timeout"
        r = self.client.post(url)
        self.assertEqual(r.status_code, 400)

        url = f"{base_url}/{self.agent.agent_id}123/meshcentral/recover/"
        r = self.client.post(url)
        self.assertEqual(r.status_code, 404)

        self.check_not_authenticated("post", url)

    @patch("agents.tasks.run_script_email_results_task.delay")
    @patch("agents.models.Agent.run_script")
    def test_run_script(self, run_script, email_task):
        from .models import AgentCustomField, Note
        from clients.models import ClientCustomField, SiteCustomField

        run_script.return_value = "ok"
        url = f"/agents/{self.agent.agent_id}/runscript/"
        script = baker.make_recipe("scripts.script")

        # test wait
        data = {
            "script": script.pk,
            "output": "wait",
            "args": [],
            "timeout": 15,
        }

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        run_script.assert_called_with(
            scriptpk=script.pk, args=[], timeout=18, wait=True, history_pk=0
        )
        run_script.reset_mock()

        # test email default
        data = {
            "script": script.pk,
            "output": "email",
            "args": ["abc", "123"],
            "timeout": 15,
            "emailMode": "default",
            "emails": ["admin@example.com", "bob@example.com"],
        }
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        email_task.assert_called_with(
            agentpk=self.agent.pk,
            scriptpk=script.pk,
            nats_timeout=18,
            emails=[],
            args=["abc", "123"],
        )
        email_task.reset_mock()

        # test email overrides
        data["emailMode"] = "custom"
        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        email_task.assert_called_with(
            agentpk=self.agent.pk,
            scriptpk=script.pk,
            nats_timeout=18,
            emails=["admin@example.com", "bob@example.com"],
            args=["abc", "123"],
        )

        # test fire and forget
        data = {
            "script": script.pk,
            "output": "forget",
            "args": ["hello", "world"],
            "timeout": 22,
        }

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        run_script.assert_called_with(
            scriptpk=script.pk, args=["hello", "world"], timeout=25, history_pk=0
        )
        run_script.reset_mock()

        # test collector

        # save to agent custom field
        custom_field = baker.make("core.CustomField", model="agent")
        data = {
            "script": script.pk,
            "output": "collector",
            "args": ["hello", "world"],
            "timeout": 22,
            "custom_field": custom_field.id,  # type: ignore
            "save_all_output": True,
        }

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        run_script.assert_called_with(
            scriptpk=script.pk,
            args=["hello", "world"],
            timeout=25,
            wait=True,
            history_pk=0,
        )
        run_script.reset_mock()

        self.assertEqual(
            AgentCustomField.objects.get(agent=self.agent.pk, field=custom_field).value,
            "ok",
        )

        # save to site custom field
        custom_field = baker.make("core.CustomField", model="site")
        data = {
            "script": script.pk,
            "output": "collector",
            "args": ["hello", "world"],
            "timeout": 22,
            "custom_field": custom_field.id,  # type: ignore
            "save_all_output": False,
        }

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        run_script.assert_called_with(
            scriptpk=script.pk,
            args=["hello", "world"],
            timeout=25,
            wait=True,
            history_pk=0,
        )
        run_script.reset_mock()

        self.assertEqual(
            SiteCustomField.objects.get(
                site=self.agent.site.pk, field=custom_field
            ).value,
            "ok",
        )

        # save to client custom field
        custom_field = baker.make("core.CustomField", model="client")
        data = {
            "script": script.pk,
            "output": "collector",
            "args": ["hello", "world"],
            "timeout": 22,
            "custom_field": custom_field.id,  # type: ignore
            "save_all_output": False,
        }

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        run_script.assert_called_with(
            scriptpk=script.pk,
            args=["hello", "world"],
            timeout=25,
            wait=True,
            history_pk=0,
        )
        run_script.reset_mock()

        self.assertEqual(
            ClientCustomField.objects.get(
                client=self.agent.client.pk, field=custom_field
            ).value,
            "ok",
        )

        # test save to note
        data = {
            "script": script.pk,
            "output": "note",
            "args": ["hello", "world"],
            "timeout": 22,
        }

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        run_script.assert_called_with(
            scriptpk=script.pk,
            args=["hello", "world"],
            timeout=25,
            wait=True,
            history_pk=0,
        )
        run_script.reset_mock()

        self.assertEqual(Note.objects.get(agent=self.agent).note, "ok")

    def test_get_notes(self):
        url = f"{base_url}/notes/"

        # setup
        agent = baker.make_recipe("agents.agent")
        notes = baker.make("agents.Note", agent=agent, _quantity=4)

        r = self.client.get(url)
        serializer = AgentNoteSerializer(notes, many=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 4)  # type: ignore
        self.assertEqual(r.data, serializer.data)  # type: ignore

        # test with agent_id
        url = f"{base_url}/{agent.agent_id}/notes/"

        r = self.client.get(url)
        serializer = AgentNoteSerializer(notes, many=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data), 4)  # type: ignore
        self.assertEqual(r.data, serializer.data)  # type: ignore

        self.check_not_authenticated("get", url)

    def test_add_note(self):
        url = f"{base_url}/notes/"
        agent = baker.make_recipe("agents.agent")

        data = {"note": "This is a note", "agent_id": agent.agent_id}
        r = self.client.post(url, data)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Note.objects.filter(agent=agent).exists())  # type: ignore

        self.check_not_authenticated("post", url)

    def test_get_note(self):
        # setup
        agent = baker.make_recipe("agents.agent")
        note = baker.make("agents.Note", agent=agent)
        url = f"{base_url}/notes/{note.id}/"

        # test not found
        r = self.client.get(f"{base_url}/notes/500/")
        self.assertEqual(r.status_code, 404)

        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)

        self.check_not_authenticated("put", url)

    def test_update_note(self):
        # setup
        agent = baker.make_recipe("agents.agent")
        note = baker.make("agents.Note", agent=agent)
        url = f"{base_url}/notes/{note.id}/"

        # test not found
        r = self.client.put(f"{base_url}/notes/500/")
        self.assertEqual(r.status_code, 404)

        data = {"note": "New"}
        r = self.client.put(url, data)
        self.assertEqual(r.status_code, 200)

        new_note = Note.objects.get(pk=note.id)  # type: ignore
        self.assertEqual(new_note.note, data["note"])

        self.check_not_authenticated("put", url)

    def test_delete_note(self):
        # setup
        agent = baker.make_recipe("agents.agent")
        note = baker.make("agents.Note", agent=agent)
        url = f"{base_url}/notes/{note.id}/"

        # test not found
        r = self.client.delete(f"{base_url}/notes/500/")
        self.assertEqual(r.status_code, 404)

        r = self.client.delete(url)
        self.assertEqual(r.status_code, 200)

        self.assertFalse(Note.objects.filter(pk=note.id).exists())  # type: ignore

        self.check_not_authenticated("delete", url)

    def test_get_agent_history(self):

        # setup data
        agent = baker.make_recipe("agents.agent")
        history = baker.make("agents.AgentHistory", agent=agent, _quantity=30)
        url = f"{base_url}/{agent.agent_id}/history/"

        # test agent not found
        r = self.client.get(f"{base_url}/{agent.agent_id}123/history/", format="json")
        self.assertEqual(r.status_code, 404)

        # test pulling data
        r = self.client.get(url, format="json")
        ctx = {"default_tz": pytz.timezone("America/Los_Angeles")}
        data = AgentHistorySerializer(history, many=True, context=ctx).data
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, data)  # type:ignore


class TestAgentViewsNew(TacticalTestCase):
    def setUp(self):
        self.authenticate()
        self.setup_coresettings()

    def test_agent_maintenance_mode(self):
        url = f"{base_url}/maintenance/bulk/"

        # setup data
        agent = baker.make_recipe("agents.agent")

        # Test client toggle maintenance mode
        data = {"type": "Client", "id": agent.site.client.id, "action": True}  # type: ignore

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Agent.objects.get(pk=agent.pk).maintenance_mode)

        # Test site toggle maintenance mode
        data = {"type": "Site", "id": agent.site.id, "action": False}  # type: ignore

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Agent.objects.get(pk=agent.pk).maintenance_mode)

        # Test invalid payload
        data = {"type": "Invalid", "id": agent.id, "action": True}

        r = self.client.post(url, data, format="json")
        self.assertEqual(r.status_code, 400)

        self.check_not_authenticated("post", url)


class TestAgentPermissions(TacticalTestCase):
    def setUp(self):
        self.client_setup()
        self.setup_coresettings()

    def test_list_agents_permissions(self):
        # create user with empty role
        user = self.create_user_with_roles([])
        self.client.force_authenticate(user=user)  # type: ignore

        url = f"{base_url}/"

        sites = baker.make("clients.Site", _quantity=5)
        agents = baker.make_recipe("agents.agent", site=cycle(sites), _quantity=10)

        # test getting all agents

        # user with empty role should fail
        self.check_not_authorized("get", url)

        # add can_list_agents roles and should succeed
        user.role.can_list_agents = True
        user.role.save()

        # all agents should be returned
        response = self.check_authorized("get", url)
        self.assertEqual(len(response.data), 10)  # type: ignore

        # limit user to specific client. only 1 agent should be returned
        user.role.can_view_clients.set([agents[4].client])
        response = self.check_authorized("get", url)
        self.assertEqual(len(response.data), 2)  # type: ignore

        # limit agent to specific site. 2 should be returned now
        user.role.can_view_sites.set([agents[6].site])
        response = self.check_authorized("get", url)
        self.assertEqual(len(response.data), 4)  # type: ignore

        # make sure superusers work
        self.check_authorized_superuser("get", url)

    @patch("agents.models.Agent.nats_cmd")
    @patch("agents.views.reload_nats")
    def test_get_edit_uninstall_permissions(self, reload_nats, nats_cmd):
        # create user with empty role
        user = self.create_user_with_roles([])
        self.client.force_authenticate(user=user)  # type: ignore

        agent = baker.make_recipe("agents.agent")
        methods = ["get", "put", "delete"]
        url = f"{base_url}/{agent.agent_id}/"

        # test user with no roles
        for method in methods:
            self.check_not_authorized(method, url)

        # add correct roles for view edit and delete
        user.role.can_list_agents = True
        user.role.can_edit_agent = True
        user.role.can_uninstall_agents = True
        user.role.save()

        for method in methods:
            self.check_authorized(method, url)

        # test limiting users to clients and sites
        sites = baker.make("clients.Site", _quantity=5)
        agents = baker.make_recipe("agents.agent", site=cycle(sites), _quantity=10)

        # limit to client
        user.role.can_view_clients.set([agents[5].client])

        for method in methods:
            self.check_not_authorized(method, f"{base_url}/{agents[6].agent_id}/")
            self.check_authorized(method, f"{base_url}/{agents[5].agent_id}/")

        # limit to site
        user.role.can_view_clients.clear()
        user.role.can_view_sites.set([agents[1].site, agents[7].site])

        for method in methods:
            self.check_not_authorized(method, f"{base_url}/{agents[4].agent_id}/")
            self.check_authorized(method, f"{base_url}/{agents[1].agent_id}/")

        # limit both client and site
        user.role.can_view_clients.set([agents[0].client])

        for method in methods:
            self.check_not_authorized(method, f"{base_url}/{agents[9].agent_id}/")
            self.check_authorized(method, f"{base_url}/{agents[0].agent_id}/")

        # make sure superusers work
        for method in methods:
            self.check_authorized_superuser(method, f"{base_url}/{agents[9].agent_id}/")

    @patch("time.sleep")
    @patch("agents.models.Agent.nats_cmd", return_value="ok")
    def test_agent_actions_permissions(self, nats_cmd, sleep):

        agent = baker.make_recipe("agents.agent")
        unauthorized_agent = baker.make_recipe("agents.agent")

        test_data = [
            {"method": "post", "action": "cmd", "role": "can_send_cmd"},
            {"method": "post", "action": "runscript", "role": "can_run_scripts"},
            {"method": "post", "action": "wmi", "role": "can_edit_agent"},
            {"method": "post", "action": "recover", "role": "can_recover_agents"},
            {"method": "post", "action": "reboot", "role": "can_reboot_agents"},
            {"method": "patch", "action": "reboot", "role": "can_reboot_agents"},
            {"method": "get", "action": "ping", "role": "can_ping_agents"},
            {"method": "get", "action": "meshcentral", "role": "can_use_mesh"},
            {"method": "post", "action": "meshcentral/recover", "role": "can_use_mesh"},
            {"method": "get", "action": "processes", "role": "can_manage_procs"},
            {"method": "delete", "action": "processes/1", "role": "can_manage_procs"},
            {
                "method": "get",
                "action": "eventlog/Application/30",
                "role": "can_view_eventlogs",
            },
        ]

        for test in test_data:
            url = f"{base_url}/{agent.agent_id}/{test['action']}/"

            # test superuser access
            self.check_authorized_superuser(test["method"], url)

            user = self.create_user_with_roles([])
            self.client.force_authenticate(user=user)  # type: ignore

            # test user without role
            self.check_not_authorized(test["method"], url)

            # add user to role and test
            setattr(user.role, test["role"], True)
            user.role.save()

            self.check_authorized(test["method"], url)
            self.check_authorized(
                test["method"],
                f"{base_url}/{unauthorized_agent.agent_id}/{test['action']}/",
            )

            # limit user to client
            user.role.can_view_clients.set([agent.client])
            self.check_authorized(test["method"], url)
            self.check_not_authorized(
                test["method"],
                f"{base_url}/{unauthorized_agent.agent_id}/{test['action']}/",
            )

    def test_agent_maintenance_permissions(self):
        site = baker.make("clients.Site")
        client = baker.make("clients.Client")

        site_data = {"id": site.id, "type": "Site", "action": True}

        client_data = {"id": client.id, "type": "Client", "action": True}

        url = f"{base_url}/maintenance/bulk/"

        # test superuser access
        self.check_authorized_superuser("post", url, site_data)
        self.check_authorized_superuser("post", url, client_data)

        user = self.create_user_with_roles([])
        self.client.force_authenticate(user=user)  # type: ignore

        # test user without role
        self.check_not_authorized("post", url, site_data)
        self.check_not_authorized("post", url, client_data)

        # add user to role and test
        user.role.can_edit_agent = True
        user.role.save()

        self.check_authorized("post", url, site_data)
        self.check_authorized("post", url, client_data)

        # limit user to client
        user.role.can_view_clients.set([client])
        self.check_not_authorized("post", url, site_data)
        self.check_authorized("post", url, client_data)

        # also limit to site
        user.role.can_view_sites.set([site])
        self.check_authorized("post", url, site_data)
        self.check_authorized("post", url, client_data)

    @patch("agents.tasks.send_agent_update_task.delay")
    def test_agent_update_permissions(self, update_task):
        agents = baker.make_recipe("agents.agent", _quantity=5)
        other_agents = baker.make_recipe("agents.agent", _quantity=7)

        url = f"{base_url}/update/"

        data = {
            "agent_ids": [agent.agent_id for agent in agents]
            + [agent.agent_id for agent in other_agents]
        }

        # test superuser access
        self.check_authorized_superuser("post", url, data)
        update_task.assert_called_with(agent_ids=data["agent_ids"])
        update_task.reset_mock()

        user = self.create_user_with_roles([])
        self.client.force_authenticate(user=user)  # type: ignore

        self.check_not_authorized("post", url, data)
        update_task.assert_not_called()

        user.role.can_update_agents = True
        user.role.save()

        self.check_authorized("post", url, data)
        update_task.assert_called_with(agent_ids=data["agent_ids"])
        update_task.reset_mock()

        # limit to client
        user.role.can_view_clients.set([agents[0].client])
        self.check_authorized("post", url, data)
        update_task.assert_called_with(agent_ids=[agent.agent_id for agent in agents])
        update_task.reset_mock()

        # add site
        user.role.can_view_sites.set([other_agents[0].site])
        self.check_authorized("post", url, data)
        update_task.assert_called_with(agent_ids=data["agent_ids"])
        update_task.reset_mock()

        # remove client permissions
        user.role.can_view_clients.clear()
        self.check_authorized("post", url, data)
        update_task.assert_called_with(
            agent_ids=[agent.agent_id for agent in other_agents]
        )

    def test_get_agent_version_permissions(self):
        agents = baker.make_recipe("agents.agent", _quantity=5)
        other_agents = baker.make_recipe("agents.agent", _quantity=7)

        url = f"{base_url}/versions/"

        # test superuser access
        response = self.check_authorized_superuser("get", url)
        self.assertEqual(len(response.data["agents"]), 12)  # type: ignore

        user = self.create_user_with_roles([])
        self.client.force_authenticate(user=user)  # type: ignore

        self.check_not_authorized("get", url)

        user.role.can_list_agents = True
        user.role.save()

        response = self.check_authorized("get", url)
        self.assertEqual(len(response.data["agents"]), 12)  # type: ignore

        # limit to client
        user.role.can_view_clients.set([agents[0].client])
        response = self.check_authorized("get", url)
        self.assertEqual(len(response.data["agents"]), 5)  # type: ignore

        # add site
        user.role.can_view_sites.set([other_agents[0].site])
        response = self.check_authorized("get", url)
        self.assertEqual(len(response.data["agents"]), 12)  # type: ignore

        # remove client permissions
        user.role.can_view_clients.clear()
        response = self.check_authorized("get", url)
        self.assertEqual(len(response.data["agents"]), 7)  # type: ignore

    def test_generating_agent_installer_permissions(self):

        client = baker.make("clients.Client")
        client_site = baker.make("clients.Site", client=client)
        site = baker.make("clients.Site")

        url = f"{base_url}/installer/"

        # test superuser access
        self.check_authorized_superuser("post", url)

        user = self.create_user_with_roles([])
        self.client.force_authenticate(user=user)  # type: ignore

        self.check_not_authorized("post", url)

        user.role.can_install_agents = True
        user.role.save()

        self.check_authorized("post", url)

        # limit user to client
        user.role.can_view_clients.set([client])

        data = {
            "client": client.id,
            "site": client_site.id,
            "version": settings.LATEST_AGENT_VER,
            "arch": "64",
        }

        self.check_authorized("post", url, data)

        data = {
            "client": site.client.id,
            "site": site.id,
            "version": settings.LATEST_AGENT_VER,
            "arch": "64",
        }

        self.check_not_authorized("post", url, data)

        # assign site
        user.role.can_view_clients.clear()
        user.role.can_view_sites.set([site])
        data = {
            "client": site.client.id,
            "site": site.id,
            "version": settings.LATEST_AGENT_VER,
            "arch": "64",
        }

        self.check_authorized("post", url, data)

        data = {
            "client": client.id,
            "site": client_site.id,
            "version": settings.LATEST_AGENT_VER,
            "arch": "64",
        }

        self.check_not_authorized("post", url, data)

    def test_agent_notes_permissions(self):

        agent = baker.make_recipe("agents.agent")
        notes = baker.make("agents.Note", agent=agent, _quantity=5)

        unauthorized_agent = baker.make_recipe("agents.agent")
        unauthorized_notes = baker.make(
            "agents.Note", agent=unauthorized_agent, _quantity=7
        )

        test_data = [
            {"url": f"{base_url}/notes/", "method": "get", "role": "can_list_notes"},
            {"url": f"{base_url}/notes/", "method": "post", "role": "can_manage_notes"},
            {
                "url": f"{base_url}/notes/{notes[0].id}/",
                "method": "get",
                "role": "can_list_notes",
            },
            {
                "url": f"{base_url}/notes/{notes[0].id}/",
                "method": "put",
                "role": "can_manage_notes",
            },
            {
                "url": f"{base_url}/notes/{notes[0].id}/",
                "method": "delete",
                "role": "can_manage_notes",
            },
        ]

        # check superuser access, user with no roles access, and with with roles access
        for test in test_data:
            self.check_authorized_superuser(test["method"], test["url"])

            user = self.create_user_with_roles([])
            self.client.force_authenticate(user=user)  # type: ignore
            self.check_not_authorized(test["method"], test["url"])

            setattr(user.role, test["role"], True)
            user.role.save()
            self.check_authorized(test["method"], test["url"])

        # test limiting user to clients and sites
        user = self.create_user_with_roles(["can_list_notes", "can_manage_notes"])
        user.role.can_view_sites.set([agent.site])
        user.role.save()
        self.client.force_authenticate(user=user)  # type: ignore

        authorized_data = {"note": "Test not here", "agent_id": agent.agent_id}

        unauthorized_data = {
            "note": "Test note here",
            "agent_id": unauthorized_agent.agent_id,
        }

        # should only return the 4 allowed agent notes (one got deleted above in loop)
        r = self.client.get(f"{base_url}/notes/")
        self.assertEqual(len(r.data), 4)  # type: ignore

        # test with agent_id in url
        self.check_authorized("get", f"{base_url}/{agent.agent_id}/notes/")
        self.check_not_authorized(
            "get", f"{base_url}/{unauthorized_agent.agent_id}/notes/"
        )

        # test post get, put, and delete and make sure unauthorized is returned with unauthorized agent and works for authorized
        self.check_authorized("post", f"{base_url}/notes/", authorized_data)
        self.check_not_authorized("post", f"{base_url}/notes/", unauthorized_data)
        self.check_authorized("get", f"{base_url}/notes/{notes[2].id}/")
        self.check_not_authorized(
            "get", f"{base_url}/notes/{unauthorized_notes[2].id}/"
        )
        self.check_authorized(
            "put", f"{base_url}/notes/{notes[3].id}/", authorized_data
        )
        self.check_not_authorized(
            "put", f"{base_url}/notes/{unauthorized_notes[3].id}/", unauthorized_data
        )
        self.check_authorized("delete", f"{base_url}/notes/{notes[3].id}/")
        self.check_not_authorized(
            "delete", f"{base_url}/notes/{unauthorized_notes[3].id}/"
        )

    def test_get_agent_history_permissions(self):
        # create user with empty role
        user = self.create_user_with_roles([])
        self.client.force_authenticate(user=user)  # type: ignore

        sites = baker.make("clients.Site", _quantity=2)
        agent = baker.make_recipe("agents.agent", site=sites[0])
        history = baker.make("agents.AgentHistory", agent=agent, _quantity=5)
        unauthorized_agent = baker.make_recipe("agents.agent", site=sites[1])
        unauthorized_history = baker.make(
            "agents.AgentHistory", agent=unauthorized_agent, _quantity=6
        )

        url = f"{base_url}/history/"
        authorized_url = f"{base_url}/{agent.agent_id}/history/"
        unauthorized_url = f"{base_url}/{unauthorized_agent.agent_id}/history/"

        # test getting all agents

        # user with empty role should fail
        self.check_not_authorized("get", url)
        self.check_not_authorized("get", authorized_url)
        self.check_not_authorized("get", unauthorized_url)

        # add can_list_agents roles and should succeed
        user.role.can_list_agent_history = True
        user.role.save()

        # all agents should be returned
        r = self.check_authorized("get", url)
        self.check_authorized("get", authorized_url)
        self.check_authorized("get", unauthorized_url)
        self.assertEqual(len(r.data), 11)  # type: ignore

        # limit user to specific client.
        user.role.can_view_clients.set([agent.client])
        self.check_authorized("get", authorized_url)
        self.check_not_authorized("get", unauthorized_url)
        r = self.check_authorized("get", url)
        self.assertEqual(len(r.data), 5)  # type: ignore

        # make sure superusers work
        self.check_authorized_superuser("get", url)
        self.check_authorized_superuser("get", authorized_url)
        self.check_authorized_superuser("get", unauthorized_url)


class TestAgentTasks(TacticalTestCase):
    def setUp(self):
        self.authenticate()
        self.setup_coresettings()

    @patch("agents.utils.get_winagent_url")
    @patch("agents.models.Agent.nats_cmd")
    def test_agent_update(self, nats_cmd, get_url):
        get_url.return_value = "https://exe.tacticalrmm.io"

        from agents.tasks import agent_update

        agent_noarch = baker.make_recipe(
            "agents.agent",
            operating_system="Error getting OS",
            version=settings.LATEST_AGENT_VER,
        )
        r = agent_update(agent_noarch.agent_id)
        self.assertEqual(r, "noarch")

        agent_130 = baker.make_recipe(
            "agents.agent",
            operating_system="Windows 10 Pro, 64 bit (build 19041.450)",
            version="1.3.0",
        )
        r = agent_update(agent_130.agent_id)
        self.assertEqual(r, "not supported")

        # test __without__ code signing
        agent64_nosign = baker.make_recipe(
            "agents.agent",
            operating_system="Windows 10 Pro, 64 bit (build 19041.450)",
            version="1.4.14",
        )

        r = agent_update(agent64_nosign.agent_id)
        self.assertEqual(r, "created")
        action = PendingAction.objects.get(agent__agent_id=agent64_nosign.agent_id)
        self.assertEqual(action.action_type, "agentupdate")
        self.assertEqual(action.status, "pending")
        self.assertEqual(
            action.details["url"],
            f"https://github.com/wh1te909/rmmagent/releases/download/v{settings.LATEST_AGENT_VER}/winagent-v{settings.LATEST_AGENT_VER}.exe",
        )
        self.assertEqual(
            action.details["inno"], f"winagent-v{settings.LATEST_AGENT_VER}.exe"
        )
        self.assertEqual(action.details["version"], settings.LATEST_AGENT_VER)
        nats_cmd.assert_called_with(
            {
                "func": "agentupdate",
                "payload": {
                    "url": f"https://github.com/wh1te909/rmmagent/releases/download/v{settings.LATEST_AGENT_VER}/winagent-v{settings.LATEST_AGENT_VER}.exe",
                    "version": settings.LATEST_AGENT_VER,
                    "inno": f"winagent-v{settings.LATEST_AGENT_VER}.exe",
                },
            },
            wait=False,
        )

        # test __with__ code signing (64 bit)
        """ codesign = baker.make("core.CodeSignToken", token="testtoken123")
        agent64_sign = baker.make_recipe(
            "agents.agent",
            operating_system="Windows 10 Pro, 64 bit (build 19041.450)",
            version="1.4.14",
        )

        nats_cmd.return_value = "ok"
        get_exe.return_value = "https://exe.tacticalrmm.io"
        r = agent_update(agent64_sign.pk, codesign.token)  # type: ignore
        self.assertEqual(r, "created")
        nats_cmd.assert_called_with(
            {
                "func": "agentupdate",
                "payload": {
                    "url": f"https://exe.tacticalrmm.io/api/v1/winagents/?version={settings.LATEST_AGENT_VER}&arch=64&token=testtoken123",  # type: ignore
                    "version": settings.LATEST_AGENT_VER,
                    "inno": f"winagent-v{settings.LATEST_AGENT_VER}.exe",
                },
            },
            wait=False,
        )
        action = PendingAction.objects.get(agent__pk=agent64_sign.pk)
        self.assertEqual(action.action_type, "agentupdate")
        self.assertEqual(action.status, "pending")

        # test __with__ code signing (32 bit)
        agent32_sign = baker.make_recipe(
            "agents.agent",
            operating_system="Windows 10 Pro, 32 bit (build 19041.450)",
            version="1.4.14",
        )

        nats_cmd.return_value = "ok"
        get_exe.return_value = "https://exe.tacticalrmm.io"
        r = agent_update(agent32_sign.pk, codesign.token)  # type: ignore
        self.assertEqual(r, "created")
        nats_cmd.assert_called_with(
            {
                "func": "agentupdate",
                "payload": {
                    "url": f"https://exe.tacticalrmm.io/api/v1/winagents/?version={settings.LATEST_AGENT_VER}&arch=32&token=testtoken123",  # type: ignore
                    "version": settings.LATEST_AGENT_VER,
                    "inno": f"winagent-v{settings.LATEST_AGENT_VER}-x86.exe",
                },
            },
            wait=False,
        )
        action = PendingAction.objects.get(agent__pk=agent32_sign.pk)
        self.assertEqual(action.action_type, "agentupdate")
        self.assertEqual(action.status, "pending") """

    @patch("agents.tasks.agent_update")
    @patch("agents.tasks.sleep", return_value=None)
    def test_auto_self_agent_update_task(self, mock_sleep, agent_update):
        baker.make_recipe(
            "agents.agent",
            operating_system="Windows 10 Pro, 64 bit (build 19041.450)",
            version=settings.LATEST_AGENT_VER,
            _quantity=23,
        )
        baker.make_recipe(
            "agents.agent",
            operating_system="Windows 10 Pro, 64 bit (build 19041.450)",
            version="1.3.0",
            _quantity=33,
        )

        self.coresettings.agent_auto_update = False
        self.coresettings.save(update_fields=["agent_auto_update"])

        r = auto_self_agent_update_task.s().apply()
        self.assertEqual(agent_update.call_count, 0)

        self.coresettings.agent_auto_update = True
        self.coresettings.save(update_fields=["agent_auto_update"])

        r = auto_self_agent_update_task.s().apply()
        self.assertEqual(agent_update.call_count, 33)

    def test_agent_history_prune_task(self):
        from .tasks import prune_agent_history

        # setup data
        agent = baker.make_recipe("agents.agent")
        history = baker.make(
            "agents.AgentHistory",
            agent=agent,
            _quantity=50,
        )

        days = 0
        for item in history:  # type: ignore
            item.time = djangotime.now() - djangotime.timedelta(days=days)
            item.save()
            days = days + 5

        # delete AgentHistory older than 30 days
        prune_agent_history(30)

        self.assertEqual(AgentHistory.objects.filter(agent=agent).count(), 6)
