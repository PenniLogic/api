import importlib.util
import hashlib
from contextlib import chdir
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

SPEC = importlib.util.spec_from_file_location("quality", Path(__file__).resolve().parents[1] / "quality.py")
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)


class QualityNodeBridgeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="api-node-bridge-test-"))).resolve()
        self.sdk = self.root / "approved sdk with spaces"
        self.sdk.mkdir()
        self.node = self.sdk / ("node.exe" if os.name == "nt" else "node")
        self.node.write_bytes(b"inert validation-only fixture")
        self.npm = self.sdk / "node_modules/npm/bin/npm-cli.js"
        self.npm.parent.mkdir(parents=True)
        self.npm.write_bytes(b"inert paired npm fixture")
        self.interop = quality.script_module("money_client_interop")
        self.validate = self.enterContext(patch.object(
            self.interop, "node_runtime", wraps=self.interop.node_runtime,
        ))
        self.mutation = Mock()

        def module(name):
            self.assertIn(name, ("money_client_interop", "money_mutation"))
            return self.interop if name == "money_client_interop" else self.mutation

        self.loader = self.enterContext(patch.object(quality, "script_module", side_effect=module))
        self.output = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.errors = self.enterContext(patch("sys.stderr", new_callable=io.StringIO))

    def invoke(self, command, *arguments):
        with patch.object(sys, "argv", ["quality.py", command, *map(str, arguments)]):
            return quality.main()

    def test_gradle_preserves_one_unsplit_property_after_compiler_option_before_tasks(self):
        for selected in (None, str(self.node)):
            with self.subTest(explicit=selected is not None), patch.object(quality, "run", return_value="observed") as run:
                options = {} if selected is None else {"money_client_interop_node": selected}
                self.assertEqual("observed", quality.gradle(
                    "build", "installDist", root=self.root, capture=True, budget=17, **options,
                ))
                wrapper = self.root / ("gradlew.bat" if os.name == "nt" else "gradlew")
                command = [str(wrapper)] if os.name == "nt" else ["sh", str(wrapper)]
                expected = [*command, "--no-daemon", "--console=plain", "-Pkotlin.compiler.execution.strategy=in-process"]
                if selected is not None:
                    expected.append("-PmoneyClientInteropNode=" + selected)
                self.assertEqual(call([*expected, "build", "installDist"], self.root, True, 17), run.call_args)
        self.loader.assert_not_called()

    def test_every_gradle_dispatcher_and_self_test_receive_the_validated_path(self):
        tasks = {
            "version": ("--version",), "install": ("resolveDependencies",),
            "build": ("build", "installDist"), "test": ("test",),
            "lint": ("spotlessCheck", "compileKotlin", "compileTestKotlin"), "format": ("spotlessApply",),
            "coverage": ("jacocoTestReport", "jacocoTestCoverageVerification", "moneyCoverageReport", "moneyMutation"),
            "money-coverage": ("moneyCoverageReport",), "money-mutation": ("moneyMutation",),
        }
        with (
            patch.object(quality, "gradle") as gradle,
            patch.object(quality, "run"),
            patch.object(quality, "test_metrics"),
            patch.object(quality, "check_coverage"),
            patch.object(quality, "check_money_coverage"),
            patch.object(quality, "money_provider_module"),
            patch.object(quality, "money_budget", return_value={"enforced_seconds": 600}),
            patch.object(quality, "gate_self_test") as self_test,
        ):
            for command, expected in tasks.items():
                with self.subTest(command=command):
                    gradle.reset_mock()
                    self.validate.reset_mock()
                    self.assertEqual(0, self.invoke(
                        command, "--base", "a" * 40, "--money-client-interop-node", self.node,
                    ))
                    self.validate.assert_called_once_with(self.node)
                    self.assertEqual(expected, gradle.call_args.args)
                    options = gradle.call_args.kwargs
                    self.assertEqual(str(self.node), options["money_client_interop_node"])
                    if command in ("build", "coverage", "money-mutation"):
                        self.assertEqual({"money_client_interop_node", "budget"}, set(options))
                        self.assertGreater(options["budget"], 0)
                        self.assertLessEqual(options["budget"], 600)
                    else:
                        self.assertEqual({"money_client_interop_node"}, set(options))
            self.assertEqual(0, self.invoke(
                "gate-self-test", "--artifact-dir", self.root, "--money-client-interop-node", self.node,
            ))
            self_test.assert_called_once_with(self.root, money_client_interop_node=str(self.node))

    def test_omitted_option_keeps_existing_dispatch_without_loading_runtime_validation(self):
        with patch.object(quality, "gradle") as gradle, patch.object(quality, "gate_self_test") as self_test:
            self.assertEqual(0, self.invoke("install"))
            gradle.assert_called_once_with("resolveDependencies")
            self.assertEqual(0, self.invoke("gate-self-test", "--artifact-dir", self.root))
            self_test.assert_called_once_with(self.root)
        self.loader.assert_not_called()
        self.validate.assert_not_called()

    def test_invalid_explicit_path_and_missing_npm_refuse_without_executing_any_candidate(self):
        wrong_name = self.sdk / "other-runtime"
        wrong_name.write_bytes(b"inert wrong-basename fixture")
        invalid = (Path(self.node.name), self.root / "missing" / self.node.name, self.sdk, wrong_name)
        with patch.object(quality, "gradle") as gradle, patch.object(subprocess, "Popen") as popen:
            for path in invalid:
                with self.subTest(path_kind=path.name):
                    self.assertEqual(1, self.invoke("install", "--money-client-interop-node", path))
                    self.assertIn("Quality command failed: node-missing", self.errors.getvalue())
            self.npm.unlink()
            is_file = Path.is_file
            with patch.object(Path, "is_file", autospec=True, side_effect=lambda path: (
                path.is_relative_to(self.sdk) and is_file(path)
            )):
                self.assertEqual(1, self.invoke("install", "--money-client-interop-node", self.node))
                self.assertIn("Quality command failed: npm-missing", self.errors.getvalue())
            gradle.assert_not_called()
            popen.assert_not_called()
        self.assertEqual(self.node.read_bytes(), b"inert validation-only fixture")

    def test_non_gradle_commands_reject_the_option_without_runtime_or_process_work(self):
        with patch.object(quality, "gradle") as gradle, patch.object(quality, "run") as run:
            for command in ("money-guard", "money-coverage-report", "money-mutation-report"):
                with self.subTest(command=command), self.assertRaises(SystemExit) as error:
                    self.invoke(command, "--money-client-interop-node", self.node)
                self.assertEqual(2, error.exception.code)
            self.assertIn("--money-client-interop-node requires a Gradle-producing command", self.errors.getvalue())
            gradle.assert_not_called()
            run.assert_not_called()
        self.loader.assert_not_called()

    def test_unsafe_windows_sdk_refuses_every_cli_branch_before_other_work(self):
        commands = (
            "version", "install", "build", "test", "lint", "format", "coverage",
            "money-coverage", "money-mutation", "gate-self-test",
        )
        with (
            patch.object(quality, "os", SimpleNamespace(name="nt")),
            patch.object(quality, "gradle") as gradle,
            patch.object(quality, "run") as run,
            patch.object(quality, "gate_self_test") as self_test,
            patch.object(quality, "money_provider_module") as provider,
            patch.object(subprocess, "Popen") as popen,
        ):
            for name in ("SDK&extra&", "SDK%TEMP%", "SDK!TEMP!", "SDK^literal", "SDK(group)", "SDK & spaces"):
                sdk = self.root / name
                sdk.mkdir()
                node = sdk / self.node.name
                node.write_bytes(self.node.read_bytes())
                npm = sdk / "node_modules/npm/bin/npm-cli.js"
                npm.parent.mkdir(parents=True)
                npm.write_bytes(self.npm.read_bytes())
                for command in commands:
                    with self.subTest(path=name, command=command):
                        self.assertEqual(1, self.invoke(
                            command, "--base", "a" * 40, "--artifact-dir", self.root,
                            "--money-client-interop-node", node,
                        ))
                        self.assertIn("Windows Gradle batch operands contain unsupported shell characters",
                                      self.errors.getvalue())
            gradle.assert_not_called()
            run.assert_not_called()
            self_test.assert_not_called()
            provider.assert_not_called()
            popen.assert_not_called()

    def test_all_nine_self_test_calls_keep_root_path_tasks_and_all_seven_refusals(self):
        real_gradle = quality.gradle
        for selected in (None, str(self.node)):
            calls = []

            def planted_result(*tasks, root, capture=False, **options):
                calls.append((tasks, root, capture, options))
                self.assertNotEqual(root, quality.ROOT)
                self.assertEqual(self.root, root.parent)
                self.assertEqual({} if selected is None else {"money_client_interop_node": selected}, options)
                with patch.object(quality, "run", return_value="") as run:
                    real_gradle(*tasks, root=root, capture=capture, **options)
                    run.assert_called_once()
                    self.assertEqual(root, run.call_args.args[1])
                tests = root / "src/test/kotlin/com/pennilogic/bootstrap"
                if (tests / "GateFailureTest.kt").exists():
                    report = root / "build/test-results/test/TEST-com.pennilogic.bootstrap.GateFailureTest.xml"
                    report.parent.mkdir(parents=True, exist_ok=True)
                    report.write_text(
                        '<testsuite tests="1" failures="1"><testcase>'
                        '<failure type="org.opentest4j.AssertionFailedError"/></testcase></testsuite>', encoding="utf-8",
                    )
                    raise subprocess.CalledProcessError(1, ["synthetic-gradle", *tasks])
                if (tests / "GateLint.kt").exists():
                    raise subprocess.CalledProcessError(1, ["synthetic-gradle", *tasks],
                                                        output="GateLint.kt spotlessKotlinCheck")
                defect = tests / "GateMoney.kt"
                if defect.exists():
                    text = defect.read_text(encoding="utf-8")
                    rule = "MG003" if "toDouble" in text else ("MG002" if "amount + fee" in text else "MG001")
                    raise subprocess.CalledProcessError(1, ["synthetic-gradle", *tasks],
                                                        output="GateMoney.kt " + rule + " :moneyGuard FAILED")

            options = {} if selected is None else {"money_client_interop_node": selected}
            with self.subTest(explicit=selected is not None), patch.object(quality, "gradle", side_effect=planted_result):
                quality.gate_self_test(self.root, **options)
            self.assertEqual([("test", "spotlessCheck"), ("test",)] + [("build",)] * 7,
                             [tasks for tasks, _, _, _ in calls])
            self.assertEqual([False, False] + [True] * 6 + [False], [capture for _, _, capture, _ in calls])
            self.assertEqual(1, len({root for _, root, _, _ in calls}))
            self.assertFalse(calls[0][1].exists())
        self.assertEqual(14, sum('"rejected":true' in line.replace(" ", "")
                                 for line in self.output.getvalue().splitlines()))

    def test_direct_self_test_refuses_an_unsafe_sdk_before_the_first_wrapper(self):
        with (
            patch.object(quality, "os", SimpleNamespace(name="nt")),
            patch.object(quality, "run") as run,
            self.assertRaisesRegex(ValueError, "Windows Gradle batch operands"),
        ):
            quality.gate_self_test(self.root, money_client_interop_node=self.root / "SDK%TEMP%" / "node.exe")
        run.assert_not_called()
        self.assertFalse(list(self.root.glob("api-gate-self-test-*")))

    def test_owned_baseline_candidate_roots_and_repeated_invocations_cannot_share_node_or_report_state(self):
        modules, reports = [], {}
        for name in ("baseline", "candidate"):
            root = self.root / name
            for filename in ("quality.py", "money_client_interop.py", "process_budget.py", "materialize_money_sources.py"):
                target = root / "scripts" / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((quality.ROOT / "scripts" / filename).read_bytes())
            report = root / "build/money-client-interop/backend-launch.json"
            report.parent.mkdir(parents=True)
            reports[report] = json.dumps({"synthetic_owner": name}).encode()
            report.write_bytes(reports[report])
            spec = importlib.util.spec_from_file_location("node_bridge_" + name, root / "scripts/quality.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
        for module in modules:
            with patch.object(module, "run", return_value="") as run:
                with patch.object(sys, "argv", ["quality.py", "install", "--money-client-interop-node", str(self.node)]):
                    self.assertEqual(0, module.main())
                explicit = run.call_args
                self.assertEqual(module.ROOT, explicit.args[1])
                self.assertIn(str(module.ROOT / ("gradlew.bat" if os.name == "nt" else "gradlew")), explicit.args[0])
                self.assertIn("-PmoneyClientInteropNode=" + str(self.node), explicit.args[0])
                with patch.object(sys, "argv", ["quality.py", "install"]):
                    self.assertEqual(0, module.main())
                self.assertFalse(any(argument.startswith("-PmoneyClientInteropNode=") for argument in run.call_args.args[0]))
        self.assertNotEqual(modules[0].ROOT, modules[1].ROOT)
        self.assertEqual(reports, {path: path.read_bytes() for path in reports})


class QualityWindowsBatchBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="api-batch-boundary-"))).resolve()
        self.enterContext(patch.object(quality, "os", SimpleNamespace(name="nt")))
        self.run = self.enterContext(patch.object(quality, "run", return_value="observed"))
        self.popen = self.enterContext(patch.object(subprocess, "Popen"))

    def test_cmd_expansion_is_refused_before_dispatch(self):
        for value in ("SDK&extra&", "SDK%TEMP%", "SDK%%TEMP%%", "SDK!TEMP!", "SDK!!TEMP!!",
                      "SDK^literal", "SDK^&extra^&", "SDK(group)", "SDK & spaces"):
            for budget in (None, 10):
                with self.subTest(value=value, budget=budget), self.assertRaisesRegex(
                    ValueError, "^Windows Gradle batch operands contain unsupported shell characters$",
                ):
                    quality.gradle("test", root=self.root, capture=True, budget=budget,
                                   money_client_interop_node=self.root / value / "node.exe")
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_every_shell_character_and_control_in_any_operand_refuses_without_dispatch(self):
        for character in '"%!^&|<>()' + "".join(map(chr, range(32))) + chr(127):
            for field in ("wrapper", "node", "task"):
                for capture in (False, True):
                    for budget in (None, 10):
                        root = self.root / ("checkout" + character + "path") if field == "wrapper" else self.root
                        node = self.root / ("SDK" + character + "path") / "node.exe" if field == "node" else None
                        task = "task" + character + "data" if field == "task" else "test"
                        with self.subTest(character=repr(character), field=field, capture=capture, budget=budget):
                            with self.assertRaisesRegex(ValueError, "Windows Gradle batch operands"):
                                quality.gradle(task, root=root, capture=capture, budget=budget,
                                               money_client_interop_node=node)
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_ordinary_spaces_and_non_cmd_neighbors_keep_exact_argv(self):
        root = self.root / "checkout with spaces $[]{};,+@=~'`-"
        node = root / "approved sdk with spaces" / "node.exe"
        for selected in (None, node):
            for budget in (None, 10):
                with self.subTest(explicit=selected is not None, budget=budget):
                    self.assertEqual("observed", quality.gradle(
                        "test", "installDist", root=root, capture=True, budget=budget,
                        money_client_interop_node=selected,
                    ))
                    expected = [str(root / "gradlew.bat"), "--no-daemon", "--console=plain",
                                "-Pkotlin.compiler.execution.strategy=in-process"]
                    if selected is not None:
                        expected.append("-PmoneyClientInteropNode=" + str(node))
                    self.assertEqual(call([*expected, "test", "installDist"], root, True, budget), self.run.call_args)
        self.popen.assert_not_called()

    def test_relative_windows_roots_cannot_hide_unsafe_effective_directory(self):
        unsafe = self.root / "checkout&relative-marker&"
        child = unsafe / "child"
        child.mkdir(parents=True)
        roots = [(Path("."), unsafe), (Path("child") / "..", unsafe), (Path(".."), child)]
        if os.name == "nt":
            roots.append((Path(unsafe.drive + "."), unsafe))
        for root, cwd in roots:
            for capture in (False, True):
                for budget in (None, 10):
                    with self.subTest(root=root, capture=capture, budget=budget), chdir(cwd):
                        with self.assertRaisesRegex(
                            ValueError, "^Windows Gradle batch operands contain unsupported shell characters$",
                        ):
                            quality.gradle("test", root=root, capture=capture, budget=budget)
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_relative_windows_roots_use_the_same_absolute_wrapper_and_working_directory(self):
        root = self.root / "checkout with ordinary spaces"
        child = root / "child"
        child.mkdir(parents=True)
        roots = [(Path("."), root), (Path("child") / "..", root), (Path(".."), child)]
        if os.name == "nt":
            roots.append((Path(root.drive + "."), root))
        for relative, cwd in roots:
            for capture in (False, True):
                for budget in (None, 10):
                    with self.subTest(root=relative, capture=capture, budget=budget), chdir(cwd):
                        self.assertEqual("observed", quality.gradle("test", root=relative, capture=capture, budget=budget))
                    self.assertEqual(call(
                        [str(root / "gradlew.bat"), "--no-daemon", "--console=plain",
                         "-Pkotlin.compiler.execution.strategy=in-process", "test"],
                        root, capture, budget,
                    ), self.run.call_args)
        self.popen.assert_not_called()

    def test_effective_wrapper_target_is_checked_as_well_as_working_directory(self):
        wrapper = self.root / "gradlew.bat"
        resolve = Path.resolve

        def resolved(path, *args, **kwargs):
            return self.root / "target%TEMP%" / "gradlew.bat" if path == wrapper else resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=resolved):
            with self.assertRaisesRegex(ValueError, "Windows Gradle batch operands"):
                quality.gradle("test", root=self.root)
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_posix_sh_keeps_literal_metacharacters_without_windows_restrictions(self):
        root = Path("relative & percent% bang! caret^ (group)") / ".." / "checkout"
        node = root / "SDK & spaces" / "node"
        with (
            patch.object(quality, "os", SimpleNamespace(name="posix")),
            patch.object(Path, "resolve", side_effect=AssertionError("POSIX wrapper operands must remain literal")),
        ):
            self.assertEqual("observed", quality.gradle(
                "task&data", root=root, capture=True, budget=10, money_client_interop_node=node,
            ))
        self.assertEqual(call(
            ["sh", str(root / "gradlew"), "--no-daemon", "--console=plain",
             "-Pkotlin.compiler.execution.strategy=in-process", "-PmoneyClientInteropNode=" + str(node), "task&data"],
            root, True, 10,
        ), self.run.call_args)


@unittest.skipUnless(os.name == "nt", "Native Windows batch argument boundary")
class QualityWindowsBatchNativeTest(unittest.TestCase):
    def test_relative_unsafe_checkout_is_refused_before_the_unchanged_wrapper_expands_it(self):
        wrapper = (quality.ROOT / "gradlew.bat").read_bytes()
        helper = quality.script_module("process_budget")
        with tempfile.TemporaryDirectory(prefix="api-relative-wrapper-") as temporary:
            parent = Path(temporary)
            unsafe = parent / "checkout&relative-marker&"
            child = unsafe / "child"
            child.mkdir(parents=True)
            (unsafe / "gradlew.bat").write_bytes(wrapper)
            (unsafe / "relative-marker.cmd").write_text(
                "@echo off\n> unexpected.marker echo unexpected-command\nexit /b 0\n", encoding="ascii",
            )
            environment = helper.environment({"JAVA_HOME": str(parent / "absent-jdk")})
            roots = [(Path("."), unsafe), (Path("child") / "..", unsafe),
                     (Path(".."), child), (Path(unsafe.drive + "."), unsafe)]
            with patch.dict(os.environ, environment, clear=True):
                for root, cwd in roots:
                    for capture in (False, True):
                        for budget in (None, 10):
                            with self.subTest(root=root, capture=capture, budget=budget), chdir(cwd):
                                with self.assertRaisesRegex(
                                    ValueError, "^Windows Gradle batch operands contain unsupported shell characters$",
                                ):
                                    quality.gradle("--version", root=root, capture=capture, budget=budget)
                            self.assertFalse((unsafe / "unexpected.marker").exists())
            self.assertEqual(wrapper, (unsafe / "gradlew.bat").read_bytes())

    def test_actual_cli_and_direct_dispatch_refuse_before_any_wrapper_or_extra_marker(self):
        helper = quality.script_module("process_budget")
        with tempfile.TemporaryDirectory(prefix="api-batch-native-") as temporary:
            parent = Path(temporary)
            sdk = parent / "approved sdk with spaces"
            sdk.mkdir()
            node = sdk / "node.exe"
            node.write_bytes(b"inert validation-only fixture; never executed")
            npm = sdk / "node_modules/npm/bin/npm-cli.js"
            npm.parent.mkdir(parents=True)
            npm.write_bytes(b"inert paired npm fixture")
            for root_name in ("checkout with ordinary spaces", "checkout&sec06-extra&", "checkout%TEMP%",
                              "checkout!TEMP!", "checkout^literal", "checkout(group)"):
                root = parent / root_name
                (root / "scripts").mkdir(parents=True)
                for filename in ("quality.py", "money_client_interop.py", "process_budget.py", "materialize_money_sources.py"):
                    (root / "scripts" / filename).write_bytes((quality.ROOT / "scripts" / filename).read_bytes())
                (root / "gradlew.bat").write_text(
                    "@echo off\n> gradle-reached.marker echo wrapper-reached\nexit /b 0\n", encoding="ascii",
                )
                (root / "sec06-extra.cmd").write_text(
                    "@echo off\n> sec06-extra.marker echo unexpected-command\nexit /b 0\n", encoding="ascii",
                )
                for caller in ("cli", "budgeted", "unbudgeted"):
                    with self.subTest(root=root_name, caller=caller):
                        unsafe = root_name != "checkout with ordinary spaces"
                        if caller == "cli":
                            result = helper.run(
                                [sys.executable, "-I", "-S", "-B", str(root / "scripts/quality.py"), "install",
                                 "--money-client-interop-node", str(node)],
                                root, 10, capture=True,
                            )
                            self.assertEqual(1 if unsafe else 0, result.returncode)
                            if unsafe:
                                self.assertIn("Windows Gradle batch operands", result.stdout)
                        else:
                            with patch("sys.stdout", new_callable=io.StringIO):
                                if unsafe:
                                    with self.assertRaisesRegex(ValueError, "Windows Gradle batch operands"):
                                        quality.gradle("test", root=root, capture=True,
                                                       budget=10 if caller == "budgeted" else None,
                                                       money_client_interop_node=node)
                                else:
                                    quality.gradle("test", root=root, capture=True,
                                                   budget=10 if caller == "budgeted" else None,
                                                   money_client_interop_node=node)
                        reached = root / "gradle-reached.marker"
                        self.assertEqual(not unsafe, reached.exists())
                        self.assertFalse((root / "sec06-extra.marker").exists())
                        if reached.exists():
                            self.assertEqual("wrapper-reached", reached.read_text().strip())
                            reached.unlink()
            self.assertEqual(b"inert validation-only fixture; never executed", node.read_bytes())


class GateSelfTestAdmissionCopyTest(unittest.TestCase):
    def test_real_temp_copy_preserves_inventory_and_verifies_admission_before_the_first_gradle_call(self):
        class CopyVerified(Exception):
            pass

        calls = []

        def inspect_copy(*tasks, root, **_kwargs):
            calls.append(tasks)
            self.assertEqual(("test", "spotlessCheck"), tasks)
            self.assertNotEqual(quality.ROOT, root)
            for name in (
                "scripts/prepare_database_admission.py", "scripts/materialize_money_sources.py",
                "src/main/resources/database-admission-installation.json",
                "database/admission-inventory.json",
            ):
                self.assertEqual(
                    hashlib.sha256((quality.ROOT / name).read_bytes()).hexdigest(),
                    hashlib.sha256((root / name).read_bytes()).hexdigest(),
                    "the self-test copy changed a required inventory or pinned installation file",
                )
            inventory = json.loads((root / "database/admission-inventory.json").read_text(encoding="utf-8"))
            columns = [column for script in inventory["scripts"] for column in script["columns"]]
            self.assertEqual(70, len(columns))
            self.assertEqual(70, len({column["name"]: column["sql_type"] for column in columns}))

            def snapshot(directory):
                return {
                    path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in directory.rglob("*") if path.is_file()
                }

            self.assertEqual(snapshot(quality.ROOT / "build/database-admission"), snapshot(root / "build/database-admission"))
            before = snapshot(root)
            environment = {
                key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}
            }
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(root / "scripts/prepare_database_admission.py"), "verify"],
                cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
            )
            self.assertEqual(0, result.returncode, "the real copied installation must verify without a fetch")
            self.assertFalse(result.stderr, "offline verification emitted diagnostics")
            self.assertEqual(
                {"event": "database_admission_installation", "status": "verified", "payloads": 5},
                json.loads(result.stdout),
            )
            self.assertEqual(before, snapshot(root), "offline verification mutated the self-test copy")
            raise CopyVerified

        with tempfile.TemporaryDirectory(prefix="api-admission-copy-test-") as temporary:
            with patch.object(quality, "gradle", side_effect=inspect_copy), self.assertRaises(CopyVerified):
                quality.gate_self_test(Path(temporary))
            self.assertEqual([], list(Path(temporary).iterdir()))
        self.assertEqual([("test", "spotlessCheck")], calls)


class CoverageGateTest(unittest.TestCase):
    def counters(self, covered=8, total=10):
        return {kind: {"covered": covered, "total": total} for kind in ("LINE", "BRANCH")}

    def test_exact_floors_pass(self):
        quality.enforce(self.counters(), [True] * 9 + [False])

    def test_each_repository_floor_rejects_undercoverage(self):
        for kind in ("LINE", "BRANCH"):
            with self.subTest(kind=kind):
                counters = self.counters()
                counters[kind]["covered"] = 7
                with self.assertRaisesRegex(ValueError, kind):
                    quality.enforce(counters, [True])

    def test_changed_floor_cannot_be_hidden_by_high_repository_coverage(self):
        with self.assertRaisesRegex(ValueError, "Changed"):
            quality.enforce(self.counters(10), [True] * 8 + [False] * 2)

    def test_no_changed_executable_lines_is_valid(self):
        quality.enforce(self.counters(), [])

    def test_regression_fails_even_above_floor(self):
        with self.assertRaisesRegex(ValueError, "decreased"):
            quality.enforce(self.counters(9), [True], self.counters(10))

    def test_fraction_comparison_does_not_round_away_regression(self):
        with self.assertRaisesRegex(ValueError, "decreased"):
            quality.enforce(self.counters(90000, 100001), [], self.counters(9))

    def test_invalid_counters_fail_closed(self):
        for covered, total in [(0, 0), (-1, 10), (11, 10), (8.0, 10), (True, 1)]:
            with self.subTest(covered=covered, total=total), self.assertRaises(ValueError):
                quality.enforce(self.counters(covered, total), [])

    def test_diff_handles_additions_deletions_and_single_line_hunks(self):
        diff = "@@ -2,0 +3,2 @@\n+x\n+y\n@@ -8 +10 @@\n-x\n+y\n@@ -14,2 +15,0 @@\n-x\n-y\n"
        self.assertEqual({3, 4, 10}, quality.changed_line_numbers(diff))

    def test_report_requires_real_line_and_branch_counters(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.xml"
            path.write_text('<report><counter type="LINE" missed="0" covered="10"/></report>')
            with self.assertRaisesRegex(ValueError, "BRANCH"):
                quality.read_report(path)

    def test_report_selects_executable_lines_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.xml"
            path.write_text(
                '<report><package name="example"><sourcefile name="App.kt">'
                '<line nr="1" mi="0" ci="2"/><line nr="2" mi="1" ci="0"/>'
                '<line nr="3" mi="0" ci="0"/></sourcefile></package>'
                '<counter type="LINE" missed="1" covered="1"/>'
                '<counter type="BRANCH" missed="1" covered="1"/></report>'
            )
            counters, lines = quality.read_report(path)
            self.assertEqual({"covered": 1, "total": 2}, counters["LINE"])
            self.assertEqual({1: True, 2: False}, lines["src/main/kotlin/example/App.kt"])

    def test_coverage_requires_full_explicit_base(self):
        for base in [None, "", "main", "--help", "a" * 39]:
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, "--base"):
                quality.check_coverage(base)

    @patch("sys.stdout", new_callable=io.StringIO)
    def test_missing_or_skipped_junit_tests_fail_closed(self, output):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                quality.test_metrics(root)
            reports = root / "build/test-results/test"
            reports.mkdir(parents=True)
            for counts in [
                'tests="1" skipped="1" failures="0" errors="0"',
                'tests="1" skipped="0" failures="1" errors="0"',
                'tests="1" skipped="0" failures="0" errors="1"',
            ]:
                (reports / "TEST-sample.xml").write_text(f"<testsuite {counts}/>")
                with self.subTest(counts=counts), self.assertRaises(ValueError):
                    quality.test_metrics(root)
            (reports / "TEST-sample.xml").write_text(
                '<testsuite tests="1" skipped="0" failures="0" errors="0"/>'
            )
            quality.test_metrics(root)
            self.assertEqual(
                {"event": "tests", "count": 1, "skipped": 0, "failed": 0},
                json.loads(output.getvalue().splitlines()[-1]),
            )

    def test_missing_source_in_report_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/main/kotlin/example"
            source.mkdir(parents=True)
            (source / "Missing.kt").write_text("package example\nclass Missing\n")
            with (
                patch.object(quality, "ROOT", root),
                patch.object(quality, "run", return_value=""),
                patch.object(quality, "read_report", return_value=(self.counters(), {})),
                self.assertRaisesRegex(ValueError, "inventory"),
            ):
                quality.check_coverage("a" * 40)


class CoverageRefusalDiagnosticsTest(unittest.TestCase):
    base = "a" * 40

    def fixture(self, root, counters, baseline):
        source = root / "src/main/kotlin/example/App.kt"
        source.parent.mkdir(parents=True)
        source.write_text("package example\nclass App\n", encoding="utf-8")
        report = root / quality.REPORT
        report.parent.mkdir(parents=True)
        lines = "".join(
            f'<line nr="{number + 1}" mi="{int(number >= counters["LINE"]["covered"])}" '
            f'ci="{int(number < counters["LINE"]["covered"])}"/>'
            for number in range(counters["LINE"]["total"])
        )
        totals = "".join(
            f'<counter type="{kind}" covered="{value["covered"]}" missed="{value["total"] - value["covered"]}"/>'
            for kind, value in counters.items()
        )
        report.write_text(
            f'<report><package name="example"><sourcefile name="App.kt">{lines}</sourcefile></package>{totals}</report>',
            encoding="utf-8",
        )
        baseline_file = root / quality.BASELINE
        baseline_file.parent.mkdir(parents=True)
        baseline_file.write_text(json.dumps(baseline) + "\n", encoding="utf-8")
        return baseline_file

    def git(self, baseline):
        def run(command, **_kwargs):
            if command == ["git", "cat-file", "-e", f"{self.base}^{{commit}}"]:
                return ""
            if command == ["git", "ls-tree", "-r", "--name-only", self.base]:
                return quality.BASELINE
            if command == ["git", "show", f"{self.base}:{quality.BASELINE}"]:
                return json.dumps(baseline)
            raise AssertionError("Unexpected Git operation in synthetic coverage fixture")
        return run

    def test_each_refused_guard_emits_actual_fixture_counters_without_success_or_baseline_write(self):
        for line, branch, reviewed, reason in [
            (7, 10, 8, "LINE coverage is below 80 percent"),
            (10, 7, 8, "BRANCH coverage is below 80 percent"),
            (9, 10, 10, "LINE coverage decreased from reviewed base"),
            (10, 9, 10, "BRANCH coverage decreased from reviewed base"),
            (8, 10, 8, "Changed executable line coverage is below 90 percent"),
        ]:
            counters = {"LINE": {"covered": line, "total": 10}, "BRANCH": {"covered": branch, "total": 10}}
            baseline = {kind: {"covered": reviewed, "total": 10} for kind in counters}
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = self.fixture(root, counters, baseline)
                original = target.read_bytes()
                with (
                    patch.object(quality, "ROOT", root),
                    patch.object(quality, "run", side_effect=self.git(baseline)),
                    patch("sys.stdout", new_callable=io.StringIO) as output,
                    self.assertRaisesRegex(ValueError, reason),
                ):
                    quality.check_coverage(self.base, write_baseline=True)
                self.assertEqual(original, target.read_bytes())
                self.assertEqual([{
                    "event": "coverage_refused", "reason": reason,
                    "counters": counters, "reviewed_counters": baseline,
                    "changed_covered": line, "changed_total": 10, "base": self.base,
                }], [json.loads(record) for record in output.getvalue().splitlines()])

    def test_passing_ratchets_keep_the_existing_event_before_baseline_equality_refusal(self):
        counters = {kind: {"covered": 10, "total": 10} for kind in ("LINE", "BRANCH")}
        baseline = {kind: {"covered": 9, "total": 10} for kind in counters}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.fixture(root, counters, baseline)
            original = target.read_bytes()
            with (
                patch.object(quality, "ROOT", root),
                patch.object(quality, "run", side_effect=self.git(baseline)),
                patch("sys.stdout", new_callable=io.StringIO) as output,
                self.assertRaisesRegex(ValueError, "baseline does not match"),
            ):
                quality.check_coverage(self.base)
            self.assertEqual(original, target.read_bytes())
            self.assertEqual([{
                "event": "coverage", "counters": counters, "changed_covered": 10,
                "changed_total": 10, "base": self.base,
            }], [json.loads(record) for record in output.getvalue().splitlines()])

    def test_matching_baseline_keeps_success_event_and_does_not_emit_a_refusal(self):
        counters = {kind: {"covered": 10, "total": 10} for kind in ("LINE", "BRANCH")}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.fixture(root, counters, counters)
            original = target.read_bytes()
            with (
                patch.object(quality, "ROOT", root),
                patch.object(quality, "run", side_effect=self.git(counters)),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                quality.check_coverage(self.base)
            self.assertEqual(original, target.read_bytes())
            self.assertEqual(["coverage"], [json.loads(record)["event"] for record in output.getvalue().splitlines()])


class MoneyCoverageQualificationTest(unittest.TestCase):
    def report(self, directory, classes=("Money", "CurrencyRegistry"), sources=("Money.kt", "CurrencyRegistry.kt")):
        class_nodes = "".join(f'<class name="{quality.MONEY_PACKAGE}/{name}"/>' for name in classes)
        source_nodes = "".join(f'<sourcefile name="{name}"/>' for name in sources)
        counters = (
            '<counter type="LINE" missed="3" covered="97"/>'
            '<counter type="BRANCH" missed="7" covered="93"/>'
        )
        path = Path(directory) / "money.xml"
        path.write_text(
            f'<report><package name="{quality.MONEY_PACKAGE}">{class_nodes}{source_nodes}'
            f'{counters}</package>{counters}</report>', encoding="utf-8",
        )
        return path

    def test_complete_authoritative_class_and_source_inventories_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.report(temporary)
            classes = {f"{quality.MONEY_PACKAGE}/{name}" for name in ("Money", "CurrencyRegistry")}
            sources = {"Money.kt", "CurrencyRegistry.kt"}
            counters = quality.read_money_coverage(path, classes, sources)
            self.assertEqual({"covered": 97, "total": 100}, counters["LINE"])
            self.assertEqual({"covered": 93, "total": 100}, counters["BRANCH"])
            for missing in classes:
                with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "class inventory"):
                    quality.read_money_coverage(path, classes - {missing}, sources)
            for missing in sources:
                with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "source inventory"):
                    quality.read_money_coverage(path, classes, sources - {missing})

    def test_unrelated_package_or_disagreeing_aggregate_cannot_mask_money_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self.report(temporary)
            original = path.read_text(encoding="utf-8")
            classes = {f"{quality.MONEY_PACKAGE}/{name}" for name in ("Money", "CurrencyRegistry")}
            sources = {"Money.kt", "CurrencyRegistry.kt"}
            path.write_text(original.replace(quality.MONEY_PACKAGE, "unrelated/package"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "authoritative package"):
                quality.read_money_coverage(path, classes, sources)
            path.write_text(original.replace('missed="3"', 'missed="0"', 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "counters differ"):
                quality.read_money_coverage(path, classes, sources)

    def test_money_thresholds_come_from_the_bound_strategy_not_local_defaults(self):
        class SyntheticProvider:
            BUNDLE = Path("synthetic")
            STRATEGY_FILE = Path("test-strategy.json")

            def __init__(self, package):
                self.package = package

            def read_strategy(self, _):
                return json.dumps({
                    "packages": [self.package], "floor_policy": {"money_path_ratchet": True},
                }).encode()

        package = {
            "id": "api.money", "repository": "PenniLogic/api", "money_path": True,
            "line_coverage_floor_percent": 97, "branch_coverage_floor_percent": 93,
            "mutation_score_floor_percent": 90,
        }
        floors, _ = quality.money_policy(SyntheticProvider(package))
        self.assertEqual({"LINE": 97, "BRANCH": 93, "MUTATION": 90}, floors)
        altered = {**package, "line_coverage_floor_percent": 99}
        self.assertEqual(99, quality.money_policy(SyntheticProvider(altered))[0]["LINE"])
        for invalid in (None, 0, 101, True, "97"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "numeric"):
                quality.money_policy(SyntheticProvider({**package, "line_coverage_floor_percent": invalid}))

    def test_exact_money_floors_pass_and_each_one_below_fails(self):
        floors = {"LINE": 97, "BRANCH": 93, "MUTATION": 90}
        counters = {
            "LINE": {"covered": 97, "total": 100},
            "BRANCH": {"covered": 93, "total": 100},
        }
        quality.enforce_money_coverage(counters, floors)
        for kind in ("LINE", "BRANCH"):
            insufficient = {**counters, kind: {"covered": counters[kind]["covered"] - 1, "total": 100}}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, kind):
                quality.enforce_money_coverage(insufficient, floors)

    def test_money_ratchet_refuses_a_decline_even_above_the_declared_floor(self):
        floors = {"LINE": 97, "BRANCH": 93, "MUTATION": 90}
        baseline = {"LINE": {"covered": 100, "total": 100}, "BRANCH": {"covered": 99, "total": 100}}
        unchanged = {"LINE": {"covered": 100, "total": 100}, "BRANCH": {"covered": 99, "total": 100}}
        quality.enforce_money_coverage(unchanged, floors, baseline)
        for kind in ("LINE", "BRANCH"):
            declined = {**unchanged, kind: {"covered": baseline[kind]["covered"] - 1, "total": 100}}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "decreased"):
                quality.enforce_money_coverage(declined, floors, baseline)


class FullMoneyCommandTest(unittest.TestCase):
    base = "37fc85503994fd03f2f41ab5bfd87191db6d9b56"
    tasks = {
        "build": ("build", "installDist"),
        "coverage": ("jacocoTestReport", "jacocoTestCoverageVerification", "moneyCoverageReport", "moneyMutation"),
        "money-mutation": ("moneyMutation",),
    }

    def setUp(self):
        self.clock = 1000
        self.calls = Mock()
        self.real_check_coverage = quality.check_coverage
        self.real_check_money_coverage = quality.check_money_coverage
        for name in ("gradle", "test_metrics", "check_coverage", "check_money_coverage"):
            self.enterContext(patch.object(quality, name, getattr(self.calls, name)))
        self.loader = self.enterContext(patch.object(
            quality, "script_module", return_value=Mock(check_latest=self.calls.check_latest),
        ))
        self.provider = self.enterContext(patch.object(quality, "money_provider_module"))
        self.budget = self.enterContext(patch.object(quality, "money_budget", return_value={"enforced_seconds": 600}))
        self.enterContext(patch.object(quality.time, "monotonic", side_effect=lambda: self.clock))
        self.output = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.errors = self.enterContext(patch("sys.stderr", new_callable=io.StringIO))

    def invoke(self, command, *extra):
        with patch.object(sys, "argv", ["quality.py", command, "--base", self.base, *extra]):
            return quality.main()

    def expected_calls(self, command, write_baseline=False):
        checks = {
            "build": [
                call.test_metrics(),
                call.check_coverage(self.base, write_baseline),
                call.check_money_coverage(self.base, write_baseline),
            ],
            "coverage": [
                call.check_coverage(self.base, write_baseline),
                call.check_money_coverage(self.base, write_baseline),
            ],
            "money-mutation": [],
        }
        return [call.gradle(*self.tasks[command], budget=600), *checks[command], call.check_latest()]

    def test_full_commands_verify_freshness_after_their_required_graph_and_checks(self):
        for command in self.tasks:
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.loader.reset_mock()
                self.assertEqual(0, self.invoke(command))
                self.assertEqual(self.expected_calls(command), self.calls.mock_calls)
                self.loader.assert_called_once_with("money_mutation")

    def test_build_without_base_preserves_its_graph_and_does_not_infer_an_environment_base(self):
        with (
            patch.dict(os.environ, {"BASE_SHA": self.base}),
            patch.object(sys, "argv", ["quality.py", "build"]),
        ):
            self.assertEqual(0, quality.main())
        self.assertEqual([
            call.gradle("build", "installDist", budget=600), call.test_metrics(), call.check_latest(),
        ], self.calls.mock_calls)

    def test_invalid_combined_base_refuses_before_work_and_recovers(self):
        for base in ("", "main", "--help", "a" * 39, "a" * 41, "g" * 40, "a" * 40 + "\n"):
            with self.subTest(base=base):
                self.calls.reset_mock()
                self.assertEqual(1, self.invoke("build", "--base=" + base))
                self.assertIn("full trusted base commit SHA", self.errors.getvalue())
                self.assertEqual([], self.calls.mock_calls)
                self.provider.assert_not_called()
                self.budget.assert_not_called()
                self.loader.assert_not_called()
        self.assertEqual(0, self.invoke("build"))
        self.assertEqual(self.expected_calls("build"), self.calls.mock_calls)

    def test_missing_or_stale_final_evidence_blocks_each_full_command_and_restores(self):
        for command in self.tasks:
            for error in (
                FileNotFoundError("Synthetic missing mutation record"),
                ValueError("Synthetic stale mutation inputs"),
            ):
                with self.subTest(command=command, error=type(error).__name__):
                    self.calls.reset_mock()
                    self.calls.check_latest.side_effect = error
                    self.assertEqual(1, self.invoke(command))
                    self.assertEqual(self.expected_calls(command), self.calls.mock_calls)
                    self.assertIn(str(error), self.errors.getvalue())
                    self.calls.check_latest.side_effect = None
                    self.calls.reset_mock()
                    self.assertEqual(0, self.invoke(command))
                    self.assertEqual(self.expected_calls(command), self.calls.mock_calls)

    def test_native_refresh_failure_or_timeout_never_falls_back_to_an_old_report(self):
        for command in self.tasks:
            for error in (
                subprocess.CalledProcessError(7, ["synthetic-gradle", "moneyMutation"]),
                TimeoutError("Synthetic native refresh exhausted the remaining budget"),
            ):
                with self.subTest(command=command, error=type(error).__name__):
                    self.calls.reset_mock()
                    self.calls.gradle.side_effect = error
                    self.assertEqual(1, self.invoke(command))
                    self.assertEqual([call.gradle(*self.tasks[command], budget=600)], self.calls.mock_calls)

    def test_failed_test_or_coverage_checks_prevent_full_command_success(self):
        for command, failed_check in (
            ("build", "test_metrics"), ("build", "check_coverage"), ("build", "check_money_coverage"),
            ("coverage", "check_coverage"), ("coverage", "check_money_coverage"),
        ):
            with self.subTest(command=command, failed_check=failed_check):
                self.calls.reset_mock()
                check = getattr(self.calls, failed_check)
                check.side_effect = ValueError("Synthetic qualification failure")
                self.assertEqual(1, self.invoke(command))
                self.calls.check_latest.assert_not_called()
                self.calls.gradle.assert_called_once_with(*self.tasks[command], budget=600)
                check.side_effect = None
                self.calls.reset_mock()
                self.assertEqual(0, self.invoke(command))
                self.assertEqual(self.expected_calls(command), self.calls.mock_calls)

    def test_combined_build_checks_actual_aggregate_report_then_recovers(self):
        fixture = CoverageRefusalDiagnosticsTest()
        fixture.base = self.base
        counters = {kind: {"covered": 9, "total": 10} for kind in ("LINE", "BRANCH")}
        self.calls.check_coverage.side_effect = self.real_check_coverage
        for defect in ("LINE", "BRANCH", "missing", "malformed"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = fixture.fixture(root, counters, counters)
                baseline_bytes = target.read_bytes()
                report = root / quality.REPORT
                original = report.read_bytes()
                reviewed = dict(counters)
                if defect in counters:
                    reviewed[defect] = {"covered": 10, "total": 10}
                elif defect == "missing":
                    report.unlink()
                else:
                    report.write_text("<report", encoding="utf-8")
                with (
                    patch.object(quality, "ROOT", root),
                    patch.object(quality, "run", side_effect=fixture.git(reviewed)),
                ):
                    self.calls.reset_mock()
                    self.assertEqual(1, self.invoke("build"))
                    self.calls.check_money_coverage.assert_not_called()
                    self.calls.check_latest.assert_not_called()
                    reviewed.update(counters)
                    report.write_bytes(original)
                    self.calls.reset_mock()
                    self.assertEqual(0, self.invoke("build"))
                    self.assertEqual(self.expected_calls("build"), self.calls.mock_calls)
                self.assertEqual(baseline_bytes, target.read_bytes())

    def test_combined_build_checks_actual_money_report_then_recovers(self):
        counters = {"LINE": {"covered": 97, "total": 100}, "BRANCH": {"covered": 93, "total": 100}}
        floors = {"LINE": 97, "BRANCH": 93, "MUTATION": 90}
        provider = Mock(SOURCE_REF="b" * 40, DOCS_REF="c" * 40)
        provider.verify_outputs.return_value = [Path("Money.kt"), Path("CurrencyRegistry.kt")]
        self.provider.return_value = provider
        self.calls.check_money_coverage.side_effect = self.real_check_money_coverage
        baseline = {
            "strategy_package": "api.money", "contracts_source_ref": provider.SOURCE_REF,
            "source_file_count": 2, "compiled_class_count": 2, **counters,
        }
        for defect in ("LINE", "BRANCH", "missing", "malformed"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report = MoneyCoverageQualificationTest().report(root)
                original = report.read_bytes()
                target = root / quality.MONEY_BASELINE
                target.parent.mkdir(parents=True)
                target.write_text(json.dumps(baseline), encoding="utf-8")
                baseline_bytes = target.read_bytes()
                reviewed = dict(baseline)
                if defect in counters:
                    reviewed[defect] = {"covered": counters[defect]["covered"] + 1, "total": 100}
                elif defect == "missing":
                    report.unlink()
                else:
                    report.write_text("<report", encoding="utf-8")

                def git(command, **_kwargs):
                    if command == ["git", "ls-tree", "-r", "--name-only", self.base]:
                        return quality.MONEY_BASELINE
                    if command == ["git", "show", f"{self.base}:{quality.MONEY_BASELINE}"]:
                        return json.dumps(reviewed)
                    raise AssertionError("Unexpected Git operation in synthetic Money coverage fixture")

                with (
                    patch.object(quality, "ROOT", root),
                    patch.object(quality, "MONEY_REPORT", report.relative_to(root)),
                    patch.object(quality, "run", side_effect=git),
                    patch.object(quality, "money_class_inventory", return_value={
                        f"{quality.MONEY_PACKAGE}/Money", f"{quality.MONEY_PACKAGE}/CurrencyRegistry",
                    }),
                    patch.object(quality, "money_policy", return_value=(floors, "d" * 64)),
                    patch.object(quality, "money_test_metrics", return_value={}),
                ):
                    self.calls.reset_mock()
                    self.assertEqual(1, self.invoke("build"))
                    self.calls.check_latest.assert_not_called()
                    reviewed.update(counters)
                    report.write_bytes(original)
                    self.calls.reset_mock()
                    self.assertEqual(0, self.invoke("build"))
                    self.assertEqual(self.expected_calls("build"), self.calls.mock_calls)
                self.assertEqual(baseline_bytes, target.read_bytes())

    def test_each_full_command_passes_only_its_remaining_budget_to_gradle(self):
        def read_budget(_):
            self.clock += 17
            return {"enforced_seconds": 600}

        self.budget.side_effect = read_budget
        for command in self.tasks:
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(0, self.invoke(command))
                self.calls.gradle.assert_called_once_with(*self.tasks[command], budget=583)
                self.calls.check_latest.assert_called_once_with()

    def test_final_freshness_readback_is_inside_the_full_command_deadline(self):
        def finish_native_work(*_, **__):
            self.clock += 599

        def read_latest():
            self.clock += 2

        self.calls.gradle.side_effect = finish_native_work
        self.calls.check_latest.side_effect = read_latest
        for command in self.tasks:
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(1, self.invoke(command))
                self.assertEqual(self.expected_calls(command), self.calls.mock_calls)
                self.assertIn("exceeded its enforced elapsed budget", self.errors.getvalue())

    def test_both_combined_coverage_checks_remain_inside_the_build_deadline(self):
        def finish_native_work(*_, **__):
            self.clock += 599

        def check_report(*_):
            self.clock += 2

        self.calls.gradle.side_effect = finish_native_work
        for name in ("check_coverage", "check_money_coverage"):
            with self.subTest(check=name):
                check = getattr(self.calls, name)
                check.side_effect = check_report
                self.calls.reset_mock()
                self.assertEqual(1, self.invoke("build"))
                self.assertEqual(self.expected_calls("build"), self.calls.mock_calls)
                self.assertIn("exceeded its enforced elapsed budget", self.errors.getvalue())
                check.side_effect = None
                self.calls.reset_mock()
                self.assertEqual(0, self.invoke("build"))
                self.assertEqual(self.expected_calls("build"), self.calls.mock_calls)

    def test_missing_numeric_budget_refuses_before_any_native_work(self):
        self.budget.side_effect = ValueError("Synthetic missing numeric Money budget")
        for command in self.tasks:
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(1, self.invoke(command))
                self.assertEqual([], self.calls.mock_calls)

    def test_coverage_requires_its_base_before_reading_policy_or_starting_work(self):
        with patch.object(sys, "argv", ["quality.py", "coverage"]), self.assertRaises(SystemExit) as error:
            quality.main()
        self.assertEqual(2, error.exception.code)
        self.budget.assert_not_called()
        self.provider.assert_not_called()
        self.assertEqual([], self.calls.mock_calls)

    def test_writing_coverage_baselines_still_refreshes_and_verifies_mutation(self):
        for command in ("build", "coverage"):
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(0, self.invoke(command, "--write-baseline"))
                self.assertEqual(self.expected_calls(command, True), self.calls.mock_calls)

    def test_standalone_mutation_report_remains_readonly(self):
        self.assertEqual(0, self.invoke("money-mutation-report"))
        self.assertEqual([call.check_latest()], self.calls.mock_calls)
        self.loader.assert_called_once_with("money_mutation")
        self.budget.assert_not_called()
        self.provider.assert_not_called()

    def test_focused_coverage_does_not_claim_full_mutation_qualification(self):
        for command, expected in (
            ("money-coverage", [call.gradle("moneyCoverageReport"), call.check_money_coverage(self.base, False)]),
            ("money-coverage-report", [call.check_money_coverage(self.base, False)]),
        ):
            with self.subTest(command=command):
                self.calls.reset_mock()
                self.assertEqual(0, self.invoke(command))
                self.assertEqual(expected, self.calls.mock_calls)
                self.loader.assert_not_called()
                self.budget.assert_not_called()


if __name__ == "__main__":
    unittest.main()
