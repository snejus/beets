# This file is part of beets.
# Copyright 2016, Adrian Sampson.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Tests the TerminalImportSession. The tests are the same as in the

test_importer module. But here the test importer inherits from
``TerminalImportSession``. So we test this class, too.
"""

from test import test_importer

from beets.test.helper import TerminalImportHelper


class NonAutotaggedImportTest(
    TerminalImportHelper, test_importer.NonAutotaggedImportTest
):
    pass


class ImportTest(TerminalImportHelper, test_importer.ImportTest):
    pass


class ImportSingletonTest(
    TerminalImportHelper, test_importer.ImportSingletonTest
):
    pass


class ImportTracksTest(TerminalImportHelper, test_importer.ImportTracksTest):
    pass


class ImportCompilationTest(
    TerminalImportHelper, test_importer.ImportCompilationTest
):
    pass


class ImportExistingTest(
    TerminalImportHelper, test_importer.ImportExistingTest
):
    pass


class ChooseCandidateTest(
    TerminalImportHelper, test_importer.ChooseCandidateTest
):
    pass


class GroupAlbumsImportTest(
    TerminalImportHelper, test_importer.GroupAlbumsImportTest
):
    pass


class GlobalGroupAlbumsImportTest(
    TerminalImportHelper, test_importer.GlobalGroupAlbumsImportTest
):
    pass
