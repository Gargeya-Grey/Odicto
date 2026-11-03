"""Frozen selection fixtures must not consult the owner's real keyboard state."""
import unittest
from unittest.mock import patch

from tests import test_units as frozen


class TestUnitHardwareIsolation(unittest.TestCase):
    def test_selection_fixture_never_reads_live_modifier_state(self):
        fixture = frozen.TestOdicto("test_get_selected_text_with_selection")
        with patch("typer.is_pressed", return_value=False) as hardware_read:
            try:
                fixture.setUp()
                fixture.test_get_selected_text_with_selection()
            finally:
                fixture.tearDown()
                fixture.doCleanups()
        hardware_read.assert_not_called()
