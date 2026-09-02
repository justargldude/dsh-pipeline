import pytest
from task.schema import TaskDefinition, PatchProposal, FilePatch, PatchHunk
from memory.episodic import EpisodicMemoryStore, EpisodeRecord, EpisodeStatus, EpisodeValidation
from memory.retrieval import EpisodeRetriever, StaleDetector
from context.builder import ContextBuilder


@pytest.fixture
def memory_store():
    store = EpisodicMemoryStore()
    patch = PatchProposal(
        patches=[
            FilePatch(
                file="Player.cs",
                hunks=[PatchHunk(old_text="void Update() {}", new_text="void Update() { Hook(); }")]
            )
        ],
        reason="Ported hook",
        confidence=0.96
    )
    episode = EpisodeRecord(
        task_id="T016",
        symbol="Player.Update",
        solution_patch=patch,
        version="v246",
        environment="linux",
        confidence=0.96,
        status=EpisodeStatus.VALIDATED,
        validation=EpisodeValidation(build=True, behavior=True, regression=True),
    )
    store.store_episode(episode)
    return store


def test_store_and_retrieve_episode(memory_store):
    retriever = EpisodeRetriever(memory_store)
    episodes = retriever.retrieve_advisory_episodes("Player.Update", current_version="v246")

    assert len(episodes) == 1
    assert episodes[0].task_id == "T016"
    assert episodes[0].status == EpisodeStatus.VALIDATED
    assert episodes[0].confidence == 0.96


def test_stale_detection_and_exclusion(memory_store):
    retriever = EpisodeRetriever(memory_store)
    # Stale episode is excluded by default
    episodes_default = retriever.retrieve_advisory_episodes("Player.Update", current_version="v250")
    assert len(episodes_default) == 0

    # Stale episode can be retrieved if explicitly requested
    episodes_stale = retriever.retrieve_advisory_episodes("Player.Update", current_version="v250", include_stale=True)
    assert len(episodes_stale) == 1
    assert episodes_stale[0].status == EpisodeStatus.STALE


def test_context_builder_with_advisory_memory(memory_store):
    retriever = EpisodeRetriever(memory_store)
    episodes = retriever.retrieve_advisory_episodes("Player.Update", current_version="v246")

    task = TaskDefinition(
        task_id="T017",
        title="Port Player.Update for next module",
        allowed_files=["Player.cs"],
    )

    builder = ContextBuilder()
    context = builder.build_context(
        task=task,
        file_snippets={"Player.cs": "public class Player { void Update() {} }"},
        advisory_episodes=episodes,
    )

    assert "ADVISORY MEMORY (HISTORICAL PASSED EPISODES)" in context
    assert "Task T016" in context
    assert "Patched 1 file(s)" in context
