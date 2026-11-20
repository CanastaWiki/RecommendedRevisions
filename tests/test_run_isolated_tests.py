"""Unit tests for the PHPUnit command built by .ci/run_isolated_tests.py."""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".ci"))

import run_isolated_tests  # noqa: E402

RUNNER = "/mw/tests/phpunit/phpunit.php"
TEST_DIR = "/mw/extensions/PageForms/tests/phpunit"
RESULT = "/results/PageForms.xml"


def test_command_without_exclusions_passes_test_dir():
    cmd = run_isolated_tests.build_phpunit_command(RUNNER, TEST_DIR, RESULT)
    assert cmd == ["php", RUNNER, "--log-junit", RESULT, TEST_DIR]


def test_command_with_exclusions_passes_test_dir_once_with_filter():
    excludes = [
        "tests/phpunit/integration/includes/PFFormLinkerTest.php",
        "tests/phpunit/integration/includes/PFHelperFormActionTest.php",
    ]
    cmd = run_isolated_tests.build_phpunit_command(RUNNER, TEST_DIR, RESULT, excludes)
    assert cmd == [
        "php", RUNNER, "--log-junit", RESULT,
        "--filter", r"/^(?!(?:.*\\)?(?:PFFormLinkerTest|PFHelperFormActionTest)::)/",
        TEST_DIR,
    ]
    # Exactly one test path, so PHPUnit 9 runs the whole directory.
    assert sum(1 for arg in cmd if arg.startswith("/mw/extensions/")) == 1


def _matches(pattern: str, name: str) -> bool:
    # Strip the PHP regex delimiters; the body is also valid Python regex.
    assert pattern.startswith("/") and pattern.endswith("/")
    return re.search(pattern[1:-1], name) is not None


def test_filter_skips_excluded_classes_and_keeps_others():
    pattern = run_isolated_tests.build_exclude_filter([
        "tests/phpunit/Media/AudioHandlerTest.php",
        "tests/phpunit/integration/formats/CargoFeedFormatTest.php",
    ])
    # Excluded: global and namespaced classes, with or without data sets.
    assert not _matches(pattern, "CargoFeedFormatTest::testFeed")
    assert not _matches(
        pattern,
        "MediaWiki\\Extension\\EmbedVideo\\Tests\\Media\\AudioHandlerTest::testParams"
        " with data set #0",
    )
    # Kept: other classes, including ones whose names merely contain or
    # extend an excluded name.
    assert _matches(pattern, "CargoUtilsTest::testSomething")
    assert _matches(
        pattern,
        "MediaWiki\\Extension\\EmbedVideo\\Tests\\Media\\VideoHandlerTest::testParams",
    )
    assert _matches(pattern, "OtherAudioHandlerTest::testX")
    assert _matches(pattern, "AudioHandlerTestCase::testX")
    assert _matches(pattern, "Foo::testAudioHandlerTest")
