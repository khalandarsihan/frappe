# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""
This module provides functionality for running tests in Frappe applications.

It includes utilities for running tests for specific doctypes, modules, or entire applications,
as well as functions for creating and managing test records.
"""

from __future__ import annotations

import cProfile
import importlib
import json
import logging
import os
import pstats
import sys
import time
import unittest
from dataclasses import dataclass, field
from functools import cache, wraps
from importlib import reload
from io import StringIO
from pathlib import Path
from typing import Optional, Union

import click

import frappe
import frappe.utils.scheduler
from frappe.model.naming import revert_series_if_last
from frappe.modules import get_module_name, load_doctype_module
from frappe.tests.utils import FrappeIntegrationTestCase
from frappe.utils import cint

SLOW_TEST_THRESHOLD = 2

logger = logging.getLogger(__name__)


def debug_timer(func):
	@wraps(func)
	def wrapper(*args, **kwargs):
		start_time = time.time()
		result = func(*args, **kwargs)
		end_time = time.time()
		logger.debug(f" {func.__name__} took {end_time - start_time:.3f} seconds")
		return result

	return wrapper


class TestRunner(unittest.TextTestRunner):
	def __init__(
		self,
		stream=None,
		descriptions=True,
		verbosity=1,
		failfast=False,
		buffer=False,
		resultclass=None,
		warnings=None,
		*,
		tb_locals=False,
		junit_xml_output: bool = False,
		profile: bool = False,
	):
		super().__init__(
			stream=stream,
			descriptions=descriptions,
			verbosity=verbosity,
			failfast=failfast,
			buffer=buffer,
			resultclass=resultclass or TestResult,
			warnings=warnings,
			tb_locals=tb_locals,
		)
		self.junit_xml_output = junit_xml_output
		self.profile = profile
		self.test_record_callbacks = []
		logger.debug("TestRunner initialized")

	def add_test_record_callback(self, callback):
		self.test_record_callbacks.append(callback)

	def execute_test_record_callbacks(self):
		for callback in self.test_record_callbacks:
			callback()
		self.test_record_callbacks.clear()

	def run(
		self, test_suites: tuple[unittest.TestSuite, unittest.TestSuite]
	) -> tuple[unittest.TestResult, unittest.TestResult | None]:
		unit_suite, integration_suite = test_suites

		if self.profile:
			pr = cProfile.Profile()
			pr.enable()

		# Run unit tests
		click.echo(
			"\n" + click.style(f"Running {unit_suite.countTestCases()} unit tests", fg="cyan", bold=True)
		)
		unit_result = super().run(unit_suite)

		# Run integration tests only if unit tests pass
		integration_result = None
		if unit_result.wasSuccessful():
			click.echo(
				"\n"
				+ click.style(
					f"Running {integration_suite.countTestCases()} integration tests",
					fg="cyan",
					bold=True,
				)
			)
			integration_result = super().run(integration_suite)

		if self.profile:
			pr.disable()
			s = StringIO()
			ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
			ps.print_stats()
			print(s.getvalue())

		return unit_result, integration_result

	def discover_tests(
		self, apps: list[str], config: TestConfig
	) -> tuple[unittest.TestSuite, unittest.TestSuite]:
		logger.debug(f"Discovering tests for apps: {apps}")
		unit_test_suite = unittest.TestSuite()
		integration_test_suite = unittest.TestSuite()

		for app in apps:
			app_path = Path(frappe.get_app_path(app))
			for path in app_path.rglob("test_*.py"):
				if path.parts[-4:-1] == ("doctype", "doctype", "boilerplate"):
					continue
				if path.name == "test_runner.py":
					continue
				relative_path = path.relative_to(app_path)
				if any(part in relative_path.parts for part in ["locals", ".git", "public", "__pycache__"]):
					continue

				module_name = (
					f"{app_path.stem}.{'.'.join(relative_path.parent.parts)}.{path.stem}"
					if str(relative_path.parent) != "."
					else f"{app_path.stem}.{path.stem}"
				)
				module = importlib.import_module(module_name)

				if path.parent.name == "doctype" and not config.skip_test_records:
					json_file = path.with_name(path.stem[5:] + ".json")
					if json_file.exists():
						with json_file.open() as f:
							doctype = json.loads(f.read())["name"]
							self.add_test_record_callback(lambda: make_test_records(doctype, commit=True))

				self._add_module_tests(module, unit_test_suite, integration_test_suite, config)

		logger.debug(
			f"Discovered {unit_test_suite.countTestCases()} unit tests and {integration_test_suite.countTestCases()} integration tests"
		)
		return unit_test_suite, integration_test_suite

	def discover_doctype_tests(
		self, doctypes: str | list[str], config: TestConfig, force: bool = False
	) -> tuple[unittest.TestSuite, unittest.TestSuite]:
		unit_test_suite = unittest.TestSuite()
		integration_test_suite = unittest.TestSuite()

		if isinstance(doctypes, str):
			doctypes = [doctypes]

		for doctype in doctypes:
			module = frappe.db.get_value("DocType", doctype, "module")
			if not module:
				raise TestRunnerError(f"Invalid doctype {doctype}")

			test_module = get_module_name(doctype, module, "test_")
			if force:
				frappe.db.delete(doctype)

			try:
				module = importlib.import_module(test_module)
				self._add_module_tests(module, unit_test_suite, integration_test_suite, config)
			except ImportError:
				logger.warning(f"No test module found for doctype {doctype}")

			if not config.skip_test_records:
				self.add_test_record_callback(lambda: make_test_records(doctype, force=force, commit=True))

		return unit_test_suite, integration_test_suite

	def discover_module_tests(
		self, modules, config: TestConfig
	) -> tuple[unittest.TestSuite, unittest.TestSuite]:
		unit_test_suite = unittest.TestSuite()
		integration_test_suite = unittest.TestSuite()

		modules = [modules] if not isinstance(modules, list | tuple) else modules

		for module in modules:
			module = importlib.import_module(module)
			self._add_module_tests(module, unit_test_suite, integration_test_suite, config)

		return unit_test_suite, integration_test_suite

	def _add_module_tests(
		self,
		module,
		unit_test_suite: unittest.TestSuite,
		integration_test_suite: unittest.TestSuite,
		config: TestConfig,
	):
		# Handle module test dependencies
		if hasattr(module, "test_dependencies") and not config.skip_test_records:
			for doctype in module.test_dependencies:
				make_test_records(doctype, commit=True)

		if config.case:
			test_suite = unittest.TestLoader().loadTestsFromTestCase(getattr(module, config.case))
		else:
			test_suite = unittest.TestLoader().loadTestsFromModule(module)

		for test in self._iterate_suite(test_suite):
			if config.tests and test._testMethodName not in config.tests:
				continue

			category = "integration" if isinstance(test, FrappeIntegrationTestCase) else "unit"

			if config.selected_categories and category not in config.selected_categories:
				continue

			config.categories[category].append(test)
			if category == "unit":
				unit_test_suite.addTest(test)
			else:
				integration_test_suite.addTest(test)

	@staticmethod
	def _iterate_suite(suite):
		for test in suite:
			if isinstance(test, unittest.TestSuite):
				yield from TestRunner._iterate_suite(test)
			elif isinstance(test, unittest.TestCase):
				yield test


class TestResult(unittest.TextTestResult):
	def startTest(self, test):
		logger.debug(f"--- Starting test: {test}")
		self.tb_locals = True
		self._started_at = time.monotonic()
		super(unittest.TextTestResult, self).startTest(test)
		test_class = unittest.util.strclass(test.__class__)
		if not hasattr(self, "current_test_class") or self.current_test_class != test_class:
			click.echo(f"\n{unittest.util.strclass(test.__class__)}")
			self.current_test_class = test_class

	def getTestMethodName(self, test):
		return test._testMethodName if hasattr(test, "_testMethodName") else str(test)

	def addSuccess(self, test):
		super(unittest.TextTestResult, self).addSuccess(test)
		elapsed = time.monotonic() - self._started_at
		threshold_passed = elapsed >= SLOW_TEST_THRESHOLD
		elapsed = click.style(f" ({elapsed:.03}s)", fg="red") if threshold_passed else ""
		click.echo(f"  {click.style(' ✔ ', fg='green')} {self.getTestMethodName(test)}{elapsed}")
		logger.debug(f"=== Test passed: {test}")

	def addError(self, test, err):
		super(unittest.TextTestResult, self).addError(test, err)
		click.echo(f"  {click.style(' ✖ ', fg='red')} {self.getTestMethodName(test)}")
		logger.debug(f"=== Test error: {test}")

	def addFailure(self, test, err):
		super(unittest.TextTestResult, self).addFailure(test, err)
		click.echo(f"  {click.style(' ✖ ', fg='red')} {self.getTestMethodName(test)}")
		logger.debug(f"=== Test failed: {test}")

	def addSkip(self, test, reason):
		super(unittest.TextTestResult, self).addSkip(test, reason)
		click.echo(f"  {click.style(' = ', fg='white')} {self.getTestMethodName(test)}")
		logger.debug(f"=== Test skipped: {test}")

	def addExpectedFailure(self, test, err):
		super(unittest.TextTestResult, self).addExpectedFailure(test, err)
		click.echo(f"  {click.style(' ✖ ', fg='red')} {self.getTestMethodName(test)}")
		logger.debug(f"=== Test expected failure: {test}")

	def addUnexpectedSuccess(self, test):
		super(unittest.TextTestResult, self).addUnexpectedSuccess(test)
		click.echo(f"  {click.style(' ✔ ', fg='green')} {self.getTestMethodName(test)}")
		logger.debug(f"=== Test unexpected success: {test}")

	def printErrors(self):
		click.echo("\n")
		self.printErrorList(" ERROR ", self.errors, "red")
		self.printErrorList(" FAIL ", self.failures, "red")

	def printErrorList(self, flavour, errors, color):
		for test, err in errors:
			click.echo(self.separator1)
			click.echo(f"{click.style(flavour, bg=color)} {self.getDescription(test)}")
			click.echo(self.separator2)
			click.echo(err)

	def __str__(self):
		return f"Tests: {self.testsRun}, Failing: {len(self.failures)}, Errors: {len(self.errors)}"


class TestRunnerError(Exception):
	"""Custom exception for test runner errors"""

	pass


logging.basicConfig(format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class TestConfig:
	"""Configuration class for test runner"""

	profile: bool = False
	failfast: bool = False
	junit_xml_output: bool = False
	tests: tuple = ()
	case: str | None = None
	pdb_on_exceptions: tuple | None = None
	categories: dict = field(default_factory=lambda: {"unit": [], "integration": []})
	selected_categories: list[str] = field(default_factory=list)
	skip_before_tests: bool = False
	skip_test_records: bool = False  # New attribute


def xmlrunner_wrapper(output):
	"""Convenience wrapper to keep method signature unchanged for XMLTestRunner and TextTestRunner"""
	try:
		import xmlrunner
	except ImportError:
		print("Development dependencies are required to execute this command. To install run:")
		print("$ bench setup requirements --dev")
		raise

	def _runner(*args, **kwargs):
		kwargs["output"] = output
		return xmlrunner.XMLTestRunner(*args, **kwargs)

	return _runner


def main(
	site: str | None = None,
	app: str | None = None,
	module: str | None = None,
	doctype: str | None = None,
	module_def: str | None = None,
	verbose: bool = False,
	tests: tuple = (),
	force: bool = False,
	profile: bool = False,
	junit_xml_output: str | None = None,
	doctype_list_path: str | None = None,
	failfast: bool = False,
	case: str | None = None,
	skip_test_records: bool = False,
	skip_before_tests: bool = False,
	pdb_on_exceptions: bool = False,
	selected_categories: list[str] | None = None,
) -> None:
	"""Main function to run tests"""
	logger.setLevel(logging.DEBUG if verbose else logging.INFO)
	start_time = time.time()

	# Check for mutually exclusive arguments
	exclusive_args = [doctype, doctype_list_path, module_def, module]
	if sum(arg is not None for arg in exclusive_args) > 1:
		error_message = (
			"Error: The following arguments are mutually exclusive: "
			"doctype, doctype_list_path, module_def, and module. "
			"Please specify only one of these."
		)
		logger.error(error_message)
		sys.exit(1)

	# Prepare debug log message
	debug_params = []
	for param_name in ["site", "app", "module", "doctype", "module_def", "doctype_list_path"]:
		param_value = locals()[param_name]
		if param_value is not None:
			debug_params.append(f"{param_name}={param_value}")

	if debug_params:
		logger.debug(f"Starting test run with parameters: {', '.join(debug_params)}")
	else:
		logger.debug("Starting test run with no specific parameters")

	test_config = TestConfig(
		profile=profile,
		failfast=failfast,
		junit_xml_output=bool(junit_xml_output),
		tests=tests,
		case=case,
		pdb_on_exceptions=pdb_on_exceptions,
		selected_categories=selected_categories or [],
		skip_before_tests=skip_before_tests,
		skip_test_records=skip_test_records,
	)

	_initialize_test_environment(site, test_config)

	xml_output_file = _setup_xml_output(junit_xml_output)

	try:
		# Create TestRunner instance
		runner = TestRunner(
			resultclass=TestResult if not test_config.junit_xml_output else None,
			verbosity=2 if logger.getEffectiveLevel() < logging.INFO else 1,
			failfast=test_config.failfast,
			tb_locals=logger.getEffectiveLevel() <= logging.INFO,
			junit_xml_output=test_config.junit_xml_output,
			profile=test_config.profile,
		)

		if doctype or doctype_list_path:
			doctype = _load_doctype_list(doctype_list_path) if doctype_list_path else doctype
			unit_result, integration_result = _run_doctype_tests(doctype, test_config, runner, force, app)
		elif module_def:
			unit_result, integration_result = _run_module_def_tests(
				app, module_def, test_config, runner, force
			)
		elif module:
			unit_result, integration_result = _run_module_tests(module, test_config, runner, app)
		else:
			unit_result, integration_result = _run_all_tests(app, test_config, runner)

		print_test_results(unit_result, integration_result)

		# Determine overall success
		success = unit_result.wasSuccessful() and (
			integration_result is None or integration_result.wasSuccessful()
		)

		if not success:
			sys.exit(1)

		return unit_result, integration_result

	finally:
		if xml_output_file:
			xml_output_file.close()

		end_time = time.time()
		logger.debug(f"Total test run time: {end_time - start_time:.3f} seconds")


def print_test_results(unit_result: unittest.TestResult, integration_result: unittest.TestResult | None):
	"""Print detailed test results including failures and errors"""
	click.echo("\n" + click.style("Test Results:", fg="cyan", bold=True))

	def _print_result(result, category):
		tests_run = result.testsRun
		failures = len(result.failures)
		errors = len(result.errors)
		click.echo(
			f"\n{click.style(f'{category} Tests:', bold=True)}\n"
			f"  Ran: {click.style(f'{tests_run:<3}', fg='cyan')}"
			f"  Failures: {click.style(f'{failures:<3}', fg='red' if failures else 'green')}"
			f"  Errors: {click.style(f'{errors:<3}', fg='red' if errors else 'green')}"
		)

		if failures > 0:
			click.echo(f"\n{click.style(category + ' Test Failures:', fg='red', bold=True)}")
			for i, failure in enumerate(result.failures, 1):
				click.echo(f"  {i}. {click.style(str(failure[0]), fg='yellow')}")

		if errors > 0:
			click.echo(f"\n{click.style(category + ' Test Errors:', fg='red', bold=True)}")
			for i, error in enumerate(result.errors, 1):
				click.echo(f"  {i}. {click.style(str(error[0]), fg='yellow')}")
				click.echo(click.style("     " + str(error[1]).split("\n")[-2], fg="red"))

	_print_result(unit_result, "Unit")

	if integration_result:
		_print_result(integration_result, "Integration")

	# Print overall status
	total_failures = len(unit_result.failures) + (
		len(integration_result.failures) if integration_result else 0
	)
	total_errors = len(unit_result.errors) + (len(integration_result.errors) if integration_result else 0)

	if total_failures == 0 and total_errors == 0:
		click.echo(f"\n{click.style('All tests passed successfully!', fg='green', bold=True)}")
	else:
		click.echo(f"\n{click.style('Some tests failed or encountered errors.', fg='red', bold=True)}")


@debug_timer
def _initialize_test_environment(site, config: TestConfig):
	"""Initialize the test environment"""
	logger.debug(f"Initializing test environment for site: {site}")
	frappe.init(site)
	if not frappe.db:
		frappe.connect()
	try:
		# require db access
		_disable_scheduler_if_needed()
		frappe.clear_cache()
	except Exception as e:
		logger.error(f"Error connecting to the database: {e!s}")
		raise TestRunnerError(f"Failed to connect to the database: {e}") from e

	# Set various test-related flags
	frappe.flags.in_test = True
	frappe.flags.print_messages = logger.getEffectiveLevel() < logging.INFO
	frappe.flags.tests_verbose = logger.getEffectiveLevel() < logging.INFO
	logger.debug("Test environment initialized")


def _setup_xml_output(junit_xml_output):
	"""Setup XML output for test results if specified"""
	global unittest_runner

	if junit_xml_output:
		xml_output_file = open(junit_xml_output, "wb")
		unittest_runner = xmlrunner_wrapper(xml_output_file)
		return xml_output_file
	else:
		unittest_runner = unittest.TextTestRunner
		return None


def _load_doctype_list(doctype_list_path):
	"""Load the list of doctypes from the specified file"""
	app, path = doctype_list_path.split(os.path.sep, 1)
	with open(frappe.get_app_path(app, path)) as f:
		return f.read().strip().splitlines()


def _run_module_def_tests(
	app, module_def, config: TestConfig, runner: TestRunner, force
) -> tuple[unittest.TestResult, unittest.TestResult | None]:
	"""Run tests for the specified module definition"""
	doctypes = _get_doctypes_for_module_def(app, module_def)
	return _run_doctype_tests(doctypes, config, runner, force, app)


def _get_doctypes_for_module_def(app, module_def):
	"""Get the list of doctypes for the specified module definition"""
	doctypes = []
	doctypes_ = frappe.get_list(
		"DocType",
		filters={"module": module_def, "istable": 0},
		fields=["name", "module"],
		as_list=True,
	)
	for doctype, module in doctypes_:
		test_module = get_module_name(doctype, module, "test_", app=app)
		try:
			importlib.import_module(test_module)
			doctypes.append(doctype)
		except Exception:
			pass
	return doctypes


# Global variable to track scheduler state
scheduler_disabled_by_user = False


def _disable_scheduler_if_needed():
	"""Disable scheduler if it's not already disabled"""
	global scheduler_disabled_by_user
	scheduler_disabled_by_user = frappe.utils.scheduler.is_scheduler_disabled(verbose=False)
	if not scheduler_disabled_by_user:
		frappe.utils.scheduler.disable_scheduler()


def _cleanup_after_tests():
	"""Perform cleanup operations after running tests"""
	global scheduler_disabled_by_user
	if not scheduler_disabled_by_user:
		frappe.utils.scheduler.enable_scheduler()

	if frappe.db:
		frappe.db.commit()
		frappe.clear_cache()


@debug_timer
def _run_all_tests(
	app: str | None, config: TestConfig, runner: TestRunner
) -> tuple[unittest.TestResult, unittest.TestResult | None]:
	"""Run all tests for the specified app or all installed apps"""

	apps = [app] if app else frappe.get_installed_apps()
	logger.debug(f"Running tests for apps: {apps}")
	try:
		unit_test_suite, integration_test_suite = runner.discover_tests(apps, config)
		logger.debug(
			f"Discovered {len(list(runner._iterate_suite(unit_test_suite)))} unit tests and {len(list(runner._iterate_suite(integration_test_suite)))} integration tests"
		)

		if config.pdb_on_exceptions:
			for test_suite in (unit_test_suite, integration_test_suite):
				for test_case in runner._iterate_suite(test_suite):
					if hasattr(test_case, "_apply_debug_decorator"):
						test_case._apply_debug_decorator(config.pdb_on_exceptions)

		_prepare_integration_tests(runner, integration_test_suite, config, app)
		res = runner.run((unit_test_suite, integration_test_suite))
		_cleanup_after_tests()
		return res
	except Exception as e:
		logger.error(f"Error running all tests for {app or 'all apps'}: {e!s}")
		raise TestRunnerError(f"Failed to run tests for {app or 'all apps'}: {e!s}") from e


@debug_timer
def _run_doctype_tests(
	doctypes, config: TestConfig, runner: TestRunner, force=False, app: str | None = None
) -> tuple[unittest.TestResult, unittest.TestResult | None]:
	"""Run tests for the specified doctype(s)"""

	try:
		unit_test_suite, integration_test_suite = runner.discover_doctype_tests(doctypes, config, force)

		if config.pdb_on_exceptions:
			for test_suite in (unit_test_suite, integration_test_suite):
				for test_case in runner._iterate_suite(test_suite):
					if hasattr(test_case, "_apply_debug_decorator"):
						test_case._apply_debug_decorator(config.pdb_on_exceptions)

		_prepare_integration_tests(runner, integration_test_suite, config, app)
		res = runner.run((unit_test_suite, integration_test_suite))
		_cleanup_after_tests()
		return res
	except Exception as e:
		logger.error(f"Error running tests for doctypes {doctypes}: {e!s}")
		raise TestRunnerError(f"Failed to run tests for doctypes: {e!s}") from e


@debug_timer
def _run_module_tests(
	module, config: TestConfig, runner: TestRunner, app: str | None = None
) -> tuple[unittest.TestResult, unittest.TestResult | None]:
	"""Run tests for the specified module"""
	try:
		unit_test_suite, integration_test_suite = runner.discover_module_tests(module, config)

		if config.pdb_on_exceptions:
			for test_suite in (unit_test_suite, integration_test_suite):
				for test_case in runner._iterate_suite(test_suite):
					if hasattr(test_case, "_apply_debug_decorator"):
						test_case._apply_debug_decorator(config.pdb_on_exceptions)

		_prepare_integration_tests(runner, integration_test_suite, config, app)
		res = runner.run((unit_test_suite, integration_test_suite))
		_cleanup_after_tests()
		return res
	except Exception as e:
		logger.error(f"Error running tests for module {module}: {e!s}")
		raise TestRunnerError(f"Failed to run tests for module: {e!s}") from e


def _prepare_integration_tests(
	runner: TestRunner, integration_test_suite: unittest.TestSuite, config: TestConfig, app: str
) -> None:
	"""Prepare the environment for integration tests."""
	if next(runner._iterate_suite(integration_test_suite), None) is not None:
		# Explanatory comment
		"""
		We perform specific setup steps only for integration tests:

		1. Database Connection:
		   - Initialized only for integration tests to avoid overhead in unit tests.
		   - Essential for end-to-end functionality testing in integration tests.
		   - Maintains separation between unit and integration tests.

		2. Before Tests Hooks:
		   - Executed only for integration tests unless explicitly skipped.
		   - Provides necessary environment setup for integration tests.
		   - Skipped for unit tests to maintain their independence and isolation.

		3. Test Record Creation:
		   - Performed only for integration tests unless explicitly skipped.
		   - Creates or modifies database records needed for integration tests.
		   - Ensures consistent starting state and allows for complex test scenarios.
		   - Skipped for unit tests to maintain their isolation and reproducibility.

		These steps are crucial for integration tests but unnecessary or potentially
		harmful for unit tests, which should be independent of external state and fast to execute.
		By selectively applying these setup steps, we maintain the integrity and purpose
		of both unit and integration tests while optimizing performance.
		"""
		if not config.skip_before_tests:
			_run_before_test_hooks(config, app)
		else:
			logger.debug("Skipping before_tests hooks: Explicitly skipped")

		if not config.skip_test_records:
			_execute_test_record_callbacks(runner)
		else:
			logger.debug("Skipping test record creation: Explicitly skipped")
	else:
		logger.debug("Skipping before_tests hooks and test record creation: No integration tests")


def make_test_records(doctype, force=False, commit=False):
	"""Make test records for the specified doctype"""
	logger.debug(f"Making test records for doctype: {doctype}")

	for options in get_dependencies(doctype):
		if options == "[Select]":
			continue

		if options not in frappe.local.test_objects:
			frappe.local.test_objects[options] = []
			make_test_records(options, force, commit=commit)
			make_test_records_for_doctype(options, force, commit=commit)


@cache
def get_modules(doctype):
	"""Get the modules for the specified doctype"""
	module = frappe.db.get_value("DocType", doctype, "module")
	try:
		test_module = load_doctype_module(doctype, module, "test_")
		if test_module:
			reload(test_module)
	except ImportError:
		test_module = None

	return module, test_module


@cache
def get_dependencies(doctype):
	"""Get the dependencies for the specified doctype"""
	module, test_module = get_modules(doctype)
	meta = frappe.get_meta(doctype)
	link_fields = meta.get_link_fields()

	for df in meta.get_table_fields():
		link_fields.extend(frappe.get_meta(df.options).get_link_fields())

	options_list = [df.options for df in link_fields] + [doctype]

	if hasattr(test_module, "test_dependencies"):
		options_list += test_module.test_dependencies

	options_list = list(set(options_list))

	if hasattr(test_module, "test_ignore"):
		for doctype_name in test_module.test_ignore:
			if doctype_name in options_list:
				options_list.remove(doctype_name)

	options_list.sort()

	return options_list


def make_test_records_for_doctype(doctype, force=False, commit=False):
	"""Make test records for the specified doctype"""

	test_record_log_instance = TestRecordLog()
	if not force and doctype in test_record_log_instance.get():
		return

	module, test_module = get_modules(doctype)
	logger.debug(f"Making test records for {doctype}")

	if hasattr(test_module, "_make_test_records"):
		frappe.local.test_objects[doctype] = (
			frappe.local.test_objects.get(doctype, []) + test_module._make_test_records()
		)
	elif hasattr(test_module, "test_records"):
		frappe.local.test_objects[doctype] = frappe.local.test_objects.get(doctype, []) + make_test_objects(
			doctype, test_module.test_records, force, commit=commit
		)
	else:
		test_records = frappe.get_test_records(doctype)
		if test_records:
			frappe.local.test_objects[doctype] = frappe.local.test_objects.get(
				doctype, []
			) + make_test_objects(doctype, test_records, force, commit=commit)
		elif logger.getEffectiveLevel() < logging.INFO:
			print_mandatory_fields(doctype)

	test_record_log_instance.add(doctype)


def make_test_objects(doctype, test_records=None, reset=False, commit=False):
	"""Make test objects from given list of `test_records` or from `test_records.json`"""
	logger.debug(f"Making test objects for doctype: {doctype}")
	records = []

	def revert_naming(d):
		if getattr(d, "naming_series", None):
			revert_series_if_last(d.naming_series, d.name)

	if test_records is None:
		test_records = frappe.get_test_records(doctype)

	for doc in test_records:
		if not reset:
			frappe.db.savepoint("creating_test_record")

		if not doc.get("doctype"):
			doc["doctype"] = doctype

		d = frappe.copy_doc(doc)

		if d.meta.get_field("naming_series"):
			if not d.naming_series:
				d.naming_series = "_T-" + d.doctype + "-"

		if doc.get("name"):
			d.name = doc.get("name")
		else:
			d.set_new_name()

		if frappe.db.exists(d.doctype, d.name) and not reset:
			frappe.db.rollback(save_point="creating_test_record")
			# do not create test records, if already exists
			continue

		# submit if docstatus is set to 1 for test record
		docstatus = d.docstatus

		d.docstatus = 0

		try:
			d.run_method("before_test_insert")
			d.insert(ignore_if_duplicate=True)

			if docstatus == 1:
				d.submit()

		except frappe.NameError:
			revert_naming(d)

		except Exception as e:
			if (
				d.flags.ignore_these_exceptions_in_test
				and e.__class__ in d.flags.ignore_these_exceptions_in_test
			):
				revert_naming(d)
			else:
				logger.debug(f"Error in making test record for {d.doctype} {d.name}")
				raise

		records.append(d.name)

		if commit:
			frappe.db.commit()
	return records


def print_mandatory_fields(doctype):
	"""Print mandatory fields for the specified doctype"""
	meta = frappe.get_meta(doctype)
	logger.debug(f"Please setup make_test_records for: {doctype}")
	logger.debug("-" * 60)
	logger.debug(f"Autoname: {meta.autoname or ''}")
	logger.debug("Mandatory Fields:")
	for d in meta.get("fields", {"reqd": 1}):
		logger.debug(f" - {d.parent}:{d.fieldname} | {d.fieldtype} | {d.options or ''}")
	logger.debug("")


class TestRecordLog:
	def __init__(self):
		self.log_file = Path(frappe.get_site_path(".test_log"))
		self._log = None

	def get(self):
		if self._log is None:
			self._log = self._read_log()
		return self._log

	def add(self, doctype):
		log = self.get()
		if doctype not in log:
			log.append(doctype)
			self._write_log(log)

	def _read_log(self):
		if self.log_file.exists():
			with self.log_file.open() as f:
				return f.read().splitlines()
		return []

	def _write_log(self, log):
		with self.log_file.open("w") as f:
			f.write("\n".join(l for l in log if l is not None))


# Compatibility functions
def add_to_test_record_log(doctype):
	TestRecordLog().add(doctype)


def get_test_record_log():
	return TestRecordLog().get()


@debug_timer
def _run_before_test_hooks(config: TestConfig, app: str | None):
	"""Run 'before_tests' hooks"""
	logger.debug('Running "before_tests" hooks')
	for hook_function in frappe.get_hooks("before_tests", app_name=app):
		frappe.get_attr(hook_function)()


@debug_timer
def _execute_test_record_callbacks(runner):
	"""Execute test record creation callbacks"""
	logger.debug("Running test record creation callbacks")
	runner.execute_test_record_callbacks()
