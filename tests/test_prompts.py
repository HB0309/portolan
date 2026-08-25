"""Rendering an observation into the text the model actually reads.

Found live: a discovery run selected an account type, then re-selected it
over and over -- 17 times -- burning its whole step budget without ever
reaching the submit button. The selection was working; the model just had
no way to tell, because ``render_element`` only ever echoed a control's
current value for ``role == "textbox"``. A ``<select>`` is role
``combobox``, so the model was shown the same "unselected-looking" control
after every attempt and never stopped retrying.
"""

from __future__ import annotations

from cua.discovery.prompts import render_element
from cua.surfaces.base import Element


def test_a_selected_combobox_shows_its_current_value():
    el = Element(ref="e1", role="combobox", name="Account Type", value="Holiday Club")
    assert "value:'Holiday Club'" in render_element(el)


def test_an_unselected_combobox_shows_no_value():
    el = Element(ref="e1", role="combobox", name="Account Type", value="")
    assert "value:" not in render_element(el)


def test_a_filled_textbox_still_shows_its_value():
    """The regression guard: extending the check to comboboxes must not
    disturb the textbox case this was originally written for.
    """
    el = Element(ref="e1", role="textbox", name="Member Number", value="10042")
    assert "value:'10042'" in render_element(el)


def test_a_button_never_shows_a_value():
    """Role gating still excludes roles where 'value' isn't a meaningful
    concept -- only textbox and combobox carry a value worth confirming.
    """
    el = Element(ref="e1", role="button", name="Submit", value="Submit")
    assert "value:" not in render_element(el)
