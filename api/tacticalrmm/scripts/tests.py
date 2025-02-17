import json
import os
import hmac
import hashlib

from pathlib import Path
from unittest.mock import patch

from django.test import override_settings
from django.conf import settings
from model_bakery import baker
from tacticalrmm.test import TacticalTestCase

from .models import Script, ScriptSnippet
from .serializers import (
    ScriptSerializer,
    ScriptTableSerializer,
    ScriptSnippetSerializer,
)


class TestScriptViews(TacticalTestCase):
    def setUp(self):
        self.setup_coresettings()
        self.authenticate()

    def test_get_scripts(self):
        url = "/scripts/"
        scripts = baker.make("scripts.Script", _quantity=3)

        serializer = ScriptTableSerializer(scripts, many=True)
        resp = self.client.get(url, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(serializer.data, resp.data)  # type: ignore

        self.check_not_authenticated("get", url)

    @override_settings(SECRET_KEY="Test Secret Key")
    def test_add_script(self):
        url = f"/scripts/"

        data = {
            "name": "Name",
            "description": "Description",
            "shell": "powershell",
            "category": "New",
            "script_body": "Test Script",
            "default_timeout": 99,
            "args": ["hello", "world", r"{{agent.public_ip}}"],
            "favorite": False,
        }

        # test without file upload
        resp = self.client.post(url, data, format="json")
        self.assertEqual(resp.status_code, 200)

        new_script = Script.objects.filter(name="Name").get()
        self.assertTrue(new_script)

        correct_hash = hmac.new(
            settings.SECRET_KEY.encode(), data["script_body"].encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(new_script.script_hash, correct_hash)

        self.check_not_authenticated("post", url)

    @override_settings(SECRET_KEY="Test Secret Key")
    def test_modify_script(self):
        # test a call where script doesn't exist
        resp = self.client.put("/scripts/500/", format="json")
        self.assertEqual(resp.status_code, 404)

        # make a userdefined script
        script = baker.make_recipe("scripts.script")
        url = f"/scripts/{script.pk}/"

        data = {
            "name": script.name,
            "description": "Description Change",
            "shell": script.shell,
            "script_body": "Test Script Body",  # Test
            "default_timeout": 13344556,
        }

        # test edit a userdefined script
        resp = self.client.put(url, data, format="json")
        self.assertEqual(resp.status_code, 200)
        script = Script.objects.get(pk=script.pk)
        self.assertEquals(script.description, "Description Change")

        correct_hash = hmac.new(
            settings.SECRET_KEY.encode(), data["script_body"].encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(script.script_hash, correct_hash)

        # test edit a builtin script
        data = {
            "name": "New Name",
            "description": "New Desc",
            "script_body": "aasdfdsf",
        }  # Test
        builtin_script = baker.make_recipe("scripts.script", script_type="builtin")

        resp = self.client.put(f"/scripts/{builtin_script.pk}/", data, format="json")
        self.assertEqual(resp.status_code, 400)

        data = {
            "name": script.name,
            "description": "Description Change",
            "shell": script.shell,
            "favorite": True,
            "script_body": "Test Script Body",  # Test
            "default_timeout": 54345,
        }
        # test marking a builtin script as favorite
        resp = self.client.put(f"/scripts/{builtin_script.pk}/", data, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Script.objects.get(pk=builtin_script.pk).favorite)

        self.check_not_authenticated("put", url)

    def test_get_script(self):
        # test a call where script doesn't exist
        resp = self.client.get("/scripts/500/", format="json")
        self.assertEqual(resp.status_code, 404)

        script = baker.make("scripts.Script")
        url = f"/scripts/{script.pk}/"  # type: ignore
        serializer = ScriptSerializer(script)
        resp = self.client.get(url, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(serializer.data, resp.data)  # type: ignore

        self.check_not_authenticated("get", url)

    @patch("agents.models.Agent.nats_cmd", return_value="return value")
    def test_test_script(self, run_script):
        agent = baker.make_recipe("agents.agent")
        url = f"/scripts/{agent.agent_id}/test/"

        data = {
            "code": "some_code",
            "timeout": 90,
            "args": [],
            "shell": "powershell",
        }

        resp = self.client.post(url, data, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, "return value")  # type: ignore

        self.check_not_authenticated("post", url)

    def test_delete_script(self):
        # test a call where script doesn't exist
        resp = self.client.delete("/scripts/500/", format="json")
        self.assertEqual(resp.status_code, 404)

        # test delete script
        script = baker.make_recipe("scripts.script")
        url = f"/scripts/{script.pk}/"
        resp = self.client.delete(url, format="json")
        self.assertEqual(resp.status_code, 200)

        self.assertFalse(Script.objects.filter(pk=script.pk).exists())

        # test delete community script
        script = baker.make_recipe("scripts.script", script_type="builtin")
        url = f"/scripts/{script.pk}/"
        resp = self.client.delete(url, format="json")
        self.assertEqual(resp.status_code, 400)

        self.check_not_authenticated("delete", url)

    def test_download_script(self):
        # test a call where script doesn't exist
        resp = self.client.get("/scripts/500/download/", format="json")
        self.assertEqual(resp.status_code, 404)

        # return script code property should be "Test"

        # test powershell file
        script = baker.make(
            "scripts.Script", script_body="Test Script Body", shell="powershell"
        )
        url = f"/scripts/{script.pk}/download/"  # type: ignore

        resp = self.client.get(url, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"filename": f"{script.name}.ps1", "code": "Test Script Body"})  # type: ignore

        # test batch file
        script = baker.make(
            "scripts.Script", script_body="Test Script Body", shell="cmd"
        )
        url = f"/scripts/{script.pk}/download/"  # type: ignore

        resp = self.client.get(url, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"filename": f"{script.name}.bat", "code": "Test Script Body"})  # type: ignore

        # test python file
        script = baker.make(
            "scripts.Script", script_body="Test Script Body", shell="python"
        )
        url = f"/scripts/{script.pk}/download/"  # type: ignore

        resp = self.client.get(url, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"filename": f"{script.name}.py", "code": "Test Script Body"})  # type: ignore

        self.check_not_authenticated("get", url)

    def test_community_script_json_file(self):
        valid_shells = ["powershell", "python", "cmd"]

        if not settings.DOCKER_BUILD:
            scripts_dir = os.path.join(Path(settings.BASE_DIR).parents[1], "scripts")
        else:
            scripts_dir = settings.SCRIPTS_DIR

        with open(
            os.path.join(settings.BASE_DIR, "scripts/community_scripts.json")
        ) as f:
            info = json.load(f)

        guids = []
        for script in info:
            fn: str = script["filename"]
            self.assertTrue(os.path.exists(os.path.join(scripts_dir, fn)))
            self.assertTrue(script["filename"])
            self.assertTrue(script["name"])
            self.assertTrue(script["description"])
            self.assertTrue(script["shell"])
            self.assertIn(script["shell"], valid_shells)

            if fn.endswith(".ps1"):
                self.assertEqual(script["shell"], "powershell")
            elif fn.endswith(".bat"):
                self.assertEqual(script["shell"], "cmd")
            elif fn.endswith(".py"):
                self.assertEqual(script["shell"], "python")

            if "args" in script.keys():
                self.assertIsInstance(script["args"], list)

            # allows strings as long as they can be type casted to int
            if "default_timeout" in script.keys():
                self.assertIsInstance(int(script["default_timeout"]), int)

            self.assertIn("guid", script.keys())
            guids.append(script["guid"])

        # check guids are unique
        self.assertEqual(len(guids), len(set(guids)))

    def test_load_community_scripts(self):
        with open(
            os.path.join(settings.BASE_DIR, "scripts/community_scripts.json")
        ) as f:
            info = json.load(f)

        Script.load_community_scripts()

        community_scripts_count = Script.objects.filter(script_type="builtin").count()
        if len(info) != community_scripts_count:
            raise Exception(
                f"There are {len(info)} scripts in json file but only {community_scripts_count} in database"
            )

        # test updating already added community scripts
        Script.load_community_scripts()
        community_scripts_count2 = Script.objects.filter(script_type="builtin").count()
        self.assertEqual(len(info), community_scripts_count2)

    def test_community_script_has_jsonfile_entry(self):
        with open(
            os.path.join(settings.BASE_DIR, "scripts/community_scripts.json")
        ) as f:
            info = json.load(f)

        filenames = [i["filename"] for i in info]

        # normal
        if not settings.DOCKER_BUILD:
            scripts_dir = os.path.join(Path(settings.BASE_DIR).parents[1], "scripts")
        # docker
        else:
            scripts_dir = settings.SCRIPTS_DIR

        with os.scandir(scripts_dir) as it:
            for f in it:
                if not f.name.startswith(".") and f.is_file():
                    if f.name not in filenames:
                        raise Exception(
                            f"{f.name} is missing an entry in community_scripts.json"
                        )

    def test_script_filenames_do_not_contain_spaces(self):
        with open(
            os.path.join(settings.BASE_DIR, "scripts/community_scripts.json")
        ) as f:
            info = json.load(f)
            for script in info:
                fn: str = script["filename"]
                if " " in fn:
                    raise Exception(f"{fn} must not contain spaces in filename")

    def test_script_arg_variable_replacement(self):

        agent = baker.make_recipe("agents.agent", public_ip="12.12.12.12")
        args = [
            "-Parameter",
            "-Another {{agent.public_ip}}",
            "-Client {{client.name}}",
            "-Site {{site.name}}",
        ]

        self.assertEqual(
            [
                "-Parameter",
                "-Another '12.12.12.12'",
                f"-Client '{agent.client.name}'",
                f"-Site '{agent.site.name}'",
            ],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

    def test_script_arg_replacement_custom_field(self):
        agent = baker.make_recipe("agents.agent")
        field = baker.make(
            "core.CustomField",
            name="Test Field",
            model="agent",
            type="text",
            default_value_string="DEFAULT",
        )

        args = ["-Parameter", "-Another {{agent.Test Field}}"]

        # test default value
        self.assertEqual(
            ["-Parameter", "-Another 'DEFAULT'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

        # test with set value
        baker.make(
            "agents.AgentCustomField",
            field=field,
            agent=agent,
            string_value="CUSTOM VALUE",
        )
        self.assertEqual(
            ["-Parameter", "-Another 'CUSTOM VALUE'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

    def test_script_arg_replacement_client_custom_fields(self):
        agent = baker.make_recipe("agents.agent")
        field = baker.make(
            "core.CustomField",
            name="Test Field",
            model="client",
            type="text",
            default_value_string="DEFAULT",
        )

        args = ["-Parameter", "-Another {{client.Test Field}}"]

        # test default value
        self.assertEqual(
            ["-Parameter", "-Another 'DEFAULT'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

        # test with set value
        baker.make(
            "clients.ClientCustomField",
            field=field,
            client=agent.client,
            string_value="CUSTOM VALUE",
        )
        self.assertEqual(
            ["-Parameter", "-Another 'CUSTOM VALUE'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

    def test_script_arg_replacement_site_custom_fields(self):
        agent = baker.make_recipe("agents.agent")
        field = baker.make(
            "core.CustomField",
            name="Test Field",
            model="site",
            type="text",
            default_value_string="DEFAULT",
        )

        args = ["-Parameter", "-Another {{site.Test Field}}"]

        # test default value
        self.assertEqual(
            ["-Parameter", "-Another 'DEFAULT'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

        # test with set value
        value = baker.make(
            "clients.SiteCustomField",
            field=field,
            site=agent.site,
            string_value="CUSTOM VALUE",
        )
        self.assertEqual(
            ["-Parameter", "-Another 'CUSTOM VALUE'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

        # test with set but empty field value
        value.string_value = ""  # type: ignore
        value.save()  # type: ignore

        self.assertEqual(
            ["-Parameter", "-Another 'DEFAULT'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

        # test blank default and value
        field.default_value_string = ""  # type: ignore
        field.save()  # type: ignore

        self.assertEqual(
            ["-Parameter", "-Another ''"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

    def test_script_arg_replacement_array_fields(self):
        agent = baker.make_recipe("agents.agent")
        field = baker.make(
            "core.CustomField",
            name="Test Field",
            model="agent",
            type="multiple",
            default_values_multiple=["this", "is", "an", "array"],
        )

        args = ["-Parameter", "-Another {{agent.Test Field}}"]

        # test default value
        self.assertEqual(
            ["-Parameter", "-Another 'this,is,an,array'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

        # test with set value and python shell
        baker.make(
            "agents.AgentCustomField",
            field=field,
            agent=agent,
            multiple_value=["this", "is", "new"],
        )
        self.assertEqual(
            ["-Parameter", "-Another 'this,is,new'"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

    def test_script_arg_replacement_boolean_fields(self):
        agent = baker.make_recipe("agents.agent")
        field = baker.make(
            "core.CustomField",
            name="Test Field",
            model="agent",
            type="checkbox",
            default_value_bool=True,
        )

        args = ["-Parameter", "-Another {{agent.Test Field}}"]

        # test default value with python
        self.assertEqual(
            ["-Parameter", "-Another 1"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

        # test with set value and python shell
        custom = baker.make(
            "agents.AgentCustomField",
            field=field,
            agent=agent,
            bool_value=False,
        )
        self.assertEqual(
            ["-Parameter", "-Another 0"],
            Script.parse_script_args(agent=agent, shell="python", args=args),
        )

        # test with set value and cmd shell
        self.assertEqual(
            ["-Parameter", "-Another 0"],
            Script.parse_script_args(agent=agent, shell="cmd", args=args),
        )

        # test with set value and powershell
        self.assertEqual(
            ["-Parameter", "-Another $False"],
            Script.parse_script_args(agent=agent, shell="powershell", args=args),
        )

        # test with True value powershell
        custom.bool_value = True  # type: ignore
        custom.save()  # type: ignore

        self.assertEqual(
            ["-Parameter", "-Another $True"],
            Script.parse_script_args(agent=agent, shell="powershell", args=args),
        )


class TestScriptSnippetViews(TacticalTestCase):
    def setUp(self):
        self.setup_coresettings()
        self.authenticate()

    def test_get_script_snippets(self):
        url = "/scripts/snippets/"
        snippets = baker.make("scripts.ScriptSnippet", _quantity=3)

        serializer = ScriptSnippetSerializer(snippets, many=True)
        resp = self.client.get(url, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(serializer.data, resp.data)  # type: ignore

        self.check_not_authenticated("get", url)

    def test_add_script_snippet(self):
        url = f"/scripts/snippets/"

        data = {
            "name": "Name",
            "description": "Description",
            "shell": "powershell",
            "code": "Test",
        }

        resp = self.client.post(url, data, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(ScriptSnippet.objects.filter(name="Name").exists())

        self.check_not_authenticated("post", url)

    def test_modify_script_snippet(self):
        # test a call where script doesn't exist
        resp = self.client.put("/scripts/snippets/500/", format="json")
        self.assertEqual(resp.status_code, 404)

        # make a userdefined script
        snippet = baker.make("scripts.ScriptSnippet", name="Test")
        url = f"/scripts/snippets/{snippet.pk}/"  # type: ignore

        data = {"name": "New Name"}  # type: ignore

        resp = self.client.put(url, data, format="json")
        self.assertEqual(resp.status_code, 200)
        snippet = ScriptSnippet.objects.get(pk=snippet.pk)  # type: ignore
        self.assertEquals(snippet.name, "New Name")

        self.check_not_authenticated("put", url)

    def test_get_script_snippet(self):
        # test a call where script doesn't exist
        resp = self.client.get("/scripts/snippets/500/", format="json")
        self.assertEqual(resp.status_code, 404)

        snippet = baker.make("scripts.ScriptSnippet")
        url = f"/scripts/snippets/{snippet.pk}/"  # type: ignore
        serializer = ScriptSnippetSerializer(snippet)
        resp = self.client.get(url, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(serializer.data, resp.data)  # type: ignore

        self.check_not_authenticated("get", url)

    def test_delete_script_snippet(self):
        # test a call where script doesn't exist
        resp = self.client.delete("/scripts/snippets/500/", format="json")
        self.assertEqual(resp.status_code, 404)

        # test delete script snippet
        snippet = baker.make("scripts.ScriptSnippet")
        url = f"/scripts/snippets/{snippet.pk}/"  # type: ignore
        resp = self.client.delete(url, format="json")
        self.assertEqual(resp.status_code, 200)

        self.assertFalse(ScriptSnippet.objects.filter(pk=snippet.pk).exists())  # type: ignore

        self.check_not_authenticated("delete", url)

    def test_snippet_replacement(self):

        snippet1 = baker.make(
            "scripts.ScriptSnippet", name="snippet1", code="Snippet 1 Code"
        )
        snippet2 = baker.make(
            "scripts.ScriptSnippet", name="snippet2", code="Snippet 2 Code"
        )

        test_no_snippet = "No Snippets Here"
        test_with_snippet = "Snippet 1: {{snippet1}}\nSnippet 2: {{snippet2}}"

        # test putting snippet in text
        result = Script.replace_with_snippets(test_with_snippet)
        self.assertEqual(
            result,
            f"Snippet 1: {snippet1.code}\nSnippet 2: {snippet2.code}",  # type:ignore
        )

        # test text with no snippets
        result = Script.replace_with_snippets(test_no_snippet)
        self.assertEqual(result, test_no_snippet)
