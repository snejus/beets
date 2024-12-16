"""Test module for file ui/__init__.py"""

import unittest
from copy import deepcopy
from pathlib import Path
from random import random
from unittest.mock import patch

import pytest

from beets import config, ui
from beets.exceptions import UserError
from beets.test.helper import BeetsTestCase, IOMixin


class TestInput:
    @pytest.mark.parametrize(
        "prompt,expected", [(None, ""), ("Prompt:", "Prompt: ")]
    )
    def test_passes_prompt_to_input(self, prompt, expected):
        with patch("builtins.input", return_value="answer") as input_mock:
            assert ui.input_(prompt) == "answer"

        input_mock.assert_called_once_with(expected)

    @pytest.mark.parametrize(
        "readline,expected",
        [
            (object(), "\x01\x1b[31m\x02Prompt\x01\x1b[0m\x02 "),
            (None, "\x1b[31mPrompt\x1b[0m "),
        ],
    )
    def test_marks_only_ansi_codes_as_non_printing(self, readline, expected):
        with patch("beets.ui.readline", readline):
            assert ui._render_prompt("\x1b[31mPrompt\x1b[0m") == expected

    def test_raises_user_error_at_end_of_input(self):
        with (
            patch("builtins.input", side_effect=EOFError),
            pytest.raises(UserError, match="stdin stream ended"),
        ):
            ui.input_("Prompt:")


class InputMethodsTest(IOMixin, unittest.TestCase):
    def _print_helper(self, s):
        print(s)

    def _print_helper2(self, s, prefix):
        print(prefix, s)

    def test_input_select_objects(self):
        full_items = ["1", "2", "3", "4", "5"]

        # Test no
        self.io.addinput("n")
        items = ui.input_select_objects(
            "Prompt", full_items, self._print_helper
        )
        assert items == []

        # Test yes
        self.io.addinput("y")
        items = ui.input_select_objects(
            "Prompt", full_items, self._print_helper
        )
        assert items == full_items

        # Test selective 1
        self.io.addinput("s")
        self.io.addinput("n")
        self.io.addinput("y")
        self.io.addinput("n")
        self.io.addinput("y")
        self.io.addinput("n")
        items = ui.input_select_objects(
            "Prompt", full_items, self._print_helper
        )
        assert items == ["2", "4"]

        # Test selective 2
        self.io.addinput("s")
        self.io.addinput("y")
        self.io.addinput("y")
        self.io.addinput("n")
        self.io.addinput("y")
        self.io.addinput("n")
        items = ui.input_select_objects(
            "Prompt", full_items, lambda s: self._print_helper2(s, "Prefix")
        )
        assert items == ["1", "2", "4"]

        # Test selective 3
        self.io.addinput("s")
        self.io.addinput("y")
        self.io.addinput("n")
        self.io.addinput("y")
        self.io.addinput("q")
        items = ui.input_select_objects(
            "Prompt", full_items, self._print_helper
        )
        assert items == ["1", "3"]


class ParentalDirCreation(IOMixin, BeetsTestCase):
    def test_memory_path_skips_creation_prompt(self):
        ui._ensure_db_directory_exists(Path(":memory:"))
        assert not self.io.getoutput()

    def test_create_yes(self):
        non_exist_path = self.temp_path / "nonexist" / str(random())
        # Deepcopy instead of recovering because exceptions might
        # occur; wish I can use a golang defer here.
        test_config = deepcopy(config)
        test_config["library"] = str(non_exist_path)
        self.io.addinput("y")
        lib = ui._open_library(test_config)
        lib._close()

    def test_create_no(self):
        non_exist_path_parent = self.temp_path / "nonexist"
        non_exist_path = non_exist_path_parent / str(random())
        test_config = deepcopy(config)
        test_config["library"] = str(non_exist_path)

        self.io.addinput("n")
        with pytest.raises(UserError):
            ui._open_library(test_config)
        assert not non_exist_path_parent.exists()
