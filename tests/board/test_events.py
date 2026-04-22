from api.board.events import BoardEventBus


def test_publish_without_subscribers_is_noop():
    bus = BoardEventBus()
    bus.publish("board:loaded", {"board_id": "x", "is_known": True})


def test_subscribers_receive_events_in_order():
    bus = BoardEventBus()
    received = []

    def handler(payload):
        received.append(payload)

    bus.subscribe("board:loaded", handler)
    bus.publish("board:loaded", {"board_id": "a"})
    bus.publish("board:loaded", {"board_id": "b"})
    assert [p["board_id"] for p in received] == ["a", "b"]


def test_unknown_topic_does_not_raise():
    bus = BoardEventBus()
    bus.publish("unknown:topic", {})  # should not raise
