import pytest

from api.agent.board_ref import set_board_ref, current_board_ref


@pytest.fixture(autouse=True)
def _reset():
    set_board_ref(None)
    yield
    set_board_ref(None)


def test_default_is_none():
    assert current_board_ref() is None


def test_set_and_read():
    set_board_ref("820-02016")
    assert current_board_ref() == "820-02016"
    set_board_ref(None)
    assert current_board_ref() is None
