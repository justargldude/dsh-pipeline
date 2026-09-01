import pytest
from recon.database import EvidenceDatabase, SymbolRecord
from recon.indexer import SymbolIndexer
from recon.diff import CrossVersionMatcher
from recon.evidence import EvidenceService


@pytest.fixture
def recon_db():
    db = EvidenceDatabase()  # In-memory DuckDB
    yield db
    db.close()


def test_symbol_indexing_and_retrieval(recon_db):
    indexer = SymbolIndexer(recon_db)
    indexer.index_symbol_dict(
        {
            "symbol_name": "Player.Update",
            "class_name": "Player",
            "namespace": "Assembly-CSharp",
            "signature": "void Update()",
            "rva": "0x123456",
            "return_type": "void",
            "parameters": [],
        },
        version="v246",
    )

    sym = recon_db.get_symbol("Player.Update", "v246")
    assert sym is not None
    assert sym.symbol_name == "Player.Update"
    assert sym.rva == "0x123456"
    assert sym.class_name == "Player"


def test_cross_version_symbol_matching(recon_db):
    indexer = SymbolIndexer(recon_db)

    # v246 (Old)
    indexer.index_symbol_dict(
        {
            "symbol_name": "Player.Update",
            "class_name": "Player",
            "signature": "void Update(int delta)",
            "rva": "0x1000",
            "parameters": ["int delta"],
        },
        version="v246",
    )
    indexer.index_call_edges([{"caller": "GameController.Loop", "callee": "Player.Update"}], version="v246")

    # v250 (New) candidate 1: high match
    cand1 = SymbolRecord(
        symbol_name="Player.Update",
        class_name="Player",
        signature="void Update(int delta)",
        rva="0x2000",
        version="v250",
        return_type="void",
        parameters=["int delta"],
    )
    recon_db.insert_symbol(cand1)
    recon_db.insert_call_edge("GameController.Loop", "Player.Update", "v250")

    # v250 (New) candidate 2: low match
    cand2 = SymbolRecord(
        symbol_name="Enemy.Attack",
        class_name="Enemy",
        signature="void Attack()",
        rva="0x3000",
        version="v250",
        return_type="void",
        parameters=[],
    )
    recon_db.insert_symbol(cand2)

    matcher = CrossVersionMatcher(recon_db)
    old_sym = recon_db.get_symbol("Player.Update", "v246")
    match_result = matcher.match_symbol(old_sym, [cand1, cand2], old_version="v246", new_version="v250")

    assert match_result.new_symbol == "Player.Update"
    assert match_result.confidence >= 0.90
    assert match_result.verified is True

    # Test evidence service query
    evidence_svc = EvidenceService(recon_db)
    evidence = evidence_svc.get_symbol_evidence("Player.Update", "v246", "v250")
    assert evidence["target_symbol"] == "Player.Update"
    assert evidence["confidence"] >= 0.90
    assert evidence["is_fact"] is True
