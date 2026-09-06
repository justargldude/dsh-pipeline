"""Phase B (v2.3) — Full PurityGuard for Core/Domain + 2-arg tautology asserts.

RED tests first. Contracts encoded here (v2.3 Stage 2 Phase B):
1. Files under Core/ or Domain/ must be PURE (FCIS):
   - No File.*, Directory.*, Socket/TcpClient, HttpClient
   - No methods on *DbContext*/*Repository*/*Client* types
   - No DateTime.Now / DateTime.UtcNow (pass timestamps as parameters)
   - No new Random() without a seed
   - No writes to non-readonly static fields
2. 2-argument tautological asserts (Assert.AreEqual(x, x)) are rejected
   everywhere — not just literal tautologies like Assert(true).
3. All checks run through the thin ASTGuard router end-to-end.
"""
import pytest

from safety.ast_guard import ASTGuard, ASTViolationError


def _transition(guard, old, new, path="Core/Player.cs"):
    guard.validate_csharp_transition(old, new, path)


# ==============================================================================
# 1. FCIS purity violations inside Core/Domain (must be rejected)
# ==============================================================================

class TestPurityGuardCoreDomain:
    def test_file_io_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Player { public int Hp; }"
        new = "public class Player {\n    public void Save() { System.IO.File.WriteAllText(\"p.txt\", \"x\"); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|File"):
            _transition(guard, old, new)

    def test_file_write_rejected_in_domain(self):
        guard = ASTGuard()
        old = "public class Order { public int Total; }"
        new = "public class Order {\n    public void Persist() { File.WriteAllText(\"o.txt\", Total.ToString()); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|File"):
            _transition(guard, old, new, "Domain/Order.cs")

    def test_socket_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Net { }"
        new = "public class Net {\n    public void Connect() { var s = new Socket(); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|Socket"):
            _transition(guard, old, new)

    def test_tcpclient_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Net { }"
        new = "public class Net {\n    public void Dial() { var c = new TcpClient(); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|Tcp|Socket"):
            _transition(guard, old, new)

    def test_dbcontext_method_call_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class PlayerRepo { }"
        new = "public class PlayerRepo {\n    public void Load() { gameDbContext.SaveChanges(); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|DbContext"):
            _transition(guard, old, new)

    def test_repository_method_call_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Svc { }"
        new = "public class Svc {\n    public void Go() { itemRepository.Find(1); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|Repository"):
            _transition(guard, old, new)

    def test_client_method_call_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Svc { }"
        new = "public class Svc {\n    public void Go() { apiClient.SendAsync(); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|Client"):
            _transition(guard, old, new)

    def test_datetime_now_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Player { }"
        new = "public class Player {\n    public void Stamp() { var t = DateTime.Now; }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|DateTime"):
            _transition(guard, old, new)

    def test_datetime_utcnow_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Player { }"
        new = "public class Player {\n    public void Stamp() { var t = DateTime.UtcNow; }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|DateTime"):
            _transition(guard, old, new)

    def test_unseeded_random_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Dice { }"
        new = "public class Dice {\n    public int Roll() { var r = new Random(); return r.Next(6); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)purity|Random"):
            _transition(guard, old, new)

    def test_seeded_random_allowed_in_core(self):
        guard = ASTGuard()
        old = "public class Dice { }"
        new = "public class Dice {\n    public int Roll() { var r = new Random(42); return r.Next(6); }\n}"
        # Seeded Random is deterministic — allowed (must NOT raise).
        _transition(guard, old, new)

    def test_static_mutable_field_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Cache { }"
        new = "public class Cache {\n    public static int Counter;\n}"
        with pytest.raises(ASTViolationError, match="(?i)static mutable|static"):
            _transition(guard, old, new)

    def test_static_readonly_field_allowed_in_core(self):
        guard = ASTGuard()
        old = "public class Cache { }"
        new = "public class Cache {\n    public static readonly int Limit = 10;\n}"
        # readonly/const statics are fine (must NOT raise).
        _transition(guard, old, new)

    def test_static_field_assignment_rejected_in_core(self):
        guard = ASTGuard()
        old = "public class Score { public static int Points; }"
        new = "public class Score {\n    public static int Points;\n    public void Add() { Score.Points = 5; }\n}"
        with pytest.raises(ASTViolationError, match="(?i)static mutable|static"):
            _transition(guard, old, new)


# ==============================================================================
# 2. Purity NOT enforced outside Core/Domain (shell code may do I/O)
# ==============================================================================

class TestPurityGuardShellExempt:
    def test_file_io_allowed_outside_core(self):
        guard = ASTGuard()
        old = "public class Saver { }"
        new = "public class Saver {\n    public void Save() { File.WriteAllText(\"p.txt\", \"x\"); }\n}"
        # Infrastructure/Shell layer may perform I/O (must NOT raise purity).
        _transition(guard, old, new, "Infrastructure/Saver.cs")

    def test_datetime_now_allowed_outside_core(self):
        guard = ASTGuard()
        old = "public class Clock { }"
        new = "public class Clock {\n    public void Now() { var t = DateTime.Now; }\n}"
        _transition(guard, old, new, "Services/Clock.cs")


# ==============================================================================
# 3. 2-argument tautological asserts (Assert.AreEqual(x, x))
# ==============================================================================

class TestTwoArgTautologyAssert:
    def test_assert_are_equal_same_variable_rejected(self):
        guard = ASTGuard()
        old = "public class T { public void M() { int x = 1; } }"
        new = "public class T {\n    public void M() { int x = 1; Debug.Assert(x == x); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)tautolog"):
            _transition(guard, old, new)

    def test_assert_are_equal_two_arg_same_expr_rejected(self):
        guard = ASTGuard()
        old = "public class T { public void M() { var a = new object(); } }"
        new = "public class T {\n    public void M() { var a = new object(); Assert.AreEqual(a, a); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)tautolog"):
            _transition(guard, old, new)

    def test_assert_are_equal_distinct_args_allowed(self):
        guard = ASTGuard()
        old = "public class T { public void M() { int x = 1; int y = 2; } }"
        new = "public class T {\n    public void M() { int x = 1; int y = 2; Assert.AreEqual(3, x + y); }\n}"
        # Meaningful comparison (must NOT raise).
        _transition(guard, old, new)

    def test_assert_are_same_same_variable_rejected(self):
        guard = ASTGuard()
        old = "public class T { public void M() { var o = new object(); } }"
        new = "public class T {\n    public void M() { var o = new object(); Assert.AreSame(o, o); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)tautolog"):
            _transition(guard, old, new)

    def test_assert_true_literal_still_rejected(self):
        guard = ASTGuard()
        old = "public class T { public void M() { int x = 1; } }"
        new = "public class T {\n    public void M() { int x = 1; Debug.Assert(true); }\n}"
        with pytest.raises(ASTViolationError, match="(?i)tautolog|weaken"):
            _transition(guard, old, new)
