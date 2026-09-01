import pytest
from safety.ast_guard import ASTGuard, ASTViolationError


@pytest.fixture
def ast_guard():
    return ASTGuard()


def test_ast_allows_valid_hook(ast_guard):
    old_code = """
    public class Player {
        public void Update() {
            DoOriginalWork();
        }
    }
    """
    new_code = """
    public class Player {
        public void Update() {
            DoHookWork();
            DoOriginalWork();
        }
    }
    """
    ast_guard.validate_csharp_transition(old_code, new_code, "Player.cs")


def test_ast_blocks_method_deletion(ast_guard):
    old_code = """
    public class Player {
        public void Update() { DoWork(); }
        public void Attack() { FireWeapon(); }
    }
    """
    new_code = """
    public class Player {
        public void Update() { DoWork(); }
    }
    """
    with pytest.raises(ASTViolationError) as exc:
        ast_guard.validate_csharp_transition(old_code, new_code, "Player.cs")
    assert "Disallowed method deletion" in str(exc.value)
    assert "Attack" in str(exc.value)


def test_ast_blocks_class_deletion(ast_guard):
    old_code = """
    public class Player { public void Update() {} }
    public class Enemy { public void Attack() {} }
    """
    new_code = """
    public class Player { public void Update() {} }
    """
    with pytest.raises(ASTViolationError) as exc:
        ast_guard.validate_csharp_transition(old_code, new_code, "Player.cs")
    assert "Disallowed class/type deletion" in str(exc.value)
    assert "Enemy" in str(exc.value)


def test_ast_blocks_assertion_removal(ast_guard):
    old_code = """
    public class Player {
        public void Update() {
            Debug.Assert(this.Health > 0);
            DoWork();
        }
    }
    """
    new_code = """
    public class Player {
        public void Update() {
            DoWork();
        }
    }
    """
    with pytest.raises(ASTViolationError) as exc:
        ast_guard.validate_csharp_transition(old_code, new_code, "Player.cs")
    assert "Assertion weakening/removal detected" in str(exc.value)


def test_ast_blocks_early_return_bypass(ast_guard):
    old_code = """
    public class Player {
        public void Update() {
            CalculatePhysics();
            UpdateAnimation();
        }
    }
    """
    new_code = """
    public class Player {
        public void Update() {
            return;
        }
    }
    """
    with pytest.raises(ASTViolationError) as exc:
        ast_guard.validate_csharp_transition(old_code, new_code, "Player.cs")
    assert "Early return / dummy stub bypass detected" in str(exc.value)


def test_ast_blocks_not_implemented_bypass(ast_guard):
    old_code = """
    public class Player {
        public void Update() {
            CalculatePhysics();
        }
    }
    """
    new_code = """
    public class Player {
        public void Update() {
            throw new NotImplementedException();
        }
    }
    """
    with pytest.raises(ASTViolationError) as exc:
        ast_guard.validate_csharp_transition(old_code, new_code, "Player.cs")
    assert "dummy stub bypass detected" in str(exc.value)
