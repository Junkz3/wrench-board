from api.agent.memory_seed import _SEED_FILES


def test_reliability_is_in_seed_files():
    file_names = [f for f, _ in _SEED_FILES]
    memory_paths = [p for _, p in _SEED_FILES]
    assert "simulator_reliability.json" in file_names
    assert "/knowledge/simulator_reliability.json" in memory_paths
